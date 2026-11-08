"""GET /api/alerts: public read-only feed."""

import json

from apibase import CAMPAIGN, ORIGIN_SECRET, ApiTestCase

import signup_api  # noqa: E402


def alert(sent_at, msg="Store open\n\nThnks fr th mmrs.", **kw):
    return {"campaign_id": CAMPAIGN, "sent_at": sent_at, "message": msg, "what_changed": "Store open",
            "note": "Thnks fr th mmrs.", "urls": ["https://www.falloutboy.com/x"], "segments": 1,
            "encoding": "GSM-7", "status": "published", "backfilled": False, "model": "m", **kw}


class FeedTest(ApiTestCase):
    def get(self, qs=None, headers=None):
        h = {"x-origin-verify": ORIGIN_SECRET}
        h.update(headers or {})
        ev = {"rawPath": "/api/alerts", "headers": {k: v for k, v in h.items() if v is not None},
              "queryStringParameters": qs, "requestContext": {"http": {"method": "GET"}}}
        res = signup_api.handler(ev, None)
        return res, (json.loads(res["body"]) if res.get("body") else None)

    def test_newest_first_with_cache_header_and_public_fields_only(self):
        for ts in ("2026-11-06T07:25:04.000Z", "2026-11-06T17:24:05.000Z", "2026-11-06T07:38:03.000Z"):
            self.alerts.put_item(Item=alert(ts))
        res, body = self.get({"campaign_id": CAMPAIGN})
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(res["headers"]["Cache-Control"], "public, max-age=30")
        self.assertEqual([a["sent_at"] for a in body["alerts"]],
                         ["2026-11-06T17:24:05.000Z", "2026-11-06T07:38:03.000Z", "2026-11-06T07:25:04.000Z"])
        self.assertEqual(set(body["alerts"][0]), {"sent_at", "message", "what_changed", "note", "urls", "backfilled", "status"})

    def test_limit_default_and_clamp(self):
        for i in range(8):
            self.alerts.put_item(Item=alert(f"2026-11-06T10:0{i}:00.000Z"))
        self.assertEqual(len(self.get({"limit": "3"})[1]["alerts"]), 3)
        self.assertEqual(len(self.get({"limit": "0"})[1]["alerts"]), 1)
        self.assertEqual(len(self.get({"limit": "junk"})[1]["alerts"]), 8)
        self.assertEqual(len(self.get(None)[1]["alerts"]), 8)

    def test_test_alerts_markers_and_other_campaigns_hidden(self):
        self.alerts.put_item(Item=alert("2026-11-06T10:00:00.000Z"))
        self.alerts.put_item(Item=alert("2026-11-06T10:01:00.000Z", is_test=True))
        self.alerts.put_item(Item={"campaign_id": "src#evt", "sent_at": "marker"})
        self.alerts.put_item(Item={**alert("2026-11-06T10:02:00.000Z"), "campaign_id": "other-campaign-2027"})
        self.assertEqual([a["sent_at"] for a in self.get({"campaign_id": CAMPAIGN})[1]["alerts"]], ["2026-11-06T10:00:00.000Z"])

    def test_bad_campaign_and_empty(self):
        res, body = self.get({"campaign_id": "Bad Id!"})
        self.assertEqual((res["statusCode"], res["headers"]["Cache-Control"]), (400, "no-store"))
        res, body = self.get({"campaign_id": CAMPAIGN})
        self.assertEqual((res["statusCode"], body["alerts"]), (200, []))

    def test_requires_origin_secret_and_works_from_any_country(self):
        self.assertEqual(self.get(headers={"x-origin-verify": None})[0]["statusCode"], 403)
        self.assertEqual(self.get(headers={"cloudfront-viewer-country": "IE"})[0]["statusCode"], 200)

    def test_dynamodb_error_is_503_not_cached(self):
        class Broken:
            def query(self, **kw):
                raise RuntimeError("down")
        signup_api._tables["alerts"] = Broken()
        res, body = self.get()
        self.assertEqual((res["statusCode"], res["headers"]["Cache-Control"], body["alerts"]), (503, "no-store", []))

    def test_post_to_alerts_is_not_a_route(self):
        res = self.call("/api/alerts", {})
        self.assertEqual(res["statusCode"], 404)
