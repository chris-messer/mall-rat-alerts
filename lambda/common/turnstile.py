"""Cloudflare Turnstile server-side verification (siteverify over urllib)."""

import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import datetime

log = logging.getLogger()

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
MAX_TOKEN_AGE_SECONDS = 300


def _post(url, data, timeout=5):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def verify(token, secret, remote_ip, expected_action, allowed_hostnames, post=_post, now=None):
    """True only if Cloudflare says the token is valid for this site and action and is fresh."""
    if not token or not secret or len(token) > 2048:
        return False
    try:
        res = post(SITEVERIFY_URL, {"secret": secret, "response": token, "remoteip": remote_ip or ""})
    except Exception as e:
        log.warning("turnstile siteverify failed: %s", type(e).__name__)
        return False
    if not res.get("success"):
        return False
    if allowed_hostnames and res.get("hostname") not in allowed_hostnames:
        return False
    if expected_action and res.get("action") != expected_action:
        return False
    ts = res.get("challenge_ts")
    if ts:
        try:
            issued = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return False
        if (time.time() if now is None else now) - issued > MAX_TOKEN_AGE_SECONDS:
            return False
    return True
