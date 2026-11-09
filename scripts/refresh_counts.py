#!/usr/bin/env python3
"""Recompute the per-campaign counter item (seed and reconcile). Absolute SET, safe to re-run.

  python3 scripts/refresh_counts.py                 # recompute and write
  python3 scripts/refresh_counts.py --dry-run       # print what would be written
  python3 scripts/refresh_counts.py --skip-discord  # SMS only

Writes ops item  pk=counts#<campaign_id>  sk=current  with
  sms_active, sms_opted_out        full scan of the subscribers table (status counts for the campaign)
  discord_members, discord_presence, discord_synced_at
                                   GET /guilds/{id}?with_counts=true using DISCORD_BOT_TOKEN from .env
                                   (loaded in-process, never printed). Skipped if Discord is unreachable.
  updated_at, reconciled_at

Needs boto3 (profile "default", us-east-1 unless overridden). No SMS, no Discord posts, read-only on
subscribers. Only prints aggregate numbers.
"""

import argparse
import collections
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lambda"))

from common import counts  # noqa: E402

DEFAULT_CAMPAIGN = "fob-twilight-town-mall-2026"
GUILD_NAME = "MallRatAlerts"


def load_env(path):
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def scan_sms(table, campaign_id):
    tally, kwargs = collections.Counter(), {}
    while True:
        res = table.scan(ProjectionExpression="campaign_id, #s", ExpressionAttributeNames={"#s": "status"},
                         ConsistentRead=True, **kwargs)
        for item in res["Items"]:
            if item.get("campaign_id") == campaign_id:
                tally[item.get("status")] += 1
        if "LastEvaluatedKey" not in res:
            return tally
        kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]


def discord_api(token, path):
    req = urllib.request.Request(
        "https://discord.com/api/v10" + path,
        headers={"Authorization": "Bot " + token, "User-Agent": "DiscordBot (mall-rat-alerts, 1.0)"})
    with urllib.request.urlopen(req, timeout=20) as f:
        return json.load(f)


def discord_counts(env):
    token = os.environ.get("DISCORD_BOT_TOKEN") or env.get("DISCORD_BOT_TOKEN")
    if not token:
        return None, "DISCORD_BOT_TOKEN missing"
    try:
        guild_id = os.environ.get("DISCORD_GUILD_ID") or env.get("DISCORD_GUILD_ID")
        if not guild_id:
            guilds = discord_api(token, "/users/@me/guilds")
            named = [g for g in guilds if g.get("name") == GUILD_NAME] or (guilds if len(guilds) == 1 else [])
            if not named:
                return None, "guild not found"
            guild_id = named[0]["id"]
        g = discord_api(token, f"/guilds/{guild_id}?with_counts=true")
        return {"members": int(g["approximate_member_count"]),
                "presence": int(g.get("approximate_presence_count") or 0)}, None
    except urllib.error.HTTPError as e:
        return None, f"discord http {e.code}"
    except Exception as e:
        return None, f"discord {type(e).__name__}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", default=DEFAULT_CAMPAIGN)
    ap.add_argument("--profile", default="default")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--name", default="mall-rat-alerts", help="table name prefix")
    ap.add_argument("--skip-discord", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import boto3
    ddb = boto3.Session(profile_name=args.profile, region_name=args.region).resource("dynamodb")
    subs, ops = ddb.Table(f"{args.name}-subscribers"), ddb.Table(f"{args.name}-ops")

    tally = scan_sms(subs, args.campaign)
    at = counts._now_iso()
    values = {":a": tally["ACTIVE"], ":o": tally["OPTED_OUT"], ":at": at}
    sets = ["sms_active = :a", "sms_opted_out = :o", "updated_at = :at", "reconciled_at = :at"]
    out = {"sms_active": tally["ACTIVE"], "sms_opted_out": tally["OPTED_OUT"],
           "other_statuses": {k: v for k, v in tally.items() if k not in ("ACTIVE", "OPTED_OUT")}}

    if not args.skip_discord:
        d, err = discord_counts(load_env(ROOT / ".env"))
        if d:
            values.update({":m": d["members"], ":p": d["presence"]})
            sets += ["discord_members = :m", "discord_presence = :p", "discord_synced_at = :at"]
            out.update(discord_members=d["members"], discord_presence=d["presence"])
        else:
            out["discord_skipped"] = err

    if not args.dry_run:
        ops.update_item(Key=counts.counts_key(args.campaign), UpdateExpression="SET " + ", ".join(sets),
                        ExpressionAttributeValues=values)
    out["written"] = not args.dry_run
    out["key"] = counts.counts_pk(args.campaign)
    print(json.dumps(out, sort_keys=True))


if __name__ == "__main__":
    main()
