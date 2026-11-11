# Mall Rat Alerts (Phase 1: waitlist mode)

Free, unofficial, multi-campaign fan text alerts. Current campaign: **Twilight Town Mall**
(`fob-twilight-town-mall-2026`), alerts when falloutboy.com or twilighttownmall.falloutboy.com changes. Design doc:
`../fob-site-monitor/docs/signup-site-design.md` (Phase 1, adapted as noted below).

Live: https://mallratalerts.com (and https://www.mallratalerts.com). The CloudFront default domain
https://d2l0wpmra7vopu.cloudfront.net still serves the same site.

This stack has its own Terraform root and local state and shares nothing with `fob-site-monitor`.
SMS fan-out is built but **off** (`fanout_mode=off`, `broadcast_enabled=false`, `waitlist_mode=true`). With those
flags the only texts this stack produces are the one web confirmation and TwiML auto-replies to people who text the
number first (YES/HELP/START/FOB/free text). Alerts are composed and stored (and shown on the homepage feed); see
"SMS fan-out" below for how sending is gated and the runbook.

## Layout

```text
mall-rat-alerts/
  lambda/                    signup-api Lambda (Python 3.12, stdlib + boto3)
    signup_api.py            /api/subscribe, /api/unsubscribe, /api/twilio/inbound, /api/twilio/status
    common/phone.py          US-only NANP validation (state + DC area-code allowlist), masking
    common/store.py          DynamoDB helpers: config cache, rate limiter, memberships, audit
    common/keywords.py       inbound keyword router + GSM-7 reply copy
    common/tags.py           text-to-join tag matching (FOB, "Fall Out Boy", ...)
    common/sms_text.py       SMS sanitizer + GSM-7/UCS-2 segment counter (shared with fob-site-monitor)
    common/alert_text.py     model JSON validation, "what changed / blank line / note" assembly (shared)
    common/llm_trace.py      PostHog $ai_generation / $ai_trace over urllib (shared)
    common/default_public_prompt.txt   built-in copy of the public template (fallback)
    alert_composer.py        EventBridge target: SiteChange -> public alert in the alerts table -> fan-out job (SQS)
    fanout_worker.py         SQS -> paced SMS fan-out to ACTIVE subscribers (gated, idempotent, resumable)
    common/fanout.py         fan-out modes, send gate, message body (prefix + STOP suffix), pacing math
    common/twilio_send.py    Twilio REST send (stdlib)
    common/discord.py        public alert -> Discord channel webhook (stdlib, never raises, at-most-once)
    common/twilio_sig.py     X-Twilio-Signature validation
    common/turnstile.py      Cloudflare Turnstile siteverify (gated off)
    common/analytics.py      optional server-side PostHog events (no-op without a key)
  site/                      static pages (work without JavaScript), assets/app.js, consent/ archive
  terraform/                 main, variables, dynamodb, site (S3 + CloudFront), api (API GW + Lambda), alerts, mail,
                             fanout (SQS + DLQ, worker, sends table, alarms), outputs
  scripts/
    deploy.sh                reads .env (PostHog) and runs terraform apply
    load_twilio_secret.py    copies ONLY Twilio SID + auth token into mall-rat-alerts/secrets (never prints)
    set_twilio_webhook.py    points one number's SMS URL at /api/twilio/inbound (guarded, has --dry-run)
    smoke_inbound.py         signed fake Twilio requests against the live webhook (no SMS is sent)
    template.py              get/put the public template; list/get/put text-to-join tags
    backfill_alerts.py       rebuild past alerts from the monitor's CloudWatch logs (public template)
  tests/                     stdlib unittest (boto3 stubbed): python3 -m unittest discover -s tests
```

## Data model (multi-campaign)

**subscribers** (`mall-rat-alerts-subscribers`): **PK `phone`, SK `campaign_id`**. One membership per
phone per campaign, so the same person can join several campaigns over time and opt out of one without
touching the others. Phone-first keys make inbound SMS (which only knows the phone) a single Query.
GSIs: `campaign-index` (PK `campaign_id`, SK `phone`) to list a campaign's members, and sparse
`active-index` (PK `gsi1pk = ACTIVE#<campaign_id>`, SK `confirmed_at#phone`) for future fan-out.
TTL `expires_at` (pending signups only, 30 days), PITR on, deletion protection on.

Fields snapshotted on every signup: `campaign_id`, `campaign_name`, `campaign_tags`, `source_site`,
`status` (`PENDING_SENDER` / `ACTIVE` / `OPTED_OUT` ...), `created_at`, `updated_at`,
`consent_version`, `consent_sha256`, `form_version`, `consent_checkbox`, `age_18_plus`,
`consent_captured_at`, `ip_hash`, `ua_hash` (HMAC-SHA256 with a Terraform-generated salt; raw IP/UA
never stored), `source=web`, `utm_source/medium/campaign/term/content`, `referrer`, `landing_path`,
`page_url` (from the form, else parsed from the Referer header), `is_test`, `signup_requests`.
After a YES: `confirmed_at`, `confirm_message_sid`. After opt-out: `unsubscribed_at`, `unsubscribe_method`
(`WEB`, `STOP_KEYWORD`, `CARRIER_21610`).

**audit** (`mall-rat-alerts-audit`): PK `phone`, SK `<iso ts>#<campaign_id>#<event_type>`, GSI
`campaign-index`. Append-only (the Lambda has PutItem only). Every row has `campaign_id`,
`event_type`, `actor`, and the metadata snapshot where relevant. Event types:
`consent_recorded`, `subscribe_pending`, `subscribe_duplicate`, `signup_web`, `confirm_sent`, `confirm_suppressed`, `confirm_failed`, `subscribe_confirmed`, `unsubscribe`,
`unsubscribe_noop`, `sms_stop`, `sms_help`, `sms_start`, `sms_yes_already_active`,
`sms_yes_no_pending`, `sms_other`, `carrier_21610`. Keyword bodies are stored; free-text bodies are not
(length only).

