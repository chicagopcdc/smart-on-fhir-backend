variable "name_prefix" {
  description = "Prefix applied to every secret name; each secret is named \"<prefix>/<key>\"."
  type        = string
}

variable "managed_secrets" {
  description = <<-DESC
    Secrets whose value this stack produces, keyed by the environment variable
    the application reads them as.

    Terraform writes the value, so it is in state. That is the price of having
    the database URL assembled from a cluster endpoint Terraform is the only
    thing that knows — and it is why the state backend has to be an encrypted,
    versioned, access-controlled bucket rather than a file on a laptop.

    The variable is deliberately *not* declared `sensitive = true`, which would
    be the obvious thing to write. Marking the whole map marks its keys, and
    Terraform refuses a sensitive for_each — "the sensitive value could be
    exposed as a resource instance key" — so the resources below could not
    iterate it. The protection is not lost: each `value` arrives already marked
    by whatever produced it (the database module's database_url output is
    sensitive), and a mark on a nested attribute is carried through merge and
    for_each without spreading to the keys. The keys are environment variable
    names, which are in this file anyway.
  DESC
  type = map(object({
    description = string
    value       = string
  }))
  default = {}
}

variable "placeholder_secrets" {
  description = <<-DESC
    Secrets someone else issues, keyed by the environment variable the
    application reads them as.

    Epic and Cerner hand out client credentials through their developer
    consoles; an OIDC client secret comes from whoever runs the identity
    provider. None of them can be generated here, so this creates the container
    and a placeholder, and the real value is written out of band:

        aws secretsmanager put-secret-value \
          --secret-id smart-on-fhir-dev/EPIC_CLIENT_SECRET \
          --secret-string '<the value>'

    The version is under ignore_changes, so that write survives every later
    apply instead of being reverted to the placeholder.
  DESC
  type = map(object({
    description = string
  }))
  default = {}
}

variable "generate_token_encryption_key" {
  description = <<-DESC
    Generate TOKEN_ENCRYPTION_KEY here instead of leaving a placeholder.

    The application encrypts stored EHR tokens with this key, and reads it as a
    comma-separated list so a new key can be prepended while the old one still
    decrypts what it wrote. Generating it here makes a fresh environment come up
    working; the cost is that the key sits in Terraform state alongside the
    database URL, which for the environment holding real patient data is an
    argument for turning this off and writing the key by hand:

        python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'

    Rotating it is an out-of-band write either way — see placeholder_secrets.
  DESC
  type        = bool
  default     = true
}

variable "recovery_window_in_days" {
  description = <<-DESC
    How long a deleted secret is recoverable before Secrets Manager really
    removes it. AWS allows 0 (immediate) or 7 to 30.

    Zero is what lets a dev environment be destroyed and re-applied: a name in
    the recovery window cannot be reused, so a non-zero window here turns
    `destroy` followed by `apply` into an InvalidRequestException for the next
    week. In prod the window is the undo button.
  DESC
  type        = number
  default     = 30

  validation {
    condition     = var.recovery_window_in_days == 0 || (var.recovery_window_in_days >= 7 && var.recovery_window_in_days <= 30)
    error_message = "recovery_window_in_days must be 0, or between 7 and 30."
  }
}

variable "kms_key_id" {
  description = <<-DESC
    Customer-managed KMS key to encrypt the secrets with. Null uses the
    account's aws/secretsmanager key, which is free and adequate while every
    principal that can read these secrets is one this stack created. A CMK is
    what lets the key policy deny a reader the IAM policy would have allowed.
  DESC
  type        = string
  default     = null
}

variable "tags" {
  description = "Tags merged onto every secret."
  type        = map(string)
  default     = {}
}
