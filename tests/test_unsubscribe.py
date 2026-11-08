import unittest

from apibase import CAMPAIGN, OTHER_CAMPAIGN, ApiTestCase
from fakes import TEST_PHONE


class UnsubscribeTest(ApiTestCase):
    def test_opts_out_and_audits(self):
        self.subs.put_item(Item={"phone": TEST_PHONE, "campaign_id": CAMPAIGN, "status": "PENDING_SENDER",
                                 "expires_at": 123})
        res = self.call("/api/unsubscribe", {"phone": TEST_PHONE, "campaign_id": CAMPAIGN})
        self.assertEqual(res["statusCode"], 202)
        self.assertEqual(res["json"]["status"], "unsubscribed")
        item = self.subs.items[(TEST_PHONE, CAMPAIGN)]
        self.assertEqual(item["status"], "OPTED_OUT")
        self.assertEqual(item["unsubscribe_method"], "WEB")
        self.assertNotIn("expires_at", item)
        (a,) = self.audit.items.values()
        self.assertEqual(a["event_type"], "unsubscribe")
        self.assertEqual(a["campaign_id"], CAMPAIGN)

    def test_unknown_number_gets_same_response_and_noop_audit(self):
        self.subs.put_item(Item={"phone": TEST_PHONE, "campaign_id": CAMPAIGN, "status": "ACTIVE"})
        known = self.call("/api/unsubscribe", {"phone": TEST_PHONE})
        unknown = self.call("/api/unsubscribe", {"phone": "(212) 555-2368"})
        self.assertEqual(known["body"], unknown["body"])
        self.assertNotIn(("+12125552368", CAMPAIGN), self.subs.items)
        self.assertEqual(sorted(self.audit_events()), ["unsubscribe", "unsubscribe_noop"])

    def test_already_opted_out_is_noop(self):
        self.subs.put_item(Item={"phone": TEST_PHONE, "campaign_id": CAMPAIGN, "status": "OPTED_OUT",
                                 "unsubscribed_at": "x"})
        self.call("/api/unsubscribe", {"phone": TEST_PHONE})
        self.assertEqual(self.subs.items[(TEST_PHONE, CAMPAIGN)]["unsubscribed_at"], "x")
        self.assertEqual(self.audit_events(), ["unsubscribe_noop"])

    def test_only_affects_the_named_campaign(self):
        for c in (CAMPAIGN, OTHER_CAMPAIGN):
            self.subs.put_item(Item={"phone": TEST_PHONE, "campaign_id": c, "status": "ACTIVE"})
        self.call("/api/unsubscribe", {"phone": TEST_PHONE, "campaign_id": CAMPAIGN})
        self.assertEqual(self.subs.items[(TEST_PHONE, CAMPAIGN)]["status"], "OPTED_OUT")
        self.assertEqual(self.subs.items[(TEST_PHONE, OTHER_CAMPAIGN)]["status"], "ACTIVE")

    def test_validation(self):
        self.assertEqual(self.call("/api/unsubscribe", {"phone": "nope"})["json"]["error"], "invalid_phone")
        res = self.call("/api/unsubscribe", {"phone": TEST_PHONE, "campaign_id": "missing-campaign"})
        self.assertEqual(res["json"]["error"], "unknown_campaign")

    def test_rate_limit(self):
        codes = [self.call("/api/unsubscribe", {"phone": TEST_PHONE})["statusCode"] for _ in range(11)]
        self.assertEqual(codes[:10], [202] * 10)
        self.assertEqual(codes[10], 429)

    def test_plain_form_post_redirects(self):
        res = self.call("/api/unsubscribe", {"phone": TEST_PHONE, "campaign_id": CAMPAIGN}, form=True)
        self.assertEqual(res["statusCode"], 303)
        self.assertEqual(res["headers"]["Location"], "/unsubscribe/done/")


if __name__ == "__main__":
    unittest.main()
