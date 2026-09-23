output "secret_arns" {
  description = <<-DESC
    Environment variable name to secret ARN, for every secret in this module.
    App Runner's runtime_environment_secrets takes this map verbatim, and the
    IAM module turns its values into the task role's read grant.
  DESC
  value = merge(
    { for key, secret in aws_secretsmanager_secret.managed : key => secret.arn },
    { for key, secret in aws_secretsmanager_secret.placeholder : key => secret.arn },
  )
}

output "secret_names" {
  description = "Environment variable name to the secret's name in Secrets Manager, for the put-secret-value commands that fill the placeholders."
  value = merge(
    { for key, secret in aws_secretsmanager_secret.managed : key => secret.name },
    { for key, secret in aws_secretsmanager_secret.placeholder : key => secret.name },
  )
}

output "placeholder_secret_names" {
  description = "Just the secrets still holding a placeholder, so a deploy checklist can list what has to be written before the service will work."
  value       = [for secret in aws_secretsmanager_secret.placeholder : secret.name]
}

output "keys" {
  description = "Every environment variable this module supplies a value for."
  value       = sort(local.all_secret_keys)
}
