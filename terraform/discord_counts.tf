# Scheduled Discord member count pull. No always-on Gateway bot: EventBridge invokes this Lambda
# every 15 minutes, which SETs discord_members / discord_presence on the ops counter item.
# SMS counters stay event-driven and are never written here.

locals {
  discord_count_sync_name = "${var.name}-discord-count-sync"
}

resource "aws_iam_role" "discord_count_sync" {
  name = local.discord_count_sync_name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "discord_count_sync" {
  name = local.discord_count_sync_name
  role = aws_iam_role.discord_count_sync.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.discord_count_sync.arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.ops.arn
      },
      {
        # Bot token lives under discord_bot_token in the shared secrets blob (never in Lambda env).
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.twilio.arn
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "discord_count_sync" {
  name              = "/aws/lambda/${local.discord_count_sync_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "discord_count_sync" {
  function_name    = local.discord_count_sync_name
  role             = aws_iam_role.discord_count_sync.arn
  runtime          = "python3.12"
  handler          = "discord_count_sync.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 30
  memory_size      = 128

  environment {
    variables = {
      OPS_TABLE             = aws_dynamodb_table.ops.name
      DEFAULT_CAMPAIGN_ID   = var.campaign.campaign_id
      DISCORD_GUILD_ID      = var.discord_guild_id
      DISCORD_SECRET_ID     = aws_secretsmanager_secret.twilio.arn
      DISCORD_BOT_TOKEN_KEY = "discord_bot_token"
    }
  }

  depends_on = [aws_cloudwatch_log_group.discord_count_sync, aws_iam_role_policy.discord_count_sync]
}

resource "aws_cloudwatch_event_rule" "discord_count_sync" {
  name                = local.discord_count_sync_name
  description         = "Pull Discord approximate_member_count into the ops counter every 15 minutes"
  schedule_expression = "rate(15 minutes)"
  state               = "ENABLED"
}

resource "aws_cloudwatch_event_target" "discord_count_sync" {
  rule = aws_cloudwatch_event_rule.discord_count_sync.name
  arn  = aws_lambda_function.discord_count_sync.arn
}

resource "aws_lambda_permission" "discord_count_sync_events" {
  statement_id  = "AllowEventBridgeDiscordCountSync"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.discord_count_sync.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.discord_count_sync.arn
}

output "discord_count_sync_lambda" {
  value = aws_lambda_function.discord_count_sync.function_name
}

output "discord_count_sync_rule" {
  value = aws_cloudwatch_event_rule.discord_count_sync.name
}
