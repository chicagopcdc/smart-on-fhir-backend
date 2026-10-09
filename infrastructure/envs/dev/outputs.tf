output "api_url" {
  description = <<-DESC
    Base URL of the backend, as a browser must use it.

    Falls back to the load balancer's own hostname over plain HTTP when no
    certificate is configured — which is reachable with curl but *not* from the
    frontend, because an HTTPS page may not call an HTTP origin. Set
    api_certificate_arn and api_domain before expecting the application to work
    end to end.
  DESC
  value       = var.api_domain != null ? "https://${var.api_domain}" : module.ecs.url
}

output "api_is_https" {
  description = "Whether the API is reachable over HTTPS. False means the frontend cannot call it, whatever else is configured."
  value       = var.api_certificate_arn != null
}

output "alb_dns_name" {
  description = "Hostname of the load balancer, for the DNS record that should point at it."
  value       = module.ecs.alb_dns_name
}

output "alb_zone_id" {
  description = "Hosted zone id of the load balancer, for a Route 53 alias record."
  value       = module.ecs.alb_zone_id
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

output "ecs_cluster_name" {
  description = "Cluster the service runs in."
  value       = module.ecs.cluster_name
}

output "ecs_service_name" {
  description = "Name of the ECS service, for `aws ecs update-service`."
  value       = module.ecs.service_name
}

output "task_definition_family" {
  description = "Task definition family CI renders new revisions from."
  value       = module.ecs.task_definition_family
}

output "migrate_task_definition_family" {
  description = "Task definition family for `alembic upgrade head`. Run one task from this and wait for exit 0 before updating the service."
  value       = module.ecs.migrate_task_definition_family
}

output "log_group_name" {
  description = "Where the tasks' stdout goes, with retention already set at apply time."
  value       = module.ecs.log_group_name
}

output "exec_command" {
  description = "Ready-made shell into a running task — the only path to the database. Fill in a task id from `aws ecs list-tasks`."
  value       = module.ecs.exec_command
}

output "database_endpoint" {
  description = "Aurora writer endpoint. Reachable from the tasks' security group only; there is no public path to it."
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
    Secrets still holding the placeholder. The service will not start until each
    has been written:

      aws secretsmanager put-secret-value --secret-id <name> --secret-string '<value>'
  DESC
  value       = module.secrets.placeholder_secret_names
}
