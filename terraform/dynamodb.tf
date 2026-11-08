# One membership per phone per campaign. The same phone can join several campaigns over time.
resource "aws_dynamodb_table" "subscribers" {
  name                        = "${var.name}-subscribers"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "phone"
  range_key                   = "campaign_id"
  deletion_protection_enabled = var.deletion_protection

  attribute {
    name = "phone"
    type = "S"
  }
  attribute {
    name = "campaign_id"
    type = "S"
  }
  attribute {
    name = "gsi1pk"
    type = "S"
  }
  attribute {
    name = "gsi1sk"
    type = "S"
  }

  # List members of a campaign.
  global_secondary_index {
    name               = "campaign-index"
    hash_key           = "campaign_id"
    range_key          = "phone"
    projection_type    = "INCLUDE"
    non_key_attributes = ["status"]
  }

  # Sparse: gsi1pk = "ACTIVE#<campaign_id>" is set only while ACTIVE (Phase 3 fan-out).
  global_secondary_index {
    name               = "active-index"
    hash_key           = "gsi1pk"
    range_key          = "gsi1sk"
    projection_type    = "INCLUDE"
    non_key_attributes = ["tier", "last_optout_reminder_at"]
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }
}

resource "aws_dynamodb_table" "audit" {
  name                        = "${var.name}-audit"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "phone"
  range_key                   = "sk"
  deletion_protection_enabled = var.deletion_protection

  attribute {
    name = "phone"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  attribute {
    name = "campaign_id"
    type = "S"
  }

  global_secondary_index {
    name               = "campaign-index"
    hash_key           = "campaign_id"
    range_key          = "sk"
    projection_type    = "INCLUDE"
    non_key_attributes = ["event_type"]
  }

  point_in_time_recovery {
    enabled = true
  }
}

resource "aws_dynamodb_table" "ops" {
  name         = "${var.name}-ops"
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

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

# Global kill switches. Terraform seeds it once; later edits via the CLI are kept.
resource "aws_dynamodb_table_item" "config_global" {
  table_name = aws_dynamodb_table.ops.name
  hash_key   = aws_dynamodb_table.ops.hash_key
  range_key  = aws_dynamodb_table.ops.range_key
  item = jsonencode({
    pk                        = { S = "config#global" }
    sk                        = { S = "current" }
    signups_enabled           = { BOOL = true }
    waitlist_mode             = { BOOL = var.waitlist_mode }
    broadcast_enabled         = { BOOL = var.broadcast_enabled }
    max_public_alerts_per_day = { N = "6" }
    max_subscribers           = { N = "500" }
    web_confirm_enabled       = { BOOL = true }
    web_confirm_daily_cap     = { N = "50" }
    web_confirm_dry_run       = { BOOL = false }
    # SMS fan-out (fanout.tf adds these to an already-seeded item without overwriting).
    fanout_mode                     = { S = "off" }
    fanout_allowlist                = { L = [] }
    fanout_paused                   = { BOOL = false }
    fanout_max_recipients_per_alert = { N = "200" }
    fanout_delay_seconds            = { N = "10" }
    fanout_rate_per_second          = { N = "2" }
    fanout_max_alert_age_minutes    = { N = "120" }
  })

  lifecycle {
    ignore_changes = [item]
  }
}

resource "aws_dynamodb_table_item" "config_campaign" {
  table_name = aws_dynamodb_table.ops.name
  hash_key   = aws_dynamodb_table.ops.hash_key
  range_key  = aws_dynamodb_table.ops.range_key
  item = jsonencode({
    pk              = { S = "config#campaign#${var.campaign.campaign_id}" }
    sk              = { S = "current" }
    campaign_id     = { S = var.campaign.campaign_id }
    campaign_name   = { S = var.campaign.campaign_name }
    campaign_tags   = { L = [for t in var.campaign.campaign_tags : { S = t }] }
    source_site     = { S = var.campaign.source_site }
    campaign_status = { S = var.campaign.campaign_status }
    signups_enabled = { BOOL = true }
  })
}
