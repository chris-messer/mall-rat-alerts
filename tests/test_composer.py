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
JUNK_FLIP = [{"url": "https://www.falloutboy.com/phase2", "status": {"before": 403, "after": 404},
              "title": {"before": "Twilight Town Mall | Not authorized", "after": "Fall Out Boy | Page not found"}}]
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

    def test_discord_message_keeps_full_body_sms_message_stays_capped(self):
        long_what = "A very long description of what happened on the site. " * 8 + "falloutboy.com/x"
        note = "Thnks fr th mmrs."
        out = self.build(FakeBedrock(reply({"notify": True, "what_changed": long_what, "note": note})))
        self.assertLessEqual(sms_text.units(out["message"]), 306)
        self.assertGreater(len(out["discord_message"]), len(out["message"]))
        self.assertTrue(out["discord_message"].endswith(note))
        self.assertIn("falloutboy.com/x", out["discord_message"])
        self.assertTrue(sms_text.is_gsm7(out["discord_message"]))

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

    def _signoff_case(self, **extra):
        return self.build(FakeBedrock(reply({"notify": True, "what_changed": GOOD["what_changed"], **extra})))

    def test_signoff_used_for_sms_and_discord(self):
        out = self._signoff_case(signoff="Welcome to the Aces.", note="Thnks fr th mmrs.")
        self.assertEqual(out["note"], "Welcome to the Aces.")
        self.assertTrue(out["message"].endswith("\n\nWelcome to the Aces."))
        self.assertTrue(out["discord_message"].endswith("\n\nWelcome to the Aces."))
        self.assertFalse(out["fallback"])

    def test_missing_or_blank_signoff_falls_back_to_note_then_fixed_line(self):
        self.assertEqual(self._signoff_case(note="Light 'em up.")["note"], "Light 'em up.")
        for bad in ("", "   ", None, 7, ["x"]):
            self.assertEqual(self._signoff_case(signoff=bad, note="Light 'em up.")["note"], "Light 'em up.")
        out = self.build(FakeBedrock(RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")))
        self.assertEqual(out["note"], "Thnks fr th mmrs.")

    def test_signoff_is_length_capped(self):
        out = self._signoff_case(signoff="Everything we ever wanted was a long long sign-off that goes on and on forever")
        self.assertLessEqual(sms_text.units(out["note"]), alert_text.SIGNOFF_MAX)
        self.assertTrue(out["note"].startswith("Everything we ever"))
        self.assertEqual(out["message"], out["what_changed"] + "\n\n" + out["note"])

    def test_signoff_is_gsm7_sanitized(self):
        out = self._signoff_case(signoff="Light \u2018em up \u2014 \u201cDance, Dance\u201d \U0001F496\u2026")
        self.assertEqual(out["note"], 'Light \'em up - "Dance, Dance" ...')
        self.assertTrue(sms_text.is_gsm7(out["message"]))
        self.assertTrue(sms_text.is_gsm7(out["discord_message"]))

    def test_signoff_mentioning_ai_is_dropped_not_whole_output(self):
        out = self._signoff_case(signoff="Written by AI", note="Thnks fr th mmrs.")
        self.assertEqual(out["note"], "Thnks fr th mmrs.")

    def test_priority_visit_alert_keeps_fixed_signoff(self):
        out = self.build(FakeBedrock(reply({"notify": True, "what_changed": "x", "signoff": "Nope."})), priority=True)
        self.assertEqual(out["note"], "Light 'em up.")

    def test_all_models_fail_uses_template_fallback(self):
        b = FakeBedrock(RuntimeError("boom1"), RuntimeError("boom2"), reply("nope"))
        out = self.build(b)
        self.assertTrue(out["fallback"])
        self.assertEqual(out["model"], "fallback:template")
        self.assertIn("falloutboy.com/aces-hifi-records", out["what_changed"])
        self.assertEqual(out["note"], "Thnks fr th mmrs.")
        self.assertEqual(out["message"], out["what_changed"] + "\n\n" + out["note"])
        self.assertEqual(len(b.calls), 3)

    def test_notify_false_suppresses_without_fallback_text(self):
        b = FakeBedrock(reply({**GOOD, "notify": False}))
        with self.assertLogs(ac.log, "INFO") as logs:
            out = self.build(b)
        self.assertTrue(out["suppressed"])
        self.assertFalse(out["fallback"])
        self.assertIsNone(out["fallback_reason"])
        self.assertEqual((out["model"], out["model_notify"]), ("model-a", False))
        self.assertEqual(out["what_changed"], GOOD["what_changed"])
        self.assertNotIn("Something changed on", out["what_changed"])
        self.assertNotIn("Something changed on", out["message"])
        self.assertTrue(any("suppressed" in m and "notify=false" in m for m in logs.output))

    def test_prefilter_suppresses_without_calling_the_model(self):
        b = FakeBedrock()
        with self.assertLogs(ac.log, "INFO") as logs:
            out = self.build(b, changes=JUNK_FLIP)
        self.assertEqual(b.calls, [])
        self.assertTrue(out["suppressed"])
        self.assertEqual(out["suppressed_reason"], "prefilter:status_flip")
        self.assertEqual(out["model"], "prefilter")
        self.assertFalse(out["fallback"])
        self.assertIsNone(out["model_notify"])
        self.assertTrue(out["what_changed"])  # audit text is kept
        self.assertTrue(any("prefilter:status_flip" in m for m in logs.output))

    def test_prefilter_does_not_stop_important_changes_or_priority_alerts(self):
        b = FakeBedrock(reply(GOOD))
        out = self.build(b, changes=CHANGES)
        self.assertEqual(len(b.calls), 1)
        self.assertFalse(out["suppressed"])
        self.assertIsNone(out["suppressed_reason"])
        # The Visit-tab priority path is fixed text and is never pre-filtered.
        out = self.build(FakeBedrock(), changes=JUNK_FLIP, priority=True)
        self.assertFalse(out["suppressed"])
        self.assertEqual(out["model"], "fixed:visit-tab")

    def test_tour_region_additions_reach_the_model_not_prefilter(self):
        # Same shape as the 2026-11-09 suppressed "Tour page updated with new regions" event.
        tour_regions = [{
            "url": "https://www.falloutboy.com/tour",
            "visible_text_diff": (
                "@@ -8,4 +8,7 @@\n Twilight Town Mall\n All\n US + Canada\n EU+UK\n"
                "+Australia + NZ\n+Latin America\n+Asia\n PRE-SALE:\n view more dates"),
            "html_diff": "+Australia + NZ\n+Latin America\n+Asia",
        }]
        b = FakeBedrock(reply({
            "notify": True,
            "what_changed": "Tour page updated with new regions on falloutboy.com/tour",
            "note": "This ain't a scene.",
        }))
        out = self.build(b, changes=tour_regions)
        self.assertEqual(b.calls, ["model-a"])
        self.assertFalse(out["suppressed"])
        self.assertIsNone(out["suppressed_reason"])
        self.assertTrue(out["model_notify"])
        self.assertIn("new regions", out["what_changed"])

    def test_mall_host_event_reaches_the_model_and_names_the_mall_address(self):
        mall = [{"url": "https://twilighttownmall.falloutboy.com/lost-and-found",
                 "visible_text_diff": "+A new note was found: meet at midnight"}]
        good = {"notify": True, "what_changed": "New note on twilighttownmall.falloutboy.com/lost-and-found",
                "note": "Thnks fr th mmrs."}
        b = FakeBedrock(reply(good))
        out = self.build(b, changes=mall)
        self.assertEqual(b.calls, ["model-a"])
        self.assertFalse(out["suppressed"])
        self.assertEqual(out["urls"], ["https://twilighttownmall.falloutboy.com/lost-and-found"])
        self.assertIn("twilighttownmall.falloutboy.com/lost-and-found", out["what_changed"])

    def test_fallback_keeps_the_mall_subdomain(self):
        mall = [{"url": "https://twilighttownmall.falloutboy.com/newstore", "status": {"before": 404, "after": 200}}]
        out = self.build(FakeBedrock(RuntimeError("1"), RuntimeError("2"), RuntimeError("3")), changes=mall)
        self.assertTrue(out["fallback"])
        self.assertIn("twilighttownmall.falloutboy.com/newstore", out["what_changed"])

    def test_default_prompt_covers_both_hosts(self):
        prompt = ac.load_default_prompt() if hasattr(ac, "load_default_prompt") else None
        if prompt is None:
            with open(os.path.join(os.path.dirname(ac.__file__), "common", "default_public_prompt.txt"),
                      encoding="utf-8") as f:
                prompt = f.read()
        self.assertIn("twilighttownmall.falloutboy.com", prompt)
        self.assertIn("falloutboy.com/path", prompt)
        self.assertIn("ticket", prompt.lower())
        self.assertIn("cookie", prompt.lower())

    def test_model_notify_false_records_a_reason(self):
        out = self.build(FakeBedrock(reply({**GOOD, "notify": False})))
        self.assertEqual(out["suppressed_reason"], "model:notify_false")

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
                "detail": {"campaign_id": CAMPAIGN, "changes": CHANGES, "detected_at": "2026-11-06T17:21:%02dZ" % (sum(map(ord, eid)) % 60), **detail}}

    def stored(self):
        return [i for i in self.alerts.items.values() if not i["campaign_id"].startswith(("src#", "det#"))]

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

    def test_notify_false_stored_suppressed_invalid_output_still_published(self):
        ac._bedrock["c"] = FakeBedrock(reply({**GOOD, "notify": False}))
        ac.handler(self.event("nf"), None)
        ac._bedrock["c"] = FakeBedrock(reply("x"), reply("y"), reply("z"))
        ac.handler(self.event("bad", detected_at="2026-11-06T17:22:00Z") | {"time": "2026-11-06T17:22:00Z"}, None)
        rows = sorted(self.stored(), key=lambda a: a["sent_at"])
        self.assertEqual([(a["source_event_id"], a["status"], a.get("fallback"), a.get("model_notify")) for a in rows],
                         [("nf", "suppressed", False, False), ("bad", "published", True, None)])
        self.assertNotIn("Something changed on", rows[0]["what_changed"])
        self.assertIn("Something changed on", rows[1]["what_changed"])
        self.assertTrue(rows[1]["fallback_reason"])
        self.assertNotIn("suppressed", rows[0])  # flag is not persisted; status carries it

    def test_notify_false_skips_fanout_and_discord_notify_true_does_not(self):
        fanouts, posts = [], []
        orig_fanout, orig_discord = ac.fanout_hook, ac.discord_hook
        ac.fanout_hook = lambda *a, **k: fanouts.append(a)
        ac.discord_hook = lambda *a, **k: posts.append(a)
        try:
            ac._bedrock["c"] = FakeBedrock(reply({**GOOD, "notify": False}))
            res_false = ac.handler(self.event("nf"), None)
            self.assertEqual((fanouts, posts), ([], []))
            self.assertEqual(res_false["status"], "suppressed")
            (suppressed,) = [a for a in self.stored() if a["source_event_id"] == "nf"]
            self.assertEqual(suppressed["status"], "suppressed")

            ac._bedrock["c"] = FakeBedrock(reply(GOOD))
            res_true = ac.handler(self.event("ok", detected_at="2026-11-06T17:22:00Z")
                                  | {"time": "2026-11-06T17:22:00Z"}, None)
            self.assertEqual(len(fanouts), 1)
            self.assertEqual(len(posts), 1)
            self.assertEqual(res_true["status"], "published")
            (published,) = [a for a in self.stored() if a["source_event_id"] == "ok"]
            self.assertEqual(published["status"], "published")
        finally:
            ac.fanout_hook, ac.discord_hook = orig_fanout, orig_discord

    def test_prefiltered_event_is_stored_suppressed_with_reason_and_never_sent(self):
        fanouts, posts = [], []
        orig_fanout, orig_discord = ac.fanout_hook, ac.discord_hook
        ac.fanout_hook = lambda *a, **k: fanouts.append(a)
        ac.discord_hook = lambda *a, **k: posts.append(a)
        try:
            res = ac.handler(self.event("junk", changes=JUNK_FLIP), None)
        finally:
            ac.fanout_hook, ac.discord_hook = orig_fanout, orig_discord
        self.assertEqual((res["status"], res["model"]), ("suppressed", "prefilter"))
        self.assertEqual((fanouts, posts), ([], []))
        self.assertEqual(self.bedrock.calls, [])
        (a,) = self.stored()
        self.assertEqual((a["status"], a["suppressed_reason"], a["source_event_id"]),
                         ("suppressed", "prefilter:status_flip", "junk"))
        self.assertNotIn("suppressed", a)

    def test_published_alert_has_no_suppressed_reason(self):
        ac.handler(self.event("ok"), None)
        (a,) = self.stored()
        self.assertEqual(a["status"], "published")
        self.assertNotIn("suppressed_reason", a)

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
            if not Item["campaign_id"].startswith(("src#", "det#")) and not getattr(flaky, "failed", False):
                flaky.failed = True
                raise RuntimeError("ddb down")
            return real(Item=Item, **kw)
        self.alerts.put_item = flaky
        with self.assertRaises(RuntimeError):
            ac.handler(self.event("retry"), None)
        self.assertNotIn(("src#retry", "marker"), self.alerts.items)
        det = ac.detection_marker_key(CAMPAIGN, "2026-11-06T17:21:%02dZ" % (sum(map(ord, "retry")) % 60))
        self.assertNotIn((det["campaign_id"], "marker"), self.alerts.items)  # released, so the retry is not a "twin"
        res = ac.handler(self.event("retry"), None)
        self.assertTrue(res["stored"])
        self.assertEqual(self.stored()[0]["status"], "published")

    def test_test_events_are_marked_and_status_stays_published_when_broadcast_off(self):
        self.ops.put_item(Item={"pk": "config#global", "sk": "current", "broadcast_enabled": False})
        ev = self.event("t1", test=True)
        ev["source"] = "fob-site-monitor.test"
        ac.handler(ev, None)
        (a,) = self.stored()
        self.assertTrue(a["is_test"])
        self.assertEqual(a["status"], "published")

    def test_twin_event_with_same_detected_at_is_suppressed_without_model_or_sends(self):
        ac.fanout_hook_calls = []
        orig_fan, orig_disc = ac.fanout_hook, ac.discord_hook
        ac.fanout_hook = lambda alert, cfg=None: ac.fanout_hook_calls.append(alert["source_event_id"])
        ac.discord_hook = lambda *a, **k: ac.fanout_hook_calls.append("discord:" + a[1]["source_event_id"])
        try:
            first = ac.handler(self.event("visit", detected_at="2026-11-11T21:01:07.487Z", alert_kind="visit_clickable"), None)
            calls_after_first = len(self.bedrock.calls)
            second = ac.handler(self.event("change", detected_at="2026-11-11T21:01:07.487Z", alert_kind="change"), None)
        finally:
            fanned, ac.fanout_hook, ac.discord_hook = ac.fanout_hook_calls, orig_fan, orig_disc
            del ac.fanout_hook_calls
        self.assertEqual(first["status"], "published")
        self.assertEqual(second["status"], "suppressed")
        self.assertEqual(len(self.bedrock.calls), calls_after_first)  # no model call for the twin
        self.assertEqual(fanned, ["visit", "discord:visit"])           # exactly one fan-out and one Discord post
        rows = {a["source_event_id"]: a for a in self.stored()}
        self.assertEqual(rows["change"]["suppressed_reason"], "duplicate:same_detection")
        self.assertEqual(rows["change"]["model"], "guard")
        self.assertNotIn("suppressed", rows["change"])

    def test_twin_event_fires_both_skipped_events_with_the_reason(self):
        seen = []
        orig_d, orig_s = ac._discord_skipped, ac._sms_skipped
        ac._discord_skipped = lambda c, s, reason: seen.append(("discord", reason))
        ac._sms_skipped = lambda c, s, reason, **p: seen.append(("sms", reason))
        try:
            ac.handler(self.event("one", detected_at="2026-11-11T21:01:07.487Z"), None)
            seen.clear()
            ac.handler(self.event("two", detected_at="2026-11-11T21:01:07.487Z"), None)
        finally:
            ac._discord_skipped, ac._sms_skipped = orig_d, orig_s
        self.assertEqual(sorted(seen), [("discord", "duplicate:same_detection"), ("sms", "duplicate:same_detection")])

    def test_different_detected_at_is_not_a_twin(self):
        ac.handler(self.event("p", detected_at="2026-11-11T21:01:07.487Z"), None)
        ac.handler(self.event("q", detected_at="2026-11-11T21:02:04.833Z"), None)
        self.assertEqual({a["status"] for a in self.stored()}, {"published"})

    def test_missing_detected_at_fails_open(self):
        ev1, ev2 = self.event("m1"), self.event("m2")
        ev1["detail"].pop("detected_at"); ev2["detail"].pop("detected_at")
        ac.handler(ev1, None); ac.handler(ev2, None)
        self.assertEqual({a["status"] for a in self.stored()}, {"published"})

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
        self.assertIn("signoff", text)

    def test_default_prompt_has_conservative_significance_guidance(self):
        text = ac.default_prompt()
        self.assertIn("when unsure, notify=false", text)
        self.assertNotIn("When unsure, true", text)
        for must in ("notify=true for", "notify=false for", "tour", "presale", "merch", "puzzle",
                     "sitemap.xml", "403 to 404", "Link-only"):
            self.assertIn(must, text)

    def test_default_prompt_lists_tour_regions_cities_venues_and_ticket_info(self):
        text = ac.default_prompt()
        for must in ("new tour regions", "new cities or venues", "new tour dates", "presales",
                     "ticket", "tour page that adds new fan-relevant content",
                     "Tour page updated with new regions"):
            self.assertIn(must, text)
        self.assertIn("when unsure, notify=false", text)

    def test_default_prompt_keeps_region_reorders_not_notify_worthy(self):
        text = ac.default_prompt()
        self.assertIn("reordering of tour regions", text)
        self.assertIn('"Tour page region filters reordered"', text)
        self.assertIn("nothing added", text)

    def test_default_prompt_tells_model_to_skip_repeats(self):
        text = ac.default_prompt()
        for must in ("Recent alerts already sent to fans", "already announced", "set notify=false",
                     "materially new", "new date, city, region, or product"):
            self.assertIn(must, text)


NOW = 1_794_000_000.0  # fixed clock for the context tests


def _row(minutes_ago, what, status="sent", **extra):
    return {"campaign_id": CAMPAIGN, "sent_at": ac._iso(NOW - minutes_ago * 60), "status": status,
            "what_changed": what, "note": "Light 'em up.", **extra}


class QueryTable:
    """Alerts table double for the context query: newest-first, honours the `since` bound and Limit."""

    def __init__(self, rows, fail=False):
        self.rows, self.fail, self.calls = rows, fail, []

    def query(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("boom")
        since = kw["ExpressionAttributeValues"][":s"]
        rows = sorted((r for r in self.rows if r["sent_at"] >= since), key=lambda r: r["sent_at"], reverse=True)
        start = int(kw.get("ExclusiveStartKey", {}).get("n", 0))
        page = rows[start:start + kw["Limit"]]
        res = {"Items": page}
        if start + kw["Limit"] < len(rows):
            res["LastEvaluatedKey"] = {"n": start + kw["Limit"]}
        return res


class ContextConsistencyTest(unittest.TestCase):
    def test_recent_alerts_query_is_strongly_consistent_on_every_page(self):
        rows = [{"sent_at": f"2026-11-11T20:{m:02d}:00.000Z", "status": "published", "what_changed": f"w{m}"}
                for m in range(55)]
        t = QueryTable(rows)
        ctx = ac.recent_alerts(t, CAMPAIGN, now=ac._parse_time("2026-11-11T21:00:00Z"))
        self.assertGreaterEqual(len(t.calls), 2)
        self.assertTrue(all(c.get("ConsistentRead") is True for c in t.calls))
        self.assertEqual(len(ctx["alerts"]), ac.CONTEXT_MAX_ALERTS)

    def test_context_includes_current_time_for_rollout_judgement(self):
        txt = ac.context_text({"alerts": [{"sent_at": "2026-11-11T20:59:07.000Z", "what_changed": "Visit page now works"}],
                               "suppressed": 0}, now=ac._parse_time("2026-11-11T21:01:00Z"))
        self.assertIn("Current time: 2026-11-11 21:01Z", txt)
        self.assertIn("2026-11-11 20:59Z: Visit page now works", txt)

    def test_prompt_treats_rollouts_and_visit_repeats_as_one_story(self):
        with open(os.path.join(os.path.dirname(ac.__file__), "common", "default_public_prompt.txt"), encoding="utf-8") as f:
            prompt = f.read()
        self.assertIn("Rollouts", prompt)
        self.assertIn("last 30 minutes", prompt)
        self.assertIn("Visit tab now works", prompt)


class RecentAlertsContextTest(unittest.TestCase):
    def test_empty_table_gives_no_context_block(self):
        ctx = ac.recent_alerts(QueryTable([]), CAMPAIGN, NOW)
        self.assertEqual(ctx, {"alerts": [], "suppressed": 0})
        self.assertEqual(ac.context_text(ctx), "")
        self.assertEqual(ac.user_text_for(CHANGES, ctx), ac.user_text_for(CHANGES))

    def test_caps_to_ten_newest_first(self):
        rows = [_row(m, f"alert {m}") for m in range(1, 31)]
        ctx = ac.recent_alerts(QueryTable(rows), CAMPAIGN, NOW)
        self.assertEqual(len(ctx["alerts"]), ac.CONTEXT_MAX_ALERTS)
        self.assertEqual([a["what_changed"] for a in ctx["alerts"]][:3], ["alert 1", "alert 2", "alert 3"])
        times = [a["sent_at"] for a in ctx["alerts"]]
        self.assertEqual(times, sorted(times, reverse=True))

    def test_window_is_48_hours(self):
        rows = [_row(60, "inside"), _row(47 * 60, "edge inside"), _row(49 * 60, "too old")]
        ctx = ac.recent_alerts(QueryTable(rows), CAMPAIGN, NOW)
        self.assertEqual([a["what_changed"] for a in ctx["alerts"]], ["inside", "edge inside"])

    def test_suppressed_are_counted_but_their_text_is_excluded(self):
        rows = [_row(5, "sent one"), _row(6, "SECRET SUPPRESSED TEXT", status="suppressed"),
                _row(7, "another suppressed", status="suppressed")]
        ctx = ac.recent_alerts(QueryTable(rows), CAMPAIGN, NOW)
        self.assertEqual(ctx["suppressed"], 2)
        text = ac.context_text(ctx)
        self.assertIn("sent one", text)
        self.assertNotIn("SECRET SUPPRESSED TEXT", text)
        self.assertNotIn("another suppressed", text)
        self.assertIn("2 other changes", text)

    def test_skips_test_and_fallback_rows_and_accepts_published_status(self):
        rows = [_row(1, "test row", is_test=True), _row(2, "Something changed on x", fallback=True),
                _row(3, "published row", status="published")]
        ctx = ac.recent_alerts(QueryTable(rows), CAMPAIGN, NOW)
        self.assertEqual([a["what_changed"] for a in ctx["alerts"]], ["published row"])

    def test_block_shows_time_what_changed_and_note_only(self):
        ctx = ac.recent_alerts(QueryTable([_row(10, "Tour page now shows full details at falloutboy.com/tour",
                                                phone="+15005550006", message="full sms body")]), CAMPAIGN, NOW)
        text = ac.context_text(ctx)
        self.assertIn("Recent alerts already sent to fans", text)
        self.assertIn("Tour page now shows full details at falloutboy.com/tour (Light 'em up.)", text)
        self.assertNotIn("+1500", text)
        self.assertNotIn("full sms body", text)

    def test_query_is_bounded_and_projects_only_needed_fields(self):
        t = QueryTable([_row(m, f"a{m}") for m in range(1, 400)])
        ac.recent_alerts(t, CAMPAIGN, NOW)
        self.assertLessEqual(len(t.calls), ac.CONTEXT_MAX_PAGES)
        for c in t.calls:
            self.assertFalse(c["ScanIndexForward"])
            self.assertEqual(c["Limit"], ac.CONTEXT_PAGE_LIMIT)
            self.assertNotIn("message", c["ProjectionExpression"])
            self.assertNotIn("phone", c["ProjectionExpression"])

    def test_fail_open_on_read_error(self):
        self.assertIsNone(ac.recent_alerts(QueryTable([], fail=True), CAMPAIGN, NOW))
        self.assertEqual(ac.context_text(None), "")

    def test_context_text_is_one_line_per_alert(self):
        ctx = {"alerts": [{"sent_at": "2026-11-09T18:41:07.000Z", "what_changed": "line one\nline two", "note": "n"}],
               "suppressed": 0}
        self.assertEqual(ac.context_text(ctx).count("\n- "), 1)
        self.assertIn("2026-11-09 18:41Z: line one line two (n)", ac.context_text(ctx))


class ComposeWithContextTest(unittest.TestCase):
    def setUp(self):
        self.tpl = {**ac.DEFAULTS, "system_prompt": "SYSTEM", "version": 3, "source": "dynamodb"}
        self.seen = []

        class B(FakeBedrock):
            def converse(inner, **kw):
                self.seen.append(kw["messages"][0]["content"][0]["text"])
                return super().converse(**kw)
        self.B = B

    def test_model_input_includes_context_before_changes(self):
        t = QueryTable([_row(5, "Tour page now shows full details at falloutboy.com/tour")])
        old = ac.recent_alerts
        ac.recent_alerts = lambda tbl, cid, now=None: old(tbl, cid, NOW)
        try:
            ac.build_alert(self.B(reply(GOOD)), self.tpl, CAMPAIGN, CHANGES, alerts_table=t)
        finally:
            ac.recent_alerts = old
        self.assertEqual(len(self.seen), 1)
        self.assertLess(self.seen[0].index("Recent alerts already sent"), self.seen[0].index("Changes detected:"))
        self.assertIn("Tour page now shows full details", self.seen[0])

    def test_read_failure_still_composes_without_context(self):
        out = ac.build_alert(self.B(reply(GOOD)), self.tpl, CAMPAIGN, CHANGES,
                             alerts_table=QueryTable([], fail=True))
        self.assertFalse(out["fallback"])
        self.assertTrue(self.seen[0].startswith("Changes detected:"))

    def test_no_table_means_no_context(self):
        ac.build_alert(self.B(reply(GOOD)), self.tpl, CAMPAIGN, CHANGES)
        self.assertTrue(self.seen[0].startswith("Changes detected:"))


if __name__ == "__main__":
    unittest.main()
