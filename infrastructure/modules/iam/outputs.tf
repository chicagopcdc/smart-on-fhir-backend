output "access_role_arn" {
  description = "Role App Runner assumes to pull the image."
  value       = aws_iam_role.access.arn
}

output "instance_role_arn" {
  description = "Role the running container runs as."
  value       = aws_iam_role.instance.arn
}

output "deploy_role_arn" {
  description = "Role CI assumes to publish an image and deploy. Null when no github_repository was given. This is the value that goes into the workflow's role-to-assume."
  value       = try(aws_iam_role.deploy[0].arn, null)
}
