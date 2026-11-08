"""SMS fan-out: send gate tripwires, allowlist, dry run, caps, kill switch, idempotency, pacing,
continuation, retries and 21610. Twilio is always mocked; every phone number is fictional (555-01xx)."""

import json
import os
import re
import unittest
from datetime import datetime

os.environ.update({"SUBSCRIBERS_TABLE": "subs", "AUDIT_TABLE": "audit", "OPS_TABLE": "ops", "ALERTS_TABLE": "alerts",
                   "SENDS_TABLE": "sends", "QUEUE_URL": "https://sqs.test/queue", "DLQ_URL": "https://sqs.test/dlq",
                   "TWILIO_FROM_NUMBER": "+18335550100", "TWILIO_WEBHOOK_BASE": "https://example.test"})

from fakes import FakeTable, install_stubs  # noqa: E402

install_stubs()

import alert_composer as ac  # noqa: E402
import fanout_worker as fw  # noqa: E402
from common import fanout, sms_text, store, twilio_send  # noqa: E402

CAMPAIGN = "fob-twilight-town-mall-2026"
SENT_AT = "2026-11-08T18:00:00.000Z"
ALERT_TS = 1794160800.0  # SENT_AT as epoch seconds
ALERT_ID = f"{CAMPAIGN}#{SENT_AT}"
PHONES = [f"+1202555{100 + i:04d}" for i in range(6)]  # +12025550100.. fictional
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")


class FakeSubs(FakeTable):
    """Adds the active-index GSI: sorted by gsi1sk, Limit + ExclusiveStartKey pagination."""

    def query(self, KeyConditionExpression, ExpressionAttributeValues, IndexName=None, Limit=None,
              ExclusiveStartKey=None, **kw):
        if IndexName is None:
            return super().query(KeyConditionExpression, ExpressionAttributeValues, **kw)
        self.calls.append(("query_gsi", IndexName))
        assert IndexName == "active-index"
        pk = ExpressionAttributeValues[":p"]
        rows = sorted((i for i in self.items.values() if i.get("gsi1pk") == pk), key=lambda i: i["gsi1sk"])
        if ExclusiveStartKey:
            rows = [r for r in rows if r["gsi1sk"] > ExclusiveStartKey["gsi1sk"]]
        page = rows[:Limit] if Limit else rows
        out = {"Items": [{k: r[k] for k in ("phone", "campaign_id", "gsi1pk", "gsi1sk")} for r in page]}
        if Limit and len(rows) > Limit:
            out["LastEvaluatedKey"] = {k: page[-1][k] for k in ("phone", "campaign_id", "gsi1pk", "gsi1sk")}
        return out

    def scan(self, **kw):
        raise AssertionError("fan-out must never Scan")


class FakeSqs:
    def __init__(self):
        self.sent = []

    def send_message(self, QueueUrl, MessageBody, DelaySeconds=0):
        self.sent.append((QueueUrl, json.loads(MessageBody), DelaySeconds))
        return {"MessageId": f"m{len(self.sent)}"}


class Context:
    def __init__(self, ms=300000):
        self.ms = ms

    def get_remaining_time_in_millis(self):
        return self.ms


