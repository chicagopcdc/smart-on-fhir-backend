output "api_url" {
  description = "Base URL of the backend. The frontend's build-time API base points here."
  value       = var.service_custom_domain != null ? "https://${var.service_custom_domain}" : "https://${module.app_runner.service_url}"
}

output "frontend_url" {
  description = "Where the application is served. This is FRONTEND_HOSTNAME, and it is what every EHR's registered redirect URI has to match."
  value       = module.frontend.url
}

output "frontend_bucket" {
  description = "Bucket to sync the Vite build into: `aws s3 sync dist/ s3://<this>/ --delete`."
  value       = module.frontend.bucket_name
}

output "frontend_distribution_id" {
  description = "Distribution to invalidate after a sync."
  value       = module.frontend.distribution_id
}

output "ecr_repository_url" {
  description = "Where to push the backend image."
  value       = module.ecr.repository_url
}

output "app_runner_service_arn" {
  description = "ARN of the App Runner service."
  value       = module.app_runner.service_arn
}

output "database_endpoint" {
  description = "Aurora writer endpoint. Reachable from the VPC connector only; there is no public path to it."
  value       = module.database.endpoint
}

output "deploy_role_arn" {
  description = "Role for CI to assume, or null when no github_repository was configured. Goes into the workflow's role-to-assume."
  value       = module.iam.deploy_role_arn
}

output "oidc_issuer" {
  description = "Issuer the service is configured against, or null when login is not configured in this environment."
  value       = local.oidc_issuer
}

output "cognito_user_pool_id" {
  description = "User pool id, when this environment creates one. Administrators create accounts against it."
  value       = try(module.cognito[0].user_pool_id, null)
}

output "secrets_awaiting_values" {
  description = <<-DESC
    Secrets still holding the placeholder. The service will not work until each
    has been written:

      aws secretsmanager put-secret-value --secret-id <name> --secret-string '<value>'
  DESC
  value       = module.secrets.placeholder_secret_names
}

output "log_retention_commands" {
  description = <<-DESC
    Run these once after the first apply of this environment, and again if the
    service is ever recreated — a new service gets a new id and therefore new
    log groups, which are created with retention set to Never Expire. This is
    the one part of the stack `terraform apply` does not reproduce on its own.
  DESC
  value = [
    for name in values(module.app_runner.log_group_names) :
    "aws logs put-retention-policy --retention-in-days ${var.log_retention_days} --log-group-name ${name}"
  ]
}
