"""Web sign-up double opt-in: one confirmation text, strict limits, generic responses.

Every number here is fake and nothing reaches Twilio: apibase replaces signup_api._send_sms with a recorder.
"""

import os
import re
import unittest

from apibase import CAMPAIGN, ApiTestCase
from fakes import ROOT, TEST_PHONE
from smsbase import SmsTestCase

import signup_api  # noqa: E402
from common import sms_text, store, twilio_send  # noqa: E402

P1 = "+12125550243"
P2 = "+12125550244"
P3 = "+12125550245"
P4 = "+12125550246"


class ConfirmSendTest(ApiTestCase):
    def go(self, phone, ip=None, **kw):
        self.ip = ip or "198.51.100.%d" % (len(self.sent) + len(self.audit.items) % 200 + 1)
        return self.call("/api/subscribe", self.signup(phone=phone, **kw))

    def audit_reasons(self, event):
        return [i.get("reason") for i in self.audit.items.values() if i["event_type"] == event]

    def global_cfg(self, **kw):
        self.ops.items[("config#global", "current")].update(kw)
        store.reset_config_cache()

    def test_new_signup_sends_exactly_one_gsm7_confirmation(self):
        res = self.go(P1)
        self.assertEqual(res["statusCode"], 202)
        self.assertEqual(self.subs.items[(P1, CAMPAIGN)]["status"], "PENDING_SENDER")
        self.assertEqual(len(self.sent), 1)
        to, body = self.sent[0]
        self.assertEqual(to, P1)
        self.assertTrue(sms_text.is_gsm7(body))
        self.assertLessEqual(sms_text.stats(body)["segments"], 2)
        self.assertIn("Reply YES to confirm", body)
        self.assertIn("Reply STOP", body)
        self.assertIn("Unofficial fan project", body)
        self.assertIsNone(re.search("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", body))
        self.assertIn("confirm_sent", self.audit_events())
        self.assertIn("signup_web", self.audit_events())
        sent = [i for i in self.audit.items.values() if i["event_type"] == "confirm_sent"][0]
        self.assertFalse(sent["dry_run"])
        self.assertEqual(sent["message_sid"], "SMfake1")

    def test_default_copy_is_valid(self):
        text = sms_text.sanitize(signup_api.WEB_CONFIRM_DEFAULT, "public")
        self.assertEqual(text, signup_api.WEB_CONFIRM_DEFAULT)
        self.assertTrue(sms_text.is_gsm7(text))
        self.assertLessEqual(sms_text.stats(text)["segments"], signup_api.WEB_CONFIRM_MAX_SEGMENTS)

    def test_one_confirmation_per_phone_per_24_hours(self):
        first = self.go(P1)
        second = self.go(P1)
        third = self.go(P1)
        self.assertEqual(len(self.sent), 1)
        # (audit rows written in the same millisecond share a key in the fake table, so compare as a set)
        self.assertEqual(set(self.audit_reasons("confirm_suppressed")), {"phone_24h_limit"})
        self.assertEqual(first["body"], second["body"])
        self.assertEqual(second["body"], third["body"])

    def test_global_daily_cap_blocks_and_releases_phone_claim(self):
        self.global_cfg(web_confirm_daily_cap=2)
        for p in (P1, P2, P3):
            self.go(p)
        self.assertEqual([to for to, _ in self.sent], [P1, P2])
        self.assertEqual(self.audit_reasons("confirm_suppressed"), ["daily_cap"])
        self.assertEqual(self.subs.items[(P3, CAMPAIGN)]["status"], "PENDING_SENDER")
        # the capped number is not locked out for 24h: once the cap is lifted it can be texted
        self.global_cfg(web_confirm_daily_cap=50)
        self.go(P3)
        self.assertEqual([to for to, _ in self.sent], [P1, P2, P3])

    def test_kill_switch_stores_signup_but_sends_nothing(self):
        self.global_cfg(web_confirm_enabled=False)
        res = self.go(P1)
        self.assertEqual(res["statusCode"], 202)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.audit_reasons("confirm_suppressed"), ["kill_switch"])
        self.assertIn((P1, CAMPAIGN), self.subs.items)

    def test_dry_run_audits_but_does_not_send(self):
        self.global_cfg(web_confirm_dry_run=True)
        self.go(P1)
        self.assertEqual(self.sent, [])
        sent = [i for i in self.audit.items.values() if i["event_type"] == "confirm_sent"][0]
        self.assertTrue(sent["dry_run"])
        self.assertIsNone(sent.get("message_sid"))

    def test_shared_test_number_is_never_texted(self):
        self.go(TEST_PHONE)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.audit_reasons("confirm_suppressed"), ["test_number"])

    def test_opted_out_is_not_texted_and_stays_opted_out(self):
        self.subs.put_item(Item={"phone": P1, "campaign_id": CAMPAIGN, "status": "OPTED_OUT"})
        res = self.go(P1)
        self.assertEqual(res["statusCode"], 202)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.subs.items[(P1, CAMPAIGN)]["status"], "OPTED_OUT")
        self.assertEqual(self.audit_reasons("confirm_suppressed"), ["opted_out"])
        self.assertIn("signup_web", self.audit_events())

    def test_active_is_not_texted_again(self):
        self.subs.put_item(Item={"phone": P1, "campaign_id": CAMPAIGN, "status": "ACTIVE"})
        res = self.go(P1)
        self.assertEqual(res["statusCode"], 202)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.subs.items[(P1, CAMPAIGN)]["status"], "ACTIVE")
        self.assertEqual(self.audit_reasons("confirm_suppressed"), ["already_active"])

    def test_responses_never_reveal_whether_a_number_exists(self):
        self.subs.put_item(Item={"phone": P2, "campaign_id": CAMPAIGN, "status": "ACTIVE"})
        self.subs.put_item(Item={"phone": P3, "campaign_id": CAMPAIGN, "status": "OPTED_OUT"})
        bodies = {self.go(p)["body"] for p in (P1, P2, P3)}
        self.assertEqual(len(bodies), 1)
        res = self.go(P4, ip="198.51.100.99")
        self.assertEqual(res["json"]["status"], "check_your_phone")
        self.assertNotIn("waitlist", res["body"].lower())

    def test_twilio_failure_is_swallowed_and_audited_without_detail(self):
        self.send_error = twilio_send.TwilioError(400, 21211)
        res = self.go(P1)
        self.assertEqual(res["statusCode"], 202)
        self.assertEqual(self.audit_reasons("confirm_suppressed"), [])
        failed = [i for i in self.audit.items.values() if i["event_type"] == "confirm_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["error_code"], "21211")
        self.assertNotIn("confirm_sent", self.audit_events())

    def test_unexpected_send_error_does_not_break_signup(self):
        self.send_error = RuntimeError("boom")
        self.assertEqual(self.go(P1)["statusCode"], 202)
        self.assertIn("confirm_failed", self.audit_events())

    def test_pending_sender_resubmit_refreshes_but_respects_24h_limit(self):
        self.go(P1)
        self.go(P1)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.subs.items[(P1, CAMPAIGN)]["status"], "PENDING_SENDER")

    def test_editable_copy_from_dynamodb_is_used_when_safe(self):
        msg = "Mall Rat Alerts: reply YES to join. Reply STOP to cancel. Unofficial fan project."
        self.templates.put_item(Item={"pk": "config#web_confirm", "sk": CAMPAIGN, "campaign_id": CAMPAIGN,
                                      "enabled": True, "message": msg})
        store.reset_config_cache()
        self.go(P1)
        self.assertEqual(self.sent[0][1], msg)

    def test_unsafe_editable_copy_falls_back_to_builtin(self):
        bad = ["Reply YES to join and tell friends " * 12 + " STOP",  # too long
               "Reply YES to join",  # no STOP language
               ]
        for i, msg in enumerate(bad):
            self.sent.clear()
            self.templates.put_item(Item={"pk": "config#web_confirm", "sk": CAMPAIGN, "campaign_id": CAMPAIGN,
                                          "enabled": True, "message": msg})
            store.reset_config_cache()
            self.go("+1212555025%d" % i, ip="198.51.100.%d" % (150 + i))
            self.assertEqual(self.sent[0][1], signup_api.WEB_CONFIRM_DEFAULT, msg[:20])

    def test_disabled_editable_copy_falls_back(self):
        self.templates.put_item(Item={"pk": "config#web_confirm", "sk": CAMPAIGN, "campaign_id": CAMPAIGN,
                                      "enabled": False, "message": "Reply YES. Reply STOP."})
        store.reset_config_cache()
        self.go(P1)
        self.assertEqual(self.sent[0][1], signup_api.WEB_CONFIRM_DEFAULT)

    def test_non_us_toll_free_and_premium_numbers_are_rejected(self):
        for phone in ("+14165552368", "+18005551234", "+18885551234", "+19005551234", "+15005551234", "+447700900123"):
            res = self.go(phone, ip="198.51.100.%d" % (200 + len(self.audit.items)))
            self.assertEqual(res["statusCode"], 400, phone)
            self.assertEqual(res["json"]["error"], "invalid_phone", phone)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.subs.items, {})

    def test_ip_limit_is_three_per_hour(self):
        codes = [self.call("/api/subscribe", self.signup(phone=p))["statusCode"] for p in (P1, P2, P3, P4)]
        self.assertEqual(codes, [202, 202, 202, 429])
        self.assertEqual(len(self.sent), 3)

    def test_audit_never_stores_raw_ip_or_message_body(self):
        self.go(P1)
        blob = str(self.audit.items) + str(self.subs.items)
        self.assertNotIn(self.ip, blob)
        self.assertNotIn("Reply YES to confirm", blob)

    def test_plain_form_post_redirects_and_sends(self):
        self.ip = "198.51.100.77"
        res = self.call("/api/subscribe", self.signup(phone=P1, consent="yes", age_18_plus="yes"), form=True)
        self.assertEqual(res["statusCode"], 303)
        self.assertEqual(res["headers"]["Location"], "/thanks/")
        self.assertEqual(len(self.sent), 1)

    def test_posthog_server_event_has_no_phone(self):
        events = []
        real = signup_api._track
        signup_api._track = lambda event, phone, **props: events.append((event, props))
        try:
            self.go(P1)
        finally:
            signup_api._track = real
        for _, props in events:
            self.assertNotIn(P1, str(props))


