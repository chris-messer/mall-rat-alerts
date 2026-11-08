resource "random_password" "ip_hash_salt" {
  length  = 48
  special = false
}

resource "random_password" "origin_verify" {
  length  = 48
  special = false
}

# ------------------------------------------------------------------------ IAM

resource "aws_iam_role" "signup_api" {
  name = "${var.name}-signup-api"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "signup_api" {
  name = "${var.name}-signup-api"
  role = aws_iam_role.signup_api.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Effect   = "Allow"
          Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
          Resource = "${aws_cloudwatch_log_group.signup_api.arn}:*"
        },
        {
          Effect   = "Allow"
          Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
          Resource = aws_dynamodb_table.subscribers.arn
        },
        {
          Effect   = "Allow"
          Action   = ["secretsmanager:GetSecretValue"]
          Resource = aws_secretsmanager_secret.twilio.arn
        },
        {
          Effect   = "Allow"
          Action   = ["dynamodb:PutItem"]
          Resource = aws_dynamodb_table.audit.arn
        },
        {
          Effect   = "Allow"
          Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
          Resource = aws_dynamodb_table.ops.arn
        },
        {
          Effect   = "Allow"
          Action   = ["dynamodb:Query"]
          Resource = [aws_dynamodb_table.alerts.arn, aws_dynamodb_table.templates.arn]
        },
      ],
      var.turnstile_secret_arn == "" ? [] : [{
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = var.turnstile_secret_arn
      }],
    )
  })
}

# --------------------------------------------------------------------- Lambda

data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda"
  output_path = "${path.module}/build/signup_api.zip"
  excludes    = ["__pycache__", "common/__pycache__", "mail_forwarder.py"]
}

resource "aws_cloudwatch_log_group" "signup_api" {
  name              = "/aws/lambda/${var.name}-signup-api"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "signup_api" {
  function_name                  = "${var.name}-signup-api"
  role                           = aws_iam_role.signup_api.arn
  runtime                        = "python3.12"
  handler                        = "signup_api.handler"
  filename                       = data.archive_file.lambda.output_path
  source_code_hash               = data.archive_file.lambda.output_base64sha256
  timeout                        = 10
  memory_size                    = 256
  reserved_concurrent_executions = var.lambda_reserved_concurrency

  environment {
    variables = {
      SUBSCRIBERS_TABLE      = aws_dynamodb_table.subscribers.name
      AUDIT_TABLE            = aws_dynamodb_table.audit.name
      OPS_TABLE              = aws_dynamodb_table.ops.name
      TEMPLATES_TABLE        = aws_dynamodb_table.templates.name
      ALERTS_TABLE           = aws_dynamodb_table.alerts.name
      FANOUT_SENDS_TABLE     = aws_dynamodb_table.fanout_sends.name
      KEYWORD_CONSENT_SHA256 = filesha256("${local.site_dir}/consent/sms-keyword-v1.txt")
      IP_HASH_SALT           = random_password.ip_hash_salt.result
      ORIGIN_VERIFY_SECRET   = random_password.origin_verify.result
      CONSENT_VERSION        = var.consent_version
      CONSENT_SHA256         = filesha256("${local.site_dir}/consent/${var.consent_version}.txt")
      DEFAULT_CAMPAIGN_ID    = var.campaign.campaign_id
      ALLOWED_COUNTRIES      = "US"
      SITE_HOSTNAMES         = join(",", concat(local.site_aliases, [aws_cloudfront_distribution.site.domain_name]))
      TEST_NUMBERS           = join(",", var.test_numbers)
      WAITLIST_MODE          = tostring(var.waitlist_mode)
      BROADCAST_ENABLED      = tostring(var.broadcast_enabled)
      SIGNUPS_ENABLED        = "true"
      TURNSTILE_ENABLED      = tostring(var.turnstile_enabled)
      TURNSTILE_SITE_KEY     = var.turnstile_site_key
      TURNSTILE_SECRET_ID    = var.turnstile_secret_arn
      PENDING_TTL_DAYS       = "30"
      TWILIO_SECRET_ID       = aws_secretsmanager_secret.twilio.arn
      TWILIO_TOKEN_KEY       = var.twilio_token_key
      TWILIO_FROM_NUMBER     = var.twilio_from_number
      TWILIO_WEBHOOK_BASE    = local.public_base
      PUBLIC_SITE_URL        = local.public_base
      SUPPORT_EMAIL          = var.support_email
      POSTHOG_KEY            = var.posthog_key
      POSTHOG_HOST           = var.posthog_host
    }
  }

  depends_on = [aws_cloudwatch_log_group.signup_api, aws_iam_role_policy.signup_api]
}

# ------------------------------------------------------------ API Gateway

resource "aws_apigatewayv2_api" "signup" {
  name          = "${var.name}-api"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "signup" {
  api_id                 = aws_apigatewayv2_api.signup.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.signup_api.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 10000
}

resource "aws_apigatewayv2_route" "signup" {
  for_each = toset([
    "POST /api/subscribe",
    "POST /api/unsubscribe",
    "POST /api/twilio/inbound",
    "POST /api/twilio/status",
    "GET /api/alerts",
  ])
  api_id    = aws_apigatewayv2_api.signup.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.signup.id}"
}

resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/aws/apigateway/${var.name}-api"
  retention_in_days = var.log_retention_days
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.signup.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_rate_limit  = var.api_throttle_rate
    throttling_burst_limit = var.api_throttle_burst
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId = "$context.requestId"
      time      = "$context.requestTime"
      method    = "$context.httpMethod"
      path      = "$context.path"
      status    = "$context.status"
      latency   = "$context.responseLatency"
      error     = "$context.integrationErrorMessage"
    })
  }
}

resource "aws_lambda_permission" "api" {
  statement_id  = "AllowApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.signup_api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.signup.execution_arn}/*/*"
}
