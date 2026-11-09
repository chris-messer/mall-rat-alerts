"""Mall Rat Alerts signup API (Phase 1: waitlist mode).

POST /api/subscribe       store a PENDING_SENDER membership (phone + campaign) with a consent snapshot,
                          then send ONE "Reply YES to confirm" text (double opt-in) behind strict limits.
                          Nothing is active until the person replies YES from that phone.
POST /api/unsubscribe     mark the phone OPTED_OUT for one campaign.
POST /api/twilio/inbound  Twilio SMS webhook (signed): YES / STOP / HELP / START router, TwiML replies only.
POST /api/twilio/status   Twilio status callback (signed): 21610 means the carrier says opted out.
POST /api/discord/member-event  Legacy unused route: Discord join / leave / sync from the optional local gateway
                          bot (HMAC in the body). Production Discord counts come from the scheduled
                          discord_count_sync Lambda pull instead. Kept so Terraform routes stay stable.

Subscribe and unsubscribe accept JSON (fetch from app.js) or a plain HTML form
post (works without JavaScript). Responses are the same whether or not a number
is already known, so the API can't be used to check who signed up.
"""

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import time
import urllib.parse

import boto3

from common import analytics
from common import counts
from common import keywords
from common import phone as phones
from common import sms_text
from common import store
from common import tags as tagmod
from common import turnstile
from common import twilio_send
from common import twilio_sig

log = logging.getLogger()
log.setLevel(logging.INFO)


def _env_bool(name, default):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return int(default)


def _env_list(name, default=""):
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


SUBSCRIBERS_TABLE = os.environ.get("SUBSCRIBERS_TABLE", "")
AUDIT_TABLE = os.environ.get("AUDIT_TABLE", "")
OPS_TABLE = os.environ.get("OPS_TABLE", "")
TEMPLATES_TABLE = os.environ.get("TEMPLATES_TABLE", "")
ALERTS_TABLE = os.environ.get("ALERTS_TABLE", "")
FANOUT_SENDS_TABLE = os.environ.get("FANOUT_SENDS_TABLE", "")
KEYWORD_CONSENT_SHA256 = os.environ.get("KEYWORD_CONSENT_SHA256", "")
IP_HASH_SALT = os.environ.get("IP_HASH_SALT", "")
ORIGIN_VERIFY_SECRET = os.environ.get("ORIGIN_VERIFY_SECRET", "")
CONSENT_VERSION = os.environ.get("CONSENT_VERSION", "")
CONSENT_SHA256 = os.environ.get("CONSENT_SHA256", "")
DEFAULT_CAMPAIGN_ID = os.environ.get("DEFAULT_CAMPAIGN_ID", "fob-twilight-town-mall-2026")
ALLOWED_COUNTRIES = set(_env_list("ALLOWED_COUNTRIES", "US"))
SITE_HOSTNAMES = _env_list("SITE_HOSTNAMES")
TEST_NUMBERS = frozenset(_env_list("TEST_NUMBERS"))
TURNSTILE_ENABLED = _env_bool("TURNSTILE_ENABLED", "false")
TURNSTILE_SECRET_ID = os.environ.get("TURNSTILE_SECRET_ID", "")
PENDING_TTL_DAYS = _env_int("PENDING_TTL_DAYS", "30")
TWILIO_SECRET_ID = os.environ.get("TWILIO_SECRET_ID", "")
TWILIO_TOKEN_KEY = os.environ.get("TWILIO_TOKEN_KEY", "twilio_auth_token")
TWILIO_SID_KEY = os.environ.get("TWILIO_SID_KEY", "twilio_account_sid")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")
TWILIO_WEBHOOK_BASE = os.environ.get("TWILIO_WEBHOOK_BASE", "").rstrip("/")
PUBLIC_SITE_URL = os.environ.get("PUBLIC_SITE_URL", "")
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")
DISCORD_MEMBER_SECRET_KEY = os.environ.get("DISCORD_MEMBER_SECRET_KEY", "discord_member_webhook_secret")
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "hello@mallratalerts.com")
MIN_FILL_SECONDS = 2
MAX_BODY_BYTES = 4096

LIMITS = {
    "ip_subscribe_hour": _env_int("RL_IP_SUBSCRIBE_HOUR", "3"),
    "ip_subscribe_day": _env_int("RL_IP_SUBSCRIBE_DAY", "20"),
    "ip_unsubscribe_day": _env_int("RL_IP_UNSUBSCRIBE_DAY", "10"),
    "phone_subscribe_day": _env_int("RL_PHONE_SUBSCRIBE_DAY", "3"),
    "global_subscribe_hour": _env_int("RL_GLOBAL_SUBSCRIBE_HOUR", "300"),
}

GLOBAL_DEFAULTS = {
    "signups_enabled": _env_bool("SIGNUPS_ENABLED", "true"),
    "waitlist_mode": _env_bool("WAITLIST_MODE", "true"),
    "broadcast_enabled": _env_bool("BROADCAST_ENABLED", "false"),
    # Web double opt-in confirmation text (ops config#global overrides these live).
    "web_confirm_enabled": _env_bool("WEB_CONFIRM_ENABLED", "true"),
    "web_confirm_daily_cap": _env_int("WEB_CONFIRM_DAILY_CAP", "50"),
    "web_confirm_dry_run": _env_bool("WEB_CONFIRM_DRY_RUN", "false"),
}

WEB_CONFIRM_MAX_SEGMENTS = 2
WEB_CONFIRM_DEFAULT = (
    "Mall Rat Alerts: Reply YES to confirm Twilight Town Mall (Fall Out Boy site) alerts. "
    "Msg freq varies. Msg&data rates may apply. Reply STOP to cancel, HELP for help. Unofficial fan project."
)

DISCLAIMER = (
    "Mall Rat Alerts is an unofficial fan project. It is not affiliated with, endorsed by, or "
    "sponsored by Fall Out Boy, their management, their record labels, or any artist or website we watch."
)

