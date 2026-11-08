import unittest

from apibase import CAMPAIGN, CONSENT_VERSION, OTHER_CAMPAIGN, ApiTestCase
from fakes import TEST_PHONE

import signup_api  # noqa: E402


class SubscribeTest(ApiTestCase):
    def test_new_signup_stores_pending_sender_with_snapshot(self):
        res = self.call("/api/subscribe", self.signup())
        self.assertEqual(res["statusCode"], 202)
        self.assertEqual(res["json"]["status"], "check_your_phone")

        item = self.subs.items[(TEST_PHONE, CAMPAIGN)]
        self.assertEqual(item["status"], "PENDING_SENDER")
        self.assertEqual(item["campaign_id"], CAMPAIGN)
        self.assertEqual(item["campaign_name"], "Twilight Town Mall")
        self.assertEqual(item["campaign_tags"], ["fob", "twilight-town-mall", "2026"])
        self.assertEqual(item["source_site"], "https://www.falloutboy.com")
        self.assertEqual(item["consent_version"], CONSENT_VERSION)
        self.assertEqual(item["form_version"], "landing-2026-11-06.v2")
        self.assertEqual(item["source"], "web")
        self.assertEqual(item["utm_source"], "insta")
        self.assertEqual(item["utm_medium"], "social")
        self.assertEqual(item["landing_path"], "/")
        self.assertTrue(item["is_test"])
        self.assertIn("expires_at", item)
        for k in ("created_at", "updated_at", "ip_hash", "ua_hash"):
            self.assertTrue(item[k], k)
        self.assertNotIn(self.ip, str(item))
        self.assertNotIn("unittest-agent", str(item))

        self.assertCountEqual(self.audit_events(),
                              ["consent_recorded", "subscribe_pending", "signup_web", "confirm_suppressed"])
        self.assertEqual(self.sent, [])  # the shared test number is never texted
        for a in self.audit.items.values():
            self.assertEqual(a["campaign_id"], CAMPAIGN)
            self.assertEqual(a["ip_hash"], item["ip_hash"])
            self.assertEqual(a["consent_version"], CONSENT_VERSION)

    def test_duplicate_gets_identical_response(self):
        first = self.call("/api/subscribe", self.signup())
        second = self.call("/api/subscribe", self.signup())
        self.assertEqual(first["statusCode"], second["statusCode"])
        self.assertEqual(first["body"], second["body"])
        self.assertEqual(self.subs.items[(TEST_PHONE, CAMPAIGN)]["signup_requests"], 2)

    def test_active_subscriber_is_not_downgraded(self):
        self.subs.put_item(Item={"phone": TEST_PHONE, "campaign_id": CAMPAIGN, "status": "ACTIVE"})
        res = self.call("/api/subscribe", self.signup())
        self.assertEqual(res["json"]["status"], "check_your_phone")
        self.assertEqual(self.subs.items[(TEST_PHONE, CAMPAIGN)]["status"], "ACTIVE")
        self.assertCountEqual(self.audit_events(), ["subscribe_duplicate", "signup_web", "confirm_suppressed"])

    def test_web_form_does_not_resubscribe_opted_out_numbers(self):
        self.subs.put_item(Item={"phone": TEST_PHONE, "campaign_id": CAMPAIGN, "status": "OPTED_OUT",
                                 "unsubscribed_at": "2026-11-01T00:00:00.000Z"})
        res = self.call("/api/subscribe", self.signup())
        self.assertEqual(res["statusCode"], 202)
        self.assertEqual(res["json"]["status"], "check_your_phone")
        item = self.subs.items[(TEST_PHONE, CAMPAIGN)]
        self.assertEqual(item["status"], "OPTED_OUT")
        self.assertEqual(self.sent, [])

    def test_same_phone_can_join_multiple_campaigns(self):
        self.call("/api/subscribe", self.signup())
        self.call("/api/subscribe", self.signup(campaign_id=OTHER_CAMPAIGN))
        self.assertIn((TEST_PHONE, CAMPAIGN), self.subs.items)
        self.assertIn((TEST_PHONE, OTHER_CAMPAIGN), self.subs.items)

    def test_campaign_defaults_when_omitted(self):
        res = self.call("/api/subscribe", self.signup(campaign_id=None))
        self.assertEqual(res["statusCode"], 202)
        self.assertIn((TEST_PHONE, CAMPAIGN), self.subs.items)

    def test_validation_errors(self):
        cases = [
            (self.signup(phone="not a phone"), "invalid_phone"),
            (self.signup(phone="+14165552368"), "invalid_phone"),
            (self.signup(consent=False), "consent_required"),
            (self.signup(consent=None), "consent_required"),
            (self.signup(age_18_plus=False), "age_required"),
            (self.signup(consent_version="old"), "stale_consent_version"),
            (self.signup(campaign_id="Bad Slug!"), "unknown_campaign"),
            (self.signup(campaign_id="not-a-real-campaign"), "unknown_campaign"),
        ]
        for payload, err in cases:
            res = self.call("/api/subscribe", payload)
            self.assertEqual(res["statusCode"], 400, err)
            self.assertEqual(res["json"]["error"], err)
        self.assertEqual(self.subs.items, {})
        self.assertEqual(self.audit.items, {})

    def test_malformed_body(self):
        ev = self.event("/api/subscribe", {})
        ev["body"] = "{not json"
        res = signup_api.handler(ev, None)
        self.assertEqual(res["statusCode"], 400)
        ev["body"] = "x" * 5000
        self.assertEqual(signup_api.handler(ev, None)["statusCode"], 400)

    def test_honeypot_and_too_fast_are_silently_dropped(self):
        import time
        for payload in (self.signup(website="http://spam"), self.signup(form_rendered_at=int(time.time()))):
            res = self.call("/api/subscribe", payload)
            self.assertEqual(res["statusCode"], 202)
        self.assertEqual(self.subs.items, {})

    def test_requests_must_come_through_cloudfront_from_us(self):
        res = self.call("/api/subscribe", self.signup(), headers={"x-origin-verify": None})
        self.assertEqual(res["statusCode"], 403)
        self.assertEqual(res["json"]["error"], "forbidden")
        res = self.call("/api/subscribe", self.signup(), headers={"cloudfront-viewer-country": "CA"})
        self.assertEqual(res["json"]["error"], "region_not_supported")

    def test_ip_rate_limit(self):
        codes = []
        for i in range(7):
            codes.append(self.call("/api/subscribe", self.signup())["statusCode"])
        self.assertEqual(codes[:3], [202] * 3)
        self.assertEqual(codes[3:], [429] * 4)

    def test_phone_rate_limit_is_silent(self):
        signup_api.LIMITS["phone_subscribe_day"], old = 1, signup_api.LIMITS["phone_subscribe_day"]
        try:
            self.call("/api/subscribe", self.signup())
            self.ip = "198.51.100.8"
            res = self.call("/api/subscribe", self.signup())
        finally:
            signup_api.LIMITS["phone_subscribe_day"] = old
        self.assertEqual(res["statusCode"], 202)
        self.assertEqual(self.subs.items[(TEST_PHONE, CAMPAIGN)]["signup_requests"], 1)

    def test_kill_switch_and_closed_campaign(self):
        self.ops.items[("config#global", "current")]["signups_enabled"] = False
        self.assertEqual(self.call("/api/subscribe", self.signup())["json"]["error"], "signups_paused")
        self.ops.items[("config#global", "current")]["signups_enabled"] = True
        self.ops.items[(f"config#campaign#{CAMPAIGN}", "current")]["campaign_status"] = "ended"
        from common import store
        store.reset_config_cache()
        self.assertEqual(self.call("/api/subscribe", self.signup())["json"]["error"], "campaign_closed")

    def test_plain_form_post_redirects_on_success(self):
        payload = self.signup(consent="yes", age_18_plus="yes")
        res = self.call("/api/subscribe", payload, form=True)
        self.assertEqual(res["statusCode"], 303)
        self.assertEqual(res["headers"]["Location"], "/thanks/")
        self.assertIn((TEST_PHONE, CAMPAIGN), self.subs.items)

    def test_plain_form_post_error_returns_html(self):
        res = self.call("/api/subscribe", self.signup(phone="nope", consent="yes", age_18_plus="yes"), form=True)
        self.assertEqual(res["statusCode"], 400)
        self.assertIn("text/html", res["headers"]["Content-Type"])
        self.assertIn("unofficial fan project", res["body"])

    def test_unknown_route(self):
        self.assertEqual(self.call("/api/nope", {})["statusCode"], 404)
        self.assertEqual(self.call("/api/subscribe", {}, method="GET")["statusCode"], 404)


if __name__ == "__main__":
    unittest.main()
