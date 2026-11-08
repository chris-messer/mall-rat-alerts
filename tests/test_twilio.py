import base64
import re
import unittest
import urllib.parse

from apibase import CAMPAIGN, OTHER_CAMPAIGN, ORIGIN_SECRET, ApiTestCase
from fakes import TEST_PHONE

import signup_api  # noqa: E402
from common import keywords, twilio_sig  # noqa: E402

EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")
TOKEN = "test-auth-token"
BASE = "https://example.cloudfront.net"


class SignatureTest(unittest.TestCase):
    def test_twilio_documented_vector(self):
        params = [("CallSid", "CA1234567890ABCDE"), ("Caller", "+12349013030"), ("Digits", "1234"),
                  ("From", "+12349013030"), ("To", "+18005551212")]
        url = "https://mycompany.com/myapp.php?foo=1&bar=2"
        self.assertEqual(twilio_sig.compute("12345", url, params), "0/KCTR6DLpKmkAf8muzZqo1nDgQ=")
        self.assertTrue(twilio_sig.valid("12345", url, params, "0/KCTR6DLpKmkAf8muzZqo1nDgQ="))

    def test_rejects_tampering_and_missing(self):
        params = [("Body", "YES"), ("From", TEST_PHONE)]
        sig = twilio_sig.compute(TOKEN, BASE + "/api/twilio/inbound", params)
        self.assertFalse(twilio_sig.valid(TOKEN, BASE + "/api/twilio/inbound", [("Body", "STOP"), ("From", TEST_PHONE)], sig))
        self.assertFalse(twilio_sig.valid(TOKEN, BASE + "/api/twilio/other", params, sig))
        self.assertFalse(twilio_sig.valid("", BASE + "/api/twilio/inbound", params, sig))
        self.assertFalse(twilio_sig.valid(TOKEN, BASE + "/api/twilio/inbound", params, ""))


class KeywordTest(unittest.TestCase):
    def test_classify(self):
        cases = {
            "STOP": ["STOP", "stop", " Stop. ", "STOPALL", "unsubscribe", "Cancel", "END", "quit", "REVOKE",
                     "opt out", "OPTOUT", "please stop texting me"],
            "START": ["START", "unstop"],
            "HELP": ["HELP", "help!", "INFO"],
            "YES": ["YES", "yes", "Y", "Yes!", "confirm"],
            "OTHER": ["hi", "", "what is this", "x" * 80 + " stop"],
        }
        for expected, bodies in cases.items():
            for b in bodies:
                self.assertEqual(keywords.classify(b), expected, b)

    def test_reply_copy_is_branded_gsm7_and_short(self):
        texts = [
            keywords.welcome("Twilight Town Mall", "https://www.falloutboy.com", True),
            keywords.welcome("Twilight Town Mall", "https://www.falloutboy.com", False),
            keywords.already_active("Twilight Town Mall"),
            keywords.help_text("hello@mallratalerts.com"),
            keywords.start_text("https://d1234567890.cloudfront.net"),
            keywords.no_signup("https://mallratalerts.com"),
        ]
        for t in texts:
            self.assertTrue(t.startswith("Mall Rat Alerts:"), t)
            self.assertTrue(keywords.is_gsm7(t), t)
            self.assertLessEqual(keywords.segments(t), 2, t)
            self.assertIsNone(EMOJI.search(t))
            self.assertIn("STOP", t)
        self.assertIn("Unofficial fan project", texts[0])
        self.assertIn("Msg & data rates may apply", texts[0])
        self.assertIn("falloutboy.com", texts[0])


