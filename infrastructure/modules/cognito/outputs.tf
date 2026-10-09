output "user_pool_id" {
  description = "Id of the user pool."
  value       = aws_cognito_user_pool.this.id
}

output "user_pool_arn" {
  description = "ARN of the user pool."
  value       = aws_cognito_user_pool.this.arn
}

output "issuer" {
  description = <<-DESC
    OIDC_ISSUER. Cognito publishes its discovery document at this URL plus
    /.well-known/openid-configuration, which is where app/auth/oidc.py reads the
    authorization, token and JWKS endpoints from — so nothing else about the
    provider has to be configured.
  DESC
  value       = "https://cognito-idp.${data.aws_region.current.name}.amazonaws.com/${aws_cognito_user_pool.this.id}"
}

output "client_id" {
  description = "OIDC_CLIENT_ID."
  value       = aws_cognito_user_pool_client.this.id
}

output "client_secret" {
  description = <<-DESC
    OIDC_CLIENT_SECRET, when one was generated.

    Marked sensitive, and deliberately not wired into the secrets module: doing
    that would be tidier and would put the value in two places in state instead
    of one. Read it once and write it where the service looks:

        aws cognito-idp describe-user-pool-client \
          --user-pool-id <pool> --client-id <client> \
          --query 'UserPoolClient.ClientSecret' --output text \
        | aws secretsmanager put-secret-value \
            --secret-id <prefix>/OIDC_CLIENT_SECRET --secret-string file:///dev/stdin
  DESC
  value       = try(aws_cognito_user_pool_client.this.client_secret, null)
  sensitive   = true
}

output "hosted_ui_domain" {
  description = "Hosted UI domain. The authorization endpoint in the discovery document points here."
  value       = "https://${aws_cognito_user_pool_domain.this.domain}.auth.${data.aws_region.current.name}.amazoncognito.com"
}
