variable "name_prefix" {
  description = "Prefix applied to every resource name."
  type        = string
}

variable "bucket_name" {
  description = <<-DESC
    Bucket name, or null to use \"<name_prefix>-frontend-<account id>\".

    The account id is in the default because S3 bucket names are global: without
    it, two AWS accounts deploying this stack with the same prefix collide, and
    the error surfaces as a bucket someone else already owns.
  DESC
  type        = string
  default     = null
}

variable "price_class" {
  description = <<-DESC
    Which CloudFront edge locations serve the site. PriceClass_100 is North
    America and Europe and is the cheapest; PriceClass_All adds the rest of the
    world at a higher per-request rate. The users here are clinicians at US
    sites, so the default is the narrow one.
  DESC
  type        = string
  default     = "PriceClass_100"

  validation {
    condition     = contains(["PriceClass_100", "PriceClass_200", "PriceClass_All"], var.price_class)
    error_message = "price_class must be PriceClass_100, PriceClass_200 or PriceClass_All."
  }
}

variable "aliases" {
  description = "Domain names the distribution answers on. Requires acm_certificate_arn; CloudFront rejects an alias with no certificate covering it."
  type        = list(string)
  default     = []
}

variable "acm_certificate_arn" {
  description = <<-DESC
    Certificate for the aliases.

    It has to live in us-east-1 whatever region the rest of this stack is in —
    CloudFront is a global service and only reads certificates from there. A
    certificate in the stack's own region is a valid ARN that CloudFront will
    reject at apply.
  DESC
  type        = string
  default     = null

  validation {
    condition     = var.acm_certificate_arn == null || can(regex("^arn:aws[a-z-]*:acm:us-east-1:", coalesce(var.acm_certificate_arn, "arn:aws:acm:us-east-1:")))
    error_message = "acm_certificate_arn must be a certificate in us-east-1; CloudFront reads certificates from no other region."
  }
}

variable "content_security_policy" {
  description = <<-DESC
    Content-Security-Policy header value, or null to send none.

    Null by default because a wrong CSP breaks the application in the browser
    rather than failing an apply, and the right one depends on where the API and
    the identity provider live. The shape it needs, once those are known:

      default-src 'self';
      connect-src 'self' https://<api host> https://<oidc issuer>;
      img-src 'self' data:;
      style-src 'self' 'unsafe-inline';
      frame-ancestors 'none'

    style-src has to allow inline as long as the frontend uses MUI's emotion
    runtime, which injects <style> elements it generates at render time.
  DESC
  type        = string
  default     = null
}

variable "enable_access_logging" {
  description = "Write CloudFront access logs to a second bucket. Off by default: the logs cost storage and the standard log format records the viewer's IP, which is worth deciding about deliberately for a clinical application."
  type        = bool
  default     = false
}

variable "log_retention_days" {
  description = "Days before an access log object expires, when logging is on."
  type        = number
  default     = 90
}

variable "tags" {
  description = "Tags merged onto every resource this module creates."
  type        = map(string)
  default     = {}
}
