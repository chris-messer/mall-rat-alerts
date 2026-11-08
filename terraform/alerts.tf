# Public alert pipeline: templates + tags table, alerts table, EventBridge rule, composer Lambda.
# The monitor (fob-site-monitor) publishes SiteChange events on the default bus. This stack only listens.

variable "model_ids" {
  description = "Bedrock models the composer tries in order (a template's model_ids field overrides this)."
  type        = list(string)
  default = [
    "us.amazon.nova-2-lite-v1:0",
    "us.amazon.nova-pro-v1:0",
    "us.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
  ]
}

variable "discord_channels" {
  description = "campaign_id -> key in the mall-rat-alerts/secrets secret holding that campaign's Discord webhook URL. Names only, never URLs. An empty map turns Discord posting off."
  type        = map(string)
  default = {
    "fob-twilight-town-mall-2026" = "discord_webhook_twilight_town_mall"
  }
}

# ----------------------------------------------------------------- tables

# Editable copy and mappings, read live by the composer and the inbound SMS handler:
#   pk "template#public#<campaign_id>" / sk "current"   public alert template
#   pk "config#tags" / sk "<TAG>"                        text-to-join tag -> campaign + welcome message
resource "aws_dynamodb_table" "templates" {
  name         = "${var.name}-templates"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}

# Alerts shown on the homepage. PK campaign_id, SK sent_at (ISO UTC, newest first via reverse query).
# Idempotency markers use campaign_id = "src#<source event id>" and expire by TTL.
resource "aws_dynamodb_table" "alerts" {
  name                        = "${var.name}-alerts"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "campaign_id"
  range_key                   = "sent_at"
  deletion_protection_enabled = var.deletion_protection

  attribute {
    name = "campaign_id"
    type = "S"
  }
  attribute {
    name = "sent_at"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }
}

# Seeded once. Edit live with scripts/template.py (ignore_changes keeps your edits).
resource "aws_dynamodb_table_item" "public_template" {
  table_name = aws_dynamodb_table.templates.name
  hash_key   = aws_dynamodb_table.templates.hash_key
  range_key  = aws_dynamodb_table.templates.range_key
  item = jsonencode({
    pk            = { S = "template#public#${var.campaign.campaign_id}" }
    sk            = { S = "current" }
    system_prompt = { S = trimspace(file("${path.module}/../lambda/common/default_public_prompt.txt")) }
    max_chars     = { N = "306" }
    target_chars  = { N = "160" }
    temperature   = { N = "0.2" }
    max_tokens    = { N = "500" }
    version       = { N = "1" }
    updated_at    = { S = "2026-11-06T18:00:00Z" }
    updated_by    = { S = "terraform-seed" }
    fallback_note = { S = "Thnks fr th mmrs." }
  })

  lifecycle {
    ignore_changes = [item]
  }
}

resource "aws_dynamodb_table_item" "tag_fob" {
  table_name = aws_dynamodb_table.templates.name
  hash_key   = aws_dynamodb_table.templates.hash_key
  range_key  = aws_dynamodb_table.templates.range_key
  item = jsonencode({
    pk            = { S = "config#tags" }
    sk            = { S = "FOB" }
    tag           = { S = "FOB" }
    aliases       = { L = [{ S = "FOB" }, { S = "FALLOUTBOY" }] }
    campaign_id   = { S = var.campaign.campaign_id }
    campaign_name = { S = var.campaign.campaign_name }
    enabled       = { BOOL = true }
    version       = { N = "1" }
    welcome_message = { S = join("\n\n", [
      "Mall Rat Alerts: You're in for Twilight Town Mall (Fall Out Boy site alerts).",
      "We'll text when the site changes. Msg freq varies. Msg&data rates may apply. Reply STOP to opt out, HELP for help. Unofficial fan project.",
    ]) }
  })

  lifecycle {
    ignore_changes = [item]
  }
}

