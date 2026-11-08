#!/usr/bin/env python3
"""Send signed, Twilio-shaped requests to the live inbound webhook. No SMS is sent: the TwiML
comes back to this script, not to Twilio. Uses the Twilio magic test number by default.

  python3 scripts/smoke_inbound.py --base https://<cloudfront-domain> HELP YES STOP
  python3 scripts/smoke_inbound.py --base https://<cloudfront-domain> --cleanup FOB "fall out boy" HELP

The signed requests go to the webhook directly, so the TwiML reply is printed here and Twilio never
sends anything. Keyword joins (FOB) create a real ACTIVE row for the from-number, so use the Twilio
magic number (default) and pass --cleanup to delete that number's rows afterwards.
"""

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0] + "/lambda")
from common import twilio_sig  # noqa: E402

AWS = ["aws", "--profile", "default", "--region", "us-east-1"]


def post(url, params, signature):
    data = urllib.parse.urlencode(params).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if signature:
        headers["X-Twilio-Signature"] = signature
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=15) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--from-number", default="+15005550006")
    ap.add_argument("--secret", default="mall-rat-alerts/secrets")
    ap.add_argument("--cleanup", action="store_true", help="delete the test number's subscriber rows after")
    ap.add_argument("bodies", nargs="+")
    args = ap.parse_args()

    raw = subprocess.check_output(AWS + ["secretsmanager", "get-secret-value", "--secret-id", args.secret,
                                         "--query", "SecretString", "--output", "text"])
    token = json.loads(raw)["twilio_auth_token"]
    url = args.base.rstrip("/") + "/api/twilio/inbound"

    probe = [("Body", "HELP"), ("From", args.from_number)]
    print("unsigned ->", post(url, probe, None)[0])
    print("bad signature ->", post(url, probe, "bm90LXZhbGlk")[0])
    for i, body in enumerate(args.bodies):
        params = [("AccountSid", "ACsmoketest"), ("Body", body), ("From", args.from_number),
                  ("MessageSid", f"SMsmoketest{i:04d}"), ("To", "+18335961548")]
        status, text = post(url, params, twilio_sig.compute(token, url, params))
        print(f"{body!r} -> {status} {text}")

    if args.cleanup:
        if args.from_number != "+15005550006":
            sys.exit("--cleanup only runs for the +15005550006 test number")
        out = json.loads(subprocess.check_output(AWS + [
            "dynamodb", "query", "--table-name", "mall-rat-alerts-subscribers",
            "--key-condition-expression", "phone = :p",
            "--expression-attribute-values", json.dumps({":p": {"S": args.from_number}}),
            "--projection-expression", "campaign_id"]))
        for item in out["Items"]:
            subprocess.check_call(AWS + [
                "dynamodb", "delete-item", "--table-name", "mall-rat-alerts-subscribers", "--key",
                json.dumps({"phone": {"S": args.from_number}, "campaign_id": item["campaign_id"]})])
            print("deleted subscriber row", item["campaign_id"]["S"])


if __name__ == "__main__":
    main()
