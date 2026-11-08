#!/usr/bin/env python3
"""View or replace the public alert template and the text-to-join tags (DynamoDB, no redeploy).

Table mall-rat-alerts-templates:
  pk template#public#<campaign_id> / sk current    public alert prompt + settings (read on every alert)
  pk config#tags / sk <TAG>                        keyword -> campaign mapping + welcome message

Public template
  python3 scripts/template.py get                          # settings + prompt
  python3 scripts/template.py get --out prompt.txt         # save the prompt to a file
  python3 scripts/template.py put --file prompt.txt        # replace the prompt (version + 1)
  python3 scripts/template.py put --max-chars 306 --target-chars 160 --temperature 0.2
  python3 scripts/template.py put --model-ids us.amazon.nova-2-lite-v1:0,us.amazon.nova-pro-v1:0
  python3 scripts/template.py put --model-ids ""           # clear the override (use Lambda MODEL_IDS)

Tags (text-to-join)
  python3 scripts/template.py tags                         # list tags
  python3 scripts/template.py tag-get FOB
  python3 scripts/template.py tag-put FOB --welcome-file welcome.txt
  python3 scripts/template.py tag-put FOB --alias "FALL OUT BOY" --alias FALLOUTBOY
  python3 scripts/template.py tag-put NEWTAG --campaign-id some-campaign-2027 --campaign-name "Some Campaign" \\
        --welcome-file welcome.txt                         # the campaign must exist in the ops table
  python3 scripts/template.py tag-put FOB --disable

Add --dry-run to any put to print the new record instead of writing it.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal

import boto3

DEFAULT_CAMPAIGN = "fob-twilight-town-mall-2026"
TAGS_PK = "config#tags"


def jsonable(o):
    if isinstance(o, Decimal):
        return int(o) if o == int(o) else float(o)
    raise TypeError(type(o))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def table(args):
    return boto3.Session(profile_name=args.profile, region_name=args.region).resource("dynamodb").Table(args.table)


def tpl_key(args):
    return {"pk": f"template#public#{args.campaign}", "sk": "current"}


def show(item, out=None):
    prompt = item.get("system_prompt")
    if out and prompt is not None:
        with open(out, "w", encoding="utf-8") as f:
            f.write(prompt + "\n")
        print(f"prompt saved to {out}")
    meta = {k: v for k, v in item.items() if k not in ("system_prompt", "welcome_message")}
    print(json.dumps(meta, indent=2, default=jsonable, sort_keys=True))
    for field in ("system_prompt", "welcome_message"):
        if field in item:
            print(f"\n--- {field} ---\n{item[field]}")


def write(t, item, dry):
    if dry:
        print("DRY RUN, would write:")
        show(item)
        return
    t.put_item(Item=item)
    print(f"written: {item['pk']} / {item['sk']} (version {item.get('version')})")


def cmd_get(args):
    item = table(args).get_item(Key=tpl_key(args), ConsistentRead=True).get("Item")
    if not item:
        sys.exit(f"no template at {tpl_key(args)['pk']}")
    show(item, args.out)


def cmd_put(args):
    t = table(args)
    cur = t.get_item(Key=tpl_key(args), ConsistentRead=True).get("Item") or {}
    new = {**tpl_key(args), **{k: v for k, v in cur.items() if k not in ("pk", "sk")}}
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            text = f.read().strip()
        if not text:
            sys.exit("prompt file is empty")
        new["system_prompt"] = text
    if "system_prompt" not in new:
        sys.exit("no existing prompt; pass --file")
    if args.max_chars is not None:
        new["max_chars"] = args.max_chars
    if args.target_chars is not None:
        new["target_chars"] = args.target_chars
    if args.temperature is not None:
        new["temperature"] = Decimal(str(args.temperature))
    if args.max_tokens is not None:
        new["max_tokens"] = args.max_tokens
    if args.fallback_note is not None:
        new["fallback_note"] = args.fallback_note
    if args.model_ids is not None:
        ids = [m.strip() for m in args.model_ids.split(",") if m.strip()]
        if ids:
            new["model_ids"] = ids
        else:
            new.pop("model_ids", None)
    new["version"] = int(cur.get("version", 0)) + 1
    new["updated_at"] = now_iso()
    new["updated_by"] = "scripts/template.py"
    write(t, new, args.dry_run)


def cmd_tags(args):
    res = table(args).query(KeyConditionExpression="pk = :p", ExpressionAttributeValues={":p": TAGS_PK})
    for i in res["Items"]:
        state = "on " if i.get("enabled", True) else "off"
        print(f"{state} {i['sk']:<12} -> {i.get('campaign_id')}  aliases={list(i.get('aliases', []))}")


def cmd_tag_get(args):
    item = table(args).get_item(Key={"pk": TAGS_PK, "sk": args.tag.upper()}, ConsistentRead=True).get("Item")
    if not item:
        sys.exit(f"no tag {args.tag}")
    show(item)


def cmd_tag_put(args):
    tag = "".join(c for c in args.tag.upper() if c.isalnum())
    if not tag:
        sys.exit("tag must contain letters or digits")
    t = table(args)
    cur = t.get_item(Key={"pk": TAGS_PK, "sk": tag}, ConsistentRead=True).get("Item") or {}
    new = {"pk": TAGS_PK, "sk": tag, "tag": tag, "enabled": True, **{k: v for k, v in cur.items() if k not in ("pk", "sk")}}
    if args.campaign_id:
        new["campaign_id"] = args.campaign_id
    if args.campaign_name:
        new["campaign_name"] = args.campaign_name
    if "campaign_id" not in new:
        sys.exit("new tags need --campaign-id")
    if args.alias:
        new["aliases"] = sorted({tag, *("".join(c for c in a.upper() if c.isalnum()) for a in args.alias)} - {""})
    new.setdefault("aliases", [tag])
    if args.welcome_file:
        with open(args.welcome_file, encoding="utf-8") as f:
            new["welcome_message"] = f.read().strip()
    if "welcome_message" not in new:
        sys.exit("new tags need --welcome-file")
    if args.disable:
        new["enabled"] = False
    if args.enable:
        new["enabled"] = True
    new["version"] = int(cur.get("version", 0)) + 1
    new["updated_at"] = now_iso()
    write(t, new, args.dry_run)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="default")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--table", default="mall-rat-alerts-templates")
    ap.add_argument("--campaign", default=DEFAULT_CAMPAIGN)
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("get")
    g.add_argument("--out")
    g.set_defaults(fn=cmd_get)

    p = sub.add_parser("put")
    p.add_argument("--file")
    p.add_argument("--max-chars", type=int)
    p.add_argument("--target-chars", type=int)
    p.add_argument("--temperature", type=float)
    p.add_argument("--max-tokens", type=int)
    p.add_argument("--fallback-note")
    p.add_argument("--model-ids")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_put)

    sub.add_parser("tags").set_defaults(fn=cmd_tags)

    tg = sub.add_parser("tag-get")
    tg.add_argument("tag")
    tg.set_defaults(fn=cmd_tag_get)

    tp = sub.add_parser("tag-put")
    tp.add_argument("tag")
    tp.add_argument("--campaign-id")
    tp.add_argument("--campaign-name")
    tp.add_argument("--alias", action="append")
    tp.add_argument("--welcome-file")
    tp.add_argument("--enable", action="store_true")
    tp.add_argument("--disable", action="store_true")
    tp.add_argument("--dry-run", action="store_true")
    tp.set_defaults(fn=cmd_tag_put)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
