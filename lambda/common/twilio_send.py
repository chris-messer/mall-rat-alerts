"""Outbound SMS through the Twilio REST API (stdlib only).

Used for exactly one thing: the single "Reply YES to confirm" text after a web signup.
Credentials are passed in by the caller (read from Secrets Manager) and never logged or put in errors.
"""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.twilio.com/2010-04-01"


class TwilioError(Exception):
    """Send failed. Carries only the HTTP status and Twilio error code, never the request or credentials."""

    def __init__(self, http_status=None, code=None):
        super().__init__(f"twilio send failed http={http_status} code={code}")
        self.http_status = http_status
        self.code = code


def send_message(account_sid, auth_token, from_number, to, body, status_callback=None, timeout=5, opener=None):
    """POST one message. Returns {"sid", "status"}. Raises TwilioError."""
    url = f"{API_BASE}/Accounts/{urllib.parse.quote(account_sid, safe='')}/Messages.json"
    fields = {"To": to, "From": from_number, "Body": body}
    if status_callback:
        fields["StatusCallback"] = status_callback
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST")
    basic = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    req.add_header("Authorization", "Basic " + basic)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        code = None
        try:
            code = json.loads(e.read().decode("utf-8")).get("code")
        except Exception:
            pass
        raise TwilioError(e.code, code) from None
    except Exception as e:
        raise TwilioError(None, type(e).__name__) from None
    return {"sid": data.get("sid"), "status": data.get("status")}
