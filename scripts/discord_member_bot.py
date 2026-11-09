#!/usr/bin/env python3
"""Tiny Discord gateway listener: pushes member joins and leaves to the Mall Rat Alerts API.

It does nothing else: it never posts to Discord, never reads messages, never sends SMS. It connects to the
Discord Gateway with the GUILDS and GUILD_MEMBERS intents (the Server Members Intent must be ON in the
Developer Portal), and for the MallRatAlerts guild only:

  GUILD_CREATE         (on every connect)  -> POST {"event": "sync", "member_count": N}   absolute SET
  GUILD_MEMBER_ADD                         -> POST {"event": "join",  "user_id", "joined_at"}   +1
  GUILD_MEMBER_REMOVE  (leave, kick, ban)  -> POST {"event": "leave", "user_id"}                -1

OPTIONAL / MANUAL ONLY — production Discord counts are pulled every 15 minutes by the
mall-rat-alerts-discord-count-sync Lambda. Do not start this bot for normal ops.

If run manually, pushes signed join/leave/sync events to https://<site>/api/discord/member-event
(legacy route), signed with HMAC-SHA256 using the shared secret stored under
`discord_member_webhook_secret` in the mall-rat-alerts/secrets Secrets Manager secret. The bot token never
leaves this process. The bot's own join is ignored. Reconnects use RESUME; the server dedupes replays.

  python3 scripts/discord_member_bot.py --check     connect, confirm the intents are accepted, disconnect
  python3 scripts/discord_member_bot.py --dry-run   run, print event kinds, send nothing
  python3 scripts/discord_member_bot.py             run until stopped (Ctrl-C)

Config (env vars override .env): DISCORD_BOT_TOKEN, DISCORD_GUILD_ID (default: the MallRatAlerts id below),
MRA_BASE_URL (default https://mallratalerts.com), MRA_MEMBER_SECRET (default: read from Secrets Manager with
AWS profile "default", us-east-1). Requires: websockets, boto3 (only if MRA_MEMBER_SECRET is unset).
Logs carry event kinds and counts only, never tokens, secrets or user ids.
"""

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUILD_ID_DEFAULT = "1569100999606538352"
GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
INTENTS = (1 << 0) | (1 << 1)  # GUILDS + GUILD_MEMBERS (privileged). No presences, no message content.
SECRET_ID = "mall-rat-alerts/secrets"
SECRET_KEY = "discord_member_webhook_secret"
USER_AGENT = "MallRatAlerts-member-bot/1.0"

log = logging.getLogger("member-bot")


def load_env(path):
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def sign(secret, event, guild_id, user_id, joined_at, member_count, ts):
    msg = "\n".join(str(x if x is not None else "") for x in (event, guild_id, user_id, joined_at, member_count, ts))
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


class Pusher:
    def __init__(self, base_url, secret, guild_id, dry_run):
        self.url = base_url.rstrip("/") + "/api/discord/member-event"
        self.secret, self.guild_id, self.dry_run = secret, guild_id, dry_run

    def _post(self, payload):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(self.url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=15) as f:
                    return f.status
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    return e.code  # 4xx will not get better by retrying
                code = e.code
            except Exception as e:
                code = type(e).__name__
            log.warning("push attempt %d failed (%s)", attempt + 1, code)
            time.sleep(min(30, 2 ** attempt + random.random()))
        return None

    async def push(self, event, user_id=None, joined_at=None, member_count=None):
        if self.dry_run:
            log.info("dry-run %s member_count=%s", event, member_count)
            return
        ts = int(time.time())
        payload = {"event": event, "guild_id": self.guild_id, "ts": ts,
                   "sig": sign(self.secret, event, self.guild_id, user_id, joined_at,
                               member_count if member_count is not None else "", ts)}
        if user_id:
            payload["user_id"] = user_id
        if joined_at:
            payload["joined_at"] = joined_at
        if member_count is not None:
            payload["member_count"] = member_count
        status = await asyncio.get_running_loop().run_in_executor(None, self._post, payload)
        log.info("pushed %s -> %s", event, status)


