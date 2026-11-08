#!/usr/bin/env bash
# Apply this stack. Reads POSTHOG_KEY / POSTHOG_HOST from ../.env (gitignored) if present.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
# Stripe API keys stay out of Terraform; the site only needs the public Payment Link URL.
unset STRIPE_SECRET_KEY STRIPE_PUBLISHABLE_KEY
export TF_VAR_posthog_key="${POSTHOG_KEY:-}"
export TF_VAR_posthog_host="${POSTHOG_HOST:-https://us.i.posthog.com}"
export TF_VAR_stripe_payment_link="${STRIPE_PAYMENT_LINK:-}"
export TF_VAR_discord_invite_url="${DISCORD_INVITE_URL:-}"
if [[ -n "$TF_VAR_posthog_key" ]]; then echo "PostHog: enabled"; else echo "PostHog: off (no POSTHOG_KEY)"; fi
if [[ -n "$TF_VAR_stripe_payment_link" ]]; then echo "Tip jar: $TF_VAR_stripe_payment_link"; else echo "Tip jar: off (no STRIPE_PAYMENT_LINK)"; fi
if [[ -n "$TF_VAR_discord_invite_url" ]]; then echo "Discord CTA: $TF_VAR_discord_invite_url"; else echo "Discord CTA: off (no DISCORD_INVITE_URL)"; fi

cd terraform
terraform fmt -check
terraform validate
terraform apply "$@"

# Site assets are cached for an hour at the edge and in browsers. Invalidate after a successful apply
# so design and copy changes show up right away (the first 1000 paths a month are free).
DIST_ID="$(terraform output -raw cloudfront_distribution_id 2>/dev/null || true)"
if [[ -n "$DIST_ID" ]]; then
  aws --profile "${AWS_PROFILE:-default}" cloudfront create-invalidation --distribution-id "$DIST_ID" \
    --paths "/*" --query Invalidation.Id --output text >/dev/null && echo "CloudFront invalidation requested"
fi
