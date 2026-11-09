"""Discord public-alert posting: secret lookup, mapping, never raises, 429, idempotency, composer gating."""

import io
import json
import os
import unittest
import urllib.error
from unittest import mock

os.environ.update({"TEMPLATES_TABLE": "templates", "ALERTS_TABLE": "alerts", "OPS_TABLE": "ops",
                   "MODEL_IDS": "model-a"})

from fakes import FakeTable, install_stubs

install_stubs()

import alert_composer as ac  # noqa: E402
from common import discord, store  # noqa: E402
from test_composer import CAMPAIGN, CHANGES, GOOD, FakeBedrock, reply  # noqa: E402

URL = "https://discord.example/api/webhooks/123/SECRET-TOKEN-VALUE"
KEY = "discord_webhook_twilight_town_mall"
SENT_AT = "2026-11-06T17:21:03.000Z"


class FakeSecrets:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.calls = payload if payload is not None else {KEY: URL, "twilio_auth_token": "x"}, error, 0

    def get_secret_value(self, SecretId):
        self.calls += 1
        if self.error:
            raise self.error
        return {"SecretString": json.dumps(self.payload)}


class Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, body=None):
    return urllib.error.HTTPError("https://discord.example/x", code, "err", {}, io.BytesIO(json.dumps(body or {}).encode()))


class Base(unittest.TestCase):
    def setUp(self):
        discord._secret_cache.clear()
        self.env = mock.patch.dict(os.environ, {"DISCORD_SECRET_ID": "arn:secret",
                                                "DISCORD_CHANNELS": json.dumps({CAMPAIGN: KEY})})
        self.env.start()
        self.secrets = FakeSecrets()
        import boto3
        self._client = boto3.client
        boto3.client = lambda name, **kw: self.secrets
        self.sleeps = []
        self._sleep = discord.time.sleep
        discord.time.sleep = self.sleeps.append
        self.table = FakeTable("campaign_id", "sent_at")
        self.table.put_item(Item={"campaign_id": CAMPAIGN, "sent_at": SENT_AT, "status": "published"})

    def tearDown(self):
        import boto3
        boto3.client = self._client
        discord.time.sleep = self._sleep
        self.env.stop()
        discord._secret_cache.clear()


