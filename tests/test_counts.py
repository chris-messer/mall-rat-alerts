"""Campaign counter item (counts#<campaign_id>): SMS transitions, failure isolation, Discord member push."""

import base64
import json
import time
import unittest
import urllib.parse

from apibase import CAMPAIGN, ORIGIN_SECRET
from fakes import ClientError, FakeTable, TEST_PHONE
from smsbase import SmsTestCase

import signup_api  # noqa: E402
from common import counts, store  # noqa: E402

PK = counts.counts_pk(CAMPAIGN)
KEY = (PK, "current")
GUILD = "1569100999606538352"
SECRET = "unit-test-member-secret"
USER = "111222333444555666"


class FlakyOps(FakeTable):
    """Ops table whose counter writes fail, to prove signup and STOP never depend on the counter."""

    def update_item(self, Key, *a, **kw):
        if str(Key.get("pk", "")).startswith("counts#"):
            raise ClientError("ProvisionedThroughputExceededException")
        return super().update_item(Key, *a, **kw)


class CounterBase(SmsTestCase):
    def setUp(self):
        super().setUp()
        self.seed()

    def seed(self, active=10, opted_out=1, discord=5):
        self.ops.put_item(Item={"pk": PK, "sk": "current", "sms_active": active, "sms_opted_out": opted_out,
                                "discord_members": discord})

    def c(self):
        item = self.ops.items[KEY]
        return item["sms_active"], item["sms_opted_out"]


class DeltaTest(unittest.TestCase):
    def test_deltas(self):
        self.assertEqual(counts.deltas(None, "ACTIVE"), (1, 0))
        self.assertEqual(counts.deltas("PENDING_SENDER", "ACTIVE"), (1, 0))
        self.assertEqual(counts.deltas("PENDING", "ACTIVE"), (1, 0))
        self.assertEqual(counts.deltas("OPTED_OUT", "ACTIVE"), (1, -1))
        self.assertEqual(counts.deltas("ACTIVE", "OPTED_OUT"), (-1, 1))
        self.assertEqual(counts.deltas("PENDING_SENDER", "OPTED_OUT"), (0, 1))
        self.assertEqual(counts.deltas("ACTIVE", "ACTIVE"), (0, 0))
        self.assertEqual(counts.deltas("OPTED_OUT", "OPTED_OUT"), (0, 0))
        self.assertEqual(counts.deltas(None, "PENDING_SENDER"), (0, 0))

    def test_get_counts_missing_and_ints(self):
        ops = FakeTable("pk", "sk")
        self.assertIsNone(counts.get_counts(ops, CAMPAIGN))
        ops.put_item(Item={"pk": PK, "sk": "current", "sms_active": 3, "discord_synced_at": "x"})
        self.assertEqual(counts.get_counts(ops, CAMPAIGN), {"sms_active": 3, "discord_synced_at": "x"})

    def test_increment_never_creates_the_item(self):
        ops = FakeTable("pk", "sk")
        self.assertFalse(counts.increment_sms(ops, CAMPAIGN, active=1))
        self.assertEqual(ops.items, {})


