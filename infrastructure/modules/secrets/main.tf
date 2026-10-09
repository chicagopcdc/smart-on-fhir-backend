# Every value the application reads from the environment that must not be in
# source, as one Secrets Manager secret per environment variable.
#
# One secret per variable rather than a single JSON blob, for two reasons. App
# Runner's runtime_environment_secrets maps a variable name to a secret ARN and
# injects the whole value, so a JSON blob would arrive as a JSON string the
# application would have to parse — and app/core/config.py reads plain strings.
# And it is what makes least privilege expressible: the task role can be granted
# exactly these ARNs, so a secret added for something else is not readable by
# this service merely because it lives in the same blob.
#
# The cost is roughly $0.40 per secret per month.

locals {
  # The two sources of secret *values* differ only in where the value comes
  # from; everything about the container is the same, so they are merged into
  # one map and the resources below iterate it once.
  generated_secrets = var.generate_token_encryption_key ? {
    TOKEN_ENCRYPTION_KEY = {
      description = "Fernet key encrypting EHR access and refresh tokens at rest"

      # random_bytes emits standard base64; Fernet expects the URL-safe
      # alphabet. 32 bytes is the length the Fernet spec fixes — it is a 128-bit
      # signing key and a 128-bit encryption key concatenated — so this is not a
      # tunable.
      value = replace(replace(random_bytes.token_encryption_key[0].base64, "+", "-"), "/", "_")
    }
  } : {}

  value_secrets = merge(var.managed_secrets, local.generated_secrets)

  # A placeholder that is obviously not a credential. The application will fail
  # against it, which is the intended outcome: a service that starts with a
  # placeholder Epic secret would fail later, at the first authorization, and
  # look like an Epic problem.
  placeholder_value = "REPLACE_ME"

  placeholder_only = var.generate_token_encryption_key ? var.placeholder_secrets : merge(
    var.placeholder_secrets,
    {
      TOKEN_ENCRYPTION_KEY = {
        description = "Fernet key encrypting EHR access and refresh tokens at rest; written out of band"
      }
    }
  )

  all_secret_keys = concat(keys(local.value_secrets), keys(local.placeholder_only))
}

resource "random_bytes" "token_encryption_key" {
  count = var.generate_token_encryption_key ? 1 : 0

  length = 32
}


# --- the containers ---------------------------------------------------------
#
# Split from the versions below so that a secret's *existence* and its *value*
# are separate resources: the placeholder ones need lifecycle rules on the
# version that the managed ones must not have.

resource "aws_secretsmanager_secret" "managed" {
  for_each = local.value_secrets

  name        = "${var.name_prefix}/${each.key}"
  description = each.value.description
  kms_key_id  = var.kms_key_id

  recovery_window_in_days = var.recovery_window_in_days

  tags = merge(var.tags, {
    Name    = "${var.name_prefix}/${each.key}"
    Managed = "terraform"
  })
}

resource "aws_secretsmanager_secret_version" "managed" {
  for_each = local.value_secrets

  secret_id     = aws_secretsmanager_secret.managed[each.key].id
  secret_string = each.value.value
}

resource "aws_secretsmanager_secret" "placeholder" {
  for_each = local.placeholder_only

  name        = "${var.name_prefix}/${each.key}"
  description = each.value.description
  kms_key_id  = var.kms_key_id

  recovery_window_in_days = var.recovery_window_in_days

  tags = merge(var.tags, {
    Name    = "${var.name_prefix}/${each.key}"
    Managed = "out-of-band"
  })
}

resource "aws_secretsmanager_secret_version" "placeholder" {
  for_each = local.placeholder_only

  secret_id     = aws_secretsmanager_secret.placeholder[each.key].id
  secret_string = local.placeholder_value

  lifecycle {
    # The whole point. Terraform creates the first version and then never looks
    # at the value again, so whoever pastes the real credential in owns it from
    # then on and no apply takes it back.
    #
    # version_stages is ignored for the same reason: put-secret-value moves
    # AWSCURRENT onto the new version and leaves AWSPREVIOUS on this one, which
    # Terraform would otherwise read as drift it should correct.
    ignore_changes = [secret_string, version_stages]
  }
}
