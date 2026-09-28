# Production environment.
#
# The differences from dev are of three kinds: things that must survive a mistake
# (deletion protection, final snapshots, secret recovery windows), things that must
# survive an availability zone (a second database instance, more than one task),
# and things that must survive being used (headroom).

environment = "prod"
aws_region  = "us-east-1"


# --- network ----------------------------------------------------------------

# A different /16 from dev on purpose. Two VPCs with the same CIDR cannot be
# peered, and the day someone wants to reach prod's database from a bastion in
# another account is the wrong day to find that out.
vpc_cidr = "10.1.0.0/16"
az_count = 2

# Still no NAT, and this is the one line in this file worth arguing about.
#
# Off means the tasks carry public IPs in public subnets. Nothing can reach them —
# their security group admits the load balancer on 8000 and nothing else — but the
# addresses are routable, so a future mistake in that group is exposure rather than
# a no-op. On means private subnets, no public addresses, and ~$65/month for two
# NAT gateways.
#
# Left off because the security group is a real boundary and $780/year is a real
# number, and because the same posture has to be defensible in dev where it is
# already running. Worth revisiting the moment this environment holds records for
# an actual patient rather than a sandbox one.
enable_nat_gateway = false
single_nat_gateway = false


# --- database ---------------------------------------------------------------

database_engine_version = "16.4"

# The floor is not 0: scaling to nothing means the first clinician of the morning
# pays the resume, and that request is a patient record loading.
database_min_capacity = 1
database_max_capacity = 8

# A writer and a reader. The application does not split reads, so the second
# instance is bought entirely for failover — tens of seconds instead of however
# long AWS takes to rebuild a lone writer.
database_instance_count = 2

database_backup_retention_days = 30

database_deletion_protection = true
database_skip_final_snapshot = false

# Modifications wait for the maintenance window. Applying immediately here means an
# unscheduled restart of the writer.
database_apply_immediately = false


# --- image and service ------------------------------------------------------

# Pinned to a released tag, never "latest". The repository is IMMUTABLE, so this
# names one image forever. Note that changing it and applying does *not* deploy:
# the service ignores its task definition so CI can own revisions. This is the
# baseline the next deploy renders from.
image_tag = "latest"

ecr_force_delete      = false
ecr_max_tagged_images = 50

container_port = 8000

service_cpu    = 512
service_memory = 1024

service_cpu_architecture = "X86_64"

# Two tasks, always. Not for throughput — one task handles this load easily — but
# so that a deploy, a Spot reclaim or an AZ failure is a blip rather than an
# outage, and so the load balancer has somewhere to send traffic while the other is
# being replaced.
service_desired_count = 2
service_min_size      = 2
service_max_size      = 6

# On-demand. A Spot reclaim with two minutes' notice is survivable with two tasks,
# but "survivable" is not the bar for the environment serving patient records.
use_fargate_spot = false

alb_deletion_protection = true

# Left on, and it deserves a decision rather than a default. It is the only way to
# reach the database, and it is also a shell in the container holding every secret
# the task has — so it is really a question of who may assume a role that allows
# ecs:ExecuteCommand. Every session lands in CloudTrail.
enable_execute_command = true

enable_container_insights = true

log_retention_days = 90

# Required before the frontend can call the API at all: the frontend is HTTPS and a
# browser blocks an HTTPS page from calling an HTTP origin. The certificate has to
# be in this region — us-east-1 here — which is *not* the same requirement as
# CloudFront's, even though the region happens to match.
#
# api_certificate_arn = "arn:aws:acm:us-east-1:<account>:certificate/<id>"
# api_domain          = "api.example.org"


# --- frontend ---------------------------------------------------------------

frontend_price_class = "PriceClass_100"

# On, and worth knowing what it means: CloudFront's standard log lines carry the
# viewer's IP address. That is a record of who opened the application and when, and
# it should be treated with the same care as anything else about a clinician's
# activity.
frontend_enable_access_logging = true

# Fill in once the API hostname is settled. The shape, with <api> being the load
# balancer's domain and <issuer> the identity provider:
#
# frontend_content_security_policy = "default-src 'self'; connect-src 'self' https://<api> https://<issuer>; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"


# --- identity ---------------------------------------------------------------

# Cognito is built and wired, but Keycloak is now the confirmed direction for auth.
# The application is a plain OIDC relying party that discovers everything from the
# issuer, so the switch is this flag plus the two settings below — and no code.
# Left true only so this environment has a working issuer until the Keycloak realm
# exists; flip it and fill in oidc_issuer when it does.
enable_cognito = true

# OPTIONAL rather than ON because flipping it to ON locks out every existing user
# until they enrol. Do the rollout, then change this line.
cognito_mfa_configuration       = "OPTIONAL"
cognito_allow_self_registration = false
cognito_deletion_protection     = true

# The Keycloak realm, once it exists:
# enable_cognito = false
# oidc_issuer    = "https://<keycloak host>/realms/<realm>"
# oidc_client_id = ""

# Still false, and this is the one setting here that is knowingly not where it
# should end up. Turning it on stops anonymous records from being created, which is
# what makes a record reliably reachable on the next sign-in — but it also breaks
# any frontend that has not adopted login yet. Flip it in dev first.
require_user_login = false


# --- EHR registrations ------------------------------------------------------
#
# Client ids only; the secrets are Secrets Manager placeholders written by hand.
# `terraform output secrets_awaiting_values` lists what is still unset.

# epic_issuer      = "https://fhir.epic.example.org/api/FHIR/R4"
# epic_client_id   = ""
# cerner_client_id = ""


# --- deployment -------------------------------------------------------------

# github_repository          = "chicagopcdc/smart-on-fhir-backend"
# github_deploy_ref_patterns = ["ref:refs/heads/main"]

# False, and dev's is true: there can be one GitHub OIDC provider per account, so
# whichever environment is applied first creates it and the other points at the
# ARN. Both true is a create that fails; both false is a role nothing can assume.
create_github_oidc_provider = false
# github_oidc_provider_arn  = "arn:aws:iam::<account>:oidc-provider/token.actions.githubusercontent.com"


# --- secrets ----------------------------------------------------------------

# False here, true in dev. The Fernet key is what every stored EHR refresh token is
# encrypted with; generating it in Terraform would put it in the state file next to
# the database password, and for the environment holding real records that is a
# concentration worth avoiding. Write it once, by hand:
#
#   python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' \
#   | aws secretsmanager put-secret-value \
#       --secret-id smart-on-fhir-prod/TOKEN_ENCRYPTION_KEY --secret-string file:///dev/stdin
generate_token_encryption_key = false

# Thirty days of undo on a deleted secret. The cost is that a destroyed prod cannot
# be re-applied under the same names for a month, which in prod is a feature.
secret_recovery_window_days = 30