class ConfirmReplyTest(SmsTestCase):
    def test_replying_yes_to_the_confirmation_activates_the_web_signup(self):
        self.ip = "198.51.100.5"
        self.call("/api/subscribe", self.signup(phone=P1))
        self.assertEqual(self.subs.items[(P1, CAMPAIGN)]["status"], "PENDING_SENDER")
        self.assertEqual(len(self.sent), 1)
        res = self.sms("YES", frm=P1)
        self.assertEqual(self.subs.items[(P1, CAMPAIGN)]["status"], "ACTIVE")
        self.assertIn("You're in", self.message(res))

    def test_no_reply_means_never_active(self):
        self.ip = "198.51.100.6"
        self.call("/api/subscribe", self.signup(phone=P1))
        self.assertNotEqual(self.subs.items[(P1, CAMPAIGN)]["status"], "ACTIVE")


class ConfigWiringTest(unittest.TestCase):
    def test_terraform_wires_confirm_settings(self):
        def read(*p):
            with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
                return f.read()
        api, variables, dynamo = read("terraform", "api.tf"), read("terraform", "variables.tf"), read("terraform", "dynamodb.tf")
        self.assertIn("TWILIO_FROM_NUMBER", api)
        self.assertRegex(variables, r'variable "twilio_from_number"[^}]*"\+18335961548"')
        self.assertIn('default     = "sms-consent-2026-11-06.v2"', variables)
        for key in ("web_confirm_enabled", "web_confirm_daily_cap", "web_confirm_dry_run"):
            self.assertIn(key, dynamo)
            self.assertIn(key, signup_api.GLOBAL_DEFAULTS)
        self.assertEqual(signup_api.GLOBAL_DEFAULTS["web_confirm_daily_cap"], 50)
        self.assertEqual(signup_api.LIMITS["ip_subscribe_hour"], 3)


