#!/usr/bin/env python3
"""Point one Twilio number's SMS webhook at the Mall Rat Alerts inbound URL.

Refuses to touch the number if it belongs to a Messaging Service (changing the service would
affect every number in it) or if its current SMS URL points somewhere else, unless --force.
Prints only the number SID and URLs (host and path), never credentials.

  python3 scripts/set_twilio_webhook.py --number +18335961548 \
      --url https://<cloudfront-domain>/api/twilio/inbound [--dry-run]
"""

import argparse
import base64
import json
import subprocess
import urllib.parse
import urllib.request

AWS = ["aws", "--profile", "default", "--region", "us-east-1"]


def creds(secret):
    raw = subprocess.check_output(AWS + ["secretsmanager", "get-secret-value", "--secret-id", secret,
                                         "--query", "SecretString", "--output", "text"])
    d = json.loads(raw)
    return d["twilio_account_sid"], d["twilio_auth_token"]


def safe(u):
    if not u:
        return "(empty)"
    p = urllib.parse.urlsplit(u)
    return f"{p.scheme}://{p.hostname}{p.path}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--number", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--secret", default="mall-rat-alerts/secrets")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    sid, token = creds(args.secret)
    auth = "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode()

    def call(url, data=None):
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(url, data=body, headers={"Authorization": auth})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    base = f"https://api.twilio.com/2010-04-01/Accounts/{sid}"
    q = urllib.parse.urlencode({"PhoneNumber": args.number})
    nums = call(f"{base}/IncomingPhoneNumbers.json?{q}")["incoming_phone_numbers"]
    if len(nums) != 1:
        raise SystemExit(f"expected exactly one number matching, found {len(nums)}")
    n = nums[0]
    print("number SID:", n["sid"], "| friendly name:", n["friendly_name"])
    print("current sms_url:", safe(n["sms_url"]), "| sms_application_sid:", n.get("sms_application_sid") or None)

    for s in call("https://messaging.twilio.com/v1/Services?PageSize=50")["services"]:
        pn = call(f"https://messaging.twilio.com/v1/Services/{s['sid']}/PhoneNumbers?PageSize=50")["phone_numbers"]
        if any(p["phone_number"] == args.number for p in pn):
            raise SystemExit(f"number is in Messaging Service {s['sid']}; set the service inbound URL by hand instead")

    current = n["sms_url"] or ""
    if current and current != args.url and not args.force:
        raise SystemExit("number already has a different SMS URL; re-run with --force if that is intended")
    if n.get("sms_application_sid") and not args.force:
        raise SystemExit("number uses a TwiML App for SMS; re-run with --force if that is intended")

    if args.dry_run:
        print("dry run: would set sms_url to", safe(args.url))
        return
    updated = call(f"{base}/IncomingPhoneNumbers/{n['sid']}.json",
                   {"SmsUrl": args.url, "SmsMethod": "POST", "SmsFallbackUrl": "", "SmsApplicationSid": ""})
    print("updated sms_url:", safe(updated["sms_url"]), updated["sms_method"])


if __name__ == "__main__":
    main()