**templates** (`mall-rat-alerts-templates`): PK `pk`, SK `sk`. Edited live, no redeploy.
- `template#public#<campaign_id>` / `current`: `system_prompt`, `model_ids` (optional), `max_chars` (306 hard cap),
  `target_chars` (160 = one segment), `temperature`, `max_tokens`, `fallback_note`, `visit_what`, `visit_note`,
  `version`, `updated_at`. Fetched on every composer run; the built-in copy is the fallback.
- `config#tags` / `<TAG>`: text-to-join keyword -> campaign (see "Text to join").

**alerts** (`mall-rat-alerts-alerts`): PK `campaign_id`, SK `sent_at` (ISO UTC, ms). Public alert history:
`message`, `what_changed`, `note`, `urls`, `segments`, `encoding`, `status` (`published`), `source_event_id`,
`template_version`, `model`, `backfilled`, `is_test`, `trace_id`. `src#<event id>` / `marker` rows make the
composer idempotent. `det#<campaign_id>#<detected_at>` / `marker` rows are the hard same-detection guard: a
second event with the same `detected_at` is stored as `status=suppressed`, `suppressed_reason=
duplicate:same_detection`, `model=guard` (no model call, no SMS, no Discord). TTL `expires_at` on markers only.
The recent-alerts context is read with `ConsistentRead=True`. Setting `status=hidden` on an alert row removes it
from the public feed (the feed only returns `published`/`sent`).

**ops** (`mall-rat-alerts-ops`): PK `pk`, SK `sk`, TTL `expires_at`.
- `config#global` / `current`: `signups_enabled`, `waitlist_mode`, `broadcast_enabled`, caps, and the `fanout_*` keys
  (seeded once; CLI edits are kept)
- `rl#fanout#<campaign_id>` / `d#YYYYMMDD`: public alerts fanned out per campaign per UTC day; `fanout#lock` / `claim`: worker lock

**fanout-sends** (`mall-rat-alerts-fanout-sends`): PK `alert_id` (`<campaign_id>#<sent_at>`), SK `phone`. One row per
person per alert, created with a conditional put **before** the Twilio call: `status` `CLAIMED` -> `SENT` / `FAILED` /
`OPTED_OUT` / `UNKNOWN`, or `SKIPPED` (not ACTIVE at send time); `message_sid`, `error`, `segments`, and from the status
callback `delivery_status`, `delivery_error_code`. `phone = #run` holds the run state (`starting`, `running`,
`parked`, `completed`, `refused`). TTL 90 days.
- `config#campaign#<campaign_id>` / `current`: `campaign_name`, `campaign_tags`, `source_site`, `campaign_status`, `signups_enabled`
- `rl#ip#<campaign_id>#<ip_hash>#sub|unsub`, `rl#phone#<campaign_id>#<phone>#sub`, `rl#global#<campaign_id>#sub`, `rl#reply#<kind>#<phone>` / `h#YYYYMMDDHH` or `d#YYYYMMDD`

Adding a campaign = add a `campaign` item (Terraform variable or `put-item`) and a landing page whose
hidden `campaign_id` field names it (or link with `?c=<campaign_id>`).

## API

Same origin as the site through CloudFront `/api/*` (caching disabled, no CORS). Direct calls to the
API Gateway URL are rejected (CloudFront adds a secret `X-Origin-Verify` header). `/api/subscribe` and
`/api/unsubscribe` are US-only (`CloudFront-Viewer-Country`); Twilio routes are exempt and rely on the signature.

| Route | Behavior |
|---|---|
| `POST /api/subscribe` | Validate (US NANP only, toll-free/premium rejected), rate-limit, store `PENDING_SENDER`, then send ONE "Reply YES" confirmation text (see below). Always `202 {"status":"check_your_phone"}` (JSON) or `303 /thanks/` (plain form) whether new, duplicate, ACTIVE, OPTED_OUT, or suppressed. 400 on bad input, 429 per-IP limit, 503 kill switch. |
| `POST /api/unsubscribe` | Opt out of **one campaign** (default current). Always `202` / `303 /unsubscribe/done/`. |
| `GET /api/alerts?campaign_id=...&limit=50` | Public, newest first (limit max 100). Published, non-test alerts only; whitelisted fields. `Cache-Control: public, max-age=30`. CloudFront forwards only `campaign_id` and `limit` on this route. |
| `POST /api/twilio/inbound` | Signed Twilio webhook, TwiML reply. |
| `POST /api/twilio/status` | Signed status callback. `ErrorCode=21610` opts the number out everywhere. |

Rate limits: per IP 3 subscribes/hour and 20/day per campaign, 10 unsubscribes/day; per phone 3
subscribe requests/day (silent); global 300 subscribes/hour per campaign (503). Honeypot field and a
2-second minimum fill time (when JS sets it) drop bots silently.

Throttling: Lambda reserved concurrency **5**; API Gateway stage throttle **5 rps, burst 5**.
Logs: `/aws/lambda/mall-rat-alerts-signup-api` and `/aws/apigateway/mall-rat-alerts-api`, 14 days,
phone numbers masked (`+1******0006`).

## Inbound SMS (Twilio)

Number **+1 833-596-1548** (Twilio SID `PN3303d4c240ea16218ad8f41c9444fd20`, friendly name
"disconnected from versiful staging"). Its SMS webhook is
`https://mallratalerts.com/api/twilio/inbound` (POST). Voice was left as it was. Signatures are
checked against `TWILIO_WEBHOOK_BASE` (`https://mallratalerts.com`), so requests to the CloudFront
default domain now fail validation with 403.
The number is not in any Messaging Service, so no other Versiful number was touched.

Credentials: secret **`mall-rat-alerts/secrets`** with keys `twilio_account_sid`, `twilio_auth_token`
(copied from `staging-versiful_secrets` keys `twilio_account_sid` / `twilio_auth` by
`scripts/load_twilio_secret.py`; Terraform creates only the empty secret, so the token is not in state).
The Lambda can read only this secret.