class Clock:
    def __init__(self, now):
        self.t, self.sleeps = now, []

    def now(self):
        return self.t

    def mono(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class FanoutTestCase(unittest.TestCase):
    def setUp(self):
        store.reset_config_cache()
        self.subs = FakeSubs("phone", "campaign_id")
        self.audit = FakeTable("phone", "sk")
        self.ops = FakeTable("pk", "sk")
        self.alerts = FakeTable("campaign_id", "sent_at")
        self.sends = FakeTable("alert_id", "phone")
        fw._tables.clear()
        fw._tables.update(subs=self.subs, audit=self.audit, ops=self.ops, alerts=self.alerts, sends=self.sends)
        self.sqs = FakeSqs()
        fw._clients["sqs"] = self.sqs
        self.clock = Clock(ALERT_TS + 30)
        self._clock = dict(fw._clock)
        fw._clock.update(now=self.clock.now, mono=self.clock.mono, sleep=self.clock.sleep)
        fw._twilio_creds["value"] = ("ACfake", "fake-token")

        # Tripwire: the only path to Twilio. Every call is recorded; nothing leaves the process.
        self.twilio_calls = []
        self.twilio_errors = {}
        self.on_send = None
        self._send = twilio_send.send_message

        def fake_send(sid, token, frm, to, body, status_callback=None, **kw):
            self.twilio_calls.append({"to": to, "body": body, "callback": status_callback})
            err = self.twilio_errors.get(to)
            if isinstance(err, list):
                err = err.pop(0) if err else None
            if self.on_send:
                self.on_send(len(self.twilio_calls))
            if err:
                raise err
            return {"sid": f"SMfake{len(self.twilio_calls)}", "status": "queued"}
        twilio_send.send_message = fake_send

        self.ops.put_item(Item={"pk": f"config#campaign#{CAMPAIGN}", "sk": "current", "campaign_id": CAMPAIGN,
                                "campaign_status": "active"})
        self.set_config()
        self.alerts.put_item(Item={
            "campaign_id": CAMPAIGN, "sent_at": SENT_AT, "status": "published", "created_at": SENT_AT,
            "what_changed": "Aces Hifi Records is now open. Look for store 04 at falloutboy.com/aces-hifi-records",
            "note": "Thnks fr th mmrs.", "message": "x", "segments": 1})
        for i, p in enumerate(PHONES[:4]):
            self.add_sub(p, i)

    def tearDown(self):
        twilio_send.send_message = self._send
        fw._clock.update(self._clock)
        fw._twilio_creds["value"] = None
        fw._clients.clear()
        fw._tables.clear()

    def set_config(self, **kw):
        cfg = {"pk": "config#global", "sk": "current", "broadcast_enabled": False, "waitlist_mode": True,
               "fanout_mode": "off", "fanout_allowlist": [], "fanout_paused": False,
               "fanout_max_recipients_per_alert": 200, "fanout_delay_seconds": 10, "fanout_rate_per_second": 2,
               "max_public_alerts_per_day": 6}
        cfg.update(kw)
        self.ops.put_item(Item=cfg)

    def live(self, **kw):
        self.set_config(fanout_mode="live", broadcast_enabled=True, waitlist_mode=False, **kw)

    def add_sub(self, phone, i, status="ACTIVE"):
        self.subs.put_item(Item={"phone": phone, "campaign_id": CAMPAIGN, "status": status,
                                 "gsi1pk": f"ACTIVE#{CAMPAIGN}", "gsi1sk": f"2026-11-0{i + 1}T00:00:00.000Z#{phone}"})

    def job(self, **kw):
        return {"v": 1, "campaign_id": CAMPAIGN, "sent_at": SENT_AT, "seq": 0, **kw}

    def run_job(self, job=None, ctx=None):
        body = json.dumps(job or self.job())
        return fw.handler({"Records": [{"body": body}]}, ctx or Context())["results"][0]

    def alert(self):
        return self.alerts.items[(CAMPAIGN, SENT_AT)]

    def rows(self):
        return {k[1]: v for k, v in self.sends.items.items() if k[0] == ALERT_ID and k[1] != fw.RUN_SK}

    def recipients_texted(self):
        return [c["to"] for c in self.twilio_calls]


class TripwireTest(FanoutTestCase):
    """Twilio is never called unless mode is allowlist/live AND broadcast_enabled is true (live: waitlist off)."""

    def test_no_send_in_any_non_sending_configuration(self):
        configs = [
            dict(fanout_mode="off"),
            dict(fanout_mode="dry_run"),
            dict(fanout_mode="dry_run", broadcast_enabled=True, waitlist_mode=False),
            dict(fanout_mode="LIVE!"),
            dict(fanout_mode=None),
            dict(fanout_mode="live", broadcast_enabled=False, waitlist_mode=False),
            dict(fanout_mode="live", broadcast_enabled="true", waitlist_mode=False),
            dict(fanout_mode="live", broadcast_enabled=True, waitlist_mode=True),
            dict(fanout_mode="allowlist", broadcast_enabled=False, fanout_allowlist=PHONES),
            dict(fanout_mode="live", broadcast_enabled=True, waitlist_mode=False, fanout_paused=True),
        ]
        for cfg in configs:
            with self.subTest(cfg=cfg):
                self.set_config(**cfg)
                self.sends.items.clear()
                self.run_job()
                self.assertEqual(self.twilio_calls, [])
                self.assertEqual(self.rows(), {})

    def test_missing_config_item_means_off(self):
        del self.ops.items[("config#global", "current")]
        self.assertEqual(self.run_job()["skipped"], "off")
        self.assertEqual(self.twilio_calls, [])

    def test_gate_function(self):
        self.assertEqual(fanout.send_gate({})[:2], ("off", False))
        self.assertEqual(fanout.send_gate({"fanout_mode": "live", "broadcast_enabled": True,
                                           "waitlist_mode": False})[:2], ("live", True))
        self.assertEqual(fanout.send_gate({"fanout_mode": "allowlist", "broadcast_enabled": True})[:2],
                         ("allowlist", True))
        self.assertFalse(fanout.send_gate({"fanout_mode": "live", "broadcast_enabled": True})[1])

    def test_dry_run_never_reads_the_twilio_secret(self):
        fw._twilio_creds["value"] = None  # boto3 is stubbed: any secret read would raise
        self.set_config(fanout_mode="dry_run")
        self.assertIn("dry_run", self.run_job())


class AllowlistTest(FanoutTestCase):
    def test_only_allowlisted_active_members_are_texted(self):
        self.set_config(fanout_mode="allowlist", broadcast_enabled=True, fanout_allowlist=[PHONES[1], PHONES[5]])
        res = self.run_job()
        self.assertEqual(self.recipients_texted(), [PHONES[1]])  # PHONES[5] is not a member
        self.assertEqual(res["completed"]["recipients"], 1)
        self.assertEqual(set(self.rows()), {PHONES[1]})

    def test_empty_allowlist_sends_nothing(self):
        self.set_config(fanout_mode="allowlist", broadcast_enabled=True, fanout_allowlist=[])
        self.run_job()
        self.assertEqual(self.twilio_calls, [])

    def test_removing_a_number_mid_run_stops_texting_it(self):
        self.set_config(fanout_mode="allowlist", broadcast_enabled=True, fanout_allowlist=[PHONES[0], PHONES[2]])
        self.on_send = lambda n: self.set_config(fanout_mode="allowlist", broadcast_enabled=True,
                                                 fanout_allowlist=[PHONES[0]])
        self.run_job()
        self.assertEqual(self.recipients_texted(), [PHONES[0]])


class DryRunTest(FanoutTestCase):
    def test_counts_without_sending_and_status_stays_published(self):
        self.add_sub(PHONES[4], 4)
        self.subs.items[(PHONES[4], CAMPAIGN)]["status"] = "OPTED_OUT"  # stale GSI entry
        self.set_config(fanout_mode="dry_run")
        with self.assertLogs(level="INFO") as logs:
            res = self.run_job()
        self.assertEqual(res["dry_run"], {"recipients": 5, "would_send": 4, "would_skip": 1, "daily_cap_ok": True})
        self.assertEqual(self.twilio_calls, [])
        self.assertEqual(self.sends.items, {})
        a = self.alert()
        self.assertEqual(a["status"], "published")
        self.assertEqual(a["dry_run_counts"]["would_send"], 4)
        self.assertEqual(a["mode"], "dry_run")
        self.assertTrue(any("would send 4" in line for line in logs.output))
        self.assertFalse(any(p in line for line in logs.output for p in PHONES))
        self.assertIn("fanout_dry_run", [i["event_type"] for i in self.audit.items.values()])

    def test_dry_run_does_not_consume_the_daily_cap(self):
        self.set_config(fanout_mode="dry_run", max_public_alerts_per_day=1)
        self.run_job()
        self.run_job()
        self.assertFalse(any(k[0].startswith("rl#fanout#") for k in self.ops.items))


class LiveTest(FanoutTestCase):
    def test_sends_once_to_each_active_and_marks_alert_sent(self):
        self.live()
        res = self.run_job()
        self.assertEqual(sorted(self.recipients_texted()), sorted(PHONES[:4]))
        a = self.alert()
        self.assertEqual(a["sent_at"], SENT_AT)
        self.assertEqual((a["status"], a["recipients"], a["sent"], a["failed"], a["skipped"], a["mode"]),
                         ("sent", 4, 4, 0, 0, "live"))
        self.assertIn("fanout_started_at", a)
        self.assertIn("fanout_finished_at", a)
        self.assertEqual(res["completed"]["sent"], 4)
        audits = [i for i in self.audit.items.values() if i["event_type"] == "fanout_completed"]
        self.assertEqual(audits[0]["phone"], fw.AUDIT_ACTOR)
        self.assertFalse(any(p in json.dumps(audits, default=str) for p in PHONES))

    def test_body_is_gsm7_branded_with_stop_and_at_most_two_segments(self):
        self.alerts.items[(CAMPAIGN, SENT_AT)].update(
            what_changed="New store \U0001F6CD\ufe0f opened \u201cAces\u201d \u2014 go look " + "x" * 300,
            note="so cute \U0001F496")
        self.live()
        self.run_job()
        for c in self.twilio_calls:
            body = c["body"]
            self.assertTrue(sms_text.is_gsm7(body))
            self.assertIsNone(EMOJI.search(body))
            self.assertLessEqual(sms_text.segments(body), 2)
            self.assertTrue(body.startswith("Mall Rat Alerts: "))
            self.assertTrue(body.endswith("Reply STOP to opt out"))

    def test_status_recheck_skips_opted_out_and_pending(self):
        self.subs.items[(PHONES[1], CAMPAIGN)]["status"] = "OPTED_OUT"
        self.subs.items[(PHONES[2], CAMPAIGN)]["status"] = "PENDING_SENDER"
        self.live()
        self.run_job()
        self.assertEqual(sorted(self.recipients_texted()), sorted([PHONES[0], PHONES[3]]))
        self.assertEqual(self.alert()["skipped"], 2)

    def test_redelivered_job_never_double_texts(self):
        self.live()
        self.run_job()
        self.sends.items[(ALERT_ID, fw.RUN_SK)]["status"] = "running"  # as if the first run crashed before finishing
        self.alerts.items[(CAMPAIGN, SENT_AT)]["status"] = "published"
        self.run_job()
        self.assertEqual(len(self.twilio_calls), 4)

    def test_claim_happens_before_send(self):
        self.live()
        seen = []
        self.on_send = lambda n: seen.append(self.sends.items.get((ALERT_ID, self.twilio_calls[-1]["to"]), {}).get("status"))
        self.run_job()
        self.assertEqual(seen, ["CLAIMED"] * 4)

    def test_completed_run_is_not_repeated(self):
        self.live()
        self.run_job()
        self.alerts.items[(CAMPAIGN, SENT_AT)]["status"] = "published"
        self.assertEqual(self.run_job(), {"duplicate": "completed"})
        self.assertEqual(len(self.twilio_calls), 4)

    def test_status_callback_has_no_query_and_sid_maps_to_send_row(self):
        self.live()
        self.run_job()
        self.assertEqual({c["callback"] for c in self.twilio_calls}, {"https://example.test/api/twilio/status"})
        ref = self.sends.items[("sid#SMfake1", "map")]
        self.assertEqual(ref["target_alert_id"], ALERT_ID)
        self.assertIn((ALERT_ID, ref["target_phone"]), self.sends.items)


class CapsAndChecksTest(FanoutTestCase):
    def test_recipient_cap_refuses_whole_run(self):
        self.live(fanout_max_recipients_per_alert=3)
        res = self.run_job()
        self.assertEqual(res["refused"], "recipient_cap")
        self.assertEqual(self.twilio_calls, [])
        self.assertEqual(self.alert()["fanout_status"], "refused")
        self.assertEqual(self.alert()["status"], "published")

    def test_daily_cap_per_campaign(self):
        self.live(max_public_alerts_per_day=1)
        self.run_job()
        second = "2026-11-08T18:05:00.000Z"
        self.alerts.put_item(Item={**self.alert(), "sent_at": second, "status": "published"})
        res = self.run_job(self.job(sent_at=second))
        self.assertEqual(res["refused"], "daily_cap")
        self.assertEqual(len(self.twilio_calls), 4)

    def test_campaign_must_be_active(self):
        self.ops.items[(f"config#campaign#{CAMPAIGN}", "current")]["campaign_status"] = "paused"
        self.live()
        self.assertEqual(self.run_job()["refused"], "campaign_not_active")
        self.assertEqual(self.twilio_calls, [])

    def test_test_alert_never_goes_live_but_can_go_to_allowlist(self):
        self.alerts.items[(CAMPAIGN, SENT_AT)]["is_test"] = True
        self.live()
        self.assertEqual(self.run_job()["refused"], "test_alert_in_live_mode")
        self.assertEqual(self.twilio_calls, [])
        self.alerts.items[(CAMPAIGN, SENT_AT)].pop("fanout_status")
        self.set_config(fanout_mode="allowlist", broadcast_enabled=True, fanout_allowlist=[PHONES[0]])
        self.sends.items.clear()
        self.run_job()
        self.assertEqual(self.recipients_texted(), [PHONES[0]])

    def test_old_backfilled_or_already_sent_alerts_are_refused(self):
        self.live()
        for change, reason in (({"backfilled": True}, "backfilled"), ({"status": "sent"}, "alert_not_published"),
                               ({"created_at": "2026-11-08T10:00:00.000Z"}, "alert_too_old")):
            with self.subTest(reason=reason):
                self.alerts.items[(CAMPAIGN, SENT_AT)].update({"status": "published", "backfilled": False,
                                                               "created_at": SENT_AT, **change})
                self.sends.items.clear()
                self.assertEqual(self.run_job()["refused"], reason)
        self.assertEqual(self.twilio_calls, [])

    def test_missing_alert_and_bad_jobs(self):
        self.live()
        self.assertEqual(self.run_job(self.job(sent_at="2026-11-08T19:00:00.000Z"))["refused"], "alert_missing")
        with self.assertRaises(ValueError):
            self.run_job(self.job(campaign_id="src#evil"))
        self.assertEqual(self.twilio_calls, [])

    def test_second_alert_is_not_blocked_by_a_running_first_alert(self):
        self.live()
        self.sends.put_item(Item={"alert_id": f"{CAMPAIGN}#2026-11-08T17:59:50.000Z", "phone": fw.RUN_SK,
                                  "status": "running", "mode": "live"})
        res = self.run_job()
        self.assertEqual(res["completed"]["sent"], 4)
        self.assertEqual(self.sqs.sent, [])


class KillSwitchTest(FanoutTestCase):
    def test_paused_at_start_parks_job_in_dlq(self):
        self.live(fanout_paused=True)
        self.assertEqual(self.run_job(), {"parked": "paused"})
        self.assertEqual(self.twilio_calls, [])
        self.assertEqual(self.sqs.sent[0][0], fw.DLQ_URL)

    def test_pause_mid_run_stops_then_resume_finishes_without_repeats(self):
        self.live()
        self.on_send = lambda n: n == 2 and self.live(fanout_paused=True)
        self.assertEqual(self.run_job(), {"parked": "paused"})
        self.assertEqual(len(self.twilio_calls), 2)
        url, parked, _ = self.sqs.sent[-1]
        self.assertEqual((url, parked["seq"], parked["mode"]), (fw.DLQ_URL, 1, "live"))
        self.assertEqual(self.alert()["status"], "published")
        # Operator unpauses and redrives the DLQ.
        self.on_send = None
        self.live()
        self.run_job(parked)
        self.assertEqual(sorted(self.recipients_texted()), sorted(PHONES[:4]))
        self.assertEqual(self.alert()["status"], "sent")

    def test_broadcast_off_mid_run_stops(self):
        self.live()
        self.on_send = lambda n: self.set_config(fanout_mode="live", broadcast_enabled=False, waitlist_mode=False)
        self.assertEqual(self.run_job(), {"parked": "broadcast_disabled"})
        self.assertEqual(len(self.twilio_calls), 1)

    def test_mode_change_parks_continuation(self):
        self.set_config(fanout_mode="allowlist", broadcast_enabled=True, fanout_allowlist=PHONES)
        self.on_send = lambda n: self.live()
        self.assertEqual(self.run_job(), {"parked": "mode_changed"})
        self.assertEqual(len(self.twilio_calls), 1)
        _, parked, _ = self.sqs.sent[-1]
        self.assertEqual(self.run_job(parked), {"parked": "mode_changed"})  # still live: stays parked
        self.assertEqual(len(self.twilio_calls), 1)


class ContinuationTest(FanoutTestCase):
    def test_low_time_enqueues_cursor_and_continuation_finishes(self):
        for i, p in enumerate(PHONES[4:], 4):
            self.add_sub(p, i)
        self.live()
        ctx = Context()
        self.on_send = lambda n: n == 3 and setattr(ctx, "ms", 1000)
        self.assertEqual(self.run_job(ctx=ctx), {"continued": 1})
        self.assertEqual(len(self.twilio_calls), 3)
        url, cont, delay = self.sqs.sent[-1]
        self.assertEqual((url, cont["seq"], delay), (fw.QUEUE_URL, 1, fw.CONTINUE_DELAY_SECONDS))
        self.assertEqual(cont["cursor"]["phone"], PHONES[2])
        self.assertEqual(self.alert()["status"], "published")
        self.on_send = None
        res = self.run_job(cont)
        self.assertEqual(self.recipients_texted(), PHONES)
        self.assertEqual(res["completed"]["recipients"], 6)
        self.assertEqual(res["completed"]["sent"], 6)

    def test_pagination_walks_every_page(self):
        fw.PAGE_SIZE, old = 2, fw.PAGE_SIZE
        try:
            self.live()
            self.run_job()
        finally:
            fw.PAGE_SIZE = old
        self.assertEqual(self.recipients_texted(), PHONES[:4])


class TwilioErrorsTest(FanoutTestCase):
    def test_21610_opts_out_through_existing_path(self):
        self.live()
        self.twilio_errors[PHONES[1]] = twilio_send.TwilioError(400, 21610)
        self.run_job()
        sub = self.subs.items[(PHONES[1], CAMPAIGN)]
        self.assertEqual((sub["status"], sub["unsubscribe_method"]), ("OPTED_OUT", "CARRIER_21610"))
        self.assertNotIn("gsi1pk", sub)
        self.assertEqual(self.rows()[PHONES[1]]["status"], "OPTED_OUT")
        self.assertEqual([i["event_type"] for i in self.audit.items.values() if i["phone"] == PHONES[1]],
                         ["carrier_21610"])
        self.assertEqual((self.alert()["sent"], self.alert()["failed"]), (3, 1))

    def test_429_backs_off_and_retries(self):
        self.live()
        self.twilio_errors[PHONES[0]] = [twilio_send.TwilioError(429, 20429)]
        self.run_job()
        self.assertEqual(self.recipients_texted().count(PHONES[0]), 2)
        self.assertIn(2, self.clock.sleeps)
        self.assertEqual(self.rows()[PHONES[0]]["status"], "SENT")

    def test_retries_are_bounded(self):
        self.live()
        self.twilio_errors[PHONES[0]] = [twilio_send.TwilioError(503, None)] * 5
        self.run_job()
        self.assertEqual(self.recipients_texted().count(PHONES[0]), fw.MAX_ATTEMPTS)
        self.assertEqual(self.rows()[PHONES[0]]["status"], "FAILED")

    def test_ambiguous_network_error_is_never_retried(self):
        self.live()
        self.twilio_errors[PHONES[0]] = twilio_send.TwilioError(None, "TimeoutError")
        self.run_job()
        self.assertEqual(self.recipients_texted().count(PHONES[0]), 1)
        self.assertEqual(self.rows()[PHONES[0]]["status"], "UNKNOWN")

    def test_hard_failure_is_recorded_and_others_continue(self):
        self.live()
        self.twilio_errors[PHONES[2]] = twilio_send.TwilioError(400, 21211)
        self.run_job()
        self.assertEqual(self.recipients_texted().count(PHONES[2]), 1)
        self.assertEqual((self.alert()["sent"], self.alert()["failed"]), (3, 1))


class PacingTest(FanoutTestCase):
    def test_interval_math_uses_segments(self):
        self.assertEqual(fanout.send_interval(1, 2), 0.5)
        self.assertEqual(fanout.send_interval(2, 2), 1.0)
        self.assertEqual(fanout.estimate_seconds(69, 2, 2), 69.0)
        self.assertEqual(fanout.estimate_seconds(69, 1, 2), 34.5)

    def test_rate_must_stay_below_shared_limit(self):
        self.assertEqual(fanout.rate_per_second({}), 2.0)
        self.assertEqual(fanout.rate_per_second({"fanout_rate_per_second": 3}), 2.0)
        self.assertEqual(fanout.rate_per_second({"fanout_rate_per_second": 0}), 2.0)
        self.assertEqual(fanout.rate_per_second({"fanout_rate_per_second": "x"}), 2.0)
        self.assertEqual(fanout.rate_per_second({"fanout_rate_per_second": 1.5}), 1.5)

    def test_worker_paces_by_segments(self):
        self.alerts.items[(CAMPAIGN, SENT_AT)]["what_changed"] = "A" * 200  # 2 segments with prefix and suffix
        self.live()
        t0 = self.clock.t
        self.run_job()
        self.assertEqual(len(self.twilio_calls), 4)
        self.assertAlmostEqual(self.clock.t - t0, 3.0)  # 4 messages x 2 segments at 2/s: starts at 0,1,2,3 s

    def test_one_segment_paces_at_half_second(self):
        self.live()
        t0 = self.clock.t
        self.run_job()
        self.assertAlmostEqual(self.clock.t - t0, 1.5)

    def test_shared_limiter_spaces_two_workers_together(self):
        a, b = fw.Pacer(2.0, self.ops), fw.Pacer(2.0, self.ops)
        starts = []
        for p in (a, b, a, b, a, b):
            p.wait(1)
            starts.append(self.clock.t)
        gaps = [round(y - x, 3) for x, y in zip(starts, starts[1:])]
        self.assertEqual(gaps, [0.5] * 5)  # aggregate 2 segments/s, second worker starts one slot after the first

    def test_limiter_slot_is_reused_after_idle(self):
        fw.reserve_slot(self.ops, 1.0)
        self.clock.t += 60
        self.assertEqual(fw.reserve_slot(self.ops, 1.0), self.clock.t)


class BackToBackTest(FanoutTestCase):
    """Two alerts' jobs running at the same time (two workers) start within a slot of each other and
    never exceed fanout_rate_per_second together."""

    def test_two_concurrent_jobs_start_together_and_share_the_rate(self):
        import threading
        import time as real_time
        fw._clock.update(now=real_time.time, mono=real_time.monotonic, sleep=real_time.sleep)
        guard = threading.Lock()
        for table in (self.ops, self.sends, self.subs, self.alerts, self.audit):
            for name in ("get_item", "put_item", "update_item", "query", "delete_item"):
                orig = getattr(table, name)
                setattr(table, name, (lambda f: lambda *a, **k: (guard.acquire(), f(*a, **k), guard.release())[1])(orig))
        now = store.now_iso(real_time.time())
        second = store.now_iso(real_time.time() + 0.002)
        for sa in (now, second):
            self.alerts.put_item(Item={**self.alerts.items[(CAMPAIGN, SENT_AT)], "sent_at": sa, "created_at": now})
        self.live(fanout_rate_per_second=2.9)
        stamps = []
        orig_send = twilio_send.send_message

        def timed(*a, **k):
            stamps.append(real_time.time())
            return orig_send(*a, **k)
        twilio_send.send_message = timed
        threads = [threading.Thread(target=self.run_job, args=(self.job(sent_at=sa),)) for sa in (now, second)]
        t0 = real_time.time()
        for th in threads:
            th.start()
        for th in threads:
            th.join(30)
        twilio_send.send_message = orig_send
        self.assertEqual(len(stamps), 8)
        firsts = {}
        for (alert_id, phone), row in self.sends.items.items():
            if alert_id in (f"{CAMPAIGN}#{now}", f"{CAMPAIGN}#{second}") and phone != fw.RUN_SK:
                ts = datetime.fromisoformat(row["claimed_at"].replace("Z", "+00:00")).timestamp()
                firsts[alert_id] = min(firsts.get(alert_id, ts), ts)
        self.assertEqual(len(firsts), 2)
        self.assertLess(max(firsts.values()) - t0, 1.0)  # both alerts start within one second, not minutes
        stamps.sort()
        interval = 1 / 2.9
        self.assertTrue(all(y - x >= interval - 0.02 for x, y in zip(stamps, stamps[1:])))

    def test_terraform_concurrency_never_throttles_jobs(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "terraform", "fanout.tf")) as f:
            tf = f.read()
        with open(os.path.join(os.path.dirname(fw.__file__), "alert_composer.py")) as f:
            composer = f.read()
        self.assertIn("reserved_concurrent_executions = local.fanout_concurrency", tf)
        self.assertRegex(tf, r"scaling_config \{\s+maximum_concurrency = local.fanout_concurrency")
        conc = int(re.search(r"fanout_concurrency\s*=\s*(\d+)", tf).group(1))
        self.assertGreaterEqual(conc, 2)
        self.assertIn("DelaySeconds=delay", composer)


