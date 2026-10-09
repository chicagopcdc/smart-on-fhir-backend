variable "name_prefix" {
  description = "Prefix applied to the user pool and its domain."
  type        = string
}

variable "domain_prefix" {
  description = <<-DESC
    Prefix of the Cognito hosted domain, giving
    \"https://<prefix>.auth.<region>.amazoncognito.com\". Null derives it from
    name_prefix.

    A domain is not optional here even though nobody visits the hosted UI by
    name: app/auth/oidc.py reads authorization_endpoint and token_endpoint out
    of the issuer's .well-known/openid-configuration, and Cognito only publishes
    those once a domain exists.
  DESC
  type        = string
  default     = null
}

variable "callback_urls" {
  description = <<-DESC
    Redirect URIs the app client accepts.

    Must contain the exact value app/core/config.py derives — FRONTEND_HOSTNAME
    with \"/auth/login/callback\" appended — because the token exchange sends
    redirect_uri again and Cognito compares it literally. A trailing slash
    difference is a redirect_mismatch at the end of a login.
  DESC
  type        = list(string)
}

variable "logout_urls" {
  description = "Where Cognito may send the browser after a hosted-UI logout. The backend's own logout clears its session and does not use this; it matters if the hosted UI is ever linked to directly."
  type        = list(string)
  default     = []
}

variable "generate_client_secret" {
  description = <<-DESC
    Issue a client secret for the app client.

    The backend performs the code exchange server-side and sends the secret with
    HTTP Basic when one is configured, so a confidential client is the stronger
    choice and the one to prefer. PKCE is used either way — oidc.py always sends
    an S256 challenge — so a public client is also workable if the secret is
    awkward to distribute.

    The secret cannot be read back through Terraform outputs without landing in
    state, so it is fetched once and written into Secrets Manager by hand; see
    the module's client_secret output.
  DESC
  type        = bool
  default     = true
}

variable "allow_self_registration" {
  description = <<-DESC
    Let anyone with the sign-up link create an account.

    False by default, and that default is the point: this pool fronts an
    application that reaches into patient records, so accounts are created by an
    administrator rather than by whoever finds the URL. Turning it on without a
    matching restriction elsewhere makes the application self-service.
  DESC
  type        = bool
  default     = false
}

variable "mfa_configuration" {
  description = "OFF, ON (required for every user) or OPTIONAL. ON is the right answer for anything touching real records; it is not the default because it needs a rollout, not a terraform apply."
  type        = string
  default     = "OPTIONAL"

  validation {
    condition     = contains(["OFF", "ON", "OPTIONAL"], var.mfa_configuration)
    error_message = "mfa_configuration must be OFF, ON or OPTIONAL."
  }
}

variable "password_minimum_length" {
  description = "Minimum password length. Cognito's own floor is 6; length is the requirement that actually buys anything."
  type        = number
  default     = 12
}

variable "access_token_validity_minutes" {
  description = "Lifetime of the access token Cognito issues. The application does not use it — it reads the ID token — so this is only about the userinfo endpoint."
  type        = number
  default     = 60
}

variable "id_token_validity_minutes" {
  description = <<-DESC
    Lifetime of the ID token.

    Only the login exchange reads it: the backend verifies it once, mints its own
    session, and never looks at it again. So this bounds how long a login code
    remains redeemable into a usable identity, not how long a user stays signed
    in — APP_SESSION_TTL_SECONDS governs that.
  DESC
  type        = number
  default     = 60
}

variable "refresh_token_validity_days" {
  description = "Lifetime of the refresh token. The backend does not store it, so this only matters if a client ever adopts Cognito's own refresh flow."
  type        = number
  default     = 30
}

variable "deletion_protection" {
  description = "Refuse to delete the user pool. Deleting it destroys every account, and the identity the application stores is (issuer, subject) — a rebuilt pool issues new subjects, so every existing user's record becomes unreachable."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags merged onto every resource this module creates."
  type        = map(string)
  default     = {}
}