class PostTest(Base):
    def test_uses_secret_url_posts_json_and_caches(self):
        seen = []

        def fake_open(req, timeout=None):
            seen.append((req.full_url, json.loads(req.data), timeout, req.get_header("User-agent")))
            return Resp(204)

        with mock.patch("urllib.request.urlopen", fake_open):
            self.assertEqual(discord.post_public_alert(CAMPAIGN, "Hello world", SENT_AT), 204)
            self.assertEqual(discord.post_public_alert(CAMPAIGN, "Again", "2026-11-06T18:00:00.000Z"), 204)
        self.assertEqual(seen[0][0], URL)
        self.assertEqual(seen[0][2], 5)
        self.assertNotIn("python-urllib", (seen[0][3] or "").lower())
        emb = seen[0][1]["embeds"][0]
        self.assertEqual(emb["title"], "Twilight Town Mall")
        self.assertEqual(emb["description"], "Hello world")
        self.assertEqual(seen[0][1]["allowed_mentions"], {"parse": []})
        self.assertEqual(self.secrets.calls, 1)  # cached per warm container

    def test_url_never_logged(self):
        with mock.patch("urllib.request.urlopen", lambda r, timeout=None: (_ for _ in ()).throw(http_error(500))):
            with self.assertLogs(level="DEBUG") as logs:
                discord.log.info("start")
                discord.post_public_alert(CAMPAIGN, "x", SENT_AT)
        self.assertNotIn("SECRET-TOKEN-VALUE", "\n".join(logs.output))
        self.assertNotIn("discord.example", "\n".join(logs.output))

    def test_unmapped_campaign_is_skipped(self):
        with mock.patch("urllib.request.urlopen") as op:
            self.assertIsNone(discord.post_public_alert("other-campaign-2026", "x", SENT_AT))
            op.assert_not_called()
        self.assertEqual(self.secrets.calls, 0)

    def test_empty_mapping_turns_it_off(self):
        with mock.patch.dict(os.environ, {"DISCORD_CHANNELS": "{}"}), mock.patch("urllib.request.urlopen") as op:
            self.assertIsNone(discord.post_public_alert(CAMPAIGN, "x", SENT_AT))
            op.assert_not_called()

    def test_missing_secret_key_or_unreadable_secret_skips(self):
        with mock.patch("urllib.request.urlopen") as op:
            self.secrets.payload = {"twilio_auth_token": "x"}
            self.assertIsNone(discord.post_public_alert(CAMPAIGN, "x", SENT_AT))
            self.secrets.error = RuntimeError("denied")
            self.assertIsNone(discord.post_public_alert(CAMPAIGN, "x", SENT_AT))
            op.assert_not_called()

    def test_never_raises_on_http_errors_timeouts_or_garbage(self):
        for exc in (http_error(500), http_error(403), TimeoutError("t"), urllib.error.URLError("down"), ValueError("x")):
            with mock.patch("urllib.request.urlopen", side_effect=exc):
                discord.post_public_alert(CAMPAIGN, "x", SENT_AT)
        self.assertIsNone(discord.post_public_alert(CAMPAIGN, None, None, table=object()))

    def test_429_honors_retry_after_once_then_succeeds(self):
        calls = []

        def fake_open(req, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise http_error(429, {"retry_after": 1.5})
            return Resp(204)

        with mock.patch("urllib.request.urlopen", fake_open):
            self.assertEqual(discord.post_public_alert(CAMPAIGN, "x", SENT_AT), 204)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.sleeps, [1.5])

    def test_429_twice_gives_up_after_one_retry(self):
        calls = []

        def fake_open(req, timeout=None):
            calls.append(1)
            raise http_error(429, {"retry_after": 1})

        with mock.patch("urllib.request.urlopen", fake_open):
            self.assertEqual(discord.post_public_alert(CAMPAIGN, "x", SENT_AT), 429)
        self.assertEqual(len(calls), 2)

    def test_429_with_long_wait_does_not_retry(self):
        calls = []

        def fake_open(req, timeout=None):
            calls.append(1)
            raise http_error(429, {"retry_after": 120})

        with mock.patch("urllib.request.urlopen", fake_open):
            self.assertEqual(discord.post_public_alert(CAMPAIGN, "x", SENT_AT), 429)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_long_text_is_truncated_at_or_under_4096(self):
        seen = []
        words = " ".join(f"word{i}" for i in range(1200))  # well over 4000 chars, with word boundaries
        with mock.patch("urllib.request.urlopen", lambda r, timeout=None: seen.append(json.loads(r.data)) or Resp(204)):
            discord.post_public_alert(CAMPAIGN, words, SENT_AT)
        desc = seen[0]["embeds"][0]["description"]
        self.assertLessEqual(len(desc), 4096)
        self.assertLessEqual(len(desc), discord.MAX_DESCRIPTION)
        self.assertTrue(desc.endswith("..."))
        self.assertNotIn("  ", desc)  # clipped on a word, not mid-run of spaces

    def test_embed_total_chars_under_6000(self):
        seen = []
        body = " ".join(["site-change-summary"] * 500)
        with mock.patch("urllib.request.urlopen", lambda r, timeout=None: seen.append(json.loads(r.data)) or Resp(204)):
            discord.post_public_alert(CAMPAIGN, body, SENT_AT)
        emb = seen[0]["embeds"][0]
        total = discord._embed_chars(emb)
        self.assertLessEqual(total, 6000)
        self.assertLessEqual(len(emb["title"]), 256)
        self.assertLessEqual(len(emb["footer"]["text"]), 2048)

    def test_clip_words_breaks_on_word_boundary(self):
        text = "alpha beta gamma delta epsilon"
        out = discord.clip_words(text, 20)
        self.assertLessEqual(len(out), 20)
        self.assertTrue(out.endswith("..."))
        self.assertFalse(out[:-3].endswith("epsil"))  # not a mid-word cut of epsilon
        self.assertIn(out[:-3].rstrip(), ("alpha beta gamma", "alpha beta", "alpha"))

    def test_marker_prevents_double_post(self):
        calls = []
        with mock.patch("urllib.request.urlopen", lambda r, timeout=None: calls.append(1) or Resp(204)):
            self.assertEqual(discord.post_public_alert(CAMPAIGN, "x", SENT_AT, self.table), 204)
            self.assertIsNone(discord.post_public_alert(CAMPAIGN, "x", SENT_AT, self.table))
        self.assertEqual(len(calls), 1)
        self.assertIn("discord_posted_at", self.table.items[(CAMPAIGN, SENT_AT)])

    def test_definite_failure_releases_marker_timeout_keeps_it(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(500)):
            self.assertEqual(discord.post_public_alert(CAMPAIGN, "x", SENT_AT, self.table), 500)
        self.assertNotIn("discord_posted_at", self.table.items[(CAMPAIGN, SENT_AT)])
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("t")):
            self.assertIsNone(discord.post_public_alert(CAMPAIGN, "x", SENT_AT, self.table))
        self.assertIn("discord_posted_at", self.table.items[(CAMPAIGN, SENT_AT)])


