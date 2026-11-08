"""Mall Rat Alerts alert composer.

EventBridge (default bus) -> this Lambda, for events {source: "fob-site-monitor", detail-type: "SiteChange"}.

  1. Idempotency marker keyed on the source event id (a duplicate delivery does nothing).
  2. Fetch the public template live from DynamoDB (falls back to the copy shipped with the code).
  3. Ask Bedrock (Nova first) for {"notify", "what_changed", "note"}, tracing every attempt to PostHog.
  4. Assemble "what_changed\\n\\nnote" in code, sanitize to GSM-7, enforce the length budget.
  5. Store the alert in the alerts table (the homepage feed reads it).
  6. Unless ops fanout_mode is "off", enqueue one fan-out job (SQS, DelaySeconds = fanout_delay_seconds).
  7. Mirror real published alerts to the campaign's Discord channel (common/discord.py), after step 6.

No SMS is sent from here. The fan-out worker (fanout_worker.py) re-checks every switch before each text.
"""

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3

from common import alert_text
from common import discord
from common import fanout
from common import llm_trace
from common import sms_text
from common import store

log = logging.getLogger()
log.setLevel(logging.INFO)

TEMPLATES_TABLE = os.environ.get("TEMPLATES_TABLE", "")
ALERTS_TABLE = os.environ.get("ALERTS_TABLE", "")
OPS_TABLE = os.environ.get("OPS_TABLE", "")
MODEL_IDS = [m.strip() for m in os.environ.get("MODEL_IDS", "").split(",") if m.strip()]
POSTHOG_KEY = os.environ.get("POSTHOG_KEY", "")
POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")
FANOUT_QUEUE_URL = os.environ.get("FANOUT_QUEUE_URL", "")
DEFAULT_CAMPAIGN_ID = os.environ.get("DEFAULT_CAMPAIGN_ID", "fob-twilight-town-mall-2026")
APP = "mall-rat-alerts"
DISTINCT_ID = "mall-rat-alerts-composer"
MARKER_TTL_DAYS = 30

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "max_chars": 306,       # hard cap: 2 GSM-7 segments
    "target_chars": 160,    # aim for 1 segment by trimming the note first
    "temperature": 0.2,
    "max_tokens": 500,
    "version": 0,
    "fallback_note": "Thnks fr th mmrs.",
    "visit_what": "The Visit tab on falloutboy.com is now clickable. Go take a look: falloutboy.com",
    "visit_note": "Light 'em up.",
}

_tables = {}
_bedrock = {}
_sqs = {}


def tables():
    if not _tables:
        ddb = boto3.resource("dynamodb")
        _tables["templates"] = ddb.Table(TEMPLATES_TABLE)
        _tables["alerts"] = ddb.Table(ALERTS_TABLE)
        _tables["ops"] = ddb.Table(OPS_TABLE)
    return _tables


def bedrock_client():
    if "c" not in _bedrock:
        from botocore.config import Config
        _bedrock["c"] = boto3.client("bedrock-runtime", config=Config(
            read_timeout=40, connect_timeout=5, retries={"max_attempts": 1}))
    return _bedrock["c"]


def template_key(campaign_id):
    return f"template#public#{campaign_id}"


def default_prompt():
    with open(os.path.join(_HERE, "common", "default_public_prompt.txt"), encoding="utf-8") as f:
        return f.read().strip()


def load_template(templates_table, campaign_id):
    """Live read on every invocation. Never raises: a bad or missing record falls back to the default."""
    tpl = {**DEFAULTS, "system_prompt": default_prompt(), "source": "default"}
    try:
        item = store.get_template(templates_table, template_key(campaign_id)) if templates_table else None
    except Exception as e:
        log.warning("template fetch failed, using default: %s", type(e).__name__)
        return tpl
    if not item or not isinstance(item.get("system_prompt"), str) or not item["system_prompt"].strip():
        return tpl
    tpl.update({k: _plain(v) for k, v in item.items() if k not in ("pk", "sk") and v not in (None, "")})
    tpl["source"] = "dynamodb"
    return tpl


def _plain(v):
    """DynamoDB returns numbers as Decimal; templates must be JSON-serializable and Converse-friendly."""
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (list, tuple, set)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


def model_order(tpl):
    raw = tpl.get("model_ids")
    if isinstance(raw, str):
        raw = [m.strip() for m in raw.split(",")]
    ids = [m for m in (raw or []) if isinstance(m, str) and m.strip()]
    return ids or MODEL_IDS


def _urls(changes):
    out = []
    for c in changes or []:
        for u in [c.get("url")] + [p.get("url") for p in c.get("new_linked_pages", []) if isinstance(p, dict)]:
            if isinstance(u, str) and u not in out:
                out.append(u)
    return out[:10]


