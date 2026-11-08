"""SMS fan-out rules shared by the composer (enqueue) and the fan-out worker (send).

Ops config (config#global / current), all optional; a missing or invalid value falls back to the safe default:
  fanout_mode                      off (default) | dry_run | allowlist | live
  fanout_allowlist                 list of E.164 numbers; allowlist mode sends only to ACTIVE members in it
  fanout_paused                    kill switch, checked before every send
  fanout_max_recipients_per_alert  refuse the whole run above this (default 200)
  fanout_delay_seconds             SQS DelaySeconds on the job (default 10, clamped 5..900)
  fanout_rate_per_second           segments per second (default 2, must be below the shared 3/s limit)
  fanout_max_alert_age_minutes     never start or resume a run for an older alert (default 120)
  max_public_alerts_per_day        per campaign per UTC day

Twilio can be called only when send_gate() says so: mode allowlist or live, broadcast_enabled exactly true,
and for live also waitlist_mode exactly false.
"""

import re

from common import alert_text
from common import sms_text

MODES = ("off", "dry_run", "allowlist", "live")
SENDING_MODES = ("allowlist", "live")

DEFAULTS = {
    "broadcast_enabled": False,
    "waitlist_mode": True,
    "fanout_mode": "off",
    "fanout_allowlist": [],
    "fanout_paused": False,
    "fanout_max_recipients_per_alert": 200,
    "fanout_delay_seconds": 10,
    "fanout_rate_per_second": 2,
    "fanout_max_alert_age_minutes": 120,
    "max_public_alerts_per_day": 6,
}

SHARED_SEGMENTS_PER_SECOND = 3  # toll-free limit, shared with the private monitor on the same number
MIN_DELAY_SECONDS = 5
MAX_DELAY_SECONDS = 900  # SQS maximum

PREFIX = "Mall Rat Alerts: "
SUFFIX = "\nReply STOP to opt out"
MAX_SEGMENTS = 2
MAX_UNITS = 306  # 2 GSM-7 segments

_E164 = re.compile(r"^\+\d{8,15}$")


def mode_of(cfg):
    mode = str((cfg or {}).get("fanout_mode") or "off").strip().lower()
    return mode if mode in MODES else "off"


def send_gate(cfg):
    """(mode, may_send, reason). may_send is True only when real texts are allowed for this mode."""
    cfg = cfg or {}
    mode = mode_of(cfg)
    if mode not in SENDING_MODES:
        return mode, False, mode
    if cfg.get("broadcast_enabled") is not True:
        return mode, False, "broadcast_disabled"
    if mode == "live" and cfg.get("waitlist_mode") is not False:
        return mode, False, "waitlist_mode"
    return mode, True, None


def paused(cfg):
    return (cfg or {}).get("fanout_paused", False) not in (False, None)


def allowlist(cfg):
    raw = (cfg or {}).get("fanout_allowlist") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    return frozenset(p.strip() for p in raw if isinstance(p, str) and _E164.match(p.strip()))


def int_setting(cfg, key, lo, hi):
    try:
        value = int((cfg or {}).get(key, DEFAULTS[key]))
    except (TypeError, ValueError):
        return DEFAULTS[key]
    return value if lo <= value <= hi else DEFAULTS[key]


def delay_seconds(cfg):
    try:
        value = int((cfg or {}).get("fanout_delay_seconds", DEFAULTS["fanout_delay_seconds"]))
    except (TypeError, ValueError):
        value = DEFAULTS["fanout_delay_seconds"]
    return max(MIN_DELAY_SECONDS, min(MAX_DELAY_SECONDS, value))


def rate_per_second(cfg):
    """Segments per second. Anything not strictly between 0 and the shared limit uses the default."""
    try:
        value = float((cfg or {}).get("fanout_rate_per_second", DEFAULTS["fanout_rate_per_second"]))
    except (TypeError, ValueError):
        return float(DEFAULTS["fanout_rate_per_second"])
    return value if 0 < value < SHARED_SEGMENTS_PER_SECOND else float(DEFAULTS["fanout_rate_per_second"])


def send_interval(segments, rate):
    """Seconds between message starts so the sender stays at `rate` segments per second."""
    return max(1, int(segments)) / float(rate)


def estimate_seconds(recipients, segments, rate):
    return recipients * send_interval(segments, rate)


def build_body(alert):
    """Brand prefix + the composer's stored text + STOP suffix, GSM-7 only, at most 2 segments.
    Raises ValueError if the result is not safe to send."""
    what, note = alert.get("what_changed"), alert.get("note")
    if not what:
        what, _, note = str(alert.get("message") or "").partition(alert_text.SEP)
    what = sms_text.sanitize(str(what or ""), "public")
    if what.lower().startswith(PREFIX.strip().lower()):
        what = what[len(PREFIX.strip()):].lstrip(" :")
    budget = MAX_UNITS - sms_text.units(PREFIX) - sms_text.units(SUFFIX)
    core, _ = alert_text.assemble(what, str(note or ""), "public", budget, budget)
    if not core:
        raise ValueError("empty alert text")
    body = sms_text.sanitize(PREFIX + core + SUFFIX, "public")
    info = sms_text.stats(body)
    if not sms_text.is_gsm7(body) or info["segments"] > MAX_SEGMENTS or "Reply STOP" not in body:
        raise ValueError("alert body is not GSM-7 within 2 segments")
    return body, info
