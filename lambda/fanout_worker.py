"""Mall Rat Alerts SMS fan-out worker.

SQS (batch size 1, reserved concurrency 1) -> this Lambda. The composer enqueues one job per stored alert with
DelaySeconds = fanout_delay_seconds, so the private monitor's texts (sent before its EventBridge publish) are
always ahead of the public ones on the shared number.

Job body: {"v": 1, "campaign_id", "sent_at", "seq", "cursor", "mode", "counts", "started_at"}.
Up to 2 workers run at once (reserved concurrency = SQS maximum concurrency = 2, so a job is never throttled
back into the queue). Every send first takes a slot from one shared ops item, so all workers together stay at
fanout_rate_per_second, below the ~3/s the number shares with the private monitor.
seq 0 is the composer's job; later seqs are continuations (cursor = last processed active-index key).

Twilio is called only when every check passes (common/fanout.py send_gate plus the checks here):
  fanout_mode allowlist or live, broadcast_enabled true, live also needs waitlist_mode false, fanout_paused false,
  campaign_status active, alert stored with status published, not backfilled, not a test alert in live mode,
  not older than fanout_max_alert_age_minutes, recipients within fanout_max_recipients_per_alert, and the
  campaign under max_public_alerts_per_day. Config is re-read (consistent read) before every send, so
  flipping any switch stops a run between two texts.

Per (alert, phone) a send row is claimed with a conditional write BEFORE calling Twilio, so a retry, a
continuation or a redriven job never texts the same person twice for one alert (at most once: a crash
between claim and send leaves the row CLAIMED and that person is not retried).

Logs and audit rows carry counts only, never phone numbers.
"""

import json
import logging
import os
import re
import time
from datetime import datetime
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

from common import fanout
from common import store
from common import twilio_send

log = logging.getLogger()
log.setLevel(logging.INFO)

SUBSCRIBERS_TABLE = os.environ.get("SUBSCRIBERS_TABLE", "")
AUDIT_TABLE = os.environ.get("AUDIT_TABLE", "")
OPS_TABLE = os.environ.get("OPS_TABLE", "")
ALERTS_TABLE = os.environ.get("ALERTS_TABLE", "")
SENDS_TABLE = os.environ.get("SENDS_TABLE", "")
QUEUE_URL = os.environ.get("QUEUE_URL", "")
DLQ_URL = os.environ.get("DLQ_URL", "")
ACTIVE_INDEX = os.environ.get("ACTIVE_INDEX", "active-index")
TWILIO_SECRET_ID = os.environ.get("TWILIO_SECRET_ID", "")
TWILIO_SID_KEY = os.environ.get("TWILIO_SID_KEY", "twilio_account_sid")
TWILIO_TOKEN_KEY = os.environ.get("TWILIO_TOKEN_KEY", "twilio_auth_token")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")
TWILIO_WEBHOOK_BASE = os.environ.get("TWILIO_WEBHOOK_BASE", "").rstrip("/")

AUDIT_ACTOR = "system#fanout"
RUN_SK = "#run"
RATE_KEY = {"pk": "fanout#rate", "sk": "slot"}
RATE_SLOT_RETRIES = 50
RESERVE_MS = 45000           # stop and continue with this much Lambda time left
CONTINUE_DELAY_SECONDS = 1
MAX_ATTEMPTS = 3
RETRY_HTTP = (429, 503)      # Twilio did not take the message; anything ambiguous is never retried
SEND_ROW_TTL_DAYS = 90
PAGE_SIZE = 50

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_SENT_AT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")

_tables = {}
_clients = {}
_twilio_creds = {"value": None}
_clock = {"now": time.time, "mono": time.monotonic, "sleep": time.sleep}


class Stop(Exception):
    """End this invocation early: continue (time), park (switch flipped) or nothing to do."""

    def __init__(self, action, reason):
        super().__init__(reason)
        self.action, self.reason = action, reason


def tables():
    if not _tables:
        ddb = boto3.resource("dynamodb")
        _tables.update(subs=ddb.Table(SUBSCRIBERS_TABLE), audit=ddb.Table(AUDIT_TABLE), ops=ddb.Table(OPS_TABLE),
                       alerts=ddb.Table(ALERTS_TABLE), sends=ddb.Table(SENDS_TABLE))
    return _tables