class ComposerGatingTest(Base):
    def setUp(self):
        super().setUp()
        self.templates, self.alerts, self.ops = FakeTable("pk", "sk"), FakeTable("campaign_id", "sent_at"), FakeTable("pk", "sk")
        ac._tables.clear()
        ac._tables.update(templates=self.templates, alerts=self.alerts, ops=self.ops)
        ac._bedrock["c"] = FakeBedrock(*[reply(GOOD) for _ in range(5)])
        self._key, ac.POSTHOG_KEY = ac.POSTHOG_KEY, ""
        store.reset_config_cache()
        self.posts = []
        self._fn = discord.post_public_alert
        discord.post_public_alert = lambda *a, **k: self.posts.append(a) or 204

    def tearDown(self):
        discord.post_public_alert = self._fn
        store.reset_config_cache()
        ac._bedrock.clear()
        ac.POSTHOG_KEY = self._key
        super().tearDown()

    def event(self, eid="e1", **detail):
        return {"id": eid, "source": "fob-site-monitor", "detail-type": "SiteChange", "time": "2026-11-06T17:21:03Z",
                "detail": {"campaign_id": CAMPAIGN, "changes": CHANGES, "detected_at": "2026-11-06T17:21:03Z", **detail}}

    def test_real_event_posts_after_fanout_with_stored_text(self):
        order = []
        orig = ac.fanout_hook
        ac.fanout_hook = lambda *a, **k: order.append("fanout")
        discord.post_public_alert = lambda *a, **k: order.append("discord") or self.posts.append(a)
        try:
            res = ac.handler(self.event(), None)
        finally:
            ac.fanout_hook = orig
        self.assertEqual(order, ["fanout", "discord"])
        (call,) = self.posts
        self.assertEqual(call[0], CAMPAIGN)
        self.assertEqual(call[2], res["sent_at"])
        self.assertIn("Aces Hifi Records", call[1])
        stored = self.alerts.items[(CAMPAIGN, res["sent_at"])]
        self.assertIn("discord_message", stored)
        # Discord body is the full stored text, optionally plus the first site URL.
        self.assertTrue(call[1] == stored["discord_message"] or call[1].startswith(stored["discord_message"] + "\n\n"))

    def test_discord_gets_full_body_while_sms_message_stays_capped(self):
        long_what = ("Aces Hifi Records opened a brand new listening booth with rare vinyl and "
                     "signed memorabilia near store 04 on falloutboy.com/aces-hifi-records. ") * 4
        long_note = "Thnks fr th mmrs. " + ("extra lyric nod word " * 20)
        ac._bedrock["c"] = FakeBedrock(reply({"notify": True, "what_changed": long_what, "note": long_note}))
        res = ac.handler(self.event(), None)
        stored = self.alerts.items[(CAMPAIGN, res["sent_at"])]
        from common import sms_text
        self.assertLessEqual(sms_text.units(stored["message"]), 306)
        self.assertGreater(len(stored["discord_message"]), len(stored["message"]))
        (call,) = self.posts
        self.assertTrue(call[1].startswith(stored["discord_message"]) or call[1] == stored["discord_message"])
        self.assertIn("https://www.falloutboy.com/aces-hifi-records", call[1])

    def test_not_posted_for_test_events(self):
        ac.handler(self.event("a", test=True), None)
        ev = self.event("b")
        ev["source"] = "fob-site-monitor.test"
        ac.handler(ev, None)
        self.assertEqual(self.posts, [])

    def test_not_posted_for_dry_run(self):
        ac.handler(self.event("a", dry_run=True), None)
        self.ops.put_item(Item={"pk": "config#global", "sk": "current", "fanout_mode": "dry_run"})
        store.reset_config_cache()
        ac.handler(self.event("b"), None)
        self.assertEqual(self.posts, [])

    def test_posts_when_fanout_off_or_live(self):
        ac.handler(self.event("a"), None)
        self.ops.put_item(Item={"pk": "config#global", "sk": "current", "fanout_mode": "live"})
        store.reset_config_cache()
        ac.handler(self.event("b"), None)
        self.assertEqual(len(self.posts), 2)

    def test_not_posted_when_status_not_published(self):
        self.assertIsNone(ac.discord_hook(self.alerts, {"status": "draft", "message": "x"}, CAMPAIGN, SENT_AT, None, False))
        self.assertEqual(self.posts, [])

    def test_discord_text_prefers_full_body_and_appends_site_url(self):
        fields = {"discord_message": "Full site-change summary.\n\nThnks fr th mmrs.",
                  "message": "Short SMS.",
                  "urls": ["https://www.falloutboy.com/aces-hifi-records"]}
        text = ac.discord_text_for(fields)
        self.assertTrue(text.startswith(fields["discord_message"]))
        self.assertIn("https://www.falloutboy.com/aces-hifi-records", text)
        self.assertNotIn("Short SMS.", text)
        # No Discord invite is injected.
        self.assertNotIn("discord.gg", text)
        # URL already in body is not duplicated.
        already = {"discord_message": "See https://www.falloutboy.com/x now.",
                   "urls": ["https://www.falloutboy.com/x"]}
        self.assertEqual(ac.discord_text_for(already), already["discord_message"])

    def test_discord_failure_never_breaks_composer(self):
        def boom(*a, **k):
            raise RuntimeError("discord down")
        discord.post_public_alert = boom
        res = ac.handler(self.event(), None)
        self.assertTrue(res["stored"])


if __name__ == "__main__":
    unittest.main()