MESSAGES = {
    "check_your_phone": "Thanks! If that number can get texts from us, we just sent one. Reply YES to it to "
                        "confirm. You only get alerts after you reply YES.",
    "unsubscribed": "If that number was signed up for this campaign, it is now unsubscribed and will not "
                    "get texts from us about it.",
    "invalid_request": "Something was wrong with that request. Please go back and try again.",
    "invalid_phone": "That does not look like a US mobile number. Please check it and try again.",
    "consent_required": "Please tick the box agreeing to receive texts.",
    "age_required": "Please confirm you are 18 or older.",
    "stale_consent_version": "This form is out of date. Please reload the page and try again.",
    "unknown_campaign": "We could not find that alert campaign. Please reload the page and try again.",
    "campaign_closed": "Signups for this campaign are closed.",
    "captcha_failed": "We could not verify you are human. Please reload the page and try again.",
    "region_not_supported": "Sorry, signups are only available in the United States.",
    "forbidden": "Forbidden.",
    "rate_limited": "Too many attempts. Please try again later.",
    "signups_paused": "Signups are paused right now. Please try again later.",
    "not_found": "Not found.",
    "internal_error": "Something went wrong on our end. Please try again later.",
}

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_FORM_VERSION = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_TRUTHY = (True, "true", "yes", "on", "1")
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")

_tables = {}
_turnstile_secret = {"value": None}
_twilio_token = {"value": None}
_twilio_creds = {"value": None}
_member_secret = {"value": None}
_TOLL_FREE = re.compile(r"^\+18(00|33|44|55|66|77|88)\d{7}$")
_E164 = re.compile(r"^\+\d{8,15}$")
PENDING_STATUSES = ("PENDING_SENDER", "PENDING")
FINAL_STATUSES = ("delivered", "undelivered", "failed")


def tables():
    if not _tables:
        ddb = boto3.resource("dynamodb")
        _tables["subs"] = ddb.Table(SUBSCRIBERS_TABLE)
        _tables["audit"] = ddb.Table(AUDIT_TABLE)
        _tables["ops"] = ddb.Table(OPS_TABLE)
        if TEMPLATES_TABLE:
            _tables["templates"] = ddb.Table(TEMPLATES_TABLE)
        if ALERTS_TABLE:
            _tables["alerts"] = ddb.Table(ALERTS_TABLE)
        if FANOUT_SENDS_TABLE:
            _tables["sends"] = ddb.Table(FANOUT_SENDS_TABLE)
    return _tables


def turnstile_secret():
    if _turnstile_secret["value"] is None:
        sm = boto3.client("secretsmanager")
        raw = sm.get_secret_value(SecretId=TURNSTILE_SECRET_ID)["SecretString"]
        _turnstile_secret["value"] = json.loads(raw)["turnstile_secret"]
    return _turnstile_secret["value"]


def twilio_token():
    """Auth token for signature checks. Raises if no secret is configured, so webhooks fail closed."""
    if _twilio_token["value"] is None:
        if not TWILIO_SECRET_ID:
            raise RuntimeError("TWILIO_SECRET_ID not configured")
        sm = boto3.client("secretsmanager")
        raw = sm.get_secret_value(SecretId=TWILIO_SECRET_ID)["SecretString"]
        _twilio_token["value"] = json.loads(raw)[TWILIO_TOKEN_KEY]
    return _twilio_token["value"]


def member_event_secret():
    """Shared secret for the Discord member-event push (same Secrets Manager secret as Twilio). Fail closed."""
    if _member_secret["value"] is None:
        if not TWILIO_SECRET_ID:
            raise RuntimeError("secret id not configured")
        sm = boto3.client("secretsmanager")
        raw = json.loads(sm.get_secret_value(SecretId=TWILIO_SECRET_ID)["SecretString"])
        _member_secret["value"] = raw[DISCORD_MEMBER_SECRET_KEY]
    return _member_secret["value"]


def twilio_credentials():
    """(account_sid, auth_token) from the same secret as the webhook token. Never logged."""
    if _twilio_creds["value"] is None:
        if not TWILIO_SECRET_ID:
            raise RuntimeError("TWILIO_SECRET_ID not configured")
        sm = boto3.client("secretsmanager")
        raw = json.loads(sm.get_secret_value(SecretId=TWILIO_SECRET_ID)["SecretString"])
        _twilio_creds["value"] = (raw[TWILIO_SID_KEY], raw[TWILIO_TOKEN_KEY])
    return _twilio_creds["value"]


def _send_sms(to, body):
    """One outbound text from the campaign number. Tests replace this function; nothing else sends."""
    if not TWILIO_FROM_NUMBER:
        raise RuntimeError("TWILIO_FROM_NUMBER not configured")
    sid, token = twilio_credentials()
    callback = TWILIO_WEBHOOK_BASE + "/api/twilio/status" if TWILIO_WEBHOOK_BASE else None
    return twilio_send.send_message(sid, token, TWILIO_FROM_NUMBER, to, body, status_callback=callback)


def _track(event, phone, **props):
    analytics.capture(event, "srv_" + keyed_hash(phone or "")[:16], {k: v for k, v in props.items() if v is not None})


# -------------------------------------------------------------------- request


def _headers(event):
    return {k.lower(): v for k, v in (event.get("headers") or {}).items()}


def _parse_body(event, headers):
    """Returns (payload dict or None, is_form)."""
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except Exception:
            return None, False
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        return None, False
    ctype = headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype == "application/x-www-form-urlencoded":
        return {k: v[-1] for k, v in urllib.parse.parse_qs(body, keep_blank_values=True).items()}, True
    try:
        data = json.loads(body) if body else None
    except ValueError:
        return None, False
    return (data if isinstance(data, dict) else None), False


