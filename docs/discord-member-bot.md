# Discord member counts

Production Discord member counting is a **scheduled Lambda pull**, not an always-on Gateway bot.

Every 15 minutes, EventBridge rule `mall-rat-alerts-discord-count-sync` invokes Lambda
`mall-rat-alerts-discord-count-sync`, which:

1. Loads `discord_bot_token` from Secrets Manager (`mall-rat-alerts/secrets`).
2. Calls Discord REST `GET /guilds/{DISCORD_GUILD_ID}?with_counts=true`.
3. SETs `discord_members`, `discord_presence`, and `discord_synced_at` on ops item
   `counts#fob-twilight-town-mall-2026` / `current`.

SMS counters (`sms_active`, `sms_opted_out`) stay event-driven on signup/STOP and are never written by
this pull. Joins show up on the Discord counter within about 15 minutes, not instantly.

`scripts/refresh_counts.py` remains the absolute SMS + Discord reconcile path.

## Optional local Gateway bot (manual only)

`scripts/discord_member_bot.py` is kept as an optional manual tool. It is **not** used in production and
must not be started for normal ops. If you run it, it holds the Discord Gateway connection and can push
signed events to `POST /api/discord/member-event` (legacy route, still deployed but unused).

Needs: Python with `websockets` and `boto3`, AWS profile `default`, and `DISCORD_BOT_TOKEN` in `.env`.
The shared HMAC secret is read from Secrets Manager (`mall-rat-alerts/secrets`, key
`discord_member_webhook_secret`).

    python3 scripts/discord_member_bot.py --check     # confirm the intent is accepted, then exit
    python3 scripts/discord_member_bot.py --dry-run   # print event kinds, send nothing
    python3 scripts/discord_member_bot.py             # run (Ctrl-C to stop) — manual only

Set `MRA_BASE_URL` to override the API host (default `https://mallratalerts.com`).