class Gateway:
    def __init__(self, token, guild_id, pusher, check_only):
        self.token, self.guild_id, self.pusher, self.check_only = token, guild_id, pusher, check_only
        self.seq = None
        self.session_id = None
        self.resume_url = None
        self.bot_id = None
        self.stop = False

    async def run(self):
        import websockets
        backoff = 1
        while not self.stop:
            url = (self.resume_url + "/?v=10&encoding=json") if self.session_id and self.resume_url else GATEWAY
            try:
                async with websockets.connect(url, max_size=None, ping_interval=None) as ws:
                    backoff = 1
                    await self.session(ws)
            except websockets.exceptions.ConnectionClosed as e:
                code = e.rcvd.code if e.rcvd else None
                if code == 4014:
                    log.error("gateway closed 4014: Server Members Intent is not enabled for this application. "
                              "Turn on Developer Portal > Bot > Privileged Gateway Intents > SERVER MEMBERS INTENT.")
                    return 2
                if code in (4004, 4010, 4011, 4012, 4013):
                    log.error("gateway closed %s: not recoverable", code)
                    return 2
                if code in (4007, 4009):
                    self.session_id = None
                log.warning("gateway closed (%s), reconnecting", code)
            except Exception as e:
                log.warning("gateway error (%s), reconnecting", type(e).__name__)
            if self.stop:
                return 0
            if self.check_only:
                return 1
            await asyncio.sleep(backoff + random.random())
            backoff = min(60, backoff * 2)
        return 0

    async def session(self, ws):
        hello = json.loads(await ws.recv())
        interval = hello["d"]["heartbeat_interval"] / 1000.0
        hb = asyncio.create_task(self.heartbeat(ws, interval))
        try:
            if self.session_id:
                await ws.send(json.dumps({"op": 6, "d": {"token": self.token, "session_id": self.session_id,
                                                         "seq": self.seq}}))
            else:
                await ws.send(json.dumps({"op": 2, "d": {
                    "token": self.token, "intents": INTENTS,
                    "properties": {"os": sys.platform, "browser": "mall-rat-alerts", "device": "mall-rat-alerts"}}}))
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("s") is not None:
                    self.seq = msg["s"]
                op = msg["op"]
                if op == 0:
                    if await self.dispatch(msg["t"], msg["d"]):
                        return
                elif op == 7:
                    log.info("gateway asked for reconnect")
                    return
                elif op == 9:
                    log.info("invalid session (resumable=%s)", msg.get("d"))
                    if not msg.get("d"):
                        self.session_id = None
                    await asyncio.sleep(random.uniform(1, 5))
                    return
                elif op == 1:
                    await ws.send(json.dumps({"op": 1, "d": self.seq}))
        finally:
            hb.cancel()

    async def heartbeat(self, ws, interval):
        await asyncio.sleep(interval * random.random())
        while True:
            await ws.send(json.dumps({"op": 1, "d": self.seq}))
            await asyncio.sleep(interval)

    async def dispatch(self, kind, d):
        """Returns True to end the session (check mode finished)."""
        if kind == "READY":
            self.bot_id = d["user"]["id"]
            self.session_id, self.resume_url = d["session_id"], d.get("resume_gateway_url")
            log.info("ready: gateway accepted the GUILDS + GUILD_MEMBERS intents")
            if self.check_only:
                self.stop = True
                return True
        elif kind == "GUILD_CREATE" and d.get("id") == self.guild_id:
            log.info("guild available, member_count=%s", d.get("member_count"))
            if isinstance(d.get("member_count"), int):
                await self.pusher.push("sync", member_count=d["member_count"])
        elif kind == "GUILD_MEMBER_ADD" and d.get("guild_id") == self.guild_id:
            uid = (d.get("user") or {}).get("id")
            if uid and uid != self.bot_id and d.get("joined_at"):
                await self.pusher.push("join", user_id=uid, joined_at=d["joined_at"])
        elif kind == "GUILD_MEMBER_REMOVE" and d.get("guild_id") == self.guild_id:
            uid = (d.get("user") or {}).get("id")
            if uid and uid != self.bot_id:
                await self.pusher.push("leave", user_id=uid)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="connect once, verify the intents are accepted, exit")
    ap.add_argument("--dry-run", action="store_true", help="print event kinds, send nothing to the API")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("websockets").setLevel(logging.WARNING)

    env = load_env(ROOT / ".env")
    cfg = lambda k, d="": os.environ.get(k) or env.get(k) or d  # noqa: E731
    token = cfg("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN missing")
    guild_id = cfg("DISCORD_GUILD_ID", GUILD_ID_DEFAULT)
    secret = os.environ.get("MRA_MEMBER_SECRET", "")
    if not secret and not (args.check or args.dry_run):
        import boto3
        sm = boto3.Session(profile_name="default", region_name="us-east-1").client("secretsmanager")
        secret = json.loads(sm.get_secret_value(SecretId=SECRET_ID)["SecretString"])[SECRET_KEY]
    pusher = Pusher(cfg("MRA_BASE_URL", "https://mallratalerts.com"), secret, guild_id, args.dry_run or args.check)
    gw = Gateway(token, guild_id, pusher, args.check)
    try:
        sys.exit(asyncio.run(gw.run()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
