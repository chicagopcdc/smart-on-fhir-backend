output "bucket_name" {
  description = "Bucket the built frontend is synced into."
  value       = aws_s3_bucket.this.bucket
}

output "bucket_arn" {
  description = "Bucket ARN, for the deploy role's sync grant."
  value       = aws_s3_bucket.this.arn
}

output "distribution_id" {
  description = "Distribution id, for `aws cloudfront create-invalidation --distribution-id`."
  value       = aws_cloudfront_distribution.this.id
}

output "distribution_arn" {
  description = "Distribution ARN, for the deploy role's invalidation grant."
  value       = aws_cloudfront_distribution.this.arn
}

output "distribution_domain_name" {
  description = "The *.cloudfront.net hostname. This is FRONTEND_HOSTNAME until an alias is in place, and it is what every EHR's registered redirect URI has to match."
  value       = aws_cloudfront_distribution.this.domain_name
}

output "distribution_hosted_zone_id" {
  description = "CloudFront's hosted zone id, for a Route 53 alias record pointing at the distribution."
  value       = aws_cloudfront_distribution.this.hosted_zone_id
}

output "url" {
  description = "Origin the browser loads, preferring the first alias when there is one."
  value       = "https://${length(var.aliases) > 0 ? var.aliases[0] : aws_cloudfront_distribution.this.domain_name}"
}
