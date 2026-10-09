# An identity provider for the application's own login, as a Cognito user pool.
#
# The backend is a plain OIDC relying party — it discovers everything from the
# issuer's .well-known/openid-configuration — so Cognito is a configuration
# choice rather than a code one, exactly as Okta and Google are. What this module
# produces is the three settings app/core/config.py needs: OIDC_ISSUER,
# OIDC_CLIENT_ID and, for a confidential client, OIDC_CLIENT_SECRET.
#
# Worth saying plainly: this is not the only candidate. Keycloak is being looked
# at separately, and a deployment that lands on it wants none of this — point
# OIDC_ISSUER at the realm instead and leave this module out. Cognito's argument
# is that it is one more resource in a stack that already exists rather than a
# service someone has to run.
#
# Note what identity means to the application: (issuer, subject), never email.
# So the pool cannot be rebuilt without orphaning every account that existed,
# which is what deletion_protection is guarding.

data "aws_region" "current" {}

locals {
  domain_prefix = coalesce(var.domain_prefix, var.name_prefix)
}

resource "aws_cognito_user_pool" "this" {
  name = var.name_prefix

  # Email as the sign-in identifier. The application does not key anything on
  # email — it is a display attribute there — but a pool still needs something
  # for a person to type.
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  deletion_protection = var.deletion_protection ? "ACTIVE" : "INACTIVE"

  admin_create_user_config {
    allow_admin_create_user_only = !var.allow_self_registration
  }

  password_policy {
    minimum_length                   = var.password_minimum_length
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 7
  }

  mfa_configuration = var.mfa_configuration

  # Only meaningful when MFA is not OFF, and Cognito rejects the block when it
  # is. TOTP rather than SMS: SMS costs money per message and is the weaker
  # factor of the two.
  dynamic "software_token_mfa_configuration" {
    for_each = var.mfa_configuration != "OFF" ? [1] : []

    content {
      enabled = true
    }
  }

  # Cognito's own advanced security, which is what makes a stuffed credential
  # look different from a real login. AUDIT records; ENFORCED also blocks.
  user_pool_add_ons {
    advanced_security_mode = "AUDIT"
  }

  # Cognito's default sender is rate-limited to about 50 messages a day and
  # sends from an amazonaws.com address. Fine for an administrator-created pool;
  # a deployment that opens self-registration should move this to SES.
  email_configuration {
    email_sending_account = "COGNITO_DEFAULT"
  }

  schema {
    name                     = "email"
    attribute_data_type      = "String"
    required                 = true
    mutable                  = true
    developer_only_attribute = false

    string_attribute_constraints {
      min_length = 5
      max_length = 256
    }
  }

  tags = merge(var.tags, { Name = var.name_prefix })

  lifecycle {
    # Cognito's schema is append-only: an attribute cannot be removed or
    # retyped, so Terraform seeing a diff here means it wants to replace the
    # pool — and replacing the pool changes every subject.
    ignore_changes = [schema]
  }
}

resource "aws_cognito_user_pool_domain" "this" {
  domain       = local.domain_prefix
  user_pool_id = aws_cognito_user_pool.this.id
}

resource "aws_cognito_user_pool_client" "this" {
  name         = "${var.name_prefix}-backend"
  user_pool_id = aws_cognito_user_pool.this.id

  generate_secret = var.generate_client_secret

  # Authorization code only. Implicit would put tokens in the URL fragment, and
  # the backend is the thing that exchanges the code — the browser never sees a
  # token from Cognito at all.
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["openid", "profile", "email"]

  supported_identity_providers = ["COGNITO"]

  callback_urls = var.callback_urls
  logout_urls   = var.logout_urls

  # No SRP, no admin auth, no refresh flow for this client: the only path in is
  # the hosted authorization endpoint. Cognito requires at least one entry, and
  # refresh is the least-reaching one to leave.
  explicit_auth_flows = ["ALLOW_REFRESH_TOKEN_AUTH"]

  access_token_validity  = var.access_token_validity_minutes
  id_token_validity      = var.id_token_validity_minutes
  refresh_token_validity = var.refresh_token_validity_days

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }

  # Answer the same way for a real account and an unknown one. Without this,
  # a login page is an oracle for which email addresses have accounts.
  prevent_user_existence_errors = "ENABLED"

  # The claims the application actually reads. sub is the identity; email and
  # name populate the account record.
  read_attributes  = ["email", "email_verified", "name"]
  write_attributes = ["email", "name"]
}