def client_ip(event, headers):
    addr = headers.get("cloudfront-viewer-address", "")
    if addr:
        return addr.rsplit(":", 1)[0].strip("[]")
    xff = headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return ((event.get("requestContext") or {}).get("http") or {}).get("sourceIp", "")


def keyed_hash(value):
    return hmac.new(IP_HASH_SALT.encode(), (value or "").encode(), hashlib.sha256).hexdigest()[:32]


def _clean(value, limit):
    if not isinstance(value, str):
        return None
    value = re.sub(r"[\x00-\x1f\x7f]", "", value).strip()[:limit]
    return value or None


def attribution(payload, headers):
    """UTM params, referrer, and landing path from the form, falling back to the Referer header."""
    referer = _clean(headers.get("referer"), 300)
    ref_qs = {}
    ref_path = None
    if referer:
        parts = urllib.parse.urlsplit(referer)
        ref_qs = {k: v[-1] for k, v in urllib.parse.parse_qs(parts.query).items()}
        ref_path = parts.path or "/"
    out = {k: _clean(payload.get(k) or ref_qs.get(k), 100) for k in UTM_KEYS}
    out["referrer"] = _clean(payload.get("referrer"), 300) or None
    out["landing_path"] = _clean(payload.get("landing_path"), 200) or ref_path
    out["page_url"] = referer
    return {k: v for k, v in out.items() if v}


def campaign_id_of(payload):
    cid = payload.get("campaign_id")
    if cid in (None, ""):
        return DEFAULT_CAMPAIGN_ID
    return cid if isinstance(cid, str) and _SLUG.match(cid) else None


# ------------------------------------------------------------------- response


def html_page(title, message):
    t, m = html.escape(title), html.escape(message)
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{t} - Mall Rat Alerts</title><link rel=\"stylesheet\" href=\"/assets/style.css\"></head>"
        "<body><header class=\"site-header\"><a class=\"brand\" href=\"/\"><span class=\"token\" aria-hidden=\"true\">MR</span>"
        "<span class=\"brand-name\">Mall Rat Alerts</span></a></header>"
        f"<main class=\"wrap\"><section class=\"card\"><h1>{t}</h1><p>{m}</p><p><a href=\"/\">Back to the home page</a></p></section></main>"
        f"<footer class=\"footer\"><p class=\"disclaimer\">{html.escape(DISCLAIMER)}</p>"
        "<nav><a href=\"/privacy/\">Privacy</a> <a href=\"/terms/\">Terms</a> "
        "<a href=\"/sms-terms/\">SMS Terms</a> <a href=\"/unsubscribe/\">Unsubscribe</a></nav></footer>"
        "</body></html>"
    )


def respond(status, code, is_form, success_redirect=None):
    """code is a MESSAGES key, returned as `status` for 2xx and `error` otherwise."""
    if is_form and status < 300 and success_redirect:
        return {"statusCode": 303, "headers": {"Location": success_redirect, "Cache-Control": "no-store"}, "body": ""}
    message = MESSAGES[code]
    if is_form:
        title = "You're all set" if status < 300 else "Hmm, that didn't work"
        return {
            "statusCode": status,
            "headers": {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"},
            "body": html_page(title, message),
        }
    body = {"status": code} if status < 300 else {"error": code}
    body["message"] = message
    if code == "rate_limited":
        body["retry_after"] = 3600
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body),
    }


def _log(route, result, phone=None, **extra):
    log.info(json.dumps({"route": route, "result": result, "phone": phones.mask(phone) if phone else None, **extra}))


# ------------------------------------------------------------------- checks


def _edge_checks(headers, check_country=True):
    """Requests must come through CloudFront (secret origin header). Public forms are also US-only."""
    if ORIGIN_VERIFY_SECRET and not hmac.compare_digest(headers.get("x-origin-verify", ""), ORIGIN_VERIFY_SECRET):
        return 403, "forbidden"
    if check_country and ALLOWED_COUNTRIES and \
            headers.get("cloudfront-viewer-country", "").upper() not in ALLOWED_COUNTRIES:
        return 403, "region_not_supported"
    return None


def _turnstile_ok(payload, ip, action):
    if not TURNSTILE_ENABLED:
        return True
    token = payload.get("turnstile_token") or payload.get("cf-turnstile-response")
    try:
        secret = turnstile_secret()
    except Exception as e:
        log.error("turnstile secret unavailable: %s", type(e).__name__)
        return False
    return turnstile.verify(token, secret, ip, action, SITE_HOSTNAMES)


def _too_fast(payload, now):
    raw = payload.get("form_rendered_at")
    if raw in (None, ""):
        return False
    try:
        rendered = float(raw)
    except (TypeError, ValueError):
        return True
    if rendered > 1e11:
        rendered /= 1000.0
    return 0 <= now - rendered < MIN_FILL_SECONDS


# ------------------------------------------------------------------ handlers


