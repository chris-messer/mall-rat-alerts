"""DynamoDB helpers: config cache, rate limiter, per-campaign subscribers, append-only audit.

Key layout
  subscribers  PK phone, SK campaign_id   (one membership per phone per campaign)
               GSI campaign-index: PK campaign_id, SK phone
  audit        PK phone, SK "<iso ts>#<campaign_id>#<event_type>"
               GSI campaign-index: PK campaign_id, SK sk
  ops          PK pk, SK sk, TTL expires_at
               config#global / current            global kill switches
               config#campaign#<id> / current     campaign metadata and switches
               rl#<scope>#<campaign_id>#<key> / <window>   rate-limit counters
"""

import time
from datetime import datetime, timezone

from botocore.exceptions import ClientError

CONFIG_TTL_SECONDS = 60
CONFIG_SK = "current"

_config_cache = {}


def now_iso(ts=None):
    ts = time.time() if ts is None else ts
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(ts * 1000) % 1000:03d}Z"


def _is_conditional_failure(err):
    return err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


# ---------------------------------------------------------------------- config


def _get_cached(ops_table, pk, now):
    hit_ = _config_cache.get(pk)
    if hit_ and now - hit_[0] < CONFIG_TTL_SECONDS:
        return hit_[1]
    item = ops_table.get_item(Key={"pk": pk, "sk": CONFIG_SK}).get("Item")
    item = {k: v for k, v in (item or {}).items() if k not in ("pk", "sk")} if item else None
    _config_cache[pk] = (now, item)
    return item


def get_global_config(ops_table, defaults, now=None):
    """config#global merged over env defaults. Falls back to defaults if DynamoDB fails."""
    now = time.time() if now is None else now
    cfg = dict(defaults)
    try:
        cfg.update(_get_cached(ops_table, "config#global", now) or {})
    except ClientError:
        pass
    return cfg


def get_campaign(ops_table, campaign_id, now=None):
    """config#campaign#<id>, or None if the campaign isn't registered."""
    now = time.time() if now is None else now
    return _get_cached(ops_table, f"config#campaign#{campaign_id}", now)


def reset_config_cache():
    _config_cache.clear()
    _tag_cache.clear()
    _confirm_cache.clear()


# ------------------------------------------------------------ templates and tags

_tag_cache = {}
TAGS_PK = "config#tags"


def get_tags(templates_table, defaults, now=None):
    """Enabled tag items (config#tags / <TAG>), cached 60s. Falls back to `defaults` if the table is
    unreadable or empty, so keyword join keeps working."""
    now = time.time() if now is None else now
    hit_ = _tag_cache.get("tags")
    if hit_ and now - hit_[0] < CONFIG_TTL_SECONDS:
        return hit_[1]
    try:
        res = templates_table.query(KeyConditionExpression="pk = :p", ExpressionAttributeValues={":p": TAGS_PK})
        tags = [i for i in res.get("Items", []) if i.get("campaign_id") and i.get("enabled", True) is not False]
    except Exception:
        tags = []
    tags = tags or list(defaults)
    _tag_cache["tags"] = (now, tags)
    return tags


_confirm_cache = {}
WEB_CONFIRM_PK = "config#web_confirm"


def get_web_confirm_message(templates_table, campaign_id, now=None):
    """Editable web-signup confirmation text (config#web_confirm / <campaign_id>, field `message`), cached 60s.
    Returns None if absent or unreadable; the caller falls back to the built-in copy."""
    now = time.time() if now is None else now
    hit_ = _confirm_cache.get(campaign_id)
    if hit_ and now - hit_[0] < CONFIG_TTL_SECONDS:
        return hit_[1]
    message = None
    try:
        res = templates_table.query(KeyConditionExpression="pk = :p", ExpressionAttributeValues={":p": WEB_CONFIRM_PK})
        for item in res.get("Items", []):
            if item.get("campaign_id") == campaign_id and item.get("enabled", True) is not False:
                message = (item.get("message") or "").strip() or None
    except Exception:
        message = None
    _confirm_cache[campaign_id] = (now, message)
    return message


def get_template(templates_table, pk):
    """Live template read (no cache). Returns the item dict or None."""
    item = templates_table.get_item(Key={"pk": pk, "sk": CONFIG_SK}).get("Item")
    return item or None


# ------------------------------------------------------------------ rate limits


