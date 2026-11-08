"""Alert composer: format, sanitizing, length budget, fallbacks, idempotency, PostHog trace."""

import json
import os
import re
import unittest

os.environ.update({"TEMPLATES_TABLE": "templates", "ALERTS_TABLE": "alerts", "OPS_TABLE": "ops",
                   "MODEL_IDS": "model-a,model-b,model-c"})

from fakes import FakeTable, install_stubs

install_stubs()

import alert_composer as ac  # noqa: E402
from common import alert_text, llm_trace, sms_text  # noqa: E402

CAMPAIGN = "fob-twilight-town-mall-2026"
CHANGES = [{"url": "https://www.falloutboy.com/aces-hifi-records", "status": {"before": 404, "after": 200}}]
SELF_REF = re.compile(r"\b(AI|A\.I\.|artificial intelligence|language model|LLM|bot|GPT|Claude|Bedrock)\b", re.I)


def reply(obj):
    text = obj if isinstance(obj, str) else json.dumps(obj)
    return {"output": {"message": {"content": [{"text": text}]}}, "usage": {"inputTokens": 120, "outputTokens": 40},
            "stopReason": "end_turn", "ResponseMetadata": {"HTTPStatusCode": 200}}


class FakeBedrock:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def converse(self, **kw):
        self.calls.append(kw["modelId"])
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class Capture:
    def __init__(self):
        self.batches = []

    def __call__(self, url, body, timeout):
        self.batches.append((url, body))
        return 200


GOOD = {"notify": True, "what_changed": "Aces Hifi Records is now open. Look for store 04 at falloutboy.com/aces-hifi-records",
        "note": "Thnks fr th mmrs."}