def subscribe(event, headers, payload, is_form, now):
    route = "subscribe"
    ok = lambda: respond(202, "check_your_phone", is_form, "/thanks/")  # noqa: E731

    if payload.get("website"):
        _log(route, "honeypot")
        return ok()
    if _too_fast(payload, now):
        _log(route, "too_fast")
        return ok()

    ip = client_ip(event, headers)
    if not _turnstile_ok(payload, ip, "subscribe"):
        return respond(403, "captcha_failed", is_form)

    phone = phones.normalize_us(payload.get("phone"), TEST_NUMBERS)
    if not phone:
        return respond(400, "invalid_phone", is_form)
    if payload.get("consent") not in _TRUTHY:
        return respond(400, "consent_required", is_form)
    if payload.get("age_18_plus") not in _TRUTHY:
        return respond(400, "age_required", is_form)
    if payload.get("consent_version") != CONSENT_VERSION:
        return respond(400, "stale_consent_version", is_form)
    campaign_id = campaign_id_of(payload)
    if not campaign_id:
        return respond(400, "unknown_campaign", is_form)
    form_version = payload.get("form_version") or ""
    if not isinstance(form_version, str) or not _FORM_VERSION.match(form_version):
        form_version = "unknown"

    t = tables()
    cfg = store.get_global_config(t["ops"], GLOBAL_DEFAULTS, now)
    if not cfg.get("signups_enabled", True):
        return respond(503, "signups_paused", is_form)
    campaign = store.get_campaign(t["ops"], campaign_id, now)
    if not campaign:
        return respond(400, "unknown_campaign", is_form)
    if campaign.get("campaign_status") != "active" or not campaign.get("signups_enabled", True):
        return respond(503, "campaign_closed", is_form)

    ip_hash = keyed_hash(ip)
    ua_hash = keyed_hash(headers.get("user-agent", ""))
    rl_ip = f"rl#ip#{campaign_id}#{ip_hash}#sub"
    if not (store.hit(t["ops"], rl_ip, "H", LIMITS["ip_subscribe_hour"], now)
            and store.hit(t["ops"], rl_ip, "D", LIMITS["ip_subscribe_day"], now)):
        _log(route, "ip_limited", campaign_id=campaign_id)
        return respond(429, "rate_limited", is_form)
    if not store.hit(t["ops"], f"rl#global#{campaign_id}#sub", "H", LIMITS["global_subscribe_hour"], now):
        _log(route, "global_limited", campaign_id=campaign_id)
        return respond(503, "signups_paused", is_form)
    if not store.hit(t["ops"], f"rl#phone#{campaign_id}#{phone}#sub", "D", LIMITS["phone_subscribe_day"], now):
        _log(route, "phone_limited", phone, campaign_id=campaign_id)
        return ok()

    at = store.now_iso(now)
    snapshot = {
        "campaign_name": campaign.get("campaign_name"),
        "campaign_tags": list(campaign.get("campaign_tags") or []),
        "source_site": campaign.get("source_site"),
        "consent_version": CONSENT_VERSION,
        "consent_sha256": CONSENT_SHA256,
        "form_version": form_version,
        "ip_hash": ip_hash,
        "ua_hash": ua_hash,
        "source": "web",
        **attribution(payload, headers),
    }
    consent = {
        "consent_checkbox": True,
        "age_18_plus": True,
        "consent_captured_at": at,
        "turnstile_ok": TURNSTILE_ENABLED,
    }
    waitlist = bool(cfg.get("waitlist_mode", True))
    audit_base = {"actor": "user", "waitlist_mode": waitlist, **snapshot}

    existing = store.get_subscriber(t["subs"], phone, campaign_id)
    prior = (existing or {}).get("status")
    expires_at = int(now) + PENDING_TTL_DAYS * 86400
    send_ok = False  # may we text a confirmation for this record?
    suppress = None  # reason a confirmation is not allowed

    if prior == "OPTED_OUT":
        # Carrier rules: someone who texted STOP must opt back in by texting START or FOB themselves.
        # The web form changes nothing and sends nothing.
        store.audit(t["audit"], phone, campaign_id, "subscribe_duplicate", at, prior_status=prior,
                    note="opted_out_requires_sms_opt_in", **audit_base)
        result, suppress = "opted_out_ignored", "opted_out"
    elif prior == "PENDING_SENDER":
        store.refresh_pending(t["subs"], phone, campaign_id,
                              {**snapshot, **consent, "updated_at": at, "expires_at": expires_at})
        store.audit(t["audit"], phone, campaign_id, "consent_recorded", at, prior_status=prior, **consent, **audit_base)
        result, send_ok = "duplicate_refreshed", True
    elif prior in ("ACTIVE", "PENDING", "BLOCKED", "INVALID"):
        store.audit(t["audit"], phone, campaign_id, "subscribe_duplicate", at, prior_status=prior, **audit_base)
        result, suppress = "duplicate_ignored", ("already_active" if prior == "ACTIVE" else prior.lower())
    else:
        item = {
            "phone": phone,
            "campaign_id": campaign_id,
            "status": "PENDING_SENDER",
            "tier": "PUBLIC",
            "created_at": at,
            "updated_at": at,
            "signup_requests": 1,
            "is_test": phone in TEST_NUMBERS,
            "expires_at": expires_at,
            **snapshot,
            **consent,
        }
        store.put_subscriber(t["subs"], item)
        store.audit(t["audit"], phone, campaign_id, "consent_recorded", at, prior_status=prior, **consent, **audit_base)
        store.audit(t["audit"], phone, campaign_id, "subscribe_pending", at, prior_status=prior,
                    new_status="PENDING_SENDER", **audit_base)
        result, send_ok = "new", True

    store.audit(t["audit"], phone, campaign_id, "signup_web", at, prior_status=prior, result=result, **audit_base)
    if send_ok:
        _web_confirm(phone, campaign_id, cfg, t, now, at, audit_base)
    else:
        store.audit(t["audit"], phone, campaign_id, "confirm_suppressed", at, reason=suppress, **audit_base)

    _log(route, result, phone, campaign_id=campaign_id)
    _track("server_subscribe", phone, campaign_id=campaign_id, result=result, form_version=form_version,
           utm_source=snapshot.get("utm_source"))
    return ok()


def _confirm_copy(templates_table, campaign_id, now):
    """Editable copy from DynamoDB if it is safe (GSM-7 after sanitizing, within 2 segments, mentions STOP),
    otherwise the built-in text. Returns (text, source)."""
    custom = store.get_web_confirm_message(templates_table, campaign_id, now) if templates_table else None
    if custom:
        text = sms_text.sanitize(custom, "public")
        if text and "STOP" in text.upper() and sms_text.stats(text)["segments"] <= WEB_CONFIRM_MAX_SEGMENTS:
            return text, "dynamodb"
    return sms_text.sanitize(WEB_CONFIRM_DEFAULT, "public"), "builtin"


