"""Pre-computed subscriber counts per campaign, so "how many subs" never needs a table scan.

One item per campaign in the ops table:
  pk = "counts#<campaign_id>"   sk = "current"
  sms_active, sms_opted_out   number   kept current by atomic ADD on real status transitions
  discord_members             number   synced value (Discord joins never reach our Lambdas)
  discord_presence            number   synced value (approximate online count)
  discord_synced_at           ISO str  when the Discord numbers were last fetched
  updated_at                  ISO str  last write of any kind
  reconciled_at               ISO str  last absolute recompute (scripts/refresh_counts.py)

The SMS counters mirror the number of subscriber rows with status ACTIVE / OPTED_OUT. Counter updates are
best effort: a failure is logged and swallowed so signup and STOP handling never break, and the next
reconcile run (full scan, absolute SET) heals any drift. The ADD only applies if the counter item already
exists, so a deploy that lands before the first seed can never create a half-populated item.
"""

import logging
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

COUNTS_SK = "current"
INT_FIELDS = ("sms_active", "sms_opted_out", "discord_members", "discord_presence")


def counts_pk(campaign_id):
    return f"counts#{campaign_id}"


def counts_key(campaign_id):
    return {"pk": counts_pk(campaign_id), "sk": COUNTS_SK}


def _now_iso(now=None):
    ts = time.time() if now is None else now
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(ts * 1000) % 1000:03d}Z"


def deltas(prior_status, new_status):
    """(sms_active_delta, sms_opted_out_delta) for one subscriber row going prior -> new status.
    prior_status None means the row did not exist. Identical statuses change nothing."""
    active = int(new_status == "ACTIVE") - int(prior_status == "ACTIVE")
    opted_out = int(new_status == "OPTED_OUT") - int(prior_status == "OPTED_OUT")
    return active, opted_out


def get_counts(ops_table, campaign_id):
    """The counter item as a plain dict (numbers as int), or None if it does not exist or cannot be read."""
    try:
        item = ops_table.get_item(Key=counts_key(campaign_id)).get("Item")
    except Exception as e:
        log.warning("counts read failed: %s", type(e).__name__)
        return None
    if not item:
        return None
    out = {k: v for k, v in item.items() if k not in ("pk", "sk")}
    for k in INT_FIELDS:
        if k in out:
            out[k] = int(out[k])
    return out


def increment_sms(ops_table, campaign_id, active=0, opted_out=0, now=None):
    """Atomically add the deltas to the counter. Never raises. Returns True if the counter was updated."""
    if not active and not opted_out:
        return False
    try:
        ops_table.update_item(
            Key=counts_key(campaign_id),
            UpdateExpression="ADD sms_active :a, sms_opted_out :o SET updated_at = :at",
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeValues={":a": int(active), ":o": int(opted_out), ":at": _now_iso(now)},
        )
        return True
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)
        log.warning("counts update skipped (%s) campaign=%s active=%+d opted_out=%+d", code, campaign_id,
                    int(active), int(opted_out))
        return False


def record_transition(ops_table, campaign_id, prior_status, new_status, now=None):
    """Apply the counter change for one real subscriber status transition. Never raises."""
    if ops_table is None:
        return False
    try:
        active, opted_out = deltas(prior_status, new_status)
        return increment_sms(ops_table, campaign_id, active, opted_out, now)
    except Exception as e:  # defensive: the caller's signup/STOP flow must continue
        log.warning("counts transition failed: %s", type(e).__name__)
        return False


# --------------------------------------------------------------------- Discord
# Primary path: discord_count_sync Lambda pulls GET /guilds/{id}?with_counts=true on a schedule and SETs
# discord_members / discord_presence here. SMS counters stay event-driven and are never touched by that pull.
# Legacy: scripts/discord_member_bot.py can still push join/leave/sync to /api/discord/member-event (kept but
# unused in production). scripts/refresh_counts.py remains the absolute SMS+Discord reconcile path.


def _conditional_failure(err):
    return getattr(err, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def discord_adjust(ops_table, campaign_id, delta, now=None):
    """ADD +1 / -1 to discord_members. The counter must already be seeded, and a decrement never goes
    below 0. Returns "ok", "floor" (already 0, nothing changed) or "not_seeded". Other errors propagate so
    the caller can ask the bot to retry."""
    at = _now_iso(now)
    values = {":d": int(delta), ":at": at}
    cond = "attribute_exists(discord_members)"
    if delta < 0:
        cond += " AND discord_members > :zero"
        values[":zero"] = 0
    try:
        ops_table.update_item(
            Key=counts_key(campaign_id),
            UpdateExpression="ADD discord_members :d SET updated_at = :at",
            ConditionExpression=cond,
            ExpressionAttributeValues=values,
        )
        return "ok"
    except Exception as e:
        if not _conditional_failure(e):
            raise
    item = ops_table.get_item(Key=counts_key(campaign_id)).get("Item")
    return "floor" if item and "discord_members" in item else "not_seeded"


def discord_sync(ops_table, campaign_id, members, presence=None, now=None):
    """Absolute SET of Discord counts (scheduled pull or legacy bot sync). Returns "ok" or "not_seeded";
    other errors propagate. Does not touch sms_active / sms_opted_out."""
    at = _now_iso(now)
    values = {":m": max(0, int(members)), ":at": at}
    sets = "discord_members = :m, discord_synced_at = :at, updated_at = :at"
    if presence is not None:
        values[":p"] = max(0, int(presence))
        sets = "discord_members = :m, discord_presence = :p, discord_synced_at = :at, updated_at = :at"
    try:
        ops_table.update_item(
            Key=counts_key(campaign_id),
            UpdateExpression="SET " + sets,
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeValues=values,
        )
        return "ok"
    except Exception as e:
        if _conditional_failure(e):
            return "not_seeded"
        raise
