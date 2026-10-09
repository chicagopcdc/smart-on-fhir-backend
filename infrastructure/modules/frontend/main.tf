# The frontend: a Vite build in a private bucket, served by CloudFront.
#
# The bucket is never a website endpoint and never public. CloudFront reaches it
# with Origin Access Control — SigV4-signed requests from the distribution,
# authorised by a bucket policy that names the distribution — so there is no URL
# that serves the objects except through the CDN. That matters more than it
# would for an ordinary static site: the SPA is what holds a signed-in
# clinician's session, and an origin reachable around CloudFront is an origin
# reachable without the headers set below.

data "aws_caller_identity" "current" {}

locals {
  bucket_name = coalesce(var.bucket_name, "${var.name_prefix}-frontend-${data.aws_caller_identity.current.account_id}")
}


# --- origin -----------------------------------------------------------------

resource "aws_s3_bucket" "this" {
  bucket = local.bucket_name

  tags = merge(var.tags, { Name = local.bucket_name })
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket = aws_s3_bucket.this.id

  # All four. The bucket policy below grants CloudFront and only CloudFront;
  # these are what stop a later `aws s3 cp --acl public-read` from adding a
  # second way in without anyone editing the policy.
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "this" {
  bucket = aws_s3_bucket.this.id

  # ACLs off entirely, so object ownership is never in question and a sync from
  # CI cannot produce an object the bucket owner cannot read.
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }

    # Not KMS, deliberately. Every object here is a public build artifact, and
    # KMS would add a decrypt call to every origin fetch CloudFront makes.
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id

  # The rollback path. `aws s3 sync --delete` is how a deploy works, so without
  # versioning a bad build overwrites the good one with nothing kept.
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    id     = "expire-noncurrent-builds"
    status = "Enabled"

    filter {}

    # Thirty days of previous builds is more rollback than anyone will use and
    # still bounded; without this, versioning means the bucket grows forever.
    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.this]
}


# --- access logs ------------------------------------------------------------

resource "aws_s3_bucket" "logs" {
  count = var.enable_access_logging ? 1 : 0

  bucket = "${local.bucket_name}-logs"

  tags = merge(var.tags, { Name = "${local.bucket_name}-logs" })
}