def fallback_parts(changes, tpl):
    """Deterministic text when every model fails or returns invalid JSON."""
    paths = [re.sub(r"^https?://(www\.)?", "", u).rstrip("/") for u in _urls(changes)]
    paths = [p for p in paths if p][:2]
    what = ("Something changed on " + " and ".join(paths) + ".") if paths else "Something changed on falloutboy.com."
    return what, str(tpl.get("fallback_note") or DEFAULTS["fallback_note"])


def user_text_for(changes):
    return "Changes detected:\n" + json.dumps(changes, indent=1)[:24000]


def build_alert(bedrock, template, campaign_id, changes, priority=False, trace_props=None):
    """Compose one public alert. Returns the fields that go on the stored alert (no keys, no status)."""
    tpl = {**DEFAULTS, **template}
    max_chars, target = int(tpl["max_chars"]), int(tpl["target_chars"])
    trace = llm_trace.Trace(
        POSTHOG_KEY, POSTHOG_HOST, APP, DISTINCT_ID, "public_alert",
        {"campaign_id": campaign_id, "template_version": tpl.get("version"),
         "template_key": template_key(campaign_id), "template_source": tpl.get("source"), **(trace_props or {})})
    model, model_notify, fallback, attempts, fallback_reason = None, None, False, 0, None
    if priority:
        what, note, model = str(tpl["visit_what"]), str(tpl["visit_note"]), "fixed:visit-tab"
    else:
        # The monitor already decided this change was worth alerting on, so the public feed always gets an
        # entry: a model notify=false, invalid output or a compose crash all fall back to the template.
        try:
            res = alert_text.compose(bedrock, trace, tpl, user_text_for(changes), model_order(tpl), "public",
                                     {"campaign_id": campaign_id, "template_version": tpl.get("version"), "app": APP})
        except Exception as e:
            log.exception("compose raised, using fallback template")
            res, fallback_reason = None, f"compose error: {type(e).__name__}"
        if res and res["notify"] is not False:
            what, note, model = res["what_changed"], res["note"], res["model"]
            model_notify, attempts = res["notify"], res["attempts"]
        else:
            if res:
                model_notify, attempts = False, res["attempts"]
                fallback_reason = f"model {res['model']} returned notify=false"
            else:
                fallback_reason = fallback_reason or "all models failed or returned invalid output"
            log.warning(json.dumps({"alert": "fallback", "reason": fallback_reason, "campaign_id": campaign_id}))
            what, note = fallback_parts(changes, tpl)
            model, fallback = "fallback:template", True
    message, info = alert_text.assemble(what, note, "public", max_chars, target)
    what_out, _, note_out = message.partition(alert_text.SEP)
    trace.finish(input_state={"changes": len(changes or []), "urls": _urls(changes), "priority": bool(priority)},
                 output_state={"message": message, **info, "model": model, "fallback": fallback},
                 error=f"{fallback_reason}, used fallback template" if fallback else None,
                 final_model=model, segments=info["segments"], encoding=info["encoding"])
    log.info(json.dumps({"alert": "composed", "model": model, "segments": info["segments"],
                         "encoding": info["encoding"], "chars": info["chars"], "fallback": fallback,
                         "template_version": tpl.get("version")}))
    return {
        "message": message, "what_changed": what_out, "note": note_out, "urls": _urls(changes),
        "segments": info["segments"], "encoding": info["encoding"], "units": info["units"],
        "template_version": tpl.get("version"), "model": model, "model_notify": model_notify,
        "fallback": fallback, "fallback_reason": fallback_reason, "trace_id": trace.trace_id, "attempts": attempts,
    }


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(ts * 1000) % 1000:03d}Z"


def _parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def put_alert(alerts_table, campaign_id, sent_at_ts, fields):
    """Store under (campaign_id, sent_at). A clash on the same millisecond moves forward 1 ms."""
    for i in range(5):
        sent_at = _iso(sent_at_ts + i / 1000.0)
        try:
            alerts_table.put_item(Item={"campaign_id": campaign_id, "sent_at": sent_at, **fields},
                                  ConditionExpression="attribute_not_exists(sent_at)")
            return sent_at
        except Exception as e:
            if getattr(e, "response", {}).get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
    raise RuntimeError("could not find a free sent_at slot")


