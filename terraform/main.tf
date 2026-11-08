terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project = var.name
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id    = data.aws_caller_identity.current.account_id
  site_dir      = "${path.module}/../site"
  custom_domain = var.enable_custom_domain && var.domain_name != ""
  site_aliases  = local.custom_domain ? [var.domain_name, "www.${var.domain_name}"] : []
  public_base   = local.custom_domain ? "https://${var.domain_name}" : "https://${aws_cloudfront_distribution.site.domain_name}"

  posthog_assets_host = replace(var.posthog_host, ".i.posthog.com", "-assets.i.posthog.com")
  posthog_csp_hosts   = var.posthog_key == "" ? "" : " ${var.posthog_host} ${local.posthog_assets_host}"
}

# Created empty by Terraform. scripts/load_twilio_secret.py fills it, so the token never lands in TF state.
resource "aws_secretsmanager_secret" "twilio" {
  name                    = "${var.name}/secrets"
  description             = "Mall Rat Alerts Twilio credentials (twilio_account_sid, twilio_auth_token)."
  recovery_window_in_days = 7
}