def sqs():
    if "sqs" not in _clients:
        _clients["sqs"] = boto3.client("sqs")
    return _clients["sqs"]


def twilio_credentials():
    """(account_sid, auth_token). Read only when a real send is about to happen. Never logged."""
    if _twilio_creds["value"] is None:
        if not TWILIO_SECRET_ID:
            raise RuntimeError("TWILIO_SECRET_ID not configured")
        raw = json.loads(boto3.client("secretsmanager").get_secret_value(SecretId=TWILIO_SECRET_ID)["SecretString"])
        _twilio_creds["value"] = (raw[TWILIO_SID_KEY], raw[TWILIO_TOKEN_KEY])
    return _twilio_creds["value"]


def _now():
    return _clock["now"]()


def _log(event, **fields):
    log.info(json.dumps({"fanout": event, **fields}, default=str))


def _is_conditional(err):
    return isinstance(err, ClientError) and err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


# ------------------------------------------------------------------- config


def load_config(ops):
    """Fresh, strongly consistent read every time (no cache): the kill switch must take effect immediately."""
    cfg = dict(fanout.DEFAULTS)
    item = ops.get_item(Key={"pk": "config#global", "sk": "current"}, ConsistentRead=True).get("Item") or {}
    cfg.update({k: v for k, v in item.items() if k not in ("pk", "sk")})
    return cfg


def load_campaign(ops, campaign_id):
    return ops.get_item(Key={"pk": f"config#campaign#{campaign_id}", "sk": "current"}, ConsistentRead=True).get("Item")


# --------------------------------------------------------------- recipients


def _gsi_key(item):
    return {k: item[k] for k in ("gsi1pk", "gsi1sk", "phone", "campaign_id")}


def iter_active(subs, campaign_id, cursor=None):
    """ACTIVE memberships for one campaign from the sparse active-index GSI (paginated Query, never a Scan).
    Yields (item, gsi_key) in index order, starting after `cursor`."""
    kwargs = {"ExclusiveStartKey": cursor} if cursor else {}
    while True:
        res = subs.query(IndexName=ACTIVE_INDEX, KeyConditionExpression="gsi1pk = :p",
                         ExpressionAttributeValues={":p": f"ACTIVE#{campaign_id}"}, Limit=PAGE_SIZE, **kwargs)
        for item in res.get("Items", []):
            if item.get("phone") and item.get("gsi1sk"):
                yield item, _gsi_key(item)
        if not res.get("LastEvaluatedKey"):
            return
        kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]


def count_recipients(subs, campaign_id, allow):
    return sum(1 for item, _ in iter_active(subs, campaign_id) if allow is None or item["phone"] in allow)


# --------------------------------------------------------------- send rows


def _ttl():
    return int(_now()) + SEND_ROW_TTL_DAYS * 86400


def claim_send(sends, alert_id, phone, status="CLAIMED"):
    """Conditional create of the (alert, phone) row. False if this person was already handled for this alert."""
    try:
        sends.put_item(Item={"alert_id": alert_id, "phone": phone, "status": status, "claimed_at": store.now_iso(_now()),
                             "expires_at": _ttl()},
                       ConditionExpression="attribute_not_exists(phone)")
        return True
    except ClientError as e:
        if _is_conditional(e):
            return False
        raise


def finish_send(sends, alert_id, phone, status, **fields):
    names, values, sets = {"#s": "status"}, {":s": status, ":at": store.now_iso(_now())}, ["#s = :s", "finished_at = :at"]
    for i, (k, v) in enumerate(sorted((k, v) for k, v in fields.items() if v is not None)):
        names[f"#f{i}"] = k
        values[f":f{i}"] = v
        sets.append(f"#f{i} = :f{i}")
    sends.update_item(Key={"alert_id": alert_id, "phone": phone}, UpdateExpression="SET " + ", ".join(sets),
                      ExpressionAttributeNames=names, ExpressionAttributeValues=values)


