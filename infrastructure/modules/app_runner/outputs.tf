output "service_arn" {
  description = "ARN of the App Runner service."
  value       = aws_apprunner_service.this.arn
}

output "service_id" {
  description = "Service id, which is the second half of the log group paths App Runner creates."
  value       = aws_apprunner_service.this.service_id
}

output "service_url" {
  description = "Default *.awsapprunner.com hostname, without a scheme. This is what the frontend's API base URL points at until a custom domain is in place."
  value       = aws_apprunner_service.this.service_url
}

output "status" {
  description = "Service status as of the last apply."
  value       = aws_apprunner_service.this.status
}

output "custom_domain_dns_records" {
  description = <<-DESC
    Certificate validation records to create in DNS, when a custom domain was
    given. The association stays PENDING_CERTIFICATE_DNS_VALIDATION until they
    resolve, and the domain itself additionally needs a CNAME to service_url.
  DESC
  value = try([
    for record in aws_apprunner_custom_domain_association.this[0].certificate_validation_records : {
      name  = record.name
      type  = record.type
      value = record.value
    }
  ], [])
}

output "start_command" {
  description = "The command the container is started with, or empty when the image's own CMD is used. Worth surfacing because it is where the migration runs."
  value       = local.start_command
}

output "log_group_names" {
  description = <<-DESC
    The two log groups App Runner creates for this service: /service for its own
    deployment and scaling events, /application for the container's stdout.

    Returned rather than declared as resources, because their names contain the
    service id — which does not exist until the service does, so Terraform
    cannot create them first and App Runner refuses to share one it did not make.
    They are created with retention set to Never Expire, and nothing in AWS
    provides an account-wide default to override that. Set it once per
    environment, after the first apply:

        aws logs put-retention-policy --retention-in-days 30 \
          --log-group-name /aws/apprunner/<service>/<id>/application

    A known gap rather than a decision: it is the one thing in this stack that
    is not reproduced by `terraform apply` alone.
  DESC
  value = {
    service     = "/aws/apprunner/${var.service_name}/${aws_apprunner_service.this.service_id}/service"
    application = "/aws/apprunner/${var.service_name}/${aws_apprunner_service.this.service_id}/application"
  }
}