class SmsTransitionTest(CounterBase):
    def test_keyword_new_row_adds_one_active(self):
        self.sms("FOB")
        self.assertEqual(self.c(), (11, 1))

    def test_keyword_repeat_does_not_change_counter(self):
        self.sms("FOB")
        self.sms("FOB")
        self.assertEqual(self.c(), (11, 1))

    def test_keyword_promotes_pending_web_signup(self):
        self.member(status="PENDING_SENDER")
        self.sms("FOB")
        self.assertEqual(self.c(), (11, 1))

    def test_web_signup_is_pending_until_yes(self):
        res = self.call("/api/subscribe", self.signup())
        self.assertEqual(res["statusCode"], 202)
        self.assertEqual(self.c(), (10, 1))
        self.sms("YES")
        self.assertEqual(self.c(), (11, 1))
        self.sms("YES")  # repeat YES: already active, no change
        self.assertEqual(self.c(), (11, 1))

    def test_web_duplicate_signup_does_not_change_counter(self):
        self.member(status="ACTIVE")
        self.call("/api/subscribe", self.signup())
        self.assertEqual(self.c(), (10, 1))

    def test_stop_moves_active_to_opted_out_once(self):
        self.member(status="ACTIVE")
        self.sms("STOP")
        self.assertEqual(self.c(), (9, 2))
        self.sms("STOP")
        self.assertEqual(self.c(), (9, 2))

    def test_stop_then_start_then_keyword(self):
        self.member(status="ACTIVE")
        self.sms("STOP")
        self.assertEqual(self.c(), (9, 2))
        self.sms("START")  # START alone does not resubscribe in this system
        self.assertEqual(self.c(), (9, 2))
        self.sms("FOB")
        self.assertEqual(self.c(), (10, 1))
        self.assertEqual(self.subs.items[(TEST_PHONE, CAMPAIGN)]["status"], "ACTIVE")

    def test_web_unsubscribe(self):
        self.member(status="ACTIVE")
        self.call("/api/unsubscribe", {"phone": TEST_PHONE, "campaign_id": CAMPAIGN})
        self.assertEqual(self.c(), (9, 2))
        self.call("/api/unsubscribe", {"phone": TEST_PHONE, "campaign_id": CAMPAIGN})
        self.assertEqual(self.c(), (9, 2))

    def test_unsubscribe_of_pending_only_counts_opted_out(self):
        self.member(status="PENDING_SENDER")
        self.call("/api/unsubscribe", {"phone": TEST_PHONE, "campaign_id": CAMPAIGN})
        self.assertEqual(self.c(), (10, 2))

    def test_carrier_21610_status_callback(self):
        self.member(status="ACTIVE")
        res = self.sms("", path="/api/twilio/status", extra={"To": TEST_PHONE, "MessageStatus": "failed",
                                                                    "ErrorCode": "21610"})
        self.assertEqual(res["statusCode"], 204)
        self.assertEqual(self.c(), (9, 2))
        self.sms("", path="/api/twilio/status", extra={"To": TEST_PHONE, "MessageStatus": "failed",
                                                                    "ErrorCode": "21610"})
        self.assertEqual(self.c(), (9, 2))

    def test_put_subscriber_active_over_active_is_idempotent(self):
        item = {"phone": TEST_PHONE, "campaign_id": CAMPAIGN, "status": "ACTIVE"}
        store.put_subscriber(self.subs, dict(item), ops_table=self.ops)
        self.assertEqual(self.c(), (11, 1))
        store.put_subscriber(self.subs, dict(item), ops_table=self.ops)
        self.assertEqual(self.c(), (11, 1))

    def test_counter_not_seeded_is_not_created(self):
        del self.ops.items[KEY]
        self.sms("FOB")
        self.assertNotIn(KEY, self.ops.items)
        self.assertEqual(self.subs.items[(TEST_PHONE, CAMPAIGN)]["status"], "ACTIVE")


class CounterFailureTest(CounterBase):
    def setUp(self):
        super().setUp()
        flaky = FlakyOps("pk", "sk")
        flaky.items = self.ops.items
        self.ops = flaky
        signup_api._tables["ops"] = flaky

    def test_keyword_signup_survives_counter_failure(self):
        res = self.sms("FOB")
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(self.subs.items[(TEST_PHONE, CAMPAIGN)]["status"], "ACTIVE")
        self.assertIn("subscribe_sms_tag", self.audit_events())
        self.assertEqual(self.c(), (10, 1))

    def test_stop_survives_counter_failure(self):
        self.member(status="ACTIVE")
        res = self.sms("STOP")
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(self.subs.items[(TEST_PHONE, CAMPAIGN)]["status"], "OPTED_OUT")
        self.assertIn("sms_stop", self.audit_events())

    def test_yes_and_web_unsubscribe_survive_counter_failure(self):
        self.member(status="PENDING_SENDER")
        self.sms("YES")
        self.assertEqual(self.subs.items[(TEST_PHONE, CAMPAIGN)]["status"], "ACTIVE")
        res = self.call("/api/unsubscribe", {"phone": TEST_PHONE, "campaign_id": CAMPAIGN})
        self.assertEqual(res["statusCode"], 202)
        self.assertEqual(self.subs.items[(TEST_PHONE, CAMPAIGN)]["status"], "OPTED_OUT")


