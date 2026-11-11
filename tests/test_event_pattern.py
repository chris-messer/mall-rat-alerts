"""EventBridge rule pattern for the composer must match what fob-site-monitor actually publishes.

Regression: the rule required detail.test to be absent, but the monitor always sends "test": false,
so no real alert ever reached the composer. Set MRA_LIVE_AWS=1 to also check with EventBridge itself.
"""

import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAMPAIGN = "fob-twilight-town-mall-2026"


def rule_pattern():
    with open(os.path.join(ROOT, "terraform", "site_change_pattern.json.tftpl")) as f:
        return json.loads(f.read().replace("${campaign_id}", CAMPAIGN))


def monitor_event(test=False, include_test_key=True):
    """Shape of fob-site-monitor's publish_change() entry."""
    detail = {"campaign_id": CAMPAIGN, "alert_kind": "change", "detected_at": "2026-11-06T20:41:04.453Z",
              "changes": [{"url": "https://www.falloutboy.com/aces-hifi-records"}]}
    if include_test_key:
        detail["test"] = test
    return {"id": "e187219e-5be4-19b1-9e18-0f013dce7bf2", "account": "018908982481", "region": "us-east-1",
            "time": "2026-11-06T20:41:04Z", "resources": [], "detail-type": "SiteChange",
            "source": "fob-site-monitor" + (".test" if test else ""), "detail": detail}


_MISSING = object()


def _value_matches(rules, value):
    for r in rules:
        if isinstance(r, dict):
            if "exists" in r and r["exists"] == (value is not _MISSING):
                return True
        elif value is not _MISSING and value == r and type(value) is type(r):
            return True
    return False


def matches(pattern, event):
    """The subset of EventBridge matching the rule uses: exact values, exists, and $or."""
    for key, rules in pattern.items():
        if key == "$or":
            if not any(matches(p, event) for p in rules):
                return False
        elif isinstance(rules, dict):
            sub = event.get(key)
            if not isinstance(sub, dict) or not matches(rules, sub):
                return False
        elif not _value_matches(rules, event.get(key, _MISSING)):
            return False
    return True


class EventPatternTest(unittest.TestCase):
    def check(self, event, expected):
        self.assertEqual(matches(rule_pattern(), event), expected)
        if os.environ.get("MRA_LIVE_AWS") == "1":
            import boto3
            r = boto3.client("events", region_name="us-east-1").test_event_pattern(
                EventPattern=json.dumps(rule_pattern()), Event=json.dumps(event))
            self.assertEqual(r["Result"], expected)

    def test_real_monitor_alert_with_test_false_matches(self):
        self.check(monitor_event(test=False), True)

    def test_event_without_test_key_matches(self):
        self.check(monitor_event(include_test_key=False), True)

    def test_pattern_does_not_depend_on_host(self):
        for url in ("https://twilighttownmall.falloutboy.com/lost-and-found", "https://www.falloutboy.com/tour"):
            ev = monitor_event()
            ev["detail"]["changes"] = [{"url": url}]
            self.check(ev, True)
        self.assertNotIn("falloutboy", json.dumps(rule_pattern()))

    def test_monitor_test_events_do_not_match(self):
        self.check(monitor_event(test=True), False)
        ev = monitor_event(test=False)
        ev["detail"]["test"] = True
        self.check(ev, False)

    def test_other_campaigns_do_not_match(self):
        ev = monitor_event()
        ev["detail"]["campaign_id"] = "other-campaign"
        self.check(ev, False)


if __name__ == "__main__":
    unittest.main()
