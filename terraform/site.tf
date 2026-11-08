# ------------------------------------------------------------- site bucket

resource "aws_s3_bucket" "site" {
  bucket        = "${var.name}-site-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "site" {
  bucket                  = aws_s3_bucket.site.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "site" {
  bucket = aws_s3_bucket.site.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "site" {
  bucket = aws_s3_bucket.site.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# ListBucket lets S3 answer 404 (not 403) for missing keys, so only 404 maps to /404.html
# and API 403s pass through untouched.
resource "aws_s3_bucket_policy" "site" {
  bucket = aws_s3_bucket.site.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "CloudFrontRead"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.site.arn}/*"
        Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.site.arn } }
      },
      {
        Sid       = "CloudFrontList"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:ListBucket"
        Resource  = aws_s3_bucket.site.arn
        Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.site.arn } }
      },
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.site]
}

# ------------------------------------------------------------ site objects

locals {
  content_types = {
    html  = "text/html; charset=utf-8"
    css   = "text/css; charset=utf-8"
    js    = "application/javascript; charset=utf-8"
    txt   = "text/plain; charset=utf-8"
    png   = "image/png"
    svg   = "image/svg+xml"
    woff2 = "font/woff2"
  }
  binary_exts = ["png", "woff2"]
  site_files  = fileset(local.site_dir, "**")

  turnstile_script = var.turnstile_enabled ? "<script src=\"https://challenges.cloudflare.com/turnstile/v0/api.js\" async defer></script>" : ""
  turnstile_widget = var.turnstile_enabled ? "<div class=\"cf-turnstile\" data-sitekey=\"${var.turnstile_site_key}\" data-action=\"%s\"></div>" : ""
  tip_jar          = var.stripe_payment_link == "" ? "" : file("${path.module}/tip-jar.html.tpl")
  discord_cta      = var.discord_invite_url == "" ? "" : replace(file("${path.module}/discord-cta.html.tpl"), "DISCORD_INVITE_URL", var.discord_invite_url)
  sms_open_attr    = var.discord_invite_url == "" ? " open" : ""
}

resource "aws_s3_object" "site" {
  for_each = { for f in local.site_files : f => f if !contains(local.binary_exts, reverse(split(".", f))[0]) }

  bucket        = aws_s3_bucket.site.id
  key           = each.value
  content_type  = lookup(local.content_types, reverse(split(".", each.value))[0], "application/octet-stream")
  cache_control = startswith(each.value, "assets/") ? "public, max-age=3600" : "public, max-age=300"
  content = replace(
    replace(
      replace(
        replace(
          replace(file("${local.site_dir}/${each.value}"), "<!--TURNSTILE_SCRIPT-->", local.turnstile_script),
          "<!--TURNSTILE_WIDGET-->",
          var.turnstile_enabled ? format(local.turnstile_widget, startswith(each.value, "unsubscribe/") ? "unsubscribe" : "subscribe") : ""
        ),
        "<!--TIP_JAR-->",
        replace(local.tip_jar, "STRIPE_PAYMENT_LINK", var.stripe_payment_link)
      ),
      "<!--DISCORD_CTA-->",
      local.discord_cta
    ),
    "<!--SMS_OPEN-->",
    local.sms_open_attr
  )
}

# Runtime config read by app.js (same idea as Versiful's public/config.json). Empty apiKey = analytics off.
resource "aws_s3_object" "config" {
  bucket        = aws_s3_bucket.site.id
  key           = "config.json"
  content_type  = "application/json"
  cache_control = "public, max-age=300"
  content = jsonencode({
    campaignId = var.campaign.campaign_id
    posthog = {
      apiKey = var.posthog_key
      host   = var.posthog_host
    }
  })
}

resource "aws_s3_object" "site_binary" {
  for_each = { for f in local.site_files : f => f if contains(local.binary_exts, reverse(split(".", f))[0]) }

  bucket        = aws_s3_bucket.site.id
  key           = each.value
  source        = "${local.site_dir}/${each.value}"
  etag          = filemd5("${local.site_dir}/${each.value}")
  content_type  = lookup(local.content_types, reverse(split(".", each.value))[0], "application/octet-stream")
  cache_control = "public, max-age=3600"
}

# --------------------------------------------------------------- CloudFront

resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "${var.name}-site"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_function" "rewrite" {
  name    = "${var.name}-rewrite"
  runtime = "cloudfront-js-2.0"
  comment = "Map /privacy and /privacy/ to /privacy/index.html"
  publish = true
  code    = <<-EOT
    function handler(event) {
      var request = event.request;
      var uri = request.uri;
      if (uri.endsWith('/')) {
        request.uri = uri + 'index.html';
      } else if (uri.split('/').pop().indexOf('.') === -1) {
        request.uri = uri + '/index.html';
      }
      return request;
    }
  EOT
}

resource "aws_cloudfront_cache_policy" "site" {
  name        = "${var.name}-site"
  min_ttl     = 0
  default_ttl = 300
  max_ttl     = 3600

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_gzip   = true
    enable_accept_encoding_brotli = true
    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "none"
    }
  }
}

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

# GET /api/alerts: cached briefly at the edge, keyed on the two query parameters the feed accepts.
resource "aws_cloudfront_cache_policy" "alerts_feed" {
  name        = "${var.name}-alerts-feed"
  min_ttl     = 0
  default_ttl = 30
  max_ttl     = 60

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_gzip   = true
    enable_accept_encoding_brotli = true
    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "whitelist"
      query_strings {
        items = ["campaign_id", "limit"]
      }
    }
  }
}

# Host must not be forwarded to API Gateway, so viewer headers are allowlisted.
resource "aws_cloudfront_origin_request_policy" "api" {
  name = "${var.name}-api"

  cookies_config {
    cookie_behavior = "none"
  }
  headers_config {
    header_behavior = "whitelist"
    headers {
      items = [
        "Content-Type",
        "User-Agent",
        "Referer",
        "Origin",
        "CloudFront-Viewer-Address",
        "CloudFront-Viewer-Country",
        "X-Twilio-Signature",
      ]
    }
  }
  query_strings_config {
    query_string_behavior = "none"
  }
}

resource "aws_cloudfront_response_headers_policy" "security" {
  name = "${var.name}-security"

  security_headers_config {
    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      override                   = true
    }
    content_type_options {
      override = true
    }
    frame_options {
      frame_option = "DENY"
      override     = true
    }
    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }
    content_security_policy {
      content_security_policy = join("; ", [
        "default-src 'self'",
        "script-src 'self' https://challenges.cloudflare.com${local.posthog_csp_hosts}",
        "frame-src https://challenges.cloudflare.com",
        "style-src 'self'",
        "font-src 'self'",
        "object-src 'none'",
        "img-src 'self' data:",
        "connect-src 'self'${local.posthog_csp_hosts}",
        "form-action 'self'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
      ])
      override = true
    }
  }
}

resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  is_ipv6_enabled     = true
  http_version        = "http2and3"
  comment             = "${var.name} site and API"
  default_root_object = "index.html"
  price_class         = "PriceClass_100"
  aliases             = local.site_aliases

  origin {
    origin_id                = "site"
    domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  origin {
    origin_id   = "api"
    domain_name = replace(aws_apigatewayv2_api.signup.api_endpoint, "https://", "")

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }

    custom_header {
      name  = "X-Origin-Verify"
      value = random_password.origin_verify.result
    }
  }

  default_cache_behavior {
    target_origin_id           = "site"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = aws_cloudfront_cache_policy.site.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.rewrite.arn
    }
  }

  # Must stay above /api/*: first match wins. Public read-only feed, GET/HEAD only, query string forwarded.
  ordered_cache_behavior {
    path_pattern               = "/api/alerts"
    target_origin_id           = "api"
    viewer_protocol_policy     = "https-only"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = aws_cloudfront_cache_policy.alerts_feed.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
  }

  ordered_cache_behavior {
    path_pattern               = "/api/*"
    target_origin_id           = "api"
    viewer_protocol_policy     = "https-only"
    allowed_methods            = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.api.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
  }

  custom_error_response {
    error_code            = 404
    response_code         = 404
    response_page_path    = "/404.html"
    error_caching_min_ttl = 60
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = !local.custom_domain
    acm_certificate_arn            = local.custom_domain ? aws_acm_certificate_validation.site[0].certificate_arn : null
    ssl_support_method             = local.custom_domain ? "sni-only" : null
    minimum_protocol_version       = local.custom_domain ? "TLSv1.2_2021" : "TLSv1"
  }
}

# ------------------------------------------- custom domain (off until purchase)

data "aws_route53_zone" "site" {
  count        = local.custom_domain ? 1 : 0
  name         = var.domain_name
  private_zone = false
}

resource "aws_acm_certificate" "site" {
  count                     = local.custom_domain ? 1 : 0
  domain_name               = var.domain_name
  subject_alternative_names = ["www.${var.domain_name}"]
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# Keyed by the known domain names: validation options are unknown until the cert exists.
resource "aws_route53_record" "cert_validation" {
  for_each = toset(local.site_aliases)

  zone_id         = data.aws_route53_zone.site[0].zone_id
  name            = one([for d in aws_acm_certificate.site[0].domain_validation_options : d.resource_record_name if d.domain_name == each.key])
  type            = one([for d in aws_acm_certificate.site[0].domain_validation_options : d.resource_record_type if d.domain_name == each.key])
  records         = [one([for d in aws_acm_certificate.site[0].domain_validation_options : d.resource_record_value if d.domain_name == each.key])]
  ttl             = 300
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "site" {
  count                   = local.custom_domain ? 1 : 0
  certificate_arn         = aws_acm_certificate.site[0].arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

resource "aws_route53_record" "site" {
  for_each = local.custom_domain ? toset(flatten([for a in local.site_aliases : ["${a}|A", "${a}|AAAA"]])) : toset([])

  zone_id = data.aws_route53_zone.site[0].zone_id
  name    = split("|", each.value)[0]
  type    = split("|", each.value)[1]

  alias {
    name                   = aws_cloudfront_distribution.site.domain_name
    zone_id                = aws_cloudfront_distribution.site.hosted_zone_id
    evaluate_target_health = false
  }
}
