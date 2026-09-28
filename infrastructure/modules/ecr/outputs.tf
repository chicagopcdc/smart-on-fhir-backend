output "repository_name" {
  description = "Name of the repository."
  value       = aws_ecr_repository.this.name
}

output "repository_arn" {
  description = "ARN of the repository, for the pull grant on the ECS execution role and the push grant on the deploy role."
  value       = aws_ecr_repository.this.arn
}

output "repository_url" {
  description = "Registry URL without a tag. `docker push $(terraform output -raw repository_url):<tag>` is the whole publish step."
  value       = aws_ecr_repository.this.repository_url
}

output "registry_id" {
  description = "Account id that owns the registry, for `aws ecr get-login-password`."
  value       = aws_ecr_repository.this.registry_id
}