# Web double opt-in confirmation text. Editable live (ignore_changes keeps edits); the Lambda falls back to
# built-in copy if this is missing, not GSM-7, over 2 segments, or lacks STOP.
resource "aws_dynamodb_table_item" "web_confirm" {
  table_name = aws_dynamodb_table.templates.name
  hash_key   = aws_dynamodb_table.templates.hash_key
  range_key  = aws_dynamodb_table.templates.range_key
  item = jsonencode({
    pk          = { S = "config#web_confirm" }
    sk          = { S = var.campaign.campaign_id }
    campaign_id = { S = var.campaign.campaign_id }
    enabled     = { BOOL = true }
    version     = { N = "1" }
    message     = { S = "Mall Rat Alerts: Reply YES to confirm Twilight Town Mall (Fall Out Boy site) alerts. Msg freq varies. Msg&data rates may apply. Reply STOP to cancel, HELP for help. Unofficial fan project." }
  })

  lifecycle {
    ignore_changes = [item]
  }
}

# ----------------------------------------------------------------- composer

resource "aws_iam_role" "composer" {
  name = "${var.name}-alert-composer"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "composer" {
  name = "${var.name}-alert-composer"
  role = aws_iam_role.composer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.composer.arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = [aws_dynamodb_table.templates.arn, aws_dynamodb_table.ops.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.alerts.arn
      },
      {
        # Discord webhook URLs live in this secret (key discord_webhook_*). Only this secret, read only.
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.twilio.arn
      },
      {
        Effect = "Allow"
        Action = ["bedrock:InvokeModel"]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/anthropic.*",
          "arn:aws:bedrock:*::foundation-model/amazon.*",
          "arn:aws:bedrock:${var.region}:${local.account_id}:inference-profile/*",
        ]
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "composer" {
  name              = "/aws/lambda/${var.name}-alert-composer"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "composer" {
  function_name    = "${var.name}-alert-composer"
  role             = aws_iam_role.composer.arn
  runtime          = "python3.12"
  handler          = "alert_composer.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 120
  memory_size      = 256

  environment {
    variables = {
      TEMPLATES_TABLE     = aws_dynamodb_table.templates.name
      ALERTS_TABLE        = aws_dynamodb_table.alerts.name
      OPS_TABLE           = aws_dynamodb_table.ops.name
      DEFAULT_CAMPAIGN_ID = var.campaign.campaign_id
      MODEL_IDS           = join(",", var.model_ids)
      BROADCAST_ENABLED   = tostring(var.broadcast_enabled)
      POSTHOG_KEY         = var.posthog_key
      POSTHOG_HOST        = var.posthog_host
      FANOUT_QUEUE_URL    = aws_sqs_queue.fanout.id
      DISCORD_SECRET_ID   = aws_secretsmanager_secret.twilio.arn
      DISCORD_CHANNELS    = jsonencode(var.discord_channels)
    }
  }

  depends_on = [aws_cloudwatch_log_group.composer, aws_iam_role_policy.composer]
}

# Only real monitor events. The "test" guard and the exact source keep synthetic events out of the feed.
resource "aws_cloudwatch_event_rule" "site_change" {
  name        = "${var.name}-site-change"
  description = "fob-site-monitor SiteChange events for the public alert feed"
  # The monitor always sends detail.test (false for real alerts); tests/test_event_pattern.py checks this.
  event_pattern = jsonencode(jsondecode(templatefile("${path.module}/site_change_pattern.json.tftpl", {
    campaign_id = var.campaign.campaign_id
  })))
}

resource "aws_cloudwatch_event_target" "composer" {
  rule = aws_cloudwatch_event_rule.site_change.name
  arn  = aws_lambda_function.composer.arn

  retry_policy {
    maximum_retry_attempts       = 2
    maximum_event_age_in_seconds = 3600
  }
}

resource "aws_lambda_permission" "composer_events" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.composer.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.site_change.arn
}

output "alerts_table" {
  value = aws_dynamodb_table.alerts.name
}

output "templates_table" {
  value = aws_dynamodb_table.templates.name
}

output "composer_lambda" {
  value = aws_lambda_function.composer.function_name
}

output "composer_log_group" {
  value = aws_cloudwatch_log_group.composer.name
}

output "alerts_feed_url" {
  value = "https://${aws_cloudfront_distribution.site.domain_name}/api/alerts?campaign_id=${var.campaign.campaign_id}&limit=50"
}