class DelayAndComposerTest(unittest.TestCase):
    def test_delay_defaults_and_clamps(self):
        self.assertEqual(fanout.delay_seconds({}), 10)
        self.assertEqual(fanout.delay_seconds({"fanout_delay_seconds": 0}), 5)
        self.assertEqual(fanout.delay_seconds({"fanout_delay_seconds": 5000}), 900)
        self.assertEqual(fanout.delay_seconds({"fanout_delay_seconds": "x"}), 10)
        self.assertEqual(fanout.delay_seconds({"fanout_delay_seconds": 30}), 30)

    def setUp(self):
        self.sqs = FakeSqs()
        ac._sqs["c"] = self.sqs
        self._url, ac.FANOUT_QUEUE_URL = ac.FANOUT_QUEUE_URL, "https://sqs.test/queue"

    def tearDown(self):
        ac._sqs.clear()
        ac.FANOUT_QUEUE_URL = self._url

    def test_composer_enqueues_one_delayed_job(self):
        alert = {"campaign_id": CAMPAIGN, "sent_at": SENT_AT, "is_test": True}
        self.assertEqual(ac.fanout_hook(alert, {"fanout_mode": "dry_run", "fanout_delay_seconds": 12}), "m1")
        ((url, job, delay),) = self.sqs.sent
        self.assertEqual((job["campaign_id"], job["sent_at"], job["seq"], job["is_test"], delay),
                         (CAMPAIGN, SENT_AT, 0, True, 12))

    def test_composer_enqueues_nothing_when_off_or_unset(self):
        alert = {"campaign_id": CAMPAIGN, "sent_at": SENT_AT}
        for cfg in (None, {}, {"fanout_mode": "off", "broadcast_enabled": True}, {"fanout_mode": "nonsense"}):
            self.assertIsNone(ac.fanout_hook(alert, cfg))
        self.assertEqual(self.sqs.sent, [])

    def test_enqueue_failure_never_raises(self):
        class Broken:
            def send_message(self, **kw):
                raise RuntimeError("sqs down")
        ac._sqs["c"] = Broken()
        self.assertIsNone(ac.fanout_hook({"campaign_id": CAMPAIGN, "sent_at": SENT_AT}, {"fanout_mode": "live"}))


if __name__ == "__main__":
    unittest.main()
