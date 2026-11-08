output "site_url" {
  value = "https://${aws_cloudfront_distribution.site.domain_name}"
}

output "api_url" {
  description = "Public API base (same origin as the site, through CloudFront)."
  value       = "https://${aws_cloudfront_distribution.site.domain_name}/api"
}

output "api_gateway_endpoint" {
  description = "Direct API Gateway URL. Requests that skip CloudFront are rejected with 403."
  value       = aws_apigatewayv2_api.signup.api_endpoint
}

output "intended_domain" {
  value = local.custom_domain ? "https://${var.domain_name} (live)" : "${var.domain_name} (not purchased or bound yet)"
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.site.id
}

output "site_bucket" {
  value = aws_s3_bucket.site.bucket
}

output "signup_lambda" {
  value = aws_lambda_function.signup_api.function_name
}

output "signup_log_group" {
  value = aws_cloudwatch_log_group.signup_api.name
}

output "subscribers_table" {
  value = aws_dynamodb_table.subscribers.name
}

output "audit_table" {
  value = aws_dynamodb_table.audit.name
}

output "ops_table" {
  value = aws_dynamodb_table.ops.name
}

output "twilio_inbound_webhook_url" {
  value = "${local.public_base}/api/twilio/inbound"
}

output "twilio_status_callback_url" {
  value = "${local.public_base}/api/twilio/status"
}

output "twilio_secret_name" {
  value = aws_secretsmanager_secret.twilio.name
}

output "posthog_enabled" {
  value = var.posthog_key != ""
}

output "campaign_id" {
  value = var.campaign.campaign_id
}
