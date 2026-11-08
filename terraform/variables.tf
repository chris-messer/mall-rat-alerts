variable "name" {
  description = "Prefix for every resource in this stack."
  type        = string
  default     = "mall-rat-alerts"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "aws_profile" {
  type    = string
  default = "default"
}

# ------------------------------------------------------------------ domain

variable "domain_name" {
  description = "Intended brand domain. Only bound to CloudFront (ACM + Route 53) when enable_custom_domain = true."
  type        = string
  default     = "mallratalerts.com"
}

variable "enable_custom_domain" {
  description = "Binds domain_name to CloudFront (ACM + Route 53). The domain is registered and live; setting this false removes the aliases and breaks Twilio signature checks."
  type        = bool
  default     = true
}

# ---------------------------------------------------------------- campaign

variable "campaign" {
  description = "The current campaign, seeded into the ops table as config#campaign#<campaign_id>."
  type = object({
    campaign_id     = string
    campaign_name   = string
    campaign_tags   = list(string)
    source_site     = string
    campaign_status = string
  })
  default = {
    campaign_id     = "fob-twilight-town-mall-2026"
    campaign_name   = "Twilight Town Mall"
    campaign_tags   = ["fob", "twilight-town-mall", "2026"]
    source_site     = "https://www.falloutboy.com"
    campaign_status = "active"
  }
}

variable "consent_version" {
  description = "Must match the hidden consent_version field on the landing page and a file in site/consent/."
  type        = string
  default     = "sms-consent-2026-11-06.v2"
}

# -------------------------------------------------------------- switches

variable "waitlist_mode" {
  type    = bool
  default = true
}

variable "broadcast_enabled" {
  type    = bool
  default = false
}

variable "twilio_from_number" {
  description = "Campaign number that sends the one web-signup confirmation text (E.164). Same number as the inbound webhook."
  type        = string
  default     = "+18335961548"
}

variable "test_numbers" {
  description = "Numbers that bypass the US area-code allowlist (flagged is_test). Empty this before public launch."
  type        = list(string)
  default     = ["+15005550006"]
}

# -------------------------------------------------------------- turnstile

variable "turnstile_enabled" {
  type    = bool
  default = false
}

variable "turnstile_site_key" {
  type    = string
  default = "TURNSTILE_SITE_KEY_PLACEHOLDER"
}

variable "turnstile_secret_arn" {
  description = "Secrets Manager secret (created by hand) holding {\"turnstile_secret\": \"...\"}. Empty until Turnstile is set up."
  type        = string
  default     = ""
}

# ----------------------------------------------------------------- twilio

variable "twilio_number" {
  description = "Sender whose SMS webhook points at /api/twilio/inbound (set by scripts/set_twilio_webhook.py, not Terraform)."
  type        = string
  default     = "+18335961548"
}

variable "twilio_token_key" {
  description = "JSON key in the mall-rat-alerts/secrets secret holding the Twilio auth token used for signature checks."
  type        = string
  default     = "twilio_auth_token"
}

variable "support_email" {
  type    = string
  default = "hello@mallratalerts.com"
}

# ---------------------------------------------------------------- posthog

variable "posthog_key" {
  description = "PostHog project API key (phc_..., public by design). Empty = analytics off. Set via TF_VAR_posthog_key from .env."
  type        = string
  default     = ""
}

variable "posthog_host" {
  type    = string
  default = "https://us.i.posthog.com"
}

variable "stripe_payment_link" {
  description = "Public Stripe Payment Link URL for the tip jar. Empty = no tip button. Set via TF_VAR_stripe_payment_link from .env. Never put Stripe API keys here."
  type        = string
  default     = ""
  validation {
    condition     = var.stripe_payment_link == "" || can(regex("^https://(buy|donate)\\.stripe\\.com/[A-Za-z0-9_]+$", var.stripe_payment_link))
    error_message = "stripe_payment_link must be empty or a https://buy.stripe.com/ or https://donate.stripe.com/ URL."
  }
}

variable "discord_invite_url" {
  description = "Public Discord invite URL for the homepage Join the Discord CTA. Empty = hide Discord CTA and leave SMS open. Set via TF_VAR_discord_invite_url from .env."
  type        = string
  default     = ""
  validation {
    condition = var.discord_invite_url == "" || can(regex("^https://discord\\.(gg|com/invite)/[A-Za-z0-9_-]+/?$", var.discord_invite_url))
    error_message = "discord_invite_url must be empty or start with https://discord.gg/ or https://discord.com/invite/."
  }
}

# ------------------------------------------------------------ throttling

variable "lambda_reserved_concurrency" {
  type    = number
  default = 5
}

variable "api_throttle_rate" {
  description = "API Gateway steady-state requests per second."
  type        = number
  default     = 5
}

variable "api_throttle_burst" {
  type    = number
  default = 5
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "deletion_protection" {
  description = "DynamoDB deletion protection on subscribers and audit."
  type        = bool
  default     = true
}