class DiscordMemberEventTest(CounterBase):
    def setUp(self):
        super().setUp()
        self._saved = (signup_api.DISCORD_GUILD_ID, signup_api._member_secret["value"])
        signup_api.DISCORD_GUILD_ID = GUILD
        signup_api._member_secret["value"] = SECRET
        self.addCleanup(lambda: (setattr(signup_api, "DISCORD_GUILD_ID", self._saved[0]),
                                 signup_api._member_secret.update(value=self._saved[1])))

    def post(self, event="join", user=USER, joined_at="2026-11-09T18:00:00.000000+00:00", guild=GUILD,
             member_count=None, ts=None, secret=SECRET, sig=None, origin=ORIGIN_SECRET):
        ts = int(time.time()) if ts is None else ts
        user = "" if event == "sync" else user
        sig = sig or signup_api.member_event_sig(secret, event, guild, user,
                                                 joined_at if event == "join" else "",
                                                 member_count if member_count is not None else "", ts)
        body = {"event": event, "guild_id": guild, "ts": ts, "sig": sig}
        if user and event != "sync":
            body["user_id"] = user
        if event == "join":
            body["joined_at"] = joined_at
        if member_count is not None:
            body["member_count"] = member_count
        headers = {"content-type": "application/json"}
        if origin:
            headers["x-origin-verify"] = origin
        ev = {"rawPath": "/api/discord/member-event", "headers": headers,
              "requestContext": {"http": {"method": "POST"}}, "body": json.dumps(body), "isBase64Encoded": False}
        return signup_api.handler(ev, None)

    def members(self):
        return self.ops.items[KEY]["discord_members"]

    def test_join_adds_one_and_leaves_sms_untouched(self):
        self.assertEqual(self.post("join")["statusCode"], 204)
        self.assertEqual(self.members(), 6)
        self.assertEqual(self.c(), (10, 1))

    def test_replayed_join_is_deduped_but_a_rejoin_counts(self):
        self.post("join")
        self.post("join")
        self.assertEqual(self.members(), 6)
        self.post("join", joined_at="2026-11-09T19:00:00.000000+00:00")
        self.assertEqual(self.members(), 7)

    def test_leave_subtracts_one_and_duplicate_leave_is_collapsed(self):
        self.post("leave")
        self.post("leave")
        self.assertEqual(self.members(), 4)

    def test_leave_never_goes_below_zero(self):
        self.seed(discord=0)
        self.assertEqual(self.post("leave")["statusCode"], 204)
        self.assertEqual(self.members(), 0)

    def test_sync_sets_absolute_count(self):
        self.assertEqual(self.post("sync", member_count=27)["statusCode"], 204)
        self.assertEqual(self.members(), 27)
        self.assertIn("discord_synced_at", self.ops.items[KEY])

    def test_bad_signature_is_rejected_and_changes_nothing(self):
        self.assertEqual(self.post("join", secret="wrong")["statusCode"], 401)
        self.assertEqual(self.post("join", sig="0" * 64)["statusCode"], 401)
        self.assertEqual(self.members(), 5)

    def test_tampered_event_is_rejected(self):
        good = signup_api.member_event_sig(SECRET, "join", GUILD, USER, "2026-11-09T18:00:00.000000+00:00", "",
                                           int(time.time()))
        self.assertEqual(self.post("leave", sig=good)["statusCode"], 401)
        self.assertEqual(self.members(), 5)

    def test_stale_timestamp_is_rejected(self):
        self.assertEqual(self.post("join", ts=int(time.time()) - 3600)["statusCode"], 400)
        self.assertEqual(self.members(), 5)

    def test_wrong_guild_is_ignored(self):
        self.assertEqual(self.post("join", guild="999999999999999999")["statusCode"], 204)
        self.assertEqual(self.members(), 5)

    def test_requires_origin_header(self):
        self.assertEqual(self.post("join", origin=None)["statusCode"], 403)
        self.assertEqual(self.members(), 5)

    def test_route_disabled_without_guild_id(self):
        signup_api.DISCORD_GUILD_ID = ""
        self.assertEqual(self.post("join")["statusCode"], 404)

    def test_missing_secret_fails_closed(self):
        signup_api._member_secret["value"] = None
        old = signup_api.TWILIO_SECRET_ID
        signup_api.TWILIO_SECRET_ID = ""
        self.addCleanup(lambda: setattr(signup_api, "TWILIO_SECRET_ID", old))
        self.assertEqual(self.post("join")["statusCode"], 503)
        self.assertEqual(self.members(), 5)

    def test_not_seeded_counter_is_not_created(self):
        del self.ops.items[KEY]
        self.assertEqual(self.post("join")["statusCode"], 204)
        self.assertNotIn(KEY, self.ops.items)

    def test_counter_failure_returns_503_and_a_retry_still_counts(self):
        good_ops = self.ops
        flaky = FlakyOps("pk", "sk")
        flaky.items = good_ops.items
        signup_api._tables["ops"] = flaky
        self.assertEqual(self.post("join")["statusCode"], 503)
        self.assertEqual(self.members(), 5)
        signup_api._tables["ops"] = good_ops
        self.assertEqual(self.post("join")["statusCode"], 204)
        self.assertEqual(self.members(), 6)

    def test_malformed_requests(self):
        self.assertEqual(self.post("join", user="abc")["statusCode"], 400)
        self.assertEqual(self.post("bogus")["statusCode"], 400)
        self.assertEqual(self.post("sync", member_count=-4)["statusCode"], 400)
        self.assertEqual(self.members(), 5)


if __name__ == "__main__":
    unittest.main()
