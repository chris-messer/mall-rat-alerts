"""Post the public alert text to a Discord channel webhook (stdlib only).

The webhook URL is a secret (anyone holding it can post). It lives in the Secrets Manager secret named by
DISCORD_SECRET_ID, under the key mapped to the campaign by DISCORD_CHANNELS (JSON {campaign_id: secret_key}).
The URL is never logged, returned or put in an exception message; logs carry only HTTP status codes and
exception type names.

post_public_alert() never raises. It is called after the alert is stored and after SMS fan-out was enqueued,
so a Discord problem can never affect either.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request

log = logging.getLogger()

# Discord limits: content 2000; embed title 256; description 4096; footer 2048;
# total characters across all embeds in one message 6000. We stay under those.
MAX_DESCRIPTION = 4000      # leave headroom under Discord's 4096 description cap
MAX_TITLE = 256
MAX_FOOTER = 2048
MAX_EMBEDS_TOTAL = 6000
TIMEOUT_SECONDS = 5
MAX_RETRY_AFTER_SECONDS = 10  # one 429 retry at most, and only if Discord asks for a short wait
USER_AGENT = "MallRatAlerts (fan project alert relay, 1.0)"  # Discord's edge rejects the default urllib agent
TITLES = {"fob-twilight-town-mall-2026": "Twilight Town Mall"}
DEFAULT_TITLE = "Mall Rat Alerts"
MARKER_ATTR = "discord_posted_at"

_secret_cache = {}


def channel_map():
    try:
        raw = json.loads(os.environ.get("DISCORD_CHANNELS", "") or "{}")
    except ValueError:
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str) and v} if isinstance(raw, dict) else {}


def _webhook_for(secret_key):
    """The webhook URL for a secret key, cached per warm container. None if unavailable (never raises)."""
    if secret_key in _secret_cache:
        return _secret_cache[secret_key]
    secret_id = os.environ.get("DISCORD_SECRET_ID", "")
    if not secret_id:
        log.warning("discord skipped: no secret configured")
        return None
    try:
        import boto3
        raw = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"]
        url = json.loads(raw).get(secret_key)
    except Exception as e:
        log.warning("discord skipped: secret unreadable (%s)", type(e).__name__)
        return None
    if not isinstance(url, str) or not url.startswith("https://"):
        log.warning("discord skipped: no usable value for the mapped secret key")
        return None
    _secret_cache[secret_key] = url
    return url


def clip(text, limit=MAX_DESCRIPTION):
    """Hard truncate with an ellipsis. Prefer clip_words for long bodies."""
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def clip_words(text, limit=MAX_DESCRIPTION):
    """Truncate at a word boundary so len(result) <= limit, ending with '...' when cut."""
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    if limit <= 3:
        return ""
    budget = limit - 3
    cut = text[:budget]
    sp = cut.rfind(" ")
    # Only break on a space if it keeps a meaningful prefix (at least half the budget).
    if sp >= max(1, budget // 2):
        cut = cut[:sp]
    return cut.rstrip(" ,;:-") + "..."


def _embed_chars(embed):
    n = len(embed.get("title") or "") + len(embed.get("description") or "")
    n += len((embed.get("footer") or {}).get("text") or "")
    n += len((embed.get("author") or {}).get("name") or "")
    for field in embed.get("fields") or []:
        n += len(field.get("name") or "") + len(field.get("value") or "")
    return n


def payload_for(campaign_id, text, alert_id):
    """One embed: long body in description (word-boundary clip), title + footer within limits."""
    title = clip(TITLES.get(campaign_id, DEFAULT_TITLE), MAX_TITLE)
    footer = clip(f"Alert {alert_id}", min(100, MAX_FOOTER))
    # Leave room so title + description + footer stay under the per-message embed total.
    desc_limit = min(MAX_DESCRIPTION, MAX_EMBEDS_TOTAL - len(title) - len(footer))
    description = clip_words(text, max(desc_limit, 0))
    embed = {"title": title, "description": description, "footer": {"text": footer}}
    # Last-resort clip if somehow still over (should not happen with the budget above).
    while _embed_chars(embed) > MAX_EMBEDS_TOTAL and embed["description"]:
        over = _embed_chars(embed) - MAX_EMBEDS_TOTAL
        new_limit = max(len(embed["description"]) - max(over, 1), 0)
        new_desc = clip_words(embed["description"], new_limit)
        if new_desc == embed["description"]:
            embed["description"] = clip(embed["description"], new_limit)
            break
        embed["description"] = new_desc
    return {
        "embeds": [embed],
        "allowed_mentions": {"parse": []},  # a mention in the text must never ping anyone
    }


def _send(url, body):
    """POST once. Returns (status, retry_after_seconds or None). Never raises, never includes the URL."""
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return resp.status, None
    except urllib.error.HTTPError as e:
        retry_after = None
        if e.code == 429:
            try:
                retry_after = float(json.loads(e.read().decode("utf-8")).get("retry_after"))
            except Exception:
                try:
                    retry_after = float(e.headers.get("Retry-After"))
                except Exception:
                    retry_after = None
        return e.code, retry_after
    except Exception as e:
        log.warning("discord post failed (%s)", type(e).__name__)
        return None, None


def _claim(table, campaign_id, alert_id):
    """True the first time. Conditional update so a retry or duplicate invocation never double-posts."""
    try:
        table.update_item(
            Key={"campaign_id": campaign_id, "sent_at": alert_id},
            UpdateExpression=f"SET {MARKER_ATTR} = :t",
            ConditionExpression=f"attribute_exists(sent_at) AND attribute_not_exists({MARKER_ATTR})",
            ExpressionAttributeValues={":t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        return True
    except Exception as e:
        if getattr(e, "response", {}).get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            log.warning("discord marker update failed (%s)", type(e).__name__)
        return False


def _release(table, campaign_id, alert_id):
    try:
        table.update_item(Key={"campaign_id": campaign_id, "sent_at": alert_id},
                          UpdateExpression=f"REMOVE {MARKER_ATTR}")
    except Exception as e:
        log.warning("discord marker release failed (%s)", type(e).__name__)


def post_public_alert(campaign_id, text, alert_id, table=None):
    """Post one public alert. alert_id is the alert's sent_at (its sort key). `table` is the alerts table; when
    given, a conditional marker (discord_posted_at) makes the post at-most-once. Returns the HTTP status code
    (int) when a post was attempted, else None. Never raises."""
    try:
        secret_key = channel_map().get(campaign_id)
        if not secret_key:
            log.info("discord skipped: campaign not mapped")
            return None
        url = _webhook_for(secret_key)
        if not url:
            return None
        if table is not None and not _claim(table, campaign_id, alert_id):
            log.info("discord skipped: already posted or marker unavailable")
            return None
        body = payload_for(campaign_id, text, alert_id)
        status, retry_after = _send(url, body)
        if status == 429 and retry_after is not None and retry_after <= MAX_RETRY_AFTER_SECONDS:
            time.sleep(max(retry_after, 0))
            status, _ = _send(url, body)
        ok = status is not None and 200 <= status < 300
        log.info("discord post status=%s", status)
        if status in (401, 404):
            _secret_cache.pop(secret_key, None)  # webhook rotated or deleted: re-read the secret next time
        if not ok and status is not None and table is not None:
            _release(table, campaign_id, alert_id)  # definite failure, safe to retry; a timeout keeps the claim
        return status
    except Exception as e:  # last resort: nothing here may reach the composer
        log.warning("discord post error (%s)", type(e).__name__)
        return None