def _web_confirm(phone, campaign_id, cfg, t, now, at, audit_base):
    """Send at most one "Reply YES" text for a stored web signup. Never raises, never tells the caller anything.

    Guards, in order: kill switch, test numbers, one per phone per rolling 24h, global daily cap, dry-run."""
    base = {k: v for k, v in audit_base.items() if k in ("actor", "ip_hash", "ua_hash", "consent_version")}

    def suppressed(reason):
        store.audit(t["audit"], phone, campaign_id, "confirm_suppressed", at, reason=reason, **base)
        _log("subscribe", "confirm_suppressed", phone, campaign_id=campaign_id, reason=reason)
        return False

    try:
        if not cfg.get("web_confirm_enabled", True):
            return suppressed("kill_switch")
        if phone in TEST_NUMBERS:
            return suppressed("test_number")
        claim_key = f"rl#confirm#phone#{phone}"
        if not store.claim(t["ops"], claim_key, 86400, now):
            return suppressed("phone_24h_limit")
        try:
            cap = int(cfg.get("web_confirm_daily_cap", 50))
        except (TypeError, ValueError):
            cap = 50
        if not store.hit(t["ops"], "rl#global#confirm#send", "D", cap, now):
            store.release_claim(t["ops"], claim_key)
            return suppressed("daily_cap")
        text, source = _confirm_copy(t.get("templates"), campaign_id, now)
        info = sms_text.stats(text)
        dry_run = bool(cfg.get("web_confirm_dry_run", False))
        sid = None
        if not dry_run:
            sid = (_send_sms(phone, text) or {}).get("sid")
        store.audit(t["audit"], phone, campaign_id, "confirm_sent", at, message_sid=sid, dry_run=dry_run,
                    segments=info["segments"], encoding=info["encoding"], copy_source=source, **base)
        _log("subscribe", "confirm_dry_run" if dry_run else "confirm_sent", phone, campaign_id=campaign_id,
             segments=info["segments"], encoding=info["encoding"])
        _track("server_confirm_sent", phone, campaign_id=campaign_id, dry_run=dry_run, segments=info["segments"])
        return True
    except twilio_send.TwilioError as e:
        store.audit(t["audit"], phone, campaign_id, "confirm_failed", at, error_code=str(e.code or ""),
                    http_status=e.http_status, **base)
        _log("subscribe", "confirm_failed", phone, campaign_id=campaign_id, error_code=str(e.code or ""))
        return False
    except Exception as e:
        log.error("web confirm failed: %s", type(e).__name__)
        try:
            store.audit(t["audit"], phone, campaign_id, "confirm_failed", at, error_code=type(e).__name__, **base)
        except Exception:
            pass
        return False


def unsubscribe(event, headers, payload, is_form, now):
    route = "unsubscribe"
    done = lambda: respond(202, "unsubscribed", is_form, "/unsubscribe/done/")  # noqa: E731
    if payload.get("website"):
        _log(route, "honeypot")
        return done()

    ip = client_ip(event, headers)
    if not _turnstile_ok(payload, ip, "unsubscribe"):
        return respond(403, "captcha_failed", is_form)

    phone = phones.normalize_us(payload.get("phone"), TEST_NUMBERS)
    if not phone:
        return respond(400, "invalid_phone", is_form)
    campaign_id = campaign_id_of(payload)
    if not campaign_id:
        return respond(400, "unknown_campaign", is_form)

    t = tables()
    if not store.get_campaign(t["ops"], campaign_id, now):
        return respond(400, "unknown_campaign", is_form)
    ip_hash = keyed_hash(ip)
    if not store.hit(t["ops"], f"rl#ip#{campaign_id}#{ip_hash}#unsub", "D", LIMITS["ip_unsubscribe_day"], now):
        _log(route, "ip_limited", campaign_id=campaign_id)
        return respond(429, "rate_limited", is_form)

    at = store.now_iso(now)
    changed = store.mark_opted_out(t["subs"], phone, campaign_id, at, "WEB", ops_table=t["ops"])
    store.audit(
        t["audit"], phone, campaign_id, "unsubscribe" if changed else "unsubscribe_noop", at,
        actor="user", method="WEB", ip_hash=ip_hash, ua_hash=keyed_hash(headers.get("user-agent", "")),
        new_status="OPTED_OUT" if changed else None,
    )
    _log(route, "opted_out" if changed else "noop", phone, campaign_id=campaign_id)
    _track("server_unsubscribe", phone, campaign_id=campaign_id, result="opted_out" if changed else "noop")
    return done()


# ------------------------------------------------------------ Twilio webhooks


def twiml(message=None):
    """TwiML reply. Every body goes through the GSM-7 sanitizer, so edited copy can't force UCS-2."""
    message = sms_text.sanitize(message, "public") if message else None
    if message:
        info = sms_text.stats(message)
        log.info(json.dumps({"route": "twiml", "segments": info["segments"], "encoding": info["encoding"],
                             "chars": info["chars"]}))
    inner = f"<Message>{html.escape(message, quote=False)}</Message>" if message else ""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/xml; charset=utf-8", "Cache-Control": "no-store"},
        "body": f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner}</Response>',
    }


def _pick_campaign(mems):
    """Campaign an inbound message applies to: the most recently updated pending membership, else the
    most recently updated membership of any status, else the default campaign."""
    by_recent = sorted(mems, key=lambda m: m.get("updated_at", ""), reverse=True)
    pending = [m for m in by_recent if m.get("status") in PENDING_STATUSES]
    if pending:
        return pending[0]
    return by_recent[0] if by_recent else None