Signature: every Twilio request must carry a valid `X-Twilio-Signature` computed over
`TWILIO_WEBHOOK_BASE + path` (set by Terraform to the public site URL). **There is no unsigned mode:**
if the secret is missing or unreadable, or the signature is wrong, the webhook returns 403 (fails closed).

Router order: STOP words, HELP, YES, START, **tag match (text to join)**, anything else. Every TwiML
reply goes through `sms_text.sanitize(..., "public")` (GSM-7 only) and logs `segments` and `encoding`.

Keyword rules:
- **STOP**, STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT, REVOKE, OPTOUT, "opt out", and short free text that
  clearly asks to stop: opt out of **every** campaign for that phone (STOP on a shared sender stops
  all texts from that number, as the SMS Terms say). Empty TwiML reply, because Twilio's own opt-out
  handling sends the STOP confirmation and blocks further sends.
- **YES** (Y, YEP, YEAH, CONFIRM): the target campaign is the **most recently updated PENDING_SENDER or
  PENDING membership** for that phone. That one becomes `ACTIVE` with a welcome reply. Already ACTIVE
  gets "already signed up". No pending signup gets "sign up at the website" (at most once a day).
- **HELP**/INFO: help text with support email and "Text FOB to join Twilight Town Mall alerts" (up to 5 a day). **START**/UNSTOP: "you can get texts
  again, sign up at the website" (status unchanged, so rejoining always goes through the web consent form).
- **Tag** (see below): subscribes the sender to the matching campaign and replies with the welcome.
- Anything else: at most one auto-reply per day (lists the known tags, or the HELP text for members). Inbound from toll-free numbers is ignored (bot loops).
- If no membership exists, audit rows use the default campaign `fob-twilight-town-mall-2026`.
- If Twilio sends `OptOutType` (Advanced Opt-Out already replied), we don't reply a second time.

Smoke test without sending SMS: `python3 scripts/smoke_inbound.py --base <site_url> HELP YES STOP`.
Keyword test (uses the Twilio magic number and deletes its rows): `... --cleanup FOB "fall out boy" HELP`.

## Web sign-up on computers (double opt-in)

The homepage ships both variants in HTML; CSS `@media (hover: none) and (pointer: coarse)` shows the
Subscribe Now `sms:` pill on phones and the phone-number form everywhere else (`.cta-phone` / `.cta-desktop`).
The form POSTs to `/api/subscribe` (303 `/thanks/` without JS; fetch + inline message with JS) with the archived
consent text `site/consent/sms-consent-2026-11-06.v2.txt` (`consent_version` v2).

After storing `PENDING_SENDER`, `_web_confirm` sends exactly one text from `TWILIO_FROM_NUMBER` via the Twilio REST
API (`common/twilio_send.py`, credentials from the `mall-rat-alerts/secrets` secret, never logged). Replying YES
promotes it to ACTIVE through the normal inbound handler. Nothing is ever active without that reply.

Guards, in order (each skip writes audit `confirm_suppressed` with a `reason`; sends write `confirm_sent`, failures
`confirm_failed`; the API response never changes):

| Guard | Config | Reason |
| --- | --- | --- |
| kill switch | ops `config#global` `web_confirm_enabled` (default true) | `kill_switch` |
| shared test numbers | `TEST_NUMBERS` env | `test_number` |
| 1 per phone per rolling 24h | fixed | `phone_24h_limit` |
| global daily cap | `web_confirm_daily_cap` (default 50) | `daily_cap` |
| dry run (audit only, no SMS) | `web_confirm_dry_run` (default false) | audit `confirm_sent` with `dry_run=true` |
| ACTIVE / OPTED_OUT / PENDING | n/a | `already_active` / `opted_out` / `pending` (no text) |

Per-IP-hash limit: 3 subscribes/hour (`ip_subscribe_hour`). OPTED_OUT numbers are never texted or re-enrolled from
the web; they must text START or FOB themselves. The confirmation copy lives in templates item
`config#web_confirm` / `<campaign_id>` (`message`, `enabled`); the Lambda falls back to built-in copy if it is not
GSM-7, longer than 2 segments, or lacks STOP. Edit live, for example:
`aws dynamodb update-item --table-name mall-rat-alerts-ops --key '{"pk":{"S":"config#global"},"sk":{"S":"current"}}' --update-expression "SET web_confirm_enabled = :f" --expression-attribute-values '{":f":{"BOOL":false}}'`.
Turnstile is still disabled; enabling it is the next abuse layer for this form.

## Text to join (keyword subscribe)

People can text a tag to the number instead of using the web form. Tags live in the templates table
(`pk = config#tags`, `sk = <TAG>`), editable without a deploy (60 s cache):
`tag`, `campaign_id`, `campaign_name`, `aliases` (list), `welcome_message`, `enabled`.
Matching is case-insensitive and alphanumeric only after stripping, so `FOB`, `fob`, `FOB!`,
`  Fall   Out Boy `, `FALLOUTBOY` all select the FOB tag. The body must be short (60 chars, 8 words);
longer free text is not treated as a join. Several tags in one body subscribe to each and send one
combined welcome; unknown extra words are ignored when a tag matched.

On a match the Lambda upserts the `phone` + `campaign_id` row (`source=sms`, `status=ACTIVE`,
`consent_version=sms-keyword-v1`, metadata snapshot), writes audit `subscribe_sms_tag` (tag, campaign,
consent version, captured time), and tracks PostHog `sms_subscribe` (`tag`, `campaign_id`,
`source=sms`; never the phone). Already ACTIVE gets a friendly reply (once a day, audit
`sms_tag_already_active`). OPTED_OUT re-opts in. A website PENDING_SENDER row is promoted to ACTIVE.
In waitlist mode the welcome ends with "Alerts start at launch." The keyword consent text is archived at
`site/consent/sms-keyword-v1.txt` (hash in `KEYWORD_CONSENT_SHA256`).

