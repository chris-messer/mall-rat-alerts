"""PostHog capture for Discord posts and SMS fan-out blasts. Never phones, webhooks, or secrets."""

import json
import os
import re
import unittest
from unittest import mock

os.environ.update({"TEMPLATES_TABLE": "templates", "ALERTS_TABLE": "alerts", "OPS_TABLE": "ops",
                   "MODEL_IDS": "model-a,model-b,model-c", "SUBSCRIBERS_TABLE": "subs", "AUDIT_TABLE": "audit",
                   "SENDS_TABLE": "sends", "QUEUE_URL": "https://sqs.test/queue", "DLQ_URL": "https://sqs.test/dlq",
                   "TWILIO_FROM_NUMBER": "+18335550100", "TWILIO_WEBHOOK_BASE": "https://example.test"})
from fakes import FakeTable, install_stubs

install_stubs()

import alert_composer as ac  # noqa: E402
import fanout_worker as fw  # noqa: E402
from common import analytics, discord, store  # noqa: E402
from test_composer import CAMPAIGN, CHANGES, GOOD, FakeBedrock, reply  # noqa: E402
from test_discord import Base as DiscordBase, Resp, URL, KEY, SENT_AT  # noqa: E402
from test_fanout import FanoutTestCase, PHONES, ALERT_ID  # noqa: E402

PHONE_SHAPED = re.compile(r"\+?\d{10,}")


class CaptureSpy:
    def __init__(self):
        self.events = []

    def __call__(self, event, distinct_id, properties=None, **kw):
        self.events.append((event, distinct_id, dict(properties or {})))
        return True

    def named(self, name):
        return [e for e in self.events if e[0] == name]

    def blob(self):
        return json.dumps(self.events, default=str)


class AnalyticsCaptureTest(unittest.TestCase):
    def test_missing_key_is_noop(self):
        self.assertFalse(analytics.capture("x", "id", key=""))

    def test_failure_does_not_raise(self):
        def boom(*a, **k):
            raise RuntimeError("down")
        self.assertFalse(analytics.capture("x", "id", post=boom, key="phc_test"))


class DiscordPostHogTest(DiscordBase):
    def setUp(self):
        super().setUp()
        self.spy = CaptureSpy()
        self._cap = analytics.capture
        analytics.capture = self.spy
        self._key = analytics.POSTHOG_KEY
        analytics.POSTHOG_KEY = "phc_test_key"

    def tearDown(self):
        analytics.capture = self._cap
        analytics.POSTHOG_KEY = self._key
        super().tearDown()

    def test_capture_on_successful_post(self):
        with mock.patch("urllib.request.urlopen", lambda r, timeout=None: Resp(204)):
            self.assertEqual(discord.post_public_alert(CAMPAIGN, "Hello mall rats", SENT_AT), 204)
        posted = self.spy.named("alert_discord_posted")
        self.assertEqual(len(posted), 1)
        event, did, props = posted[0]
        self.assertEqual(did, f"alert:{CAMPAIGN}:{SENT_AT}")
        self.assertEqual(props["campaign_id"], CAMPAIGN)
        self.assertEqual(props["alert_id"], SENT_AT)
        self.assertEqual(props["channel"], "alerts")
        self.assertEqual(props["http_status"], 204)
        self.assertEqual(props["body_chars"], len("Hello mall rats"))
        self.assertFalse(props["skipped"])
        self.assertNotIn("webhook", self.spy.blob().lower())
        self.assertIsNone(PHONE_SHAPED.search(self.spy.blob()))

    def test_skipped_on_already_posted(self):
        with mock.patch("urllib.request.urlopen", lambda r, timeout=None: Resp(204)):
            discord.post_public_alert(CAMPAIGN, "x", SENT_AT, self.table)
            discord.post_public_alert(CAMPAIGN, "x", SENT_AT, self.table)
        self.assertEqual(len(self.spy.named("alert_discord_posted")), 1)
        skipped = self.spy.named("alert_discord_skipped")
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0][2]["skipped_reason"], "already_posted")

    def test_skipped_on_unmapped_campaign(self):
        with mock.patch("urllib.request.urlopen") as op:
            self.assertIsNone(discord.post_public_alert("other-campaign-2026", "x", SENT_AT))
            op.assert_not_called()
        skipped = self.spy.named("alert_discord_skipped")
        self.assertEqual(skipped[0][2]["skipped_reason"], "unmapped")

    def test_capture_failure_does_not_raise_or_block_post(self):
        def boom(*a, **k):
            raise RuntimeError("posthog down")
        analytics.capture = boom
        with mock.patch("urllib.request.urlopen", lambda r, timeout=None: Resp(204)):
            self.assertEqual(discord.post_public_alert(CAMPAIGN, "x", SENT_AT), 204)


