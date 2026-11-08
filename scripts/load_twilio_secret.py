#!/usr/bin/env python3
"""Copy ONLY the Twilio account SID and auth token from a source secret into mall-rat-alerts/secrets.

Values are never printed, and the temp file holding them is 0600 and deleted right away.

  python3 scripts/load_twilio_secret.py --source staging-versiful_secrets \
      --sid-key twilio_account_sid --token-key twilio_auth
"""

import argparse
import json
import os
import subprocess
import tempfile

AWS = ["aws", "--profile", "default", "--region", "us-east-1"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--sid-key", default="twilio_account_sid")
    ap.add_argument("--token-key", default="twilio_auth")
    ap.add_argument("--target", default="mall-rat-alerts/secrets")
    args = ap.parse_args()

    raw = subprocess.check_output(AWS + ["secretsmanager", "get-secret-value", "--secret-id", args.source,
                                         "--query", "SecretString", "--output", "text"])
    src = json.loads(raw)
    missing = [k for k in (args.sid_key, args.token_key) if not src.get(k)]
    if missing:
        raise SystemExit(f"source secret is missing keys: {missing}")
    value = json.dumps({"twilio_account_sid": src[args.sid_key], "twilio_auth_token": src[args.token_key]})
    del src, raw

    fd, path = tempfile.mkstemp(prefix="mra-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(value)
        subprocess.check_call(AWS + ["secretsmanager", "put-secret-value", "--secret-id", args.target,
                                     "--secret-string", f"file://{path}"], stdout=subprocess.DEVNULL)
    finally:
        os.remove(path)
    print(f"stored keys twilio_account_sid, twilio_auth_token in {args.target} (values not shown)")


if __name__ == "__main__":
    main()