```bash
python3 scripts/template.py tags                       # list
python3 scripts/template.py tag-get FOB
python3 scripts/template.py tag-put FOB --campaign-id fob-twilight-town-mall-2026 \
  --campaign-name "Twilight Town Mall" --alias FALLOUTBOY --welcome-file welcome.txt
python3 scripts/template.py tag-put FOB --disable      # stop accepting the keyword
```

The FOB welcome (seeded, GSM-7, no emojis):
`Mall Rat Alerts: You're in for Twilight Town Mall (Fall Out Boy site alerts).` blank line
`We'll text when the site changes. Msg freq varies. Msg&data rates may apply. Reply STOP to opt out, HELP for help. Unofficial fan project.`

## Alert pipeline and public template

`fob-site-monitor` publishes an EventBridge event on the default bus (`source fob-site-monitor`,
`detail-type SiteChange`, detail = `campaign_id`, `alert_kind`, `detected_at`, `changes`, `test`).
Rule `mall-rat-alerts-site-change` (skips events with `detail.test`) invokes Lambda
`mall-rat-alerts-alert-composer`, which: claims an idempotency marker for the event id, fetches the
public template live from DynamoDB, asks Bedrock (Nova first, then the fallbacks) for JSON
`{notify, what_changed, note}`, assembles `what_changed + blank line + note`, sanitizes to GSM-7,
trims the note before the description to stay at one segment (hard cap 306), and stores the alert.
`fanout_hook()` then enqueues one fan-out job unless `fanout_mode` is `off` (see "SMS fan-out").

Edit the public template without a deploy (applies on the next event, version bumps by one):

```bash
python3 scripts/template.py get
python3 scripts/template.py get --out public.txt
python3 scripts/template.py put --file public.txt
python3 scripts/template.py put --max-chars 306 --target-chars 160 --temperature 0.2
```

Rebuild history from the monitor's logs (never prints phone numbers; stores `backfilled=true`):
`python3 scripts/backfill_alerts.py --dry-run`, then without `--dry-run`.

## SMS fan-out

```text
monitor: private texts (2 recipients, blocking) -> EventBridge SiteChange
  -> composer: store alert -> SQS mall-rat-alerts-fanout (DelaySeconds = fanout_delay_seconds, default 10)
  -> mall-rat-alerts-fanout-worker (batch 1, reserved = max concurrency 2, timeout 300 s)
       -> per ACTIVE subscriber: re-check switches + status, claim send row, pace, Twilio
  -> mall-rat-alerts-fanout-dlq (failed jobs after 5 receives, and work parked by the kill switch)
```

**A real text goes out only if all of these hold** (checked at job start, and the switches again before every send,
with strongly consistent reads):

| Check | Where |
| --- | --- |
| `fanout_mode` is `allowlist` or `live` (anything else, missing or misspelled = `off`) | ops `config#global` |
| `broadcast_enabled` is the boolean `true` | ops |
| `live` only: `waitlist_mode` is the boolean `false` | ops |
| `fanout_paused` is not true | ops, before every send |
| `allowlist` only: phone is in `fanout_allowlist` (and ACTIVE) | ops, before every send |
| campaign `campaign_status = active` | ops `config#campaign#<id>` |
| alert exists, `status = published`, not `backfilled`, not older than `fanout_max_alert_age_minutes` (120) | alerts |
| `live` only: alert is not `is_test` (test alerts can only go to the allowlist) | alerts |
| ACTIVE count (after allowlist filter) <= `fanout_max_recipients_per_alert` (200), else the whole run is refused | active-index |
| under `max_public_alerts_per_day` (15) for the campaign, counted per UTC day | ops `rl#fanout#...` |
| subscriber still `ACTIVE` at send time (OPTED_OUT / PENDING are skipped) | subscribers, consistent read |
| no send row yet for (alert, phone) | fanout-sends, conditional put before the Twilio call |

`fanout_mode=off` also means the composer does not enqueue anything. `dry_run` runs every check, writes no send rows,
never reads the Twilio secret, logs `fanout dry_run: would send N`, keeps the alert `published` and adds
`dry_run_counts`. A completed live/allowlist run sets `status=sent` (if at least one text went out), `recipients`, `sent`,
`failed`, `skipped`, `mode`, `fanout_started_at`, `fanout_finished_at`; `sent_at` never changes. Audit rows use
`phone = system#fanout` (`fanout_started`, `fanout_dry_run`, `fanout_completed`, `fanout_refused`, `fanout_parked`,
`fanout_skipped`, `fanout_continued`), counts only. Logs never contain phone numbers.

Message: `Mall Rat Alerts: <what changed>` blank line `<note>` newline `Reply STOP to opt out`, the composer's stored
text run through the GSM-7 sanitizer, the note trimmed first so the total stays within 2 segments (306). The STOP
suffix is on every message (simpler than monthly). A body that is not GSM-7 or is over 2 segments refuses the run.

Pacing: `fanout_rate_per_second` is **segments** per second (default 2; any value >= 3 or <= 0 falls back to 2),
so a 2-segment text starts every 1 s and a 1-segment text every 0.5 s. That leaves at least 1 segment/s of the
number's ~3/s for the private monitor. Wall time = recipients x segments / rate: 72 ACTIVE is about 36 s at 1 segment
and 72 s at 2 segments, plus the 10 s delay. The rate is shared, not per worker: every send (and retry backoff)
first reserves a slot on the ops item `fanout#rate`/`slot` with a compare-and-set on `next_at`, so two workers running
back-to-back alerts together still send at 2 segments/s in total. Runs that would pass the Lambda timeout hand off to a continuation job
(cursor = last processed active-index key); send rows make that safe.

Twilio errors: 429 and 503 retry up to 3 attempts with 2 s / 4 s backoff; timeouts and other ambiguous errors are
**not** retried (the message may have been accepted) and the row is `UNKNOWN`; 21610 opts the number out of every
campaign through the same path as the status webhook (`CARRIER_21610`, audit `carrier_21610`). Every send passes
`StatusCallback=/api/twilio/status` with no query string (CloudFront drops query strings on `/api/*`, which would
break the signature check). The worker writes a `sid#<MessageSid>`/`map` row pointing at the send row, and final
statuses (`delivered`, `undelivered`, `failed`) land on the send row. A crash between claim and send leaves the row `CLAIMED`; that person is not retried (at most once).