def claim(alerts_table, source_event_id):
    """True the first time this source event id is seen. Marker rows live in the alerts table under a
    `src#<id>` partition, so they never appear in a campaign's feed."""
    try:
        alerts_table.put_item(
            Item={"campaign_id": f"src#{source_event_id}", "sent_at": "marker", "created_at": _iso(time.time()),
                  "expires_at": int(time.time()) + MARKER_TTL_DAYS * 86400},
            ConditionExpression="attribute_not_exists(campaign_id)")
        return True
    except Exception as e:
        if getattr(e, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def release(alerts_table, source_event_id):
    try:
        alerts_table.delete_item(Key={"campaign_id": f"src#{source_event_id}", "sent_at": "marker"})
    except Exception:
        log.warning("could not release marker for %s", source_event_id)


def sqs_client():
    if "c" not in _sqs:
        _sqs["c"] = boto3.client("sqs")
    return _sqs["c"]


def fanout_hook(alert, cfg=None):
    """Enqueue one fan-out job for a stored alert, delayed so the private texts always go first.
    Mode "off" (the default, and what a missing config means) enqueues nothing. Never raises: the alert is
    already stored and the feed must not depend on SMS."""
    mode = fanout.mode_of(cfg)
    if mode == "off" or not alert.get("campaign_id") or not alert.get("sent_at"):
        return None
    if not FANOUT_QUEUE_URL:
        log.warning(json.dumps({"fanout": "enqueue_skipped", "reason": "no_queue", "mode": mode}))
        return None
    delay = fanout.delay_seconds(cfg)
    job = {"v": 1, "campaign_id": alert["campaign_id"], "sent_at": alert["sent_at"], "seq": 0,
           "source_event_id": alert.get("source_event_id"), "is_test": bool(alert.get("is_test"))}
    try:
        res = sqs_client().send_message(QueueUrl=FANOUT_QUEUE_URL, MessageBody=json.dumps(job), DelaySeconds=delay)
    except Exception as e:
        log.error(json.dumps({"fanout": "enqueue_failed", "error": type(e).__name__, "mode": mode}))
        return None
    log.info(json.dumps({"fanout": "enqueued", "mode": mode, "delay_seconds": delay,
                         "campaign_id": alert["campaign_id"], "sent_at": alert["sent_at"]}))
    return res.get("MessageId") or "enqueued"


def discord_hook(alerts_table, fields, campaign_id, sent_at, cfg, is_test, event_dry_run=False):
    """Mirror a real, published alert to the campaign's Discord channel. Runs after the alert is stored and the
    fan-out job is enqueued. Skips test events and dry runs (event flag or ops fanout_mode dry_run). Never raises."""
    try:
        if is_test or event_dry_run or fanout.mode_of(cfg) == "dry_run" or fields.get("status") != "published":
            return None
        return discord.post_public_alert(campaign_id, fields.get("message"), sent_at, alerts_table)
    except Exception as e:
        log.error(json.dumps({"discord": "hook_failed", "error": type(e).__name__}))
        return None


def handler(event, context):
    event = event or {}
    detail = event.get("detail") or {}
    source_event_id = str(event.get("id") or detail.get("event_id") or uuid.uuid4())
    campaign_id = detail.get("campaign_id") or DEFAULT_CAMPAIGN_ID
    if not isinstance(campaign_id, str) or not _SLUG.match(campaign_id):
        raise ValueError("bad campaign_id")
    changes = detail.get("changes") or []
    is_test = bool(detail.get("test")) or str(event.get("source", "")).endswith(".test")
    t = tables()

    if not claim(t["alerts"], source_event_id):
        log.info("duplicate source event %s, skipping", source_event_id)
        return {"duplicate": True, "source_event_id": source_event_id}
    try:
        tpl = load_template(t["templates"], campaign_id)
        built = build_alert(bedrock_client(), tpl, campaign_id, changes, bool(detail.get("priority")),
                            {"source_event_id": source_event_id, "is_test": is_test or None})
        fields = {
            **{k: v for k, v in built.items() if v is not None and k not in ("trace_id", "attempts")},
            "source_event_id": source_event_id,
            "status": "published",
            "backfilled": False,
            "detected_at": detail.get("detected_at"),
            "created_at": _iso(time.time()),
        }
        if is_test:
            fields["is_test"] = True
        fields = {k: v for k, v in fields.items() if v is not None}
        sent_at = put_alert(t["alerts"], campaign_id, _parse_time(event.get("time") or detail.get("detected_at")), fields)
    except Exception:
        release(t["alerts"], source_event_id)
        log.exception("compose failed for %s", source_event_id)
        raise
    try:
        cfg = store.get_global_config(t["ops"], dict(fanout.DEFAULTS))
    except Exception as e:
        log.error(json.dumps({"fanout": "config_unreadable", "error": type(e).__name__}))
        cfg = None
    fanout_hook({**fields, "campaign_id": campaign_id, "sent_at": sent_at}, cfg)
    discord_hook(t["alerts"], fields, campaign_id, sent_at, cfg, is_test, bool(detail.get("dry_run")))
    return {"stored": True, "campaign_id": campaign_id, "sent_at": sent_at, "segments": built["segments"],
            "encoding": built["encoding"], "model": built["model"], "test": is_test}
