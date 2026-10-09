output "cluster_name" {
  description = "Name of the ECS cluster."
  value       = aws_ecs_cluster.this.name
}

output "cluster_arn" {
  description = "ARN of the ECS cluster."
  value       = aws_ecs_cluster.this.arn
}

output "service_name" {
  description = "Name of the ECS service."
  value       = aws_ecs_service.this.name
}

output "task_definition_family" {
  description = "Task definition family. CI renders its new revisions from the latest revision of this family."
  value       = aws_ecs_task_definition.app.family
}

output "task_definition_arn" {
  description = "ARN of the revision Terraform registered. Not necessarily the one running — see the service's ignore_changes."
  value       = aws_ecs_task_definition.app.arn
}

output "migrate_task_definition_family" {
  description = "Task definition family for `alembic upgrade head`. CI runs one task from this and waits for exit code 0 before updating the service."
  value       = aws_ecs_task_definition.migrate.family
}

output "alb_dns_name" {
  description = "The load balancer's own hostname. No certificate can be issued for it, so it is reachable over HTTP only."
  value       = aws_lb.this.dns_name
}

output "alb_zone_id" {
  description = "Hosted zone id of the load balancer, for a Route 53 alias record pointing at it."
  value       = aws_lb.this.zone_id
}

output "alb_arn" {
  description = "ARN of the load balancer."
  value       = aws_lb.this.arn
}

output "target_group_arn" {
  description = "ARN of the target group the tasks register into."
  value       = aws_lb_target_group.this.arn
}

output "url" {
  description = <<-DESC
    Base URL of the API as the browser must use it. HTTPS once a certificate is
    configured; otherwise HTTP, which the frontend cannot call from an HTTPS page.
  DESC
  value       = var.certificate_arn != null ? "https://${aws_lb.this.dns_name}" : "http://${aws_lb.this.dns_name}"
}

output "log_group_name" {
  description = "Log group the tasks write to, with retention already set."
  value       = aws_cloudwatch_log_group.app.name
}

output "exec_command" {
  description = <<-DESC
    A ready-made shell into a running task, for reaching Aurora without a bastion.
    Replace <task id> with one from `aws ecs list-tasks --cluster <cluster>
    --service-name <service>`.
  DESC
  value = var.enable_execute_command ? join(" ", [
    "aws ecs execute-command",
    "--cluster ${aws_ecs_cluster.this.name}",
    "--task <task id>",
    "--container app",
    "--interactive",
    "--command /bin/sh",
  ]) : "disabled: enable_execute_command is false"
}

output "deploy_manifest_parameter" {
  description = "SSM parameter holding cluster, service, task definition families and network configuration, for a deploy workflow that has no Terraform state access."
  value       = aws_ssm_parameter.deploy_manifest.name
}
