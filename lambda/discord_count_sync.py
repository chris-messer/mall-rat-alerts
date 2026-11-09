"""Pull Discord approximate guild member count into the ops counter on a schedule.

EventBridge invokes this every 15 minutes. Loads the bot token from Secrets Manager, calls
GET /guilds/{DISCORD_GUILD_ID}?with_counts=true, and SETs discord_members / discord_presence /
discord_synced_at via common.counts.discord_sync. Never touches sms_active / sms_opted_out, never
posts to Discord, never sends SMS.

Env: OPS_TABLE, DEFAULT_CAMPAIGN_ID, DISCORD_GUILD_ID, DISCORD_SECRET_ID,
     DISCORD_BOT_TOKEN_KEY (default discord_bot_token).
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request

from common import counts

log = logging.getLogger()
log.setLevel(logging.INFO)

OPS_TABLE = os.environ.get("OPS_TABLE", "")
CAMPAIGN_ID = os.environ.get("DEFAULT_CAMPAIGN_ID", "")
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")
DISCORD_SECRET_ID = os.environ.get("DISCORD_SECRET_ID", "")
DISCORD_BOT_TOKEN_KEY = os.environ.get("DISCORD_BOT_TOKEN_KEY", "discord_bot_token")
USER_AGENT = "MallRatAlerts (discord count sync, 1.0)"
API_BASE = "https://discord.com/api/v10"
TIMEOUT_SECONDS = 10
MAX_ATTEMPTS = 3  # initial try + light retries on 429 / 5xx

_token_cache = {"value": None}
_ops_table = {"value": None}


def _bot_token():
    """Bot token from Secrets Manager, cached per warm container. Never logged."""
    if _token_cache["value"]:
        return _token_cache["value"]
    if not DISCORD_SECRET_ID:
        raise RuntimeError("DISCORD_SECRET_ID not configured")
    import boto3

    raw = json.loads(boto3.client("secretsmanager").get_secret_value(SecretId=DISCORD_SECRET_ID)["SecretString"])
    token = raw.get(DISCORD_BOT_TOKEN_KEY)
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("discord bot token missing from secret")
    _token_cache["value"] = token.strip()
    return _token_cache["value"]


def fetch_guild_counts(token, guild_id):
    """GET /guilds/{id}?with_counts=true. Returns (members, presence). Raises urllib.error.HTTPError
    or RuntimeError on missing fields. Token is never included in exceptions or logs."""
    req = urllib.request.Request(
        f"{API_BASE}/guilds/{guild_id}?with_counts=true",
        headers={"Authorization": "Bot " + token, "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        body = json.load(resp)
    if "approximate_member_count" not in body:
        raise RuntimeError("discord response missing approximate_member_count")
    members = int(body["approximate_member_count"])
    presence = int(body.get("approximate_presence_count") or 0)
    return members, presence


def _retry_after_seconds(err):
    """Best-effort Retry-After / retry_after for 429. Caps at a few seconds for Lambda."""
    try:
        raw = err.read()
        if raw:
            return min(float(json.loads(raw.decode("utf-8")).get("retry_after", 1)), 5.0)
    except Exception:
        pass
    try:
        return min(float(err.headers.get("Retry-After", 1)), 5.0)
    except Exception:
        return 1.0


def pull_with_retries(token, guild_id):
    """Light retries on Discord 429 and 5xx, then re-raise. Auth errors fail immediately."""
    last = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            return fetch_guild_counts(token, guild_id)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 401 or e.code == 403:
                raise
            if e.code != 429 and not (500 <= e.code < 600):
                raise
            if attempt + 1 >= MAX_ATTEMPTS:
                raise
            wait = _retry_after_seconds(e) if e.code == 429 else min(1.0 * (attempt + 1), 3.0)
            log.warning("discord http %s attempt=%d; retrying in %.1fs", e.code, attempt + 1, wait)
            time.sleep(wait)
        except Exception:
            raise
    raise last  # pragma: no cover


def _ops():
    if _ops_table["value"] is None:
        if not OPS_TABLE:
            raise RuntimeError("OPS_TABLE not configured")
        import boto3

        _ops_table["value"] = boto3.resource("dynamodb").Table(OPS_TABLE)
    return _ops_table["value"]


def sync_once(ops_table=None, token=None, guild_id=None, campaign_id=None):
    """Fetch and write. Returns {"members", "presence", "result"}. Raises on Discord/HTTP failure."""
    guild_id = guild_id if guild_id is not None else DISCORD_GUILD_ID
    campaign_id = campaign_id if campaign_id is not None else CAMPAIGN_ID
    if not guild_id:
        raise RuntimeError("DISCORD_GUILD_ID not configured")
    if not campaign_id:
        raise RuntimeError("DEFAULT_CAMPAIGN_ID not configured")
    token = token if token is not None else _bot_token()
    members, presence = pull_with_retries(token, guild_id)
    table = ops_table if ops_table is not None else _ops()
    result = counts.discord_sync(table, campaign_id, members, presence=presence)
    log.info("discord count sync members=%d presence=%d result=%s campaign=%s",
             members, presence, result, campaign_id)
    return {"members": members, "presence": presence, "result": result}


def handler(event, context):
    return sync_once()
