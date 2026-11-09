"""Optional server-side PostHog events over urllib. A no-op unless POSTHOG_KEY is set.

Never send phone numbers, emails, webhook URLs, IPs, or message bodies.
distinct_id must be anonymous (salted hash, alert id, or fan-out run id) — never a phone.
"""

import json
import logging
import os
import urllib.request

log = logging.getLogger()

POSTHOG_KEY = os.environ.get("POSTHOG_KEY", "")
POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com").rstrip("/")


def _post(url, body, timeout=2):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def capture(event, distinct_id, properties=None, post=_post, key=None):
    """Fail-open: returns False on missing key or transport errors; never raises."""
    key = POSTHOG_KEY if key is None else key
    if not key:
        return False
    props = {"source": "server", "$process_person_profile": False, **(properties or {})}
    try:
        post(f"{POSTHOG_HOST}/i/v0/e/", {"api_key": key, "event": event, "distinct_id": distinct_id, "properties": props})
        return True
    except Exception as e:
        log.warning("posthog capture failed: %s", type(e).__name__)
        return False


def alert_distinct_id(campaign_id, alert_id):
    """Stable anonymous id for one alert (sent_at / fan-out alert_id). Never a phone."""
    return f"alert:{campaign_id}:{alert_id}"
