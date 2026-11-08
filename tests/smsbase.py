"""Signed inbound-SMS helpers shared by the keyword and tag tests."""

import base64
import re
import urllib.parse

from apibase import CAMPAIGN, ORIGIN_SECRET, ApiTestCase
from fakes import TEST_PHONE

import signup_api  # noqa: E402
from common import twilio_sig  # noqa: E402

TOKEN = "test-auth-token"
BASE = "https://example.cloudfront.net"
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")


class SmsTestCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        signup_api._twilio_token["value"] = TOKEN
        self._base = signup_api.TWILIO_WEBHOOK_BASE
        signup_api.TWILIO_WEBHOOK_BASE = BASE
        self.tracked = []
        self._capture = signup_api.analytics.capture
        signup_api.analytics.capture = lambda event, did, props=None, **kw: self.tracked.append((event, did, props or {}))

    def tearDown(self):
        signup_api._twilio_token["value"] = None
        signup_api.TWILIO_WEBHOOK_BASE = self._base
        signup_api.analytics.capture = self._capture

    def sms(self, body, frm=TEST_PHONE, path="/api/twilio/inbound", extra=None):
        params = [("AccountSid", "ACtest"), ("Body", body), ("From", frm), ("MessageSid", "SMtest123"),
                  ("To", "+18335550100")] + list((extra or {}).items())
        h = {"content-type": "application/x-www-form-urlencoded", "x-origin-verify": ORIGIN_SECRET,
             "x-twilio-signature": twilio_sig.compute(TOKEN, BASE + path, params)}
        ev = {"rawPath": path, "headers": h, "requestContext": {"http": {"method": "POST"}},
              "body": base64.b64encode(urllib.parse.urlencode(params).encode()).decode(), "isBase64Encoded": True}
        return signup_api.handler(ev, None)

    def message(self, res):
        m = re.search(r"<Message>(.*?)</Message>", res["body"], re.S)
        if not m:
            return None
        import html
        return html.unescape(m.group(1))

    def member(self, campaign=CAMPAIGN, status="PENDING_SENDER", updated="2026-11-06T10:00:00.000Z", **extra):
        self.subs.put_item(Item={"phone": TEST_PHONE, "campaign_id": campaign, "status": status,
                                 "campaign_name": "Twilight Town Mall", "updated_at": updated, **extra})