### Runbook

All commands use `--profile default --region us-east-1` (or `export AWS_PROFILE=default AWS_REGION=us-east-1`).
`OPS_KEY='{"pk":{"S":"config#global"},"sk":{"S":"current"}}'`. Check the flags at any time:

```bash
aws dynamodb get-item --table-name mall-rat-alerts-ops --key "$OPS_KEY" --consistent-read \
  --projection-expression 'fanout_mode,broadcast_enabled,waitlist_mode,fanout_paused,fanout_allowlist'
```

**1. Dry run with a synthetic alert** (no SMS possible: broadcast stays false).

```bash
aws dynamodb update-item --table-name mall-rat-alerts-ops --key "$OPS_KEY" \
  --update-expression 'SET fanout_mode = :d' --condition-expression 'broadcast_enabled = :f' \
  --expression-attribute-values '{":d":{"S":"dry_run"},":f":{"BOOL":false}}'
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ); EID="fanout-dryrun-test-$(date +%s)"
cat > /tmp/ev.json <<EOF
{"id":"$EID","source":"fob-site-monitor.test","detail-type":"SiteChange","time":"$NOW","detail":{"campaign_id":"fob-twilight-town-mall-2026","alert_kind":"change","detected_at":"$NOW","test":true,"changes":[{"url":"https://www.falloutboy.com/fanout-dry-run-test","status":{"before":404,"after":200}}]}}
EOF
aws lambda invoke --function-name mall-rat-alerts-alert-composer --cli-binary-format raw-in-base64-out \
  --payload file:///tmp/ev.json /tmp/out.json && cat /tmp/out.json        # note sent_at
# ~15 s later:
aws logs filter-log-events --log-group-name /aws/lambda/mall-rat-alerts-fanout-worker \
  --start-time $(( ($(date +%s) - 300) * 1000 )) --filter-pattern '"would send"' --query 'events[].message'
aws dynamodb update-item --table-name mall-rat-alerts-ops --key "$OPS_KEY" \
  --update-expression 'SET fanout_mode = :o' --expression-attribute-values '{":o":{"S":"off"}}'
# clean up (SENT_AT from /tmp/out.json)
aws dynamodb delete-item --table-name mall-rat-alerts-alerts --condition-expression 'is_test = :t' \
  --expression-attribute-values '{":t":{"BOOL":true}}' \
  --key '{"campaign_id":{"S":"fob-twilight-town-mall-2026"},"sent_at":{"S":"SENT_AT"}}'
aws dynamodb delete-item --table-name mall-rat-alerts-alerts --key "{\"campaign_id\":{\"S\":\"src#$EID\"},\"sent_at\":{\"S\":\"marker\"}}"
```

**2. First controlled live test: your own number only.** Replace `+1YOURNUMBER` (E.164). Your number must be an ACTIVE
member of the campaign (text FOB to the number first if it is not). The test alert is stored directly with
`is_test=true` and clearly labelled text, so live mode could never send it to everyone, and the job is enqueued by hand.

```bash
# a) allowlist = only you, mode allowlist (only while broadcast is still off)
aws dynamodb update-item --table-name mall-rat-alerts-ops --key "$OPS_KEY" \
  --update-expression 'SET fanout_allowlist = :l, fanout_mode = :m' --condition-expression 'broadcast_enabled = :f' \
  --expression-attribute-values '{":l":{"L":[{"S":"+1YOURNUMBER"}]},":m":{"S":"allowlist"},":f":{"BOOL":false}}'
# b) broadcast on (only if the mode really is allowlist)
aws dynamodb update-item --table-name mall-rat-alerts-ops --key "$OPS_KEY" \
  --update-expression 'SET broadcast_enabled = :t' --condition-expression 'fanout_mode = :m' \
  --expression-attribute-values '{":t":{"BOOL":true},":m":{"S":"allowlist"}}'
# c) synthetic, clearly labelled test alert + one job
SENT_AT=$(date -u +%Y-%m-%dT%H:%M:%S.000Z)
aws dynamodb put-item --table-name mall-rat-alerts-alerts --condition-expression 'attribute_not_exists(sent_at)' --item "{
  \"campaign_id\":{\"S\":\"fob-twilight-town-mall-2026\"},\"sent_at\":{\"S\":\"$SENT_AT\"},\"created_at\":{\"S\":\"$SENT_AT\"},
  \"status\":{\"S\":\"published\"},\"is_test\":{\"BOOL\":true},\"backfilled\":{\"BOOL\":false},
  \"what_changed\":{\"S\":\"TEST ONLY: fan-out test, please ignore.\"},\"note\":{\"S\":\"Thnks fr th mmrs.\"},
  \"message\":{\"S\":\"TEST ONLY: fan-out test, please ignore.\"}}"
aws sqs send-message --queue-url https://sqs.us-east-1.amazonaws.com/018908982481/mall-rat-alerts-fanout \
  --delay-seconds 10 --message-body "{\"v\":1,\"campaign_id\":\"fob-twilight-town-mall-2026\",\"sent_at\":\"$SENT_AT\",\"seq\":0}"
# d) expect exactly one text; the worker logs {"fanout": "completed", ... "sent": 1}
aws logs filter-log-events --log-group-name /aws/lambda/mall-rat-alerts-fanout-worker \
  --start-time $(( ($(date +%s) - 300) * 1000 )) --filter-pattern '"fanout"' --query 'events[].message'
# e) broadcast off again, mode off, allowlist emptied
aws dynamodb update-item --table-name mall-rat-alerts-ops --key "$OPS_KEY" \
  --update-expression 'SET broadcast_enabled = :f, fanout_mode = :o, fanout_allowlist = :e' \
  --expression-attribute-values '{":f":{"BOOL":false},":o":{"S":"off"},":e":{"L":[]}}'
# f) optional cleanup of the test alert (sent rows expire after 90 days)
aws dynamodb delete-item --table-name mall-rat-alerts-alerts --condition-expression 'is_test = :t' \
  --expression-attribute-values '{":t":{"BOOL":true}}' \
  --key "{\"campaign_id\":{\"S\":\"fob-twilight-town-mall-2026\"},\"sent_at\":{\"S\":\"$SENT_AT\"}}"
```