class ComposerDiscordSkipPostHogTest(DiscordBase):
    def setUp(self):
        super().setUp()
        self.templates, self.alerts, self.ops = FakeTable("pk", "sk"), FakeTable("campaign_id", "sent_at"), FakeTable("pk", "sk")
        ac._tables.clear()
        ac._tables.update(templates=self.templates, alerts=self.alerts, ops=self.ops)
        ac._bedrock["c"] = FakeBedrock(*[reply(GOOD) for _ in range(5)])
        self._ph_key, ac.POSTHOG_KEY = ac.POSTHOG_KEY, ""
        store.reset_config_cache()
        self.spy = CaptureSpy()
        self._cap = analytics.capture
        analytics.capture = self.spy
        analytics.POSTHOG_KEY = "phc_test_key"
        self.posts = []
        self._fn = discord.post_public_alert
        discord.post_public_alert = lambda *a, **k: self.posts.append(a) or 204

    def tearDown(self):
        discord.post_public_alert = self._fn
        analytics.capture = self._cap
        analytics.POSTHOG_KEY = ""
        store.reset_config_cache()
        ac._bedrock.clear()
        ac.POSTHOG_KEY = self._ph_key
        super().tearDown()

    def event(self, eid="e1", **detail):
        return {"id": eid, "source": "fob-site-monitor", "detail-type": "SiteChange", "time": "2026-11-06T17:21:03Z",
                "detail": {"campaign_id": CAMPAIGN, "changes": CHANGES, "detected_at": "2026-11-06T17:21:03Z", **detail}}

    def test_test_event_fires_skipped(self):
        ac.handler(self.event("a", test=True), None)
        skipped = self.spy.named("alert_discord_skipped")
        self.assertTrue(any(p.get("skipped_reason") == "test" for _, _, p in skipped))
        self.assertEqual(self.posts, [])

    def test_dry_run_fires_skipped(self):
        ac.handler(self.event("a", dry_run=True), None)
        self.assertTrue(any(p.get("skipped_reason") == "event_dry_run" for _, _, p in self.spy.named("alert_discord_skipped")))
        self.ops.put_item(Item={"pk": "config#global", "sk": "current", "fanout_mode": "dry_run"})
        store.reset_config_cache()
        ac.handler(self.event("b"), None)
        self.assertTrue(any(p.get("skipped_reason") == "fanout_dry_run" for _, _, p in self.spy.named("alert_discord_skipped")))

    def test_notify_false_fires_skipped_not_posted(self):
        ac._bedrock["c"] = FakeBedrock(reply({**GOOD, "notify": False}))
        res = ac.handler(self.event("nf"), None)
        self.assertEqual(res["status"], "suppressed")
        self.assertEqual(self.posts, [])
        skipped = self.spy.named("alert_discord_skipped")
        self.assertTrue(skipped)
        self.assertEqual(self.spy.named("alert_discord_posted"), [])
        self.assertIsNone(PHONE_SHAPED.search(self.spy.blob()))


class FanoutPostHogTest(FanoutTestCase):
    def setUp(self):
        super().setUp()
        self.spy = CaptureSpy()
        self._cap = analytics.capture
        analytics.capture = self.spy
        self._key = analytics.POSTHOG_KEY
        analytics.POSTHOG_KEY = "phc_test_key"

    def tearDown(self):
        analytics.capture = self._cap
        analytics.POSTHOG_KEY = self._key
        super().tearDown()

    def test_blast_events_on_live_complete(self):
        self.live()
        res = self.run_job()
        self.assertEqual(res["completed"]["sent"], 4)
        blasts = self.spy.named("alert_sms_blast")
        self.assertEqual(len(blasts), 2)  # started + completed
        phases = {p["phase"] for _, _, p in blasts}
        self.assertEqual(phases, {"started", "completed"})
        done = [p for _, _, p in blasts if p["phase"] == "completed"][0]
        self.assertEqual(done["campaign_id"], CAMPAIGN)
        self.assertEqual(done["alert_id"], ALERT_ID)
        self.assertEqual(done["recipient_count"], 4)
        self.assertEqual(done["sent_count"], 4)
        self.assertEqual(done["failed_count"], 0)
        self.assertEqual(done["skipped_count"], 0)
        self.assertEqual(done["fanout_mode"], "live")
        self.assertEqual(done["segments_per_message"], 1)
        for _, did, props in blasts:
            self.assertEqual(did, f"fanout:{ALERT_ID}")
            self.assertIsNone(PHONE_SHAPED.search(did))
            self.assertIsNone(PHONE_SHAPED.search(json.dumps(props)))
            for phone in PHONES:
                self.assertNotIn(phone, did)
                self.assertNotIn(phone, json.dumps(props))

    def test_blast_on_dry_run_complete(self):
        self.set_config(fanout_mode="dry_run")
        self.run_job()
        blasts = self.spy.named("alert_sms_blast")
        self.assertTrue(any(p["phase"] == "started" for _, _, p in blasts))
        done = [p for _, _, p in blasts if p["phase"] == "completed"][0]
        self.assertEqual(done["fanout_mode"], "dry_run")
        self.assertEqual(done["sent_count"], 0)
        self.assertEqual(done["would_send"], 4)

    def test_capture_failure_does_not_break_fanout(self):
        def boom(*a, **k):
            raise OSError("posthog down")

        self.assertFalse(self._cap("alert_sms_blast", "fanout:x", post=boom, key="phc_test"))
        analytics.capture = lambda *a, **k: False
        self.live()
        res = self.run_job()
        self.assertEqual(res["completed"]["sent"], 4)

    def test_no_capture_without_key(self):
        posts = []

        def gated(event, distinct_id, properties=None, post=None, key=None):
            k = analytics.POSTHOG_KEY if key is None else key
            if not k:
                return False
            posts.append(event)
            return True

        analytics.capture = gated
        analytics.POSTHOG_KEY = ""
        self.live()
        self.run_job()
        self.assertEqual(posts, [])
        self.assertEqual(len(self.recipients_texted()), 4)


if __name__ == "__main__":
    unittest.main()