def tally(sends, alert_id):
    """Final counters from the send rows (authoritative across retries and continuations)."""
    counts = {"sent": 0, "failed": 0, "skipped": 0, "opted_out": 0, "unknown": 0}
    kwargs = {}
    while True:
        res = sends.query(KeyConditionExpression="alert_id = :a", ExpressionAttributeValues={":a": alert_id},
                          ConsistentRead=True, **kwargs)
        for row in res.get("Items", []):
            st = row.get("status")
            if row.get("phone") == RUN_SK:
                continue
            if st == "SENT":
                counts["sent"] += 1
            elif st == "SKIPPED":
                counts["skipped"] += 1
            elif st == "OPTED_OUT":
                counts["opted_out"] += 1
                counts["failed"] += 1
            elif st in ("UNKNOWN", "CLAIMED"):
                counts["unknown"] += 1
                counts["failed"] += 1
            else:
                counts["failed"] += 1
        if not res.get("LastEvaluatedKey"):
            return counts
        kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]


# --------------------------------------------------------------------- runs


def start_run(sends, alert_id, mode, started_at):
    """Create the run row once per alert. Returns (created, row)."""
    row = {"alert_id": alert_id, "phone": RUN_SK, "status": "starting", "mode": mode, "started_at": started_at,
           "expires_at": _ttl()}
    try:
        sends.put_item(Item=row, ConditionExpression="attribute_not_exists(phone)")
        return True, row
    except ClientError as e:
        if not _is_conditional(e):
            raise
    return False, sends.get_item(Key={"alert_id": alert_id, "phone": RUN_SK}, ConsistentRead=True).get("Item") or {}


def set_run(sends, alert_id, status, **fields):
    try:
        finish_send(sends, alert_id, RUN_SK, status, **fields)
    except Exception:
        log.warning("run row update failed")


def daily_quota(ops, campaign_id, cfg, consume):
    limit = fanout.int_setting(cfg, "max_public_alerts_per_day", 0, 1000)
    pk = f"rl#fanout#{campaign_id}"
    if consume:
        return store.hit(ops, pk, "D", limit, _now())
    sk, _ = store.window_id("D", _now())
    used = (ops.get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=True).get("Item") or {}).get("cnt", 0)
    return int(used) < limit


# ------------------------------------------------------------------ pacing


def reserve_slot(ops, seconds):
    """Shared rate limiter for every worker: atomically take the next send slot of length `seconds` from one
    ops item (compare-and-set on next_at). Returns the slot start (epoch seconds)."""
    for _ in range(RATE_SLOT_RETRIES):
        now = _now()
        item = ops.get_item(Key=RATE_KEY, ConsistentRead=True).get("Item") or {}
        old = item.get("next_at")
        start = max(now, float(old)) if old is not None else now
        new = Decimal(str(round(start + seconds, 3)))
        kwargs = {"ConditionExpression": "attribute_not_exists(next_at)"} if old is None else \
            {"ConditionExpression": "next_at = :old"}
        values = {":n": new, ":exp": int(now) + 86400}
        if old is not None:
            values[":old"] = old
        try:
            ops.update_item(Key=RATE_KEY, UpdateExpression="SET next_at = :n, expires_at = :exp",
                            ExpressionAttributeValues=values, **kwargs)
            return start
        except ClientError as e:
            if not _is_conditional(e):
                raise
    raise RuntimeError("could not reserve a send slot")


class Pacer:
    """Spaces message starts so the sender's total stays at `rate` segments per second across all workers."""

    def __init__(self, rate, ops):
        self.rate, self.ops = rate, ops

    def _wait_for(self, seconds):
        start = reserve_slot(self.ops, seconds)
        delay = start - _now()
        if delay > 0:
            _clock["sleep"](delay)

    def wait(self, segments):
        self._wait_for(fanout.send_interval(segments, self.rate))

    def back_off(self, seconds):
        self._wait_for(seconds)
        _clock["sleep"](seconds)


def status_callback_url():
    # No query string: CloudFront strips it on /api/*, which would break the signature check.
    return f"{TWILIO_WEBHOOK_BASE}/api/twilio/status" if TWILIO_WEBHOOK_BASE else None


def map_sid(sends, sid, alert_id, phone):
    """sid#<MessageSid> -> send row, so the status callback (which only knows the SID) can find it."""
    if not sid:
        return
    try:
        sends.put_item(Item={"alert_id": f"sid#{sid}", "phone": "map", "target_alert_id": alert_id,
                             "target_phone": phone, "expires_at": _ttl()})
    except Exception:
        log.warning("sid map write failed")