**3. Go fully live** (only after TFV for this sender is sorted out; see Open TODOs). All three flags are required;
`waitlist_mode=false` also drops "Alerts start at launch." from the keyword welcome. The next real SiteChange fans out
to every ACTIVE subscriber, 10 s after the private texts.

```bash
aws dynamodb update-item --table-name mall-rat-alerts-ops --key "$OPS_KEY" \
  --update-expression 'SET fanout_mode = :l, broadcast_enabled = :t, waitlist_mode = :f, fanout_paused = :f, fanout_allowlist = :e' \
  --expression-attribute-values '{":l":{"S":"live"},":t":{"BOOL":true},":f":{"BOOL":false},":e":{"L":[]}}'
```

**Kill switch** (stops a running fan-out before its next text; the rest is parked in the DLQ):

```bash
aws dynamodb update-item --table-name mall-rat-alerts-ops --key "$OPS_KEY" \
  --update-expression 'SET fanout_paused = :t' --expression-attribute-values '{":t":{"BOOL":true}}'
```

Resume: set `fanout_paused` to false, then move parked jobs back (people already texted are skipped; parked
alerts older than 120 minutes are refused instead of sent):

```bash
aws sqs start-message-move-task --source-arn arn:aws:sqs:us-east-1:018908982481:mall-rat-alerts-fanout-dlq \
  --destination-arn arn:aws:sqs:us-east-1:018908982481:mall-rat-alerts-fanout
```

Or drop the parked work for good: `aws sqs purge-queue --queue-url https://sqs.us-east-1.amazonaws.com/018908982481/mall-rat-alerts-fanout-dlq`.

**Turn it off** (no new jobs are enqueued; a run in progress parks before its next text):

```bash
aws dynamodb update-item --table-name mall-rat-alerts-ops --key "$OPS_KEY" \
  --update-expression 'SET fanout_mode = :o, broadcast_enabled = :f' \
  --expression-attribute-values '{":o":{"S":"off"},":f":{"BOOL":false}}'
```

Alarms (no notification target yet): `mall-rat-alerts-fanout-dlq-not-empty`, `mall-rat-alerts-fanout-worker-errors`.
Back-to-back alerts: up to two jobs run at once (reserved concurrency equals the event source mapping's maximum
concurrency, so jobs are never throttled into the 330 s visibility timeout), each starting about 10 s after its alert,
sharing the one rate limiter. A third concurrent job waits in the queue until a worker is free.

## Discord channel posts

Every real published alert is also posted to a public Discord channel. It is a mirror only: it runs in the composer
after the alert is stored and after `fanout_hook()` returns, inside a try/except, so it cannot delay or affect SMS.
Fan-out gating, pacing and the priority delay are untouched.

- **Where the webhook lives:** Secrets Manager secret `mall-rat-alerts/secrets`, key `discord_webhook_twilight_town_mall`
  (next to the Twilio keys). The composer role has read-only `secretsmanager:GetSecretValue` on that one secret.
  The URL is a password: never paste it in chat, logs, Terraform or git. The local copy is `.env` key
  `TWILIGHT_TOWN_WEB_HOOK` (gitignored).
- **Mapping:** Terraform variable `discord_channels` (campaign_id -> secret key name, in `terraform/alerts.tf`), passed to
  the composer as `DISCORD_CHANNELS`. Unmapped campaigns are logged and skipped.
- **What is posted:** an embed titled `Twilight Town Mall` with the public alert text and an `Alert <sent_at>` footer.
  No phone numbers or counts. `allowed_mentions` is empty so text can never ping anyone.
- **When:** only for alerts with `status = published`. Never for test events (`test=true` or source ending `.test`) and
  never for dry runs (event `dry_run=true` or ops `fanout_mode = dry_run`). Posting does not depend on the other ops flags.
- **Safety:** 5 s timeout; every failure logs only a status code or exception type and is swallowed; a Discord 429 is
  retried once if `retry_after` is 10 s or less. The alert row gets `discord_posted_at` via a conditional update before
  the post, so a retry or duplicate delivery never double-posts. A definite HTTP failure clears the marker so a retry can
  post; a timeout keeps it (at most once). The URL is cached per warm container; a 401/404 drops the cache.

**Rotate the webhook:**

1. In Discord: channel settings, Integrations, Webhooks, delete the old webhook, create a new one, copy its URL.
2. Put the new URL in `.env` as `TWILIGHT_TOWN_WEB_HOOK`.
3. Update only that key in the secret (read the JSON, set the key, write it back, keep the Twilio keys). Do it from a script
   that reads `.env` and passes the JSON through a mode-600 temp file deleted right away (see how
   `scripts/load_twilio_secret.py` does it); never echo the value.
4. Warm composer containers cache the old URL until they recycle or hit a 401/404. To refresh immediately, touch the
   composer config, for example `aws lambda update-function-configuration --function-name mall-rat-alerts-alert-composer
   --description "rotate $(date +%s)"`.
5. Verify with a one-off call of `common.discord.post_public_alert` with a clearly labeled test text. It prints a status of 204.

**Add a release (new campaign) with its own channel:** create the Discord webhook, add a key such as
`discord_webhook_<release>` to the secret as above, add `"<campaign_id>" = "discord_webhook_<release>"` to
`discord_channels` (optionally a title in `TITLES` in `common/discord.py`), then apply with a target on
`aws_lambda_function.composer`.

**Turn Discord posting off:** remove the mapping (set `discord_channels = {}`) and apply the composer. Nothing else changes.
The secret key can stay or be deleted.

## Homepage alert feed