class InboundTest(ApiTestCase):
    def setUp(self):
        super().setUp()
        signup_api._twilio_token["value"] = TOKEN
        self._base = signup_api.TWILIO_WEBHOOK_BASE
        signup_api.TWILIO_WEBHOOK_BASE = BASE

    def tearDown(self):
        signup_api._twilio_token["value"] = None
        signup_api.TWILIO_WEBHOOK_BASE = self._base

    def sms(self, body, frm=TEST_PHONE, sign=True, b64=True, path="/api/twilio/inbound", extra=None, headers=None):
        params = [("AccountSid", "ACtest"), ("Body", body), ("From", frm), ("MessageSid", "SMtest123"),
                  ("To", "+18335550100")] + list((extra or {}).items())
        raw = urllib.parse.urlencode(params)
        h = {"content-type": "application/x-www-form-urlencoded", "x-origin-verify": ORIGIN_SECRET}
        if sign:
            h["x-twilio-signature"] = twilio_sig.compute(TOKEN, BASE + path, params)
        h.update(headers or {})
        ev = {
            "rawPath": path,
            "headers": {k: v for k, v in h.items() if v is not None},
            "requestContext": {"http": {"method": "POST"}},
            "body": base64.b64encode(raw.encode()).decode() if b64 else raw,
            "isBase64Encoded": b64,
        }
        return signup_api.handler(ev, None)

    def message(self, res):
        m = re.search(r"<Message>(.*?)</Message>", res["body"], re.S)
        return m.group(1) if m else None

    def pending(self, campaign=CAMPAIGN, status="PENDING_SENDER", updated="2026-11-06T10:00:00.000Z"):
        self.subs.put_item(Item={"phone": TEST_PHONE, "campaign_id": campaign, "status": status,
                                 "campaign_name": "Twilight Town Mall", "source_site": "https://www.falloutboy.com",
                                 "updated_at": updated, "expires_at": 1})

    def test_signature_required(self):
        self.assertEqual(self.sms("YES", sign=False)["statusCode"], 403)
        self.assertEqual(self.sms("YES", headers={"x-twilio-signature": "bogus"})["statusCode"], 403)
        self.assertEqual(self.sms("YES", headers={"x-origin-verify": None})["statusCode"], 403)

    def test_fails_closed_without_secret(self):
        signup_api._twilio_token["value"] = None
        old, signup_api.TWILIO_SECRET_ID = signup_api.TWILIO_SECRET_ID, ""
        try:
            self.assertEqual(self.sms("YES")["statusCode"], 403)
        finally:
            signup_api.TWILIO_SECRET_ID = old

    def test_no_country_check_for_twilio(self):
        self.pending()
        res = self.sms("HELP", headers={"cloudfront-viewer-country": "IE"})
        self.assertEqual(res["statusCode"], 200)

    def test_yes_confirms_pending(self):
        self.pending()
        res = self.sms("yes", b64=False)
        self.assertEqual(res["statusCode"], 200)
        self.assertIn("text/xml", res["headers"]["Content-Type"])
        msg = self.message(res)
        self.assertTrue(msg.startswith("Mall Rat Alerts: You&#x27;re in") or msg.startswith("Mall Rat Alerts: You're in"))
        item = self.subs.items[(TEST_PHONE, CAMPAIGN)]
        self.assertEqual(item["status"], "ACTIVE")
        self.assertEqual(item["confirm_message_sid"], "SMtest123")
        self.assertEqual(item["gsi1pk"], f"ACTIVE#{CAMPAIGN}")
        self.assertNotIn("expires_at", item)
        self.assertEqual(self.audit_events(), ["subscribe_confirmed"])
        a = next(iter(self.audit.items.values()))
        self.assertEqual(a["campaign_id"], CAMPAIGN)
        self.assertEqual(a["body"], "yes")

    def test_yes_picks_most_recent_pending_campaign(self):
        self.pending(CAMPAIGN, updated="2026-11-01T00:00:00.000Z")
        self.pending(OTHER_CAMPAIGN, updated="2026-11-05T00:00:00.000Z")
        self.sms("YES")
        self.assertEqual(self.subs.items[(TEST_PHONE, OTHER_CAMPAIGN)]["status"], "ACTIVE")
        self.assertEqual(self.subs.items[(TEST_PHONE, CAMPAIGN)]["status"], "PENDING_SENDER")

    def test_yes_without_pending(self):
        res = self.sms("YES")
        self.assertIn("couldn", self.message(res))
        self.assertEqual(self.audit_events(), ["sms_yes_no_pending"])
        self.assertIsNone(self.message(self.sms("YES")))  # at most one such reply per day

    def test_yes_when_already_active(self):
        self.pending(status="ACTIVE")
        self.assertIn("already signed up", self.message(self.sms("YES")))

    def test_stop_opts_out_every_campaign_and_replies_nothing(self):
        self.pending(CAMPAIGN)
        self.pending(OTHER_CAMPAIGN, status="ACTIVE")
        res = self.sms("STOP")
        self.assertIsNone(self.message(res))
        for c in (CAMPAIGN, OTHER_CAMPAIGN):
            item = self.subs.items[(TEST_PHONE, c)]
            self.assertEqual(item["status"], "OPTED_OUT")
            self.assertEqual(item["unsubscribe_method"], "STOP_KEYWORD")
        self.assertEqual(sorted(self.audit_events()), ["sms_stop", "sms_stop"])

    def test_stop_from_unknown_number_is_audited(self):
        self.sms("unsubscribe")
        (a,) = self.audit.items.values()
        self.assertEqual((a["event_type"], a["campaign_id"]), ("sms_stop", CAMPAIGN))

    def test_help_and_start(self):
        help_msg = self.message(self.sms("HELP"))
        self.assertIn("hello@mallratalerts.com", help_msg)
        self.assertIn("not affiliated", help_msg)
        self.assertIn("rejoin", self.message(self.sms("START")))
        self.assertIsNone(self.message(self.sms("HELP", extra={"OptOutType": "HELP"})))

    def test_free_text_gets_one_reply_per_day_and_body_not_stored(self):
        self.assertIsNotNone(self.message(self.sms("who is this")))
        self.assertIsNone(self.message(self.sms("hello?")))
        for a in self.audit.items.values():
            self.assertNotIn("body", a)
            self.assertEqual(a["event_type"], "sms_other")

    def test_toll_free_and_garbage_senders_ignored(self):
        self.assertIsNone(self.message(self.sms("HELP", frm="+18005550100")))
        self.assertIsNone(self.message(self.sms("HELP", frm="not-a-number")))
        self.assertEqual(self.audit.items, {})

    def test_internal_error_still_returns_200(self):
        class Broken:
            def query(self, **kw):
                raise RuntimeError("boom")
        signup_api._tables["subs"] = Broken()
        res = self.sms("YES")
        self.assertEqual(res["statusCode"], 200)
        self.assertIn("<Response></Response>", res["body"])

    def test_status_callback_21610_opts_out(self):
        self.pending(status="ACTIVE")
        res = self.sms("", path="/api/twilio/status",
                       extra={"MessageStatus": "failed", "ErrorCode": "21610", "To": TEST_PHONE})
        self.assertEqual(res["statusCode"], 204)
        self.assertEqual(self.subs.items[(TEST_PHONE, CAMPAIGN)]["unsubscribe_method"], "CARRIER_21610")
        self.assertEqual(self.audit_events(), ["carrier_21610"])

    def test_replies_have_no_emoji(self):
        self.pending()
        for body in ("YES", "HELP", "START", "random"):
            self.assertIsNone(EMOJI.search(self.sms(body)["body"]))


if __name__ == "__main__":
    unittest.main()