def _sms_subscribe(frm, body, sid, matched, t, cfg, now, at, autoreply_ok):
    """Text-to-join. The person texted the tag, so that text is the opt-in: membership becomes ACTIVE.
    Returns (reply_text_or_None, result_label)."""
    waitlist = bool(cfg.get("waitlist_mode", True))
    joined, dups = [], []
    for tag in matched:
        cid = tag["campaign_id"]
        campaign = store.get_campaign(t["ops"], cid, now)
        if not campaign or campaign.get("campaign_status") != "active" or not campaign.get("signups_enabled", True):
            continue
        existing = store.get_subscriber(t["subs"], frm, cid)
        prior = (existing or {}).get("status")
        name = campaign.get("campaign_name") or tag.get("campaign_name") or tag["tag"]
        base = {"actor": "user", "source": "sms", "tag": tag["tag"], "keyword": "TAG", "message_sid": sid,
                "consent_version": tagmod.CONSENT_VERSION, "consent_sha256": KEYWORD_CONSENT_SHA256 or None,
                "consent_captured_at": at, "campaign_name": name, "body": body[:60]}
        if prior == "ACTIVE":
            store.audit(t["audit"], frm, cid, "sms_tag_already_active", at, prior_status=prior, **base)
            dups.append(name)
            continue
        if prior in ("BLOCKED", "INVALID"):
            store.audit(t["audit"], frm, cid, "sms_tag_blocked", at, prior_status=prior, **base)
            continue
        snapshot = {
            "campaign_name": name,
            "campaign_tags": list(campaign.get("campaign_tags") or []),
            "source_site": campaign.get("source_site"),
        }
        if prior in PENDING_STATUSES:
            if not store.activate(t["subs"], frm, cid, prior, at, sid, ops_table=t["ops"]):
                continue
            store.refresh_pending(t["subs"], frm, cid, {
                **snapshot, "sms_tag": tag["tag"], "sms_consent_version": tagmod.CONSENT_VERSION,
                "sms_consent_captured_at": at, "activation_source": "sms_tag", "updated_at": at})
            result = "promoted"
        else:
            item = {
                "phone": frm, "campaign_id": cid, "status": "ACTIVE", "tier": "PUBLIC", "source": "sms",
                "created_at": at, "updated_at": at, "confirmed_at": at, "confirm_message_sid": sid,
                "gsi1pk": f"ACTIVE#{cid}", "gsi1sk": f"{at}#{frm}", "signup_requests": 1,
                "is_test": frm in TEST_NUMBERS, "sms_tag": tag["tag"],
                "consent_version": tagmod.CONSENT_VERSION, "consent_sha256": KEYWORD_CONSENT_SHA256 or "",
                "consent_captured_at": at, "form_version": "sms-keyword",
                **snapshot,
            }
            if prior == "OPTED_OUT":
                item["previous_unsubscribed_at"] = existing.get("unsubscribed_at")
            store.put_subscriber(t["subs"], item, ops_table=t["ops"])
            result = "resubscribed" if prior else "new"
        store.audit(t["audit"], frm, cid, "subscribe_sms_tag", at, prior_status=prior, new_status="ACTIVE",
                    waitlist_mode=waitlist, **base)
        _track("sms_subscribe", frm, tag=tag["tag"], campaign_id=cid, source="sms", result=result)
        joined.append((tag, name))

    if joined:
        reply = None
        if autoreply_ok("welcome", 5):
            if len(joined) == 1:
                reply = keywords.tag_welcome(joined[0][0], waitlist)
            else:
                reply = keywords.multi_welcome([n for _, n in joined], waitlist)
        return reply, "tag_" + str(len(joined))
    if dups:
        reply = keywords.already_active(" and ".join(dups)) if autoreply_ok("tagdup", 1) else None
        return reply, "tag_already_active"
    return None, "tag_unavailable"


def inbound(params, now):
    frm = params.get("From", "")
    body = params.get("Body", "")
    sid = params.get("MessageSid", "")
    already_replied = bool(params.get("OptOutType"))
    if not _E164.match(frm) or _TOLL_FREE.match(frm):
        _log("inbound", "ignored_sender")
        return twiml()

    kw = keywords.classify(body)
    t = tables()
    at = store.now_iso(now)
    mems = store.memberships(t["subs"], frm)
    target = _pick_campaign(mems)
    cid = (target or {}).get("campaign_id") or DEFAULT_CAMPAIGN_ID
    cfg = store.get_global_config(t["ops"], GLOBAL_DEFAULTS, now)
    base = {"actor": "twilio", "message_sid": sid, "keyword": kw}
    if kw != "OTHER":
        base["body"] = body[:160]
    else:
        base["body_length"] = len(body)

    def autoreply_ok(kind, limit):
        return store.hit(t["ops"], f"rl#reply#{kind}#{frm}", "D", limit, now)

    reply, result = None, kw.lower()
    tag_list = store.get_tags(t["templates"], tagmod.DEFAULT_TAGS, now) if t.get("templates") else tagmod.DEFAULT_TAGS
    matched = tagmod.match(body, tag_list) if kw == "OTHER" else []
    if matched:
        reply, result = _sms_subscribe(frm, body, sid, matched, t, cfg, now, at, autoreply_ok)
        _log("inbound", result, frm, campaign_id=matched[0]["campaign_id"], replied=bool(reply))
        return twiml(reply)
    if kw == "STOP":
        changed = [m for m in mems if m.get("status") != "OPTED_OUT"]
        for m in changed:
            if store.mark_opted_out(t["subs"], frm, m["campaign_id"], at, "STOP_KEYWORD", ops_table=t["ops"]):
                store.audit(t["audit"], frm, m["campaign_id"], "sms_stop", at, prior_status=m.get("status"),
                            new_status="OPTED_OUT", **base)
        if not changed:
            store.audit(t["audit"], frm, cid, "sms_stop", at, note="no_active_membership", **base)
        result = f"stop_{len(changed)}"
    elif kw == "HELP":
        store.audit(t["audit"], frm, cid, "sms_help", at, **base)
        if not already_replied and autoreply_ok("help", 5):
            reply = keywords.help_text(SUPPORT_EMAIL, tag_list)
    elif kw == "START":
        store.audit(t["audit"], frm, cid, "sms_start", at, **base)
        if not already_replied and autoreply_ok("start", 3):
            reply = keywords.start_text(PUBLIC_SITE_URL, tag_list)
    elif kw == "YES":
        if target and target.get("status") in PENDING_STATUSES and \
                store.activate(t["subs"], frm, cid, target["status"], at, sid, ops_table=t["ops"]):
            store.audit(t["audit"], frm, cid, "subscribe_confirmed", at, prior_status=target["status"],
                        new_status="ACTIVE", campaign_name=target.get("campaign_name"), **base)
            reply = keywords.welcome(target.get("campaign_name") or "fan", target.get("source_site"),
                                     bool(cfg.get("waitlist_mode", True)))
            result = "confirmed"
        elif any(m.get("status") == "ACTIVE" for m in mems):
            active = next(m for m in mems if m.get("status") == "ACTIVE")
            store.audit(t["audit"], frm, active["campaign_id"], "sms_yes_already_active", at, **base)
            if autoreply_ok("info", 1):
                reply = keywords.already_active(active.get("campaign_name") or "fan")
            result = "already_active"
        else:
            store.audit(t["audit"], frm, cid, "sms_yes_no_pending", at, **base)
            if autoreply_ok("info", 1):
                reply = keywords.no_signup(PUBLIC_SITE_URL, tag_list)
            result = "no_pending"
    else:
        store.audit(t["audit"], frm, cid, "sms_other", at, **base)
        if autoreply_ok("info", 1):
            active = any(m.get("status") == "ACTIVE" for m in mems)
            reply = keywords.help_text(SUPPORT_EMAIL, tag_list) if active else keywords.unknown_text(tag_list)

    _log("inbound", result, frm, campaign_id=cid, replied=bool(reply))
    _track("server_sms_inbound", frm, campaign_id=cid, keyword=kw, result=result, replied=bool(reply))
    return twiml(reply)


