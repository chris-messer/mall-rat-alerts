# SMS fan-out: composer -> SQS job (delayed) -> worker Lambda (reserved concurrency 1) -> Twilio.
# Nothing sends unless ops config#global has fanout_mode allowlist/live AND broadcast_enabled = true
# (live also needs waitlist_mode = false). Those flags are edited by hand, never by Terraform.

locals {
  fanout_worker_name = "${var.name}-fanout-worker"
  fanout_timeout     = 300
  fanout_concurrency = 2
}

# ------------------------------------------------------------- send rows

# PK alert_id = "<campaign_id>#<sent_at>", SK phone. One row per person per alert, written with a conditional
# put BEFORE the Twilio call (idempotency). phone = "#run" holds the run state. Rows expire after 90 days.
resource "aws_dynamodb_table" "fanout_sends" {
  name         = "${var.name}-fanout-sends"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "alert_id"
  range_key    = "phone"

  attribute {
    name = "alert_id"
    type = "S"
  }
  attribute {
    name = "phone"
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

# ------------------------------------------------------------------ queues

resource "aws_sqs_queue" "fanout_dlq" {
  name                      = "${var.name}-fanout-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "fanout" {
  name                       = "${var.name}-fanout"
  visibility_timeout_seconds = local.fanout_timeout + 30
  message_retention_seconds  = 86400
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.fanout_dlq.arn
    maxReceiveCount     = 5
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "fanout_dlq" {
  queue_url = aws_sqs_queue.fanout_dlq.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.fanout.arn]
  })
}

# ------------------------------------------------------------------ worker

resource "aws_iam_role" "fanout_worker" {
  name = local.fanout_worker_name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "fanout_worker" {
  name = local.fanout_worker_name
  role = aws_iam_role.fanout_worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.fanout_worker.arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"]
        Resource = aws_sqs_queue.fanout.arn
      },
      {
        # Continuations go back on the queue; parked work (kill switch) goes to the DLQ.
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = [aws_sqs_queue.fanout.arn, aws_sqs_queue.fanout_dlq.arn]
      },
      {
        # Status re-check at send time, memberships for 21610, and the opt-out update.
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.subscribers.arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:Query"]
        Resource = "${aws_dynamodb_table.subscribers.arn}/index/active-index"
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = [aws_dynamodb_table.alerts.arn, aws_dynamodb_table.ops.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        Resource = aws_dynamodb_table.fanout_sends.arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = aws_dynamodb_table.audit.arn
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.twilio.arn
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "fanout_worker" {
  name              = "/aws/lambda/${local.fanout_worker_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "fanout_worker" {
  function_name                  = local.fanout_worker_name
  role                           = aws_iam_role.fanout_worker.arn
  runtime                        = "python3.12"
  handler                        = "fanout_worker.handler"
  filename                       = data.archive_file.lambda.output_path
  source_code_hash               = data.archive_file.lambda.output_base64sha256
  timeout                        = local.fanout_timeout
  memory_size                    = 256
  reserved_concurrent_executions = local.fanout_concurrency

  environment {
    variables = {
      SUBSCRIBERS_TABLE   = aws_dynamodb_table.subscribers.name
      AUDIT_TABLE         = aws_dynamodb_table.audit.name
      OPS_TABLE           = aws_dynamodb_table.ops.name
      ALERTS_TABLE        = aws_dynamodb_table.alerts.name
      SENDS_TABLE         = aws_dynamodb_table.fanout_sends.name
      QUEUE_URL           = aws_sqs_queue.fanout.id
      DLQ_URL             = aws_sqs_queue.fanout_dlq.id
      ACTIVE_INDEX        = "active-index"
      TWILIO_SECRET_ID    = aws_secretsmanager_secret.twilio.arn
      TWILIO_TOKEN_KEY    = var.twilio_token_key
      TWILIO_FROM_NUMBER  = var.twilio_from_number
      TWILIO_WEBHOOK_BASE = local.public_base
    }
  }

  depends_on = [aws_cloudwatch_log_group.fanout_worker, aws_iam_role_policy.fanout_worker]
}

resource "aws_lambda_event_source_mapping" "fanout" {
  event_source_arn = aws_sqs_queue.fanout.arn
  function_name    = aws_lambda_function.fanout_worker.arn
  batch_size       = 1

  # Must equal reserved concurrency: above it, extra jobs are throttled and wait out the visibility timeout.
  # Aggregate send rate is enforced by the shared limiter (ops fanout#rate), not by concurrency.
  scaling_config {
    maximum_concurrency = local.fanout_concurrency
  }
}

# ------------------------------------------------- composer and webhook access

resource "aws_iam_role_policy" "composer_fanout" {
  name = "${var.name}-alert-composer-fanout"
  role = aws_iam_role.composer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sqs:SendMessage"]
      Resource = aws_sqs_queue.fanout.arn
    }]
  })
}

# /api/twilio/status records delivery status on the send row.
resource "aws_iam_role_policy" "signup_api_fanout" {
  name = "${var.name}-signup-api-fanout"
  role = aws_iam_role.signup_api.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
      Resource = aws_dynamodb_table.fanout_sends.arn
    }]
  })
}

# ------------------------------------------------------------------ alarms

resource "aws_cloudwatch_metric_alarm" "fanout_dlq" {
  alarm_name          = "${var.name}-fanout-dlq-not-empty"
  alarm_description   = "Fan-out jobs failed or were parked (kill switch). Inspect before redriving."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.fanout_dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_metric_alarm" "fanout_errors" {
  alarm_name          = "${var.name}-fanout-worker-errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.fanout_worker.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
}

# --------------------------------------------------------- ops config keys

# The config#global seed (dynamodb.tf) ignores later edits, so an existing item never gets new keys from it.
# Add each fan-out key only if it is missing; never touches broadcast_enabled or waitlist_mode, and never
# overwrites a value someone already set.
resource "terraform_data" "fanout_config_keys" {
  triggers_replace = ["fanout-config-v1", aws_dynamodb_table.ops.name]

  provisioner "local-exec" {
    command = <<-EOT
      aws dynamodb update-item --region ${var.region} --profile ${var.aws_profile} \
        --table-name ${aws_dynamodb_table.ops.name} \
        --key '{"pk":{"S":"config#global"},"sk":{"S":"current"}}' \
        --condition-expression 'attribute_exists(pk)' \
        --update-expression 'SET fanout_mode = if_not_exists(fanout_mode, :mode), fanout_allowlist = if_not_exists(fanout_allowlist, :allow), fanout_paused = if_not_exists(fanout_paused, :paused), fanout_max_recipients_per_alert = if_not_exists(fanout_max_recipients_per_alert, :cap), fanout_delay_seconds = if_not_exists(fanout_delay_seconds, :delay), fanout_rate_per_second = if_not_exists(fanout_rate_per_second, :rate), fanout_max_alert_age_minutes = if_not_exists(fanout_max_alert_age_minutes, :age)' \
        --expression-attribute-values '{":mode":{"S":"off"},":allow":{"L":[]},":paused":{"BOOL":false},":cap":{"N":"200"},":delay":{"N":"10"},":rate":{"N":"2"},":age":{"N":"120"}}'
    EOT
  }

  depends_on = [aws_dynamodb_table_item.config_global]
}

output "fanout_queue_url" {
  value = aws_sqs_queue.fanout.id
}

output "fanout_dlq_url" {
  value = aws_sqs_queue.fanout_dlq.id
}

output "fanout_worker_lambda" {
  value = aws_lambda_function.fanout_worker.function_name
}

output "fanout_sends_table" {
  value = aws_dynamodb_table.fanout_sends.name
}