def window_id(kind, now):
    t = datetime.fromtimestamp(now, timezone.utc)
    if kind == "H":
        return "h#" + t.strftime("%Y%m%d%H"), (int(now) // 3600 + 1) * 3600
    if kind == "D":
        return "d#" + t.strftime("%Y%m%d"), (int(now) // 86400 + 1) * 86400
    raise ValueError(kind)


def hit(ops_table, pk, kind, limit, now=None):
    """Count one request in the current window. Returns False once `limit` is reached.

    One atomic UpdateItem per check: ADD with a condition on the current count.
    The item expires (TTL) one day after its window ends.
    """
    now = time.time() if now is None else now
    sk, window_end = window_id(kind, now)
    try:
        ops_table.update_item(
            Key={"pk": pk, "sk": sk},
            UpdateExpression="ADD cnt :one SET expires_at = if_not_exists(expires_at, :exp)",
            ConditionExpression="attribute_not_exists(cnt) OR cnt < :limit",
            ExpressionAttributeValues={":one": 1, ":limit": limit, ":exp": window_end + 86400},
        )
        return True
    except ClientError as e:
        if _is_conditional_failure(e):
            return False
        raise


def claim(ops_table, pk, seconds, now=None):
    """Rolling-window lock: True if nobody holds `pk` right now, and then holds it for `seconds`.
    Used for "at most one confirmation text per phone per 24 hours"."""
    now = time.time() if now is None else now
    until = int(now) + int(seconds)
    try:
        ops_table.update_item(
            Key={"pk": pk, "sk": "claim"},
            UpdateExpression="SET until_ts = :until, expires_at = :exp",
            ConditionExpression="attribute_not_exists(until_ts) OR until_ts < :now",
            ExpressionAttributeValues={":until": until, ":exp": until + 86400, ":now": int(now)},
        )
        return True
    except ClientError as e:
        if _is_conditional_failure(e):
            return False
        raise


def release_claim(ops_table, pk):
    """Give a claim back (the text was never attempted). Best effort."""
    try:
        ops_table.update_item(
            Key={"pk": pk, "sk": "claim"},
            UpdateExpression="SET until_ts = :zero",
            ExpressionAttributeValues={":zero": 0},
        )
    except ClientError:
        pass


# ----------------------------------------------------------------- subscribers


def get_subscriber(subs_table, phone, campaign_id):
    return subs_table.get_item(Key={"phone": phone, "campaign_id": campaign_id}, ConsistentRead=True).get("Item")


def put_subscriber(subs_table, item):
    subs_table.put_item(Item=item)


def refresh_pending(subs_table, phone, campaign_id, fields):
    """Overwrite the signup snapshot on an existing PENDING_SENDER record (latest consent wins)."""
    names, values, sets = {}, {":zero": 0, ":one": 1}, []
    for i, (k, v) in enumerate(sorted(fields.items())):
        names[f"#f{i}"] = k
        values[f":v{i}"] = v
        sets.append(f"#f{i} = :v{i}")
    sets.append("signup_requests = if_not_exists(signup_requests, :zero) + :one")
    subs_table.update_item(
        Key={"phone": phone, "campaign_id": campaign_id},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def memberships(subs_table, phone):
    """Every campaign membership for one phone (a Query on the partition key, never a Scan)."""
    items, kwargs = [], {}
    while True:
        res = subs_table.query(
            KeyConditionExpression="phone = :p",
            ExpressionAttributeValues={":p": phone},
            ConsistentRead=True,
            **kwargs,
        )
        items.extend(res.get("Items", []))
        if "LastEvaluatedKey" not in res:
            return items
        kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]


def activate(subs_table, phone, campaign_id, from_status, at, message_sid):
    """PENDING/PENDING_SENDER -> ACTIVE after a YES reply. Returns False if the status changed underneath us."""
    try:
        subs_table.update_item(
            Key={"phone": phone, "campaign_id": campaign_id},
            UpdateExpression="SET #s = :active, confirmed_at = :at, confirm_message_sid = :sid, updated_at = :at, "
                             "gsi1pk = :g1, gsi1sk = :g2 REMOVE expires_at",
            ConditionExpression="#s = :from",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":active": "ACTIVE", ":at": at, ":sid": message_sid or "", ":from": from_status,
                ":g1": f"ACTIVE#{campaign_id}", ":g2": f"{at}#{phone}",
            },
        )
        return True
    except ClientError as e:
        if _is_conditional_failure(e):
            return False
        raise


def mark_opted_out(subs_table, phone, campaign_id, at, method):
    """Returns True if the status changed, False if already opted out or never signed up."""
    try:
        subs_table.update_item(
            Key={"phone": phone, "campaign_id": campaign_id},
            UpdateExpression="SET #s = :out, unsubscribed_at = :at, unsubscribe_method = :m, updated_at = :at "
                             "REMOVE expires_at, gsi1pk, gsi1sk",
            ConditionExpression="attribute_exists(phone) AND #s <> :out",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":out": "OPTED_OUT", ":at": at, ":m": method},
        )
        return True
    except ClientError as e:
        if _is_conditional_failure(e):
            return False
        raise


def carrier_opt_out(subs_table, audit_table, phone, at, actor, message_sid=None):
    """Twilio error 21610 (the carrier says this number opted out): OPTED_OUT in every campaign.
    Returns the number of memberships changed."""
    changed = 0
    for m in memberships(subs_table, phone):
        if mark_opted_out(subs_table, phone, m["campaign_id"], at, "CARRIER_21610"):
            audit(audit_table, phone, m["campaign_id"], "carrier_21610", at, actor=actor,
                  message_sid=message_sid, new_status="OPTED_OUT")
            changed += 1
    return changed


# ----------------------------------------------------------------------- audit


def audit(audit_table, phone, campaign_id, event_type, at, **fields):
    item = {
        "phone": phone,
        "sk": f"{at}#{campaign_id}#{event_type}",
        "campaign_id": campaign_id,
        "event_type": event_type,
        "at": at,
    }
    item.update({k: v for k, v in fields.items() if v is not None})
    audit_table.put_item(Item=item)
    return item