def _record_delivery(params, to, now):
    """Fan-out sends are found by MessageSid (sid#<sid> map row). Final statuses land on the send row."""
    status = params.get("MessageStatus", "")
    sid = params.get("MessageSid") or ""
    table = tables().get("sends")
    if table is None or status not in FINAL_STATUSES or not _E164.match(to) or not re.match(r"^[A-Z]{2}\w{10,64}$", sid):
        return
    values = {":s": status, ":at": store.now_iso(now), ":c": str(params.get("ErrorCode") or "")}
    try:
        ref = table.get_item(Key={"alert_id": f"sid#{sid}", "phone": "map"}).get("Item")
        if not ref or ref.get("target_phone") != to:
            return
        table.update_item(
            Key={"alert_id": ref["target_alert_id"], "phone": to},
            UpdateExpression="SET delivery_status = :s, delivery_updated_at = :at, delivery_error_code = :c",
            ConditionExpression="attribute_exists(phone)",
            ExpressionAttributeValues=values,
        )
    except Exception as e:
        log.warning("delivery status not recorded: %s", type(e).__name__)


def status_callback(params, now):
    to = params.get("To", "")
    code = str(params.get("ErrorCode") or "")
    _log("status", params.get("MessageStatus", ""), to if _E164.match(to) else None, error_code=code or None)
    _record_delivery(params, to, now)
    if code == "21610" and _E164.match(to):
        t = tables()
        store.carrier_opt_out(t["subs"], t["audit"], to, store.now_iso(now), "twilio", params.get("MessageSid"),
                              ops_table=t["ops"])
    return {"statusCode": 204, "headers": {}, "body": ""}


MEMBER_EVENT_WINDOW_SECONDS = 300
MEMBER_JOIN_DEDUPE_SECONDS = 7 * 86400   # a replayed join carries the same joined_at
MEMBER_LEAVE_DEDUPE_SECONDS = 120        # leave events have no unique id; collapse reconnect replays
_SNOWFLAKE = re.compile(r"^\d{5,25}$")
_ISO_TS = re.compile(r"^[0-9T:.+\-Zz]{10,40}$")


def member_event_sig(secret, event, guild_id, user_id, joined_at, member_count, ts):
    msg = "\n".join(str(x if x is not None else "") for x in (event, guild_id, user_id, joined_at, member_count, ts))
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def discord_member_event(event, headers, now):
    """POST /api/discord/member-event from the gateway bot: join / leave (+/-1) or sync (absolute count).
    Body is JSON with an HMAC `sig` (headers other than Content-Type never reach us through CloudFront).
    Returns 204 for accepted, ignored and duplicate events; 401 for a bad signature; 503 so the bot retries."""
    def status(code):
        return {"statusCode": code, "headers": {"Cache-Control": "no-store"}, "body": ""}

    if _edge_checks(headers, check_country=False):
        return status(403)
    if not DISCORD_GUILD_ID:
        return status(404)
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except Exception:
            return status(400)
    try:
        data = json.loads(body) if len(body) <= MAX_BODY_BYTES else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return status(400)
    kind = data.get("event")
    guild_id, user_id = str(data.get("guild_id") or ""), str(data.get("user_id") or "")
    joined_at, member_count = str(data.get("joined_at") or ""), data.get("member_count")
    ts, sig = data.get("ts"), data.get("sig")
    try:
        secret = member_event_secret()
    except Exception as e:
        log.error("member event secret unavailable: %s", type(e).__name__)
        return status(503)
    try:
        ts_int = int(ts)
    except (TypeError, ValueError):
        return status(400)
    expected = member_event_sig(secret, kind, guild_id, user_id, joined_at,
                                member_count if member_count is not None else "", ts_int)
    if not isinstance(sig, str) or not hmac.compare_digest(expected, sig):
        _log("member_event", "bad_signature")
        return status(401)
    if abs(now - ts_int) > MEMBER_EVENT_WINDOW_SECONDS:
        _log("member_event", "stale")
        return status(400)
    if guild_id != DISCORD_GUILD_ID:
        _log("member_event", "wrong_guild")
        return status(204)
    t = tables()
    cid = DEFAULT_CAMPAIGN_ID
    try:
        if kind == "sync":
            if not isinstance(member_count, int) or isinstance(member_count, bool) or member_count < 0:
                return status(400)
            result = counts.discord_sync(t["ops"], cid, member_count, now)
        elif kind in ("join", "leave"):
            if not _SNOWFLAKE.match(user_id) or (kind == "join" and not _ISO_TS.match(joined_at)):
                return status(400)
            if kind == "join":
                pk, window, delta = f"dedupe#discord#join#{guild_id}#{user_id}#{joined_at}", MEMBER_JOIN_DEDUPE_SECONDS, 1
            else:
                pk, window, delta = f"dedupe#discord#leave#{guild_id}#{user_id}", MEMBER_LEAVE_DEDUPE_SECONDS, -1
            if not store.claim(t["ops"], pk, window, now):
                _log("member_event", "duplicate", kind=kind)
                return status(204)
            try:
                result = counts.discord_adjust(t["ops"], cid, delta, now)
            except Exception:
                store.release_claim(t["ops"], pk)
                raise
        else:
            return status(400)
    except Exception as e:
        log.error("member event failed: %s", type(e).__name__)
        return status(503)
    _log("member_event", result, kind=kind, campaign_id=cid)
    return status(204)


