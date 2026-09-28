output "execution_role_arn" {
  description = "Role the ECS agent uses to start the task."
  value       = aws_iam_role.execution.arn
}

output "task_role_arn" {
  description = "Role the running container runs as."
  value       = aws_iam_role.task.arn
}

output "deploy_role_arn" {
  description = "Role CI assumes to publish an image, migrate and deploy. Null when no github_repository was given. This is the value that goes into the workflow's role-to-assume."
  value       = try(aws_iam_role.deploy[0].arn, null)
}