The homepage "Recent alerts" list calls `/api/alerts?campaign_id=fob-twilight-town-mall-2026&limit=20`
and renders with `textContent` (Pacific times via `Intl`). Empty and error states show plain messages,
and `<noscript>` explains the list needs JavaScript. Site assets cache for an hour; `scripts/deploy.sh`
creates a CloudFront invalidation after each apply.

Note: `fob-site-monitor` still sends the VIP alerts from this same number. Replies to those alerts
now reach this webhook (they used to go nowhere). A reply like "lol" gets at most one
"couldn't find a signup" text a day, and STOP is recorded here as well as by Twilio.

## PostHog

Mirrors Versiful: a runtime `config.json` (Terraform-generated, like Versiful's `public/config.json`
`posthog.apiKey`), the US cloud host, and a consent notice whose "No thanks" opts out. Differences
for a fan site: **cookieless** (`persistence: "memory"`, nothing stored except the notice choice),
no autocapture, no session recording or surveys, respects Do Not Track and Global Privacy Control,
and never sends phone numbers or form input.

Enable/disable: put `POSTHOG_KEY=phc_...` and `POSTHOG_HOST=https://us.i.posthog.com` in
`mall-rat-alerts/.env` (gitignored, mode 600), then `scripts/deploy.sh`. An empty key ships analytics off
(config.json has an empty key and the CSP drops PostHog hosts).

LLM tracing: the composer sends `$ai_generation` (one per Bedrock attempt, fallbacks included) and one
`$ai_trace` per alert, `app = mall-rat-alerts`, `distinct_id = mall-rat-alerts-composer`. View under
**LLM analytics > Traces / Generations**. Phone-like strings are scrubbed to `[phone]`; send failures
never affect alerts.

Events (all carry `campaign_id`): `$pageview` on every page, `subscribe_submit`, `subscribe_success`,
`subscribe_error` (`error` code; all three carry `device=desktop`, never the phone), `subscribe_sms_click` (`device=mobile`), `unsubscribe_submit`, `unsubscribe_success`, `unsubscribe_error`.
Server-side (Lambda, `distinct_id` = salted hash, `$process_person_profile=false`): `server_subscribe`,
`server_unsubscribe`, `server_sms_inbound` (`keyword`, `result`), `sms_subscribe` (`tag`, `campaign_id`, `source=sms`). In the PostHog project settings,
consider turning on "Discard client IP data".

Automated browsers (`navigator.webdriver`) are treated as bots by posthog-js and send nothing, so test
with a normal browser and check PostHog Live Events.

## Deploy and test

```bash
python3 -m unittest discover -s tests          # 257 tests
scripts/deploy.sh                              # fmt -check, validate, apply (prompts)
python3 scripts/smoke_inbound.py --base https://mallratalerts.com HELP
```

Kill switches:

```bash
aws dynamodb update-item --table-name mall-rat-alerts-ops \
  --key '{"pk":{"S":"config#global"},"sk":{"S":"current"}}' \
  --update-expression 'SET signups_enabled = :f' --expression-attribute-values '{":f":{"BOOL":false}}'
```

Delete a test record: `aws dynamodb delete-item --table-name mall-rat-alerts-subscribers --key '{"phone":{"S":"+15005550006"},"campaign_id":{"S":"fob-twilight-town-mall-2026"}}'`

Destroying the stack requires `deletion_protection = false` first (subscribers and audit).

## Domain: mallratalerts.com (live)

Registered 2026-11-06 in Route 53 Domains ($16/yr), 1 year, auto-renew on, WHOIS privacy on,
expires 2027-11-06. Hosted zone `Z0967302NU5M3XL3C6WV`. ACM cert (us-east-1, apex + www, DNS-validated,
auto-renews) `arn:aws:acm:us-east-1:018908982481:certificate/888962d0-39da-4ffb-a896-6e70464b0170`.
`enable_custom_domain` now defaults to `true` in `terraform/variables.tf`, so `scripts/deploy.sh` keeps
the domain. Setting it false would remove the aliases and the cert and break Twilio signature checks.

`hello@mallratalerts.com` is live via SES inbound; see "Support email (SES inbound)" below.

Steps that were followed:

1. Write `contact.json` (Route 53 `ContactDetail`: FirstName, LastName, ContactType `PERSON` or
   `COMPANY`, OrganizationName if company, AddressLine1, City, State, CountryCode `US`, ZipCode,
   PhoneNumber `+1.5555555555`, Email). Use the operator entity, not a home address if avoidable.
2. Register with WHOIS privacy:
   ```bash
   aws route53domains register-domain --region us-east-1 --profile default \
     --domain-name mallratalerts.com --duration-in-years 1 --auto-renew \
     --admin-contact file://contact.json --registrant-contact file://contact.json \
     --tech-contact file://contact.json --privacy-protect-admin-contact \
     --privacy-protect-registrant-contact --privacy-protect-tech-contact
   aws route53domains get-operation-detail --region us-east-1 --operation-id <id>   # wait for SUCCESSFUL
   ```
   Route 53 creates the hosted zone ($0.50/month). Confirm the registrant email verification link.
3. `terraform apply -var enable_custom_domain=true` (adds the ACM cert, DNS validation, CloudFront
   aliases for apex and www, and A/AAAA records). This also changes `TWILIO_WEBHOOK_BASE`, so right after:
   `python3 scripts/set_twilio_webhook.py --number +18335961548 --url https://mallratalerts.com/api/twilio/inbound --force`
4. Support email: see the next section.

## Support email (SES inbound)

`hello@mallratalerts.com` is received by Amazon SES (us-east-1), stored in S3, and forwarded by a Lambda
to `chrislmesser@gmail.com`. All of it lives in `terraform/mail.tf`; the forwarder is `lambda/mail_forwarder.py`.

```
sender -> MX 10 inbound-smtp.us-east-1.amazonaws.com -> SES active rule set INBOUND_MAIL, rule "hello"
  1. S3 action: s3://mall-rat-alerts-inbound-018908982481/hello/<messageId>  (private, SSE-S3, 30-day expiry)
  2. Lambda action (async): mall-rat-alerts-mail-forwarder -> SES SendRawEmail -> FORWARD_TO
```

