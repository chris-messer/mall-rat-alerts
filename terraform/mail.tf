# Support email: SES inbound for hello@<domain>, stored in S3 and forwarded by a Lambda.
#
# One SES receipt rule set can be active per region, and this account's active set (INBOUND_MAIL)
# belongs to Versiful (WorkMail for versiful.io). The hello rule is added to that set, after the
# Versiful rule. Terraform manages only the hello rule, never the INBOUND_MAIL set or its other rules.
# Backup of the set before the merge: docs/ses-inbound-mail-backup.json.

variable "forward_to" {
  description = "Where hello@ mail is forwarded. Must be a verified SES identity while the account is in the SES sandbox."
  type        = string
  default     = "chrislmesser@gmail.com"
}

variable "support_local_part" {
  type    = string
  default = "hello"
}

variable "inbound_rule_set" {
  description = "Existing ACTIVE receipt rule set (not managed here) that the hello rule is added to."
  type        = string
  default     = "INBOUND_MAIL"
}

variable "inbound_rule_after" {
  description = "Existing rule in inbound_rule_set that the hello rule is placed after (Versiful WorkMail rule)."
  type        = string
  default     = "m-3951844311cf4406a7982573f536e2b0"
}

locals {
  support_address  = "${var.support_local_part}@${var.domain_name}"
  inbound_bucket   = "${var.name}-inbound-${local.account_id}"
  inbound_prefix   = "${var.support_local_part}/"
  receipt_rule_arn = "arn:aws:ses:${var.region}:${local.account_id}:receipt-rule-set/${var.inbound_rule_set}:receipt-rule/${var.support_local_part}"
  identity_arn     = "arn:aws:ses:${var.region}:${local.account_id}:identity/${var.domain_name}"
  zone_id          = data.aws_route53_zone.site[0].zone_id
}

# ------------------------------------------------------- SES domain identity

resource "aws_ses_domain_identity" "mail" {
  domain = var.domain_name
}

resource "aws_route53_record" "ses_verification" {
  zone_id = local.zone_id
  name    = "_amazonses.${var.domain_name}"
  type    = "TXT"
  ttl     = 600
  records = [aws_ses_domain_identity.mail.verification_token]
}

resource "aws_ses_domain_dkim" "mail" {
  domain = aws_ses_domain_identity.mail.domain
}

resource "aws_route53_record" "ses_dkim" {
  count   = 3
  zone_id = local.zone_id
  name    = "${aws_ses_domain_dkim.mail.dkim_tokens[count.index]}._domainkey.${var.domain_name}"
  type    = "CNAME"
  ttl     = 600
  records = ["${aws_ses_domain_dkim.mail.dkim_tokens[count.index]}.dkim.amazonses.com"]
}

resource "aws_ses_domain_identity_verification" "mail" {
  domain     = aws_ses_domain_identity.mail.id
  depends_on = [aws_route53_record.ses_verification]
}

resource "aws_route53_record" "mx" {
  zone_id = local.zone_id
  name    = var.domain_name
  type    = "MX"
  ttl     = 300
  records = ["10 inbound-smtp.${var.region}.amazonaws.com"]
}

# ------------------------------------------------------------ inbound bucket

resource "aws_s3_bucket" "inbound" {
  bucket = local.inbound_bucket
}

resource "aws_s3_bucket_public_access_block" "inbound" {
  bucket                  = aws_s3_bucket.inbound.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "inbound" {
  bucket = aws_s3_bucket.inbound.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "inbound" {
  bucket = aws_s3_bucket.inbound.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "inbound" {
  bucket = aws_s3_bucket.inbound.id
  rule {
    id     = "expire-after-30-days"
    status = "Enabled"
    filter {}
    expiration {
      days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_policy" "inbound" {
  bucket = aws_s3_bucket.inbound.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowSesPutObject"
        Effect    = "Allow"
        Principal = { Service = "ses.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.inbound.arn}/${local.inbound_prefix}*"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = local.account_id
            "aws:SourceArn"     = local.receipt_rule_arn
          }
        }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.inbound.arn, "${aws_s3_bucket.inbound.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.inbound]
}

# ------------------------------------------------------------ forwarder Lambda

resource "aws_iam_role" "mail_forwarder" {
  name = "${var.name}-mail-forwarder"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_cloudwatch_log_group" "mail_forwarder" {
  name              = "/aws/lambda/${var.name}-mail-forwarder"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role_policy" "mail_forwarder" {
  name = "${var.name}-mail-forwarder"
  role = aws_iam_role.mail_forwarder.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.mail_forwarder.arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.inbound.arn}/${local.inbound_prefix}*"
      },
      {
        Effect    = "Allow"
        Action    = ["ses:SendRawEmail", "ses:SendEmail"]
        Resource  = local.identity_arn
        Condition = { StringEquals = { "ses:FromAddress" = local.support_address } }
      },
    ]
  })
}

data "archive_file" "mail_forwarder" {
  type        = "zip"
  source_file = "${path.module}/../lambda/mail_forwarder.py"
  output_path = "${path.module}/build/mail_forwarder.zip"
}

resource "aws_lambda_function" "mail_forwarder" {
  function_name                  = "${var.name}-mail-forwarder"
  role                           = aws_iam_role.mail_forwarder.arn
  runtime                        = "python3.12"
  handler                        = "mail_forwarder.handler"
  filename                       = data.archive_file.mail_forwarder.output_path
  source_code_hash               = data.archive_file.mail_forwarder.output_base64sha256
  timeout                        = 30
  memory_size                    = 256
  reserved_concurrent_executions = 2

  environment {
    variables = {
      INBOUND_BUCKET = aws_s3_bucket.inbound.bucket
      KEY_PREFIX     = local.inbound_prefix
      FORWARD_TO     = var.forward_to
      FROM_ADDRESS   = local.support_address
      DRY_RUN        = "false"
    }
  }

  depends_on = [aws_cloudwatch_log_group.mail_forwarder, aws_iam_role_policy.mail_forwarder]
}

resource "aws_lambda_function_event_invoke_config" "mail_forwarder" {
  function_name          = aws_lambda_function.mail_forwarder.function_name
  maximum_retry_attempts = 1
}

resource "aws_lambda_permission" "ses_invoke" {
  statement_id   = "AllowSesInvoke"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.mail_forwarder.function_name
  principal      = "ses.amazonaws.com"
  source_account = local.account_id
  source_arn     = local.receipt_rule_arn
}

# ------------------------------------------------------------- receipt rules

resource "aws_ses_receipt_rule" "support" {
  name          = var.support_local_part
  rule_set_name = var.inbound_rule_set
  after         = var.inbound_rule_after
  recipients    = [local.support_address]
  enabled       = true
  scan_enabled  = true
  tls_policy    = "Optional"

  s3_action {
    position          = 1
    bucket_name       = aws_s3_bucket.inbound.bucket
    object_key_prefix = local.inbound_prefix
  }

  lambda_action {
    position        = 2
    function_arn    = aws_lambda_function.mail_forwarder.arn
    invocation_type = "Event"
  }

  depends_on = [aws_s3_bucket_policy.inbound, aws_lambda_permission.ses_invoke]
}

output "mail_inbound_bucket" {
  value = aws_s3_bucket.inbound.bucket
}

output "mail_forwarder_lambda" {
  value = aws_lambda_function.mail_forwarder.function_name
}

output "mail_receipt_rule_set" {
  value = "${aws_ses_receipt_rule.support.rule_set_name}/${aws_ses_receipt_rule.support.name}"
}