def send_one(pacer, segments, phone, body, callback):
    """Bounded retry for 429/503 only (Twilio did not accept the message). Returns (status, sid, error)."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        pacer.wait(segments)
        sid, token = twilio_credentials()
        try:
            res = twilio_send.send_message(sid, token, TWILIO_FROM_NUMBER, phone, body, status_callback=callback)
            return "SENT", res.get("sid"), None
        except twilio_send.TwilioError as e:
            if str(e.code) == "21610":
                return "OPTED_OUT", None, "21610"
            if e.http_status in RETRY_HTTP and attempt < MAX_ATTEMPTS:
                pacer.back_off(2 ** attempt)
                continue
            if e.http_status is None:
                return "UNKNOWN", None, str(e.code)
            return "FAILED", None, f"http={e.http_status} code={e.code}"
    return "FAILED", None, "retries_exhausted"


# ------------------------------------------------------------------- checks


def _age_minutes(alert):
    stamp = alert.get("created_at") or alert.get("sent_at")
    try:
        ts = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("inf")
    return (_now() - ts) / 60.0


def alert_problem(alert, campaign, mode, cfg):
    if not alert:
        return "alert_missing"
    if not campaign or campaign.get("campaign_status") != "active":
        return "campaign_not_active"
    if alert.get("status") != "published":
        return "alert_not_published"
    if alert.get("backfilled"):
        return "backfilled"
    if alert.get("is_test") and mode == "live":
        return "test_alert_in_live_mode"
    if _age_minutes(alert) > fanout.int_setting(cfg, "fanout_max_alert_age_minutes", 1, 1440):
        return "alert_too_old"
    return None


def parse_job(raw):
    job = json.loads(raw) if isinstance(raw, str) else dict(raw)
    cid, sent_at = job.get("campaign_id"), job.get("sent_at")
    if not isinstance(cid, str) or not _SLUG.match(cid) or not isinstance(sent_at, str) or not _SENT_AT.match(sent_at):
        raise ValueError("bad fan-out job")
    job["seq"] = int(job.get("seq") or 0)
    if job.get("cursor") is not None and not isinstance(job["cursor"], dict):
        raise ValueError("bad cursor")
    return job


# ---------------------------------------------------------------- job flow


def _enqueue(url, job, delay):
    sqs().send_message(QueueUrl=url, MessageBody=json.dumps(job), DelaySeconds=int(delay))


def _audit(t, campaign_id, event_type, **fields):
    try:
        store.audit(t["audit"], AUDIT_ACTOR, campaign_id, event_type, store.now_iso(_now()), actor="fanout", **fields)
    except Exception:
        log.warning("audit write failed for %s", event_type)


def _update_alert(t, campaign_id, sent_at, fields):
    names, values, sets = {}, {}, []
    for i, (k, v) in enumerate(sorted(fields.items())):
        names[f"#f{i}"] = k
        values[f":f{i}"] = v
        sets.append(f"#f{i} = :f{i}")
    t["alerts"].update_item(Key={"campaign_id": campaign_id, "sent_at": sent_at},
                            UpdateExpression="SET " + ", ".join(sets), ConditionExpression="attribute_exists(sent_at)",
                            ExpressionAttributeNames=names, ExpressionAttributeValues=values)


def refuse(t, job, mode, reason, **extra):
    _log("refused", mode=mode, reason=reason, campaign_id=job["campaign_id"], seq=job["seq"], **extra)
    _audit(t, job["campaign_id"], "fanout_refused", alert_sent_at=job["sent_at"], mode=mode, reason=reason, **extra)
    if mode in fanout.SENDING_MODES:
        set_run(t["sends"], alert_id_of(job), "refused", reason=reason)
    try:
        _update_alert(t, job["campaign_id"], job["sent_at"],
                      {"fanout_status": "refused", "fanout_refused_reason": reason, "mode": mode})
    except Exception:
        log.warning("could not mark alert refused")
    return {"refused": reason, "mode": mode}


def park(t, job, reason):
    """Switch flipped mid-run (kill switch, mode or broadcast change): put the remaining work in the DLQ.
    Resume deliberately by redriving the DLQ to the queue; already-sent people are never texted again."""
    parked = {**job, "parked_reason": reason}
    _enqueue(DLQ_URL, parked, 0)
    if job.get("mode") in fanout.SENDING_MODES:
        set_run(t["sends"], alert_id_of(job), "parked", reason=reason)
    _log("parked", reason=reason, campaign_id=job["campaign_id"], seq=job["seq"])
    _audit(t, job["campaign_id"], "fanout_parked", alert_sent_at=job["sent_at"], reason=reason, seq=job["seq"],
           counts=job.get("counts"))
    return {"parked": reason}


def alert_id_of(job):
    return f"{job['campaign_id']}#{job['sent_at']}"


def handler(event, context):
    results = []
    for record in (event or {}).get("Records", []):
        results.append(process(parse_job(record["body"]), context))
    return {"results": results}


def process(job, context):
    t = tables()
    cfg = load_config(t["ops"])
    mode, may_send, reason = fanout.send_gate(cfg)
    if job["seq"] > 0 and job.get("mode") != mode:
        return park(t, job, "mode_changed")
    if mode == "off" or (mode != "dry_run" and not may_send):
        if job["seq"] == 0:
            _log("skipped", mode=mode, reason=reason, campaign_id=job["campaign_id"])
            _audit(t, job["campaign_id"], "fanout_skipped", alert_sent_at=job["sent_at"], mode=mode, reason=reason)
            return {"skipped": reason, "mode": mode}
        return park(t, job, reason)
    if fanout.paused(cfg):
        return park(t, job, "paused")

    return run(t, job, cfg, mode, context)


def run(t, job, cfg, mode, context):
    cid, sent_at, alert_id = job["campaign_id"], job["sent_at"], alert_id_of(job)
    dry = mode == "dry_run"
    alert = t["alerts"].get_item(Key={"campaign_id": cid, "sent_at": sent_at}, ConsistentRead=True).get("Item")
    problem = alert_problem(alert, load_campaign(t["ops"], cid), mode, cfg)
    if problem:
        return refuse(t, job, mode, problem)
    try:
        body, info = fanout.build_body(alert)
    except ValueError:
        return refuse(t, job, mode, "bad_body")
    segments = int(info["segments"])
    rate = fanout.rate_per_second(cfg)
    allow = fanout.allowlist(cfg) if mode == "allowlist" else None

    if job["seq"] == 0:
        recipients = count_recipients(t["subs"], cid, allow)
        cap = fanout.int_setting(cfg, "fanout_max_recipients_per_alert", 0, 100000)
        if recipients > cap:
            return refuse(t, job, mode, "recipient_cap", recipients=recipients, cap=cap)
        started_at = store.now_iso(_now())
        if dry:
            quota_ok = daily_quota(t["ops"], cid, cfg, consume=False)
        else:
            created, row = start_run(t["sends"], alert_id, mode, started_at)
            if not created and row.get("status") in ("completed", "refused"):
                return {"duplicate": row.get("status")}
            if not created and row.get("mode") != mode:
                return park(t, {**job, "mode": row.get("mode")}, "mode_changed")
            started_at = row.get("started_at") or started_at
            quota_ok = True if row.get("status") in ("running", "parked") else daily_quota(t["ops"], cid, cfg, True)
        if not quota_ok and not dry:
            return refuse(t, job, mode, "daily_cap")
        if not dry:
            set_run(t["sends"], alert_id, "running", recipients=recipients, segments=segments)
        job = {**job, "mode": mode, "started_at": started_at, "cursor": None,
               "counts": {"recipients": recipients, "would_send": 0, "would_skip": 0, "daily_cap_ok": quota_ok}}
        est = round(fanout.estimate_seconds(recipients, segments, rate), 1)
        _log("started", mode=mode, campaign_id=cid, recipients=recipients, segments=segments, rate=rate,
             estimated_seconds=est, daily_cap_ok=quota_ok)
        _audit(t, cid, "fanout_started", alert_sent_at=sent_at, mode=mode, recipients=recipients, segments=segments,
               estimated_seconds=str(est))
    elif not dry:
        row = t["sends"].get_item(Key={"alert_id": alert_id, "phone": RUN_SK}, ConsistentRead=True).get("Item") or {}
        if row.get("status") in ("completed", "refused"):
            return {"duplicate": row.get("status")}
        if row.get("mode") != mode:
            return park(t, job, "mode_changed")
        if row.get("status") == "parked":
            set_run(t["sends"], alert_id, "running")

    counts = dict(job.get("counts") or {})
    pacer = Pacer(rate, t["ops"])
    callback = None if dry else status_callback_url()
    last_key = job.get("cursor")
    try:
        for item, key in iter_active(t["subs"], cid, job.get("cursor")):
            phone = item["phone"]
            if allow is not None and phone not in allow:
                last_key = key
                continue
            if context is not None and context.get_remaining_time_in_millis() < RESERVE_MS:
                raise Stop("continue", "time")
            cfg_now = load_config(t["ops"])
            if fanout.paused(cfg_now):
                raise Stop("park", "paused")
            mode_now, may_now, why = fanout.send_gate(cfg_now)
            if mode_now != mode or (not dry and not may_now):
                raise Stop("park", why if mode_now == mode else "mode_changed")
            if mode == "allowlist" and phone not in fanout.allowlist(cfg_now):
                last_key = key
                continue

            sub = store.get_subscriber(t["subs"], phone, cid)
            active = bool(sub) and sub.get("status") == "ACTIVE" and sub.get("gsi1pk") == f"ACTIVE#{cid}"
            if dry:
                counts["would_send" if active else "would_skip"] += 1
                last_key = key
                continue
            if not active:
                claim_send(t["sends"], alert_id, phone, status="SKIPPED")
                last_key = key
                continue
            if not claim_send(t["sends"], alert_id, phone):
                last_key = key
                continue
            status, sid, error = send_one(pacer, segments, phone, body, callback)
            map_sid(t["sends"], sid, alert_id, phone)
            finish_send(t["sends"], alert_id, phone, status, message_sid=sid, error=error, segments=segments)
            if status == "OPTED_OUT":
                store.carrier_opt_out(t["subs"], t["audit"], phone, store.now_iso(_now()), "fanout")
            last_key = key
    except Stop as stop:
        nxt = {**job, "seq": job["seq"] + 1, "cursor": last_key, "counts": counts}
        if stop.action == "continue":
            _enqueue(QUEUE_URL, nxt, CONTINUE_DELAY_SECONDS)
            _log("continued", campaign_id=cid, seq=nxt["seq"], counts=counts if dry else None)
            _audit(t, cid, "fanout_continued", alert_sent_at=sent_at, mode=mode, seq=nxt["seq"])
            return {"continued": nxt["seq"]}
        return park(t, nxt, stop.reason)

    return finish(t, job, mode, counts)


def finish(t, job, mode, counts):
    cid, sent_at, alert_id = job["campaign_id"], job["sent_at"], alert_id_of(job)
    finished_at = store.now_iso(_now())
    if mode == "dry_run":
        dry_counts = {"recipients": counts.get("recipients", 0), "would_send": counts.get("would_send", 0),
                      "would_skip": counts.get("would_skip", 0), "daily_cap_ok": bool(counts.get("daily_cap_ok"))}
        _update_alert(t, cid, sent_at, {"dry_run_counts": dry_counts, "mode": mode,
                                        "fanout_started_at": job.get("started_at"), "fanout_finished_at": finished_at})
        log.info("fanout dry_run: would send %d (skip %d, recipients %d)", dry_counts["would_send"],
                 dry_counts["would_skip"], dry_counts["recipients"])
        _audit(t, cid, "fanout_dry_run", alert_sent_at=sent_at, mode=mode, **dry_counts)
        return {"dry_run": dry_counts}

    final = tally(t["sends"], alert_id)
    fields = {"recipients": int(counts.get("recipients", 0)), "sent": final["sent"], "failed": final["failed"],
              "skipped": final["skipped"], "mode": mode, "fanout_started_at": job.get("started_at"),
              "fanout_finished_at": finished_at, "fanout_status": "completed"}
    if final["sent"] > 0:
        fields["status"] = "sent"
    _update_alert(t, cid, sent_at, fields)
    set_run(t["sends"], alert_id, "completed", sent=final["sent"], failed=final["failed"], skipped=final["skipped"])
    _log("completed", mode=mode, campaign_id=cid, **{k: v for k, v in fields.items() if k in
                                                       ("recipients", "sent", "failed", "skipped")},
         opted_out=final["opted_out"], unknown=final["unknown"])
    _audit(t, cid, "fanout_completed", alert_sent_at=sent_at, mode=mode, recipients=fields["recipients"],
           sent=final["sent"], failed=final["failed"], skipped=final["skipped"], opted_out=final["opted_out"])
    return {"completed": fields}