class BuildAlertTest(unittest.TestCase):
    def setUp(self):
        self.posts = Capture()
        self._post = llm_trace._post
        llm_trace._post = self.posts
        self._key = ac.POSTHOG_KEY
        ac.POSTHOG_KEY = "phc_test_key"
        self.tpl = {**ac.DEFAULTS, "system_prompt": "SYSTEM PROMPT TEXT", "version": 3, "source": "dynamodb"}

    def tearDown(self):
        llm_trace._post = self._post
        ac.POSTHOG_KEY = self._key

    # The Trace class captures `post` at construction from module default, so patch the class default too.
    def build(self, bedrock, changes=CHANGES, priority=False, tpl=None):
        orig = llm_trace.Trace.__init__

        def init(s, *a, **k):
            k["post"] = self.posts
            orig(s, *a, **k)
        llm_trace.Trace.__init__ = init
        try:
            return ac.build_alert(bedrock, tpl or self.tpl, CAMPAIGN, changes, priority, {"source_event_id": "evt-1"})
        finally:
            llm_trace.Trace.__init__ = orig

    def test_format_is_description_blank_line_note(self):
        out = self.build(FakeBedrock(reply(GOOD)))
        self.assertEqual(out["message"], out["what_changed"] + "\n\n" + out["note"])
        self.assertEqual(out["note"], "Thnks fr th mmrs.")
        self.assertEqual((out["model"], out["encoding"]), ("model-a", "GSM-7"))
        self.assertLessEqual(out["segments"], 1)

    def test_sanitizes_dashes_quotes_emoji(self):
        bad = {"notify": True, "what_changed": "Store 04 \u2014 \u201cAces Hifi\u201d \U0001F496 is open\u2026 falloutboy.com/aces-hifi-records",
               "note": "Light \u2018em up."}
        out = self.build(FakeBedrock(reply(bad)))
        self.assertEqual(out["what_changed"], 'Store 04 - "Aces Hifi" is open... falloutboy.com/aces-hifi-records')
        self.assertEqual(out["note"], "Light 'em up.")
        self.assertTrue(sms_text.is_gsm7(out["message"]))

    def test_long_description_gets_trimmed_but_note_first(self):
        long_what = "A very long description of what happened on the site. " * 5 + "falloutboy.com/x"
        out = self.build(FakeBedrock(reply({"notify": True, "what_changed": long_what, "note": "Thnks fr th mmrs."})))
        self.assertLessEqual(sms_text.units(out["message"]), 306)
        self.assertGreaterEqual(out["segments"], 1)
        self.assertLessEqual(out["segments"], 2)

    def test_note_is_trimmed_before_description_to_hit_one_segment(self):
        what = "Aces Hifi Records is now open. Look for store 04 at falloutboy.com/aces-hifi-records"
        note = "This ain't a scene, it's a godd... word word word word word word word word word word word word word"
        out = self.build(FakeBedrock(reply({"notify": True, "what_changed": what, "note": note})))
        self.assertEqual(out["what_changed"], what)
        self.assertEqual(out["segments"], 1)
        self.assertLessEqual(sms_text.units(out["message"]), 160)
        self.assertTrue(out["note"].endswith("..."))

    def test_invalid_json_moves_to_next_model(self):
        b = FakeBedrock(reply("sorry, here is some prose"), reply(GOOD))
        out = self.build(b)
        self.assertEqual((out["model"], b.calls), ("model-b", ["model-a", "model-b"]))

    def test_missing_fields_and_wrong_types_rejected(self):
        for bad in ({"notify": True, "note": "x"}, {"notify": "maybe", "what_changed": "a", "note": "b"},
                    {"notify": True, "what_changed": ["a"], "note": "b"}):
            with self.assertRaises(alert_text.InvalidOutput):
                alert_text.parse_model_json(json.dumps(bad), "public")

    def test_self_reference_rejected(self):
        for phrase in ("Written by AI: store open", "This AI-generated note", "our language model says hi"):
            with self.assertRaises(alert_text.InvalidOutput):
                alert_text.parse_model_json(json.dumps({"notify": True, "what_changed": phrase, "note": "x"}), "public")

    def test_all_models_fail_uses_template_fallback(self):
        b = FakeBedrock(RuntimeError("boom1"), RuntimeError("boom2"), reply("nope"))
        out = self.build(b)
        self.assertTrue(out["fallback"])
        self.assertEqual(out["model"], "fallback:template")
        self.assertIn("falloutboy.com/aces-hifi-records", out["what_changed"])
        self.assertEqual(out["note"], "Thnks fr th mmrs.")
        self.assertEqual(out["message"], out["what_changed"] + "\n\n" + out["note"])
        self.assertEqual(len(b.calls), 3)

    def test_notify_false_still_produces_fallback_alert(self):
        b = FakeBedrock(reply({**GOOD, "notify": False}))
        with self.assertLogs(ac.log, "WARNING") as logs:
            out = self.build(b)
        self.assertTrue(out["fallback"])
        self.assertEqual((out["model"], out["model_notify"]), ("fallback:template", False))
        self.assertIn("notify=false", out["fallback_reason"])
        self.assertIn("falloutboy.com/aces-hifi-records", out["what_changed"])
        self.assertTrue(any("notify=false" in m for m in logs.output))

    def test_compose_crash_uses_fallback(self):
        orig = alert_text.compose
        alert_text.compose = lambda *a, **k: (_ for _ in ()).throw(ValueError("bad"))
        try:
            out = self.build(FakeBedrock())
        finally:
            alert_text.compose = orig
        self.assertTrue(out["fallback"])
        self.assertIn("ValueError", out["fallback_reason"])

    def test_priority_visit_alert_is_fixed_text_no_model(self):
        b = FakeBedrock()
        out = self.build(b, priority=True)
        self.assertEqual(b.calls, [])
        self.assertIn("Visit tab", out["what_changed"])
        self.assertEqual(out["segments"], 1)
        self.assertTrue(sms_text.is_gsm7(out["message"]))

    def test_posthog_one_trace_every_attempt_its_own_generation(self):
        self.build(FakeBedrock(RuntimeError("boom"), reply("not json"), reply(GOOD)))
        ((url, body),) = self.posts.batches
        self.assertTrue(url.endswith("/batch/"))
        self.assertEqual(body["api_key"], "phc_test_key")
        gens = [e for e in body["batch"] if e["event"] == "$ai_generation"]
        self.assertEqual(len(gens), 3)
        self.assertEqual(len({g["properties"]["$ai_trace_id"] for g in gens}), 1)
        self.assertEqual([g["properties"]["$ai_model"] for g in gens], ["model-a", "model-b", "model-c"])
        self.assertEqual([g["properties"]["$ai_is_error"] for g in gens], [True, True, False])
        p = gens[2]["properties"]
        for key in ("$ai_trace_id", "$ai_model", "$ai_provider", "$ai_input", "$ai_output_choices",
                    "$ai_input_tokens", "$ai_output_tokens", "$ai_latency", "$ai_http_status", "$ai_is_error"):
            self.assertIn(key, p)
        self.assertEqual((p["$ai_provider"], p["app"], p["template_version"], p["campaign_id"]),
                         ("bedrock", "mall-rat-alerts", 3, CAMPAIGN))
        self.assertEqual(p["distinct_id"], "mall-rat-alerts-composer")
        self.assertEqual(p["$ai_input"][0], {"role": "system", "content": "SYSTEM PROMPT TEXT"})
        self.assertEqual(p["$ai_input_tokens"], 120)
        self.assertEqual(body["batch"][-1]["event"], "$ai_trace")

    def test_posthog_never_gets_phone_numbers_or_secrets(self):
        changes = [{"url": "https://www.falloutboy.com/x", "visible_text_diff": "+Call 872-362-7625 or +15005550006 now"}]
        self.build(FakeBedrock(reply(GOOD)), changes=changes)
        blob = json.dumps(self.posts.batches)
        for n in ("872-362-7625", "5005550006", "362-7625"):
            self.assertNotIn(n, blob)
        self.assertIn("[phone]", blob)

    def test_posthog_failure_never_breaks(self):
        def boom(url, body, timeout):
            raise OSError("down")
        self.posts = boom
        out = self.build(FakeBedrock(reply(GOOD)))
        self.assertIn("Aces Hifi", out["message"])

    def test_no_posthog_without_key(self):
        ac.POSTHOG_KEY = ""
        orig = llm_trace.Trace.__init__
        t = llm_trace.Trace("", "h", "a", "d", "s", post=self.posts)
        t.generation("m", "s", "u")
        self.assertIsNone(t.finish())
        self.assertEqual(self.posts.batches, [])


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.templates = FakeTable("pk", "sk")
        self.alerts = FakeTable("campaign_id", "sent_at")
        self.ops = FakeTable("pk", "sk")
        ac._tables.clear()
        ac._tables.update(templates=self.templates, alerts=self.alerts, ops=self.ops)
        self.bedrock = FakeBedrock(*[reply(GOOD) for _ in range(5)])
        ac._bedrock["c"] = self.bedrock
        self._key = ac.POSTHOG_KEY
        ac.POSTHOG_KEY = ""

    def tearDown(self):
        ac._bedrock.clear()
        ac.POSTHOG_KEY = self._key

    def event(self, eid="evt-123", **detail):
        return {"id": eid, "source": "fob-site-monitor", "detail-type": "SiteChange", "time": "2026-11-06T17:21:03Z",
                "detail": {"campaign_id": CAMPAIGN, "changes": CHANGES, "detected_at": "2026-11-06T17:21:03Z", **detail}}

    def stored(self):
        return [i for i in self.alerts.items.values() if not i["campaign_id"].startswith("src#")]

    def test_stores_published_alert_with_all_fields(self):
        res = ac.handler(self.event(), None)
        self.assertTrue(res["stored"])
        (a,) = self.stored()
        self.assertEqual((a["status"], a["campaign_id"], a["source_event_id"], a["backfilled"]),
                         ("published", CAMPAIGN, "evt-123", False))
        self.assertEqual(a["sent_at"], "2026-11-06T17:21:03.000Z")
        self.assertEqual(a["message"], a["what_changed"] + "\n\n" + a["note"])
        for k in ("segments", "encoding", "template_version", "model", "urls"):
            self.assertIn(k, a)
        self.assertEqual(a["urls"], ["https://www.falloutboy.com/aces-hifi-records"])
        self.assertEqual(a["template_version"], 0)  # default template when the table has none
        self.assertNotIn("is_test", a)

    def test_notify_false_and_invalid_output_are_still_stored_published(self):
        ac._bedrock["c"] = FakeBedrock(reply({**GOOD, "notify": False}))
        ac.handler(self.event("nf"), None)
        ac._bedrock["c"] = FakeBedrock(reply("x"), reply("y"), reply("z"))
        ac.handler(self.event("bad", detected_at="2026-11-06T17:22:00Z") | {"time": "2026-11-06T17:22:00Z"}, None)
        rows = sorted(self.stored(), key=lambda a: a["sent_at"])
        self.assertEqual([(a["source_event_id"], a["status"], a["fallback"]) for a in rows],
                         [("nf", "published", True), ("bad", "published", True)])
        self.assertTrue(all(a["fallback_reason"] for a in rows))

    def test_live_template_is_fetched_every_run(self):
        self.templates.put_item(Item={"pk": f"template#public#{CAMPAIGN}", "sk": "current", "version": 7,
                                      "system_prompt": "FIRST", "max_chars": 306, "target_chars": 160})
        ac.handler(self.event("e1"), None)
        self.templates.put_item(Item={"pk": f"template#public#{CAMPAIGN}", "sk": "current", "version": 8,
                                      "system_prompt": "SECOND", "max_chars": 306, "target_chars": 160})
        ac.handler(self.event("e2", detected_at="2026-11-06T17:22:03Z"), None)
        versions = sorted(a["template_version"] for a in self.stored())
        self.assertEqual(versions, [7, 8])

    def test_dynamodb_decimals_in_template_do_not_break_serialization(self):
        from decimal import Decimal
        self.templates.put_item(Item={"pk": f"template#public#{CAMPAIGN}", "sk": "current", "version": Decimal(3),
                                      "system_prompt": "P", "max_chars": Decimal(306), "target_chars": Decimal(160),
                                      "temperature": Decimal("0.2"), "max_tokens": Decimal(500)})
        res = ac.handler(self.event("dec"), None)
        self.assertTrue(res["stored"])
        tpl = ac.load_template(self.templates, CAMPAIGN)
        self.assertEqual((tpl["version"], tpl["temperature"], tpl["max_tokens"]), (3, 0.2, 500))
        json.dumps(tpl)

    def test_idempotent_on_source_event_id(self):
        ac.handler(self.event("same"), None)
        res = ac.handler(self.event("same"), None)
        self.assertTrue(res["duplicate"])
        self.assertEqual(len(self.stored()), 1)
        self.assertEqual(len(self.bedrock.calls), 1)

    def test_same_millisecond_alerts_do_not_collide(self):
        ac.handler(self.event("a"), None)
        ac.handler(self.event("b"), None)
        self.assertEqual(len({a["sent_at"] for a in self.stored()}), 2)

    def test_failure_releases_marker_so_retry_works(self):
        self.bedrock.responses = []  # IndexError inside converse is swallowed per model -> fallback, so force a store failure
        real = self.alerts.put_item

        def flaky(Item, **kw):
            if not Item["campaign_id"].startswith("src#") and not getattr(flaky, "failed", False):
                flaky.failed = True
                raise RuntimeError("ddb down")
            return real(Item=Item, **kw)
        self.alerts.put_item = flaky
        with self.assertRaises(RuntimeError):
            ac.handler(self.event("retry"), None)
        self.assertNotIn(("src#retry", "marker"), self.alerts.items)
        res = ac.handler(self.event("retry"), None)
        self.assertTrue(res["stored"])

    def test_test_events_are_marked_and_status_stays_published_when_broadcast_off(self):
        self.ops.put_item(Item={"pk": "config#global", "sk": "current", "broadcast_enabled": False})
        ev = self.event("t1", test=True)
        ev["source"] = "fob-site-monitor.test"
        ac.handler(ev, None)
        (a,) = self.stored()
        self.assertTrue(a["is_test"])
        self.assertEqual(a["status"], "published")

    def test_template_read_failure_falls_back_to_default(self):
        class Broken:
            def get_item(self, **kw):
                raise RuntimeError("down")
        ac._tables["templates"] = Broken()
        res = ac.handler(self.event("x"), None)
        self.assertTrue(res["stored"])

    def test_bad_campaign_rejected(self):
        with self.assertRaises(ValueError):
            ac.handler(self.event(campaign_id="Bad Campaign!"), None)

    def test_fanout_is_a_noop_hook(self):
        self.assertIsNone(ac.fanout_hook({"message": "x"}))
        self.assertFalse(any("Messages.json" in repr(c) for c in self.alerts.calls))


class PromptCopyTest(unittest.TestCase):
    def test_default_prompt_has_no_ai_disclosure_or_emoji(self):
        text = ac.default_prompt()
        self.assertNotRegex(text, r"(?i)written by|ai-generated|generated by|summarized by|\bAI\b")
        self.assertIsNone(re.search("[\U0001F000-\U0001FAFF\u2600-\u27BF]", text))
        self.assertIn("what_changed", text)
        self.assertIn("note", text)


if __name__ == "__main__":
    unittest.main()
