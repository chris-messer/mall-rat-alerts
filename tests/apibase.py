"""Shared setup for signup API handler tests."""

import json
import os
import unittest
import urllib.parse

from fakes import FakeTable, TEST_PHONE, install_stubs

install_stubs()

CAMPAIGN = "fob-twilight-town-mall-2026"
OTHER_CAMPAIGN = "some-future-campaign-2027"
CONSENT_VERSION = "sms-consent-2026-11-06.v2"
ORIGIN_SECRET = "test-origin-secret"

os.environ.update({
    "SUBSCRIBERS_TABLE": "subs",
    "AUDIT_TABLE": "audit",
    "OPS_TABLE": "ops",
    "IP_HASH_SALT": "test-salt",
    "ORIGIN_VERIFY_SECRET": ORIGIN_SECRET,
    "CONSENT_VERSION": CONSENT_VERSION,
    "CONSENT_SHA256": "0" * 64,
    "DEFAULT_CAMPAIGN_ID": CAMPAIGN,
    "ALLOWED_COUNTRIES": "US",
    "TEST_NUMBERS": TEST_PHONE,
    "TURNSTILE_ENABLED": "false",
})

import signup_api  # noqa: E402
from common import store  # noqa: E402

CAMPAIGN_ITEM = {
    "pk": f"config#campaign#{CAMPAIGN}",
    "sk": "current",
    "campaign_id": CAMPAIGN,
    "campaign_name": "Twilight Town Mall",
    "campaign_tags": ["fob", "twilight-town-mall", "2026"],
    "source_site": "https://www.falloutboy.com",
    "campaign_status": "active",
    "signups_enabled": True,
}


FOB_TAG = {
    "pk": "config#tags", "sk": "FOB", "tag": "FOB", "aliases": ["FOB", "FALLOUTBOY"],
    "campaign_id": CAMPAIGN, "campaign_name": "Twilight Town Mall", "enabled": True,
    "welcome_message": (
        "Mall Rat Alerts: You're in for Twilight Town Mall (Fall Out Boy site alerts).\n\n"
        "We'll text when the site changes. Msg freq varies. Msg&data rates may apply. "
        "Reply STOP to opt out, HELP for help. Unofficial fan project."),
}
NEW_TAG = {
    "pk": "config#tags", "sk": "NEWTAG", "tag": "NEWTAG", "aliases": ["NEWTAG", "FUTURE"],
    "campaign_id": OTHER_CAMPAIGN, "campaign_name": "Future", "enabled": True,
    "welcome_message": "Mall Rat Alerts: You're in for Future alerts. Reply STOP to opt out, HELP for help.",
}


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        store.reset_config_cache()
        self.subs = FakeTable("phone", "campaign_id")
        self.audit = FakeTable("phone", "sk")
        self.ops = FakeTable("pk", "sk")
        self.ops.put_item(Item=dict(CAMPAIGN_ITEM))
        self.ops.put_item(Item={**CAMPAIGN_ITEM, "pk": f"config#campaign#{OTHER_CAMPAIGN}",
                                "campaign_id": OTHER_CAMPAIGN, "campaign_name": "Future"})
        self.ops.put_item(Item={"pk": "config#global", "sk": "current", "signups_enabled": True,
                                "waitlist_mode": True, "broadcast_enabled": False})
        self.templates = FakeTable("pk", "sk")
        self.alerts = FakeTable("campaign_id", "sent_at")
        self.templates.put_item(Item=dict(FOB_TAG))
        self.templates.put_item(Item=dict(NEW_TAG))
        signup_api._tables.clear()
        signup_api._tables.update(subs=self.subs, audit=self.audit, ops=self.ops,
                                  templates=self.templates, alerts=self.alerts)
        self.ip = "198.51.100.7"
        # No test may reach Twilio: every outbound confirmation is captured here instead.
        self.sent = []
        self.send_error = None
        self._real_send = signup_api._send_sms

        def fake_send(to, body):
            if self.send_error:
                raise self.send_error
            self.sent.append((to, body))
            return {"sid": "SMfake%d" % len(self.sent), "status": "queued"}

        signup_api._send_sms = fake_send
        self.addCleanup(lambda: setattr(signup_api, "_send_sms", self._real_send))

    def event(self, path, payload, form=False, headers=None, method="POST"):
        h = {
            "content-type": "application/x-www-form-urlencoded" if form else "application/json",
            "user-agent": "unittest-agent",
            "cloudfront-viewer-country": "US",
            "cloudfront-viewer-address": f"{self.ip}:44321",
            "x-origin-verify": ORIGIN_SECRET,
            "referer": "https://example.cloudfront.net/?utm_source=insta&utm_medium=social",
        }
        h.update(headers or {})
        body = urllib.parse.urlencode(payload) if form else json.dumps(payload)
        return {
            "rawPath": path,
            "headers": {k: v for k, v in h.items() if v is not None},
            "requestContext": {"http": {"method": method, "sourceIp": "203.0.113.9"}},
            "body": body,
            "isBase64Encoded": False,
        }

    def call(self, path, payload, **kw):
        res = signup_api.handler(self.event(path, payload, **kw), None)
        body = res.get("body") or ""
        if res.get("headers", {}).get("Content-Type") == "application/json":
            res["json"] = json.loads(body)
        return res

    def signup(self, phone=TEST_PHONE, **overrides):
        payload = {
            "phone": phone,
            "consent": True,
            "age_18_plus": True,
            "consent_version": CONSENT_VERSION,
            "campaign_id": CAMPAIGN,
            "form_version": "landing-2026-11-06.v2",
            "website": "",
        }
        payload.update(overrides)
        return {k: v for k, v in payload.items() if v is not None}

    def audit_events(self):
        return [i["event_type"] for _, i in sorted(self.audit.items.items(), key=lambda kv: kv[0][1])]
