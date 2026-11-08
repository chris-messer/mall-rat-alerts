"""X-Twilio-Signature validation (stdlib only).

Signature = Base64(HMAC-SHA1(auth_token, url + concat(sorted key + value for each POST param))).
`url` must be the exact public URL configured in Twilio, not one rebuilt from request headers.
"""

import base64
import hashlib
import hmac


def compute(auth_token, url, params):
    """params: list of (key, value) pairs from the form body."""
    payload = url + "".join(k + v for k, v in sorted(params, key=lambda kv: kv[0]))
    digest = hmac.new(auth_token.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def valid(auth_token, url, params, signature):
    if not auth_token or not signature:
        return False
    return hmac.compare_digest(compute(auth_token, url, params), signature)