FEED_FIELDS = ("sent_at", "message", "what_changed", "note", "urls", "backfilled", "status")


def _json_default(o):
    if hasattr(o, "as_integer_ratio"):  # Decimal from DynamoDB
        return int(o) if o == int(o) else float(o)
    return str(o)


def alerts_feed(event, now):
    """GET /api/alerts?campaign_id=...&limit=50. Public, read-only, newest first, cached 30s at the edge."""
    qs = event.get("queryStringParameters") or {}
    cid = qs.get("campaign_id") or DEFAULT_CAMPAIGN_ID
    if not isinstance(cid, str) or not _SLUG.match(cid):
        return _json(400, {"error": "unknown_campaign", "alerts": []}, "no-store")
    try:
        limit = max(1, min(100, int(qs.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50
    table = tables().get("alerts")
    if table is None:
        return _json(503, {"error": "unavailable", "alerts": []}, "no-store")
    try:
        res = table.query(KeyConditionExpression="campaign_id = :c", ExpressionAttributeValues={":c": cid},
                          ScanIndexForward=False, Limit=limit + 10)
    except Exception:
        log.exception("alerts query failed")
        return _json(503, {"error": "unavailable", "alerts": []}, "no-store")
    rows = [i for i in res.get("Items", []) if not i.get("is_test") and i.get("status") in ("published", "sent")]
    rows.sort(key=lambda i: i.get("sent_at", ""), reverse=True)
    alerts = [{k: i[k] for k in FEED_FIELDS if k in i} for i in rows[:limit]]
    return _json(200, {"campaign_id": cid, "alerts": alerts}, "public, max-age=30")


def _json(status, body, cache):
    return {"statusCode": status,
            "headers": {"Content-Type": "application/json", "Cache-Control": cache},
            "body": json.dumps(body, default=_json_default)}


def twilio_route(event, headers, path, fn):
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except Exception:
            return {"statusCode": 400, "headers": {}, "body": ""}
    pairs = urllib.parse.parse_qsl(body, keep_blank_values=True)
    url = TWILIO_WEBHOOK_BASE + path
    if event.get("rawQueryString"):
        url += "?" + event["rawQueryString"]
    try:
        token = twilio_token()
    except Exception as e:
        log.error("twilio token unavailable, rejecting webhook: %s", type(e).__name__)
        return {"statusCode": 403, "headers": {}, "body": ""}
    if not twilio_sig.valid(token, url, pairs, headers.get("x-twilio-signature", "")):
        _log(path, "bad_signature")
        return {"statusCode": 403, "headers": {}, "body": ""}
    try:
        return fn(dict(pairs), time.time())
    except Exception:
        log.exception("twilio webhook failed on %s", path)
        return twiml() if fn is inbound else {"statusCode": 204, "headers": {}, "body": ""}


ROUTES = {
    ("POST", "/api/subscribe"): subscribe,
    ("POST", "/api/unsubscribe"): unsubscribe,
}

TWILIO_ROUTES = {
    ("POST", "/api/twilio/inbound"): inbound,
    ("POST", "/api/twilio/status"): status_callback,
}


def handler(event, context):
    event = event or {}
    headers = _headers(event)
    method = ((event.get("requestContext") or {}).get("http") or {}).get("method", "")
    path = (event.get("rawPath") or "").rstrip("/")
    twilio_fn = TWILIO_ROUTES.get((method, path))
    if twilio_fn:
        blocked = _edge_checks(headers, check_country=False)
        if blocked:
            _log(path, blocked[1])
            return {"statusCode": 403, "headers": {}, "body": ""}
        return twilio_route(event, headers, path, twilio_fn)

    if (method, path) == ("POST", "/api/discord/member-event"):
        return discord_member_event(event, headers, time.time())

    if (method, path) == ("GET", "/api/alerts"):
        if _edge_checks(headers, check_country=False):
            return _json(403, {"error": "forbidden", "alerts": []}, "no-store")
        return alerts_feed(event, time.time())

    fn = ROUTES.get((method, path))
    is_form = headers.get("content-type", "").lower().startswith("application/x-www-form-urlencoded")
    if not fn:
        return respond(404, "not_found", is_form)

    blocked = _edge_checks(headers)
    if blocked:
        _log(path, blocked[1])
        return respond(blocked[0], blocked[1], is_form)

    payload, is_form = _parse_body(event, headers)
    if payload is None:
        return respond(400, "invalid_request", is_form)

    try:
        return fn(event, headers, payload, is_form, time.time())
    except Exception:
        log.exception("unhandled error on %s", path)
        return respond(500, "internal_error", is_form)