Done and verified: SES domain identity `mallratalerts.com` (verification TXT at `_amazonses`, Easy DKIM with
3 CNAMEs, DKIM signing on), bucket and policy, Lambda, IAM, invoke permission, receipt rule with scan on.

Forwarder behavior: From becomes `hello@mallratalerts.com`; Reply-To is the original Reply-To or From;
Subject gets `[hello@] ` once; Return-Path, Sender and DKIM-Signature are dropped (invalid after the From
change); the body is passed through byte for byte. Dropped without forwarding: Auto-Submitted (other than
`no`), null Return-Path, our own `X-Mall-Rat-Forwarded` marker, our own domain as sender, SES virus verdict
FAIL. SES stamps a Return-Path on every stored message, so only an empty one counts as a loop signal.
Logs hold only the SES message ID and sender domain. Env: `FORWARD_TO`, `FROM_ADDRESS`, `INBOUND_BUCKET`,
`KEY_PREFIX`, `DRY_RUN` (true = parse and rewrite, never send).

**Merged into Versiful's rule set (live 2026-11-06).** SES allows one active receipt rule set per region, and
this account's active set is `INBOUND_MAIL` (Versiful WorkMail rule `m-3951844311cf4406a7982573f536e2b0` for
`versiful.io` and `versiful.awsapps.com`). Rather than activate a separate set, the `hello` rule was added to
`INBOUND_MAIL`, placed after the Versiful rule (`after` in Terraform). Its only recipient is
`hello@mallratalerts.com`, so the two rules never match the same mail. Terraform manages only the `hello`
rule (`aws_ses_receipt_rule.support`), not the `INBOUND_MAIL` set, its activation, or the Versiful rule.
The bucket policy and Lambda permission are scoped to
`arn:aws:ses:us-east-1:018908982481:receipt-rule-set/INBOUND_MAIL:receipt-rule/hello`.
The earlier inactive `mall-rat-alerts-inbound` set was deleted.

Backup: `docs/ses-inbound-mail-backup.json` is `INBOUND_MAIL` exactly as it was before the merge (Versiful rule
only). After the merge the Versiful rule was compared against it and is identical.

Removing the hello rule without affecting Versiful (either one):

```bash
cd terraform && terraform destroy -lock-timeout=15m -target=aws_ses_receipt_rule.support -target=aws_route53_record.mx
aws ses delete-receipt-rule --region us-east-1 --rule-set-name INBOUND_MAIL --rule-name hello   # without Terraform
```

Never delete, re-create, or deactivate `INBOUND_MAIL` itself; that would stop Versiful's mail.

Terraform note: other work in this project can leave unrelated pending changes, so the mail resources were
applied with `-target`. A plain `terraform plan` will show whatever else is pending.

Sandbox: the SES account is in the sandbox (200/day, verified recipients only). `chrislmesser@gmail.com` is
already a verified SES identity (no confirmation click needed). To forward anywhere else, verify that address
first or request production access.

End-to-end test: from your own address send a message to
`hello@mallratalerts.com`; it should arrive at `chrislmesser@gmail.com` within about a minute with subject
`[hello@] ...` and Reply-To set to you. Check `/aws/lambda/mall-rat-alerts-mail-forwarder` for
`forwarded message_id=... sender_domain=...`, and `aws s3 ls s3://mall-rat-alerts-inbound-018908982481/hello/`.

## Open TODOs before public launch

- Support email: run the end-to-end test above once to confirm delivery.
- Operator legal entity, address, and governing state: replace `[OPERATOR LEGAL NAME]`,
  `[OPERATOR ADDRESS]`, `[GOVERNING STATE]` in `/privacy`, `/terms`, `/sms-terms`. EIN for TFV must match.
- Sender: the number is Versiful's staging toll-free on the **main** Twilio account (shared with Versiful
  prod), and its toll-free verification (if any) describes Versiful's use case. Before any public alert
  traffic: a Twilio subaccount and a new toll-free number with its **own TFV**, or a fresh TFV for this
  number under the Mall Rat Alerts brand. Move to subaccount credentials (API key for REST, subaccount
  token for signatures) in `mall-rat-alerts/secrets`.
- Messaging Service with Advanced Opt-Out (branded STOP/HELP/START copy; remove YES from opt-in keywords),
  Geo Permissions US only, Usage Triggers.
- Turnstile: create the widget, store `{"turnstile_secret": "..."}` in a secret, then apply with
  `turnstile_enabled=true turnstile_site_key=<key> turnstile_secret_arn=<arn>`.
- Stripe tip link (out of scope this phase).
- Opt-in screenshot for TFV (`site/img/opt-in-form.png`).
- Empty `test_numbers` before launch.
- "Unsubscribe from all campaigns" on the web (Phase 1 is per-campaign; STOP covers all).
- Phase 0 decisions from the design doc: entity/BRN, budget and `max_subscribers`, public message
  format, staging-mirror policy, VoIP policy, minimum age, Twilio isolation, overnight alerts, sunset.
- Have a lawyer glance at the legal copy.

## Differences from the design doc

- Brand and domain: Mall Rat Alerts / mallratalerts.com instead of Thnks fr th Txts.
- Subscribers are keyed `phone` + `campaign_id` (multi-campaign), not `phone` alone; status
  `OPTED_OUT` instead of `UNSUBSCRIBED`; IP and user agent are stored only as salted hashes.
- No Twilio Lookup, no confirmation or goodbye sends (Phase 2/3). YES confirmation works now for anyone
  who texts in, but the welcome says alerts start at launch.
- Geo check is done in the Lambda (from `CloudFront-Viewer-Country`) rather than a CloudFront Function.
- The Lambda also has `dynamodb:Query` on subscribers (inbound SMS needs every membership for a phone).
- `+15005550006` (Twilio magic number) is allowlisted as a test number for verification.