resource "aws_s3_bucket_public_access_block" "logs" {
  count = var.enable_access_logging ? 1 : 0

  bucket = aws_s3_bucket.logs[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "logs" {
  count = var.enable_access_logging ? 1 : 0

  bucket = aws_s3_bucket.logs[0].id

  # CloudFront's standard logging delivers objects owned by the log delivery
  # account and requires an ACL to hand them over, which BucketOwnerEnforced
  # would refuse. This is the one bucket here that cannot have ACLs disabled.
  rule {
    object_ownership = "BucketOwnerPreferred"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  count = var.enable_access_logging ? 1 : 0

  bucket = aws_s3_bucket.logs[0].id

  rule {
    id     = "expire-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = var.log_retention_days
    }
  }
}


# --- distribution -----------------------------------------------------------

resource "aws_cloudfront_origin_access_control" "this" {
  name                              = "${var.name_prefix}-frontend"
  description                       = "OAC for ${local.bucket_name}"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_response_headers_policy" "this" {
  name    = "${var.name_prefix}-frontend-security-headers"
  comment = "Security headers for the ${var.name_prefix} frontend"

  security_headers_config {
    # Two years, including subdomains, and preload-eligible. Safe here because
    # nothing on this hostname is ever meant to be served over plain HTTP; it
    # would not be safe on a domain with an http-only sibling service.
    strict_transport_security {
      access_control_max_age_sec = 63072000
      include_subdomains         = true
      preload                    = true
      override                   = true
    }

    content_type_options {
      override = true
    }

    # DENY rather than SAMEORIGIN. The SMART flow sends the browser to the EHR
    # and back; an application that can be framed is one that can be framed by
    # a page imitating that redirect.
    frame_options {
      frame_option = "DENY"
      override     = true
    }

    # Send the origin cross-site and the full URL same-site. Authorization codes
    # arrive in the callback URL's query string, and a full Referer on a
    # cross-site request is how those end up in someone else's logs.
    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }

    dynamic "content_security_policy" {
      for_each = var.content_security_policy != null ? [var.content_security_policy] : []

      content {
        content_security_policy = content_security_policy.value
        override                = true
      }
    }
  }
}

resource "aws_cloudfront_distribution" "this" {
  enabled             = true
  is_ipv6_enabled     = true
  comment             = "${var.name_prefix} frontend"
  default_root_object = "index.html"
  price_class         = var.price_class
  aliases             = var.aliases

  origin {
    origin_id                = "s3-${local.bucket_name}"
    domain_name              = aws_s3_bucket.this.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.this.id
  }

  # index.html, and every path the SPA router owns. Cached briefly rather than
  # not at all: the document is small, and a short TTL is the difference between
  # a deploy being visible in seconds and the invalidation being mandatory.
  default_cache_behavior {
    target_origin_id       = "s3-${local.bucket_name}"
    viewer_protocol_policy = "redirect-to-https"
    compress               = true

    allowed_methods = ["GET", "HEAD", "OPTIONS"]
    cached_methods  = ["GET", "HEAD"]

    # Managed-CachingOptimized: compress, no cookies, no query strings, no
    # headers in the key. The API is a different origin the browser calls
    # directly, so nothing here varies by request.
    cache_policy_id            = "658327ea-f89d-4fab-a63d-7e88639e58f6"
    response_headers_policy_id = aws_cloudfront_response_headers_policy.this.id
  }

  # Vite writes hashed filenames under assets/, so a given URL's contents can
  # never change. A year, and no invalidation needed on deploy.
  ordered_cache_behavior {
    path_pattern           = "/assets/*"
    target_origin_id       = "s3-${local.bucket_name}"
    viewer_protocol_policy = "redirect-to-https"
    compress               = true

    allowed_methods = ["GET", "HEAD", "OPTIONS"]
    cached_methods  = ["GET", "HEAD"]

    cache_policy_id            = "658327ea-f89d-4fab-a63d-7e88639e58f6"
    response_headers_policy_id = aws_cloudfront_response_headers_policy.this.id

    min_ttl     = 0
    default_ttl = 31536000
    max_ttl     = 31536000
  }

  # The SPA fallback. react-router owns /research, /auth/login/callback and the
  # rest; none of them is an object in the bucket, so S3 answers 403 — not 404,
  # because the OAC principal is not allowed s3:ListBucket and cannot be told
  # the difference between a missing key and a forbidden one. Both are rewritten
  # to index.html with a 200 so the router can read the path.
  #
  # The cost is that a genuinely missing asset also returns index.html. That is
  # the standard trade for client-side routing, and it is why /assets/* is a
  # separate behaviour: a broken script tag shows up as a syntax error rather
  # than a 404 either way.
  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    # The default *.cloudfront.net certificate, unless aliases were given. The
    # minimum protocol version is only settable on an ACM certificate; the
    # default certificate carries CloudFront's own policy.
    cloudfront_default_certificate = var.acm_certificate_arn == null
    acm_certificate_arn            = var.acm_certificate_arn
    ssl_support_method             = var.acm_certificate_arn != null ? "sni-only" : null
    minimum_protocol_version       = var.acm_certificate_arn != null ? "TLSv1.2_2021" : null
  }

  dynamic "logging_config" {
    for_each = var.enable_access_logging ? [1] : []

    content {
      bucket          = aws_s3_bucket.logs[0].bucket_domain_name
      prefix          = "cloudfront/"
      include_cookies = false
    }
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-frontend" })

  depends_on = [aws_s3_bucket_ownership_controls.logs]
}


# --- the policy that ties them together -------------------------------------

data "aws_iam_policy_document" "bucket" {
  statement {
    sid    = "AllowCloudFrontServicePrincipalReadOnly"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.this.arn}/*"]

    # Without this the statement grants every CloudFront distribution in every
    # account, since the service principal is shared. The condition is what
    # narrows it to this one.
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.this.arn]
    }
  }

  # Belt and braces against a misconfigured client: S3 would otherwise accept a
  # signed request over plain HTTP.
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.this.arn,
      "${aws_s3_bucket.this.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "this" {
  bucket = aws_s3_bucket.this.id
  policy = data.aws_iam_policy_document.bucket.json

  # The public access block's block_public_policy evaluates the policy as it is
  # written. Ordering them explicitly avoids a race where the policy lands first
  # and is judged against a bucket with no block configured yet.
  depends_on = [aws_s3_bucket_public_access_block.this]
}
