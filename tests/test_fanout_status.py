"""Status callbacks for fan-out sends: delivery status lands on the (alert, phone) send row, found by MessageSid."""

from fakes import FakeTable, TEST_PHONE
from smsbase import SmsTestCase

import signup_api  # noqa: E402

ALERT_ID = "fob-twilight-town-mall-2026#2026-11-08T18:00:00.000Z"
SID = "SMfake0000000000000000000000000001"


class FanoutStatusTest(SmsTestCase):
    def setUp(self):
        super().setUp()
        self.sends = FakeTable("alert_id", "phone")
        self.sends.put_item(Item={"alert_id": ALERT_ID, "phone": TEST_PHONE, "status": "SENT", "message_sid": SID})
        self.sends.put_item(Item={"alert_id": f"sid#{SID}", "phone": "map", "target_alert_id": ALERT_ID,
                                  "target_phone": TEST_PHONE})
        signup_api._tables["sends"] = self.sends

    def callback(self, status, code="", sid=SID, to=TEST_PHONE):
        extra = {"MessageStatus": status, "To": to}
        if code:
            extra["ErrorCode"] = code
        res = self.sms("", path="/api/twilio/status", extra=extra)
        return res

    def row(self):
        return self.sends.items[(ALERT_ID, TEST_PHONE)]

    def sms(self, body, frm=TEST_PHONE, path="/api/twilio/inbound", extra=None):
        # Same signed request as SmsTestCase, but with this send's MessageSid.
        import base64
        import urllib.parse
        from smsbase import BASE, ORIGIN_SECRET, TOKEN
        from common import twilio_sig
        params = [("AccountSid", "ACtest"), ("Body", body), ("From", frm), ("MessageSid", SID)] + list((extra or {}).items())
        h = {"content-type": "application/x-www-form-urlencoded", "x-origin-verify": ORIGIN_SECRET,
             "x-twilio-signature": twilio_sig.compute(TOKEN, BASE + path, params)}
        ev = {"rawPath": path, "headers": h, "requestContext": {"http": {"method": "POST"}},
              "body": base64.b64encode(urllib.parse.urlencode(params).encode()).decode(), "isBase64Encoded": True}
        return signup_api.handler(ev, None)

    def test_delivered_is_recorded(self):
        self.assertEqual(self.callback("delivered")["statusCode"], 204)
        self.assertEqual((self.row()["delivery_status"], self.row()["status"]), ("delivered", "SENT"))

    def test_intermediate_status_is_ignored(self):
        self.callback("sent")
        self.assertNotIn("delivery_status", self.row())

    def test_unknown_sid_or_other_recipient_changes_nothing(self):
        del self.sends.items[(f"sid#{SID}", "map")]
        self.callback("delivered")
        self.assertNotIn("delivery_status", self.row())
        self.sends.put_item(Item={"alert_id": f"sid#{SID}", "phone": "map", "target_alert_id": ALERT_ID,
                                  "target_phone": "+12025550199"})
        self.callback("delivered")
        self.assertNotIn("delivery_status", self.row())

    def test_21610_on_fanout_send_still_opts_out(self):
        self.member(status="ACTIVE")
        self.callback("undelivered", code="21610")
        self.assertEqual(self.subs.items[(TEST_PHONE, "fob-twilight-town-mall-2026")]["status"], "OPTED_OUT")
        self.assertEqual(self.row()["delivery_error_code"], "21610")
