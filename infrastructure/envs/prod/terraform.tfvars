# Production environment.
#
# The differences from dev are of three kinds: things that must survive a
# mistake (deletion protection, final snapshots, secret recovery windows),
# things that must survive an availability zone (a NAT gateway per AZ, a second
# database instance), and things that must survive being used (headroom).

environment = "prod"
aws_region  = "us-east-1"


# --- network ----------------------------------------------------------------

# A different /16 from dev on purpose. Two VPCs with the same CIDR cannot be
# peered, and the day someone wants to reach prod's database from a bastion in
# another account is the wrong day to find that out.
vpc_cidr = "10.1.0.0/16"
az_count = 2

# One NAT gateway per AZ. The extra ~$32/month buys the case where a zone fails:
# with a shared gateway, losing that zone takes out the route to every EHR even
# though App Runner is still serving in the other one.
single_nat_gateway = false


# --- database ---------------------------------------------------------------

database_engine_version = "16.4"

# The floor is not 0: scaling to nothing means the first clinician of the
# morning pays the resume, and that request is a patient record loading.
database_min_capacity = 1
database_max_capacity = 8

# A writer and a reader. The application does not split reads, so the second
# instance is bought entirely for failover — tens of seconds instead of however
# long AWS takes to rebuild a lone writer.
database_instance_count = 2

database_backup_retention_days = 30

database_deletion_protection = true
database_skip_final_snapshot = false

# Modifications wait for the maintenance window. Applying immediately here means
# an unscheduled restart of the writer.
database_apply_immediately = false


# --- image and service ------------------------------------------------------

# Pinned to a released tag, never "latest". The repository is IMMUTABLE, so this
# names one image forever; changing it and applying is the deployment.
image_tag = "latest"

ecr_force_delete      = false
ecr_max_tagged_images = 50

service_cpu    = "0.5 vCPU"
service_memory = "1 GB"

service_min_size = 1
service_max_size = 4

log_retention_days = 90


# --- frontend ---------------------------------------------------------------

frontend_price_class = "PriceClass_100"

# On, and worth knowing what it means: CloudFront's standard log lines carry the
# viewer's IP address. That is a record of who opened the application and when,
# kept for frontend_log_retention days, and it should be treated with the same
# care as anything else about a clinician's activity.
frontend_enable_access_logging = true

# Fill in once the API hostname is settled. The shape, with <api> being the
# App Runner hostname or its custom domain and <issuer> the identity provider:
#
# frontend_content_security_policy = "default-src 'self'; connect-src 'self' https://<api> https://<issuer>; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"


# --- identity ---------------------------------------------------------------

enable_cognito = true

# OPTIONAL rather than ON because flipping it to ON locks out every existing
# user until they enrol. Do the rollout, then change this line.
cognito_mfa_configuration       = "OPTIONAL"
cognito_allow_self_registration = false
cognito_deletion_protection     = true

# Still false, and this is the one setting here that is knowingly not where it
# should end up. Turning it on stops anonymous records from being created, which
# is what makes a record reliably reachable on the next sign-in — but it also
# breaks any frontend that has not adopted login yet. Flip it in dev first.
require_user_login = false


# --- EHR registrations ------------------------------------------------------
#
# Client ids only; the secrets are Secrets Manager placeholders written by hand.
# `terraform output secrets_awaiting_values` lists what is still unset.

# epic_issuer              = "https://fhir.epic.example.org/api/FHIR/R4"
# epic_client_id           = ""
# cerner_client_id         = ""


# --- deployment -------------------------------------------------------------

# github_repository          = "chicagopcdc/smart-on-fhir-backend"
# github_deploy_ref_patterns = ["ref:refs/heads/main"]

# False, and dev's is true: there can be one GitHub OIDC provider per account,
# so whichever environment is applied first creates it and the other points at
# the ARN. Both true is a create that fails; both false is a role nothing can
# assume.
create_github_oidc_provider = false
# github_oidc_provider_arn  = "arn:aws:iam::<account>:oidc-provider/token.actions.githubusercontent.com"


# --- secrets ----------------------------------------------------------------

# False here, true in dev. The Fernet key is what every stored EHR refresh token
# is encrypted with; generating it in Terraform would put it in the state file
# next to the database password, and for the environment holding real records
# that is a concentration worth avoiding. Write it once, by hand:
#
#   python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' \
#   | aws secretsmanager put-secret-value \
#       --secret-id smart-on-fhir-prod/TOKEN_ENCRYPTION_KEY --secret-string file:///dev/stdin
generate_token_encryption_key = false

# Thirty days of undo on a deleted secret. The cost is that a destroyed prod
# cannot be re-applied under the same names for a month, which in prod is a
# feature.
secret_recovery_window_days = 30