if __name__ == "__main__":
    unittest.main()


class TwilioSendTest(unittest.TestCase):
    """The REST sender, exercised with an injected opener so nothing touches the network."""

    class Resp:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.payload

    def test_builds_authenticated_form_post_and_returns_sid(self):
        import base64
        import urllib.parse
        seen = {}

        def opener(req, timeout=None):
            seen["req"], seen["timeout"] = req, timeout
            return self.Resp(b'{"sid": "SM123", "status": "queued"}')

        out = twilio_send.send_message("ACfake", "tokfake", "+18335550100", P1, "hello STOP",
                                       status_callback="https://x.test/api/twilio/status", opener=opener)
        self.assertEqual(out, {"sid": "SM123", "status": "queued"})
        req = seen["req"]
        self.assertEqual(req.full_url, "https://api.twilio.com/2010-04-01/Accounts/ACfake/Messages.json")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Basic " + base64.b64encode(b"ACfake:tokfake").decode())
        fields = dict(urllib.parse.parse_qsl(req.data.decode()))
        self.assertEqual(fields["To"], P1)
        self.assertEqual(fields["From"], "+18335550100")
        self.assertEqual(fields["StatusCallback"], "https://x.test/api/twilio/status")
        self.assertEqual(seen["timeout"], 5)

    def test_errors_carry_only_status_and_code(self):
        import io
        import urllib.error

        def opener(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b'{"code": 21211, "message": "tokfake"}'))

        with self.assertRaises(twilio_send.TwilioError) as cm:
            twilio_send.send_message("ACfake", "tokfake", "+18335550100", P1, "x", opener=opener)
        self.assertEqual((cm.exception.http_status, cm.exception.code), (400, 21211))
        self.assertNotIn("tokfake", str(cm.exception))

        def boom(req, timeout=None):
            raise OSError("tokfake at ACfake")

        with self.assertRaises(twilio_send.TwilioError) as cm:
            twilio_send.send_message("ACfake", "tokfake", "+18335550100", P1, "x", opener=boom)
        self.assertNotIn("tokfake", str(cm.exception))
