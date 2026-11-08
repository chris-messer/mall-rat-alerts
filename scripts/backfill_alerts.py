#!/usr/bin/env python3
"""Backfill the public alerts table from the real alerts the monitor has already sent.

Source: CloudWatch group /aws/lambda/fob-site-monitor. A real alert is a request whose log has both a
`changes:` line and an `sms results:` line. Test messages never log a `changes:` line, so they are
excluded (the two TEST texts from 5:44 PM and 5:46 PM PT on Nov 5 have none), and `--exclude-request`
can drop anything else. Each alert is rewritten with the PUBLIC template (emoji-free, new format),
keeps its original timestamp (the time the SMS went out), and is marked backfilled=true.
Phone numbers in the logs are never read or printed. Safe to re-run: each alert is claimed once.

  /tmp/fobvenv/bin/python scripts/backfill_alerts.py --since "2026-11-05T17:00:00-08:00" --dry-run
  /tmp/fobvenv/bin/python scripts/backfill_alerts.py --since "2026-11-05T17:00:00-08:00"
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PT = timezone(timedelta(hours=-8))


def load_env():
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def fetch_alerts(session, since_ms, exclude):
    logs = session.client("logs")
    events = []
    for pattern in ('"changes:"', '"sms results:"'):
        kw = {"logGroupName": "/aws/lambda/fob-site-monitor", "startTime": since_ms, "filterPattern": pattern}
        while True:
            r = logs.filter_log_events(**kw)
            events += r["events"]
            if "nextToken" not in r:
                break
            kw["nextToken"] = r["nextToken"]
    by_req = {}
    for e in events:
        parts = e["message"].split("\t")
        if len(parts) < 4:
            continue
        rid, body = parts[2], parts[3]
        slot = by_req.setdefault(rid, {})
        if body.startswith("changes: "):
            slot["changes_ts"], slot["changes"] = e["timestamp"], body[len("changes: "):].strip()
        elif body.startswith("sms results:"):
            slot["sent_ts"] = min(e["timestamp"], slot.get("sent_ts", e["timestamp"]))
    out = []
    for rid, s in by_req.items():
        if rid in exclude or "changes" not in s or "sent_ts" not in s:
            continue
        try:
            changes = json.loads(s["changes"])
        except ValueError:
            # The monitor logs only the first 4000 characters. Keep what is readable.
            changes = [{"raw_excerpt": s["changes"][:3500]}]
        out.append({"request_id": rid, "sent_ts": s["sent_ts"], "changes": changes})
    return sorted(out, key=lambda a: a["sent_ts"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-11-05T17:00:00-08:00")
    ap.add_argument("--campaign", default="fob-twilight-town-mall-2026")
    ap.add_argument("--exclude-request", action="append", default=[])
    ap.add_argument("--profile", default="default")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    load_env()
    os.environ.update({
        "TEMPLATES_TABLE": "mall-rat-alerts-templates", "ALERTS_TABLE": "mall-rat-alerts-alerts",
        "OPS_TABLE": "mall-rat-alerts-ops", "AWS_DEFAULT_REGION": "us-east-1", "AWS_PROFILE": args.profile,
        "MODEL_IDS": os.environ.get("MODEL_IDS", ",".join([
            "us.amazon.nova-2-lite-v1:0", "us.amazon.nova-pro-v1:0", "us.anthropic.claude-sonnet-4-6",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0"])),
    })
    sys.path.insert(0, os.path.join(ROOT, "lambda"))
    import boto3  # noqa: E402
    import alert_composer as ac  # noqa: E402

    session = boto3.Session(profile_name=args.profile, region_name="us-east-1")
    since = datetime.fromisoformat(args.since)
    alerts = fetch_alerts(session, int(since.timestamp() * 1000), set(args.exclude_request))
    print(f"{len(alerts)} real alerts since {args.since}")

    ddb = session.resource("dynamodb")
    t_templates, t_alerts = ddb.Table(ac.TEMPLATES_TABLE), ddb.Table(ac.ALERTS_TABLE)
    tpl = ac.load_template(t_templates, args.campaign)
    print(f"public template: version {tpl.get('version')} ({tpl['source']})")
    bedrock = session.client("bedrock-runtime")

    for a in alerts:
        when = datetime.fromtimestamp(a["sent_ts"] / 1000, PT).strftime("%Y-%m-%d %I:%M:%S %p PT")
        source_id = f"backfill-{a['request_id']}"
        built = ac.build_alert(bedrock, tpl, args.campaign, a["changes"], False,
                               {"source_event_id": source_id, "backfilled": True})
        print(f"\n[{when}] model={built['model']} segments={built['segments']} {built['encoding']}")
        print(built["message"])
        if args.dry_run:
            continue
        if not ac.claim(t_alerts, source_id):
            print("  already backfilled, skipped")
            continue
        fields = {k: v for k, v in {
            **built, "source_event_id": source_id, "status": "published", "backfilled": True,
            "created_at": ac._iso(time.time()),
        }.items() if v is not None and k not in ("trace_id", "attempts")}
        sent_at = ac.put_alert(t_alerts, args.campaign, a["sent_ts"] / 1000.0, fields)
        print(f"  stored at {sent_at}")


if __name__ == "__main__":
    main()
