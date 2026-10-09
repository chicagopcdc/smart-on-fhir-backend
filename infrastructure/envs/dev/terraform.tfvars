# Development environment.
#
# The theme here is that nothing is precious: the cluster can be deleted, the
# repository can be emptied, and the whole environment can be destroyed and
# rebuilt under the same names. Every setting that differs from prod differs for
# that reason or for cost.

environment = "dev"
aws_region  = "us-east-1"


# --- network ----------------------------------------------------------------

vpc_cidr = "10.0.0.0/16"
az_count = 2

# No NAT gateway. The tasks sit in the public subnets with public IPs, reach Epic
# and Cerner through the internet gateway and Aurora over the VPC's local route.
# This is the saving that moving off App Runner bought: a NAT gateway is about
# $32/month before a byte crosses it, and it was unavoidable on App Runner because
# its VPC connector's interfaces never get a public address.
#
# The tasks admit the load balancer on port 8000 and nothing else, so a public
# subnet is not a public container.
enable_nat_gateway = false


# --- database ---------------------------------------------------------------

database_engine_version = "16.4"

# Scales to near nothing overnight and up to 1 ACU under someone clicking through
# a flow. A single instance, so an AZ failure is an outage until AWS rebuilds it.
database_min_capacity   = 0.5
database_max_capacity   = 1
database_instance_count = 1

database_backup_retention_days = 1

# Both deliberate, and both wrong for prod. Together they are what make
# `terraform destroy` followed by `terraform apply` a thing that works, which is
# the point of this environment.
database_deletion_protection = false
database_skip_final_snapshot = true

# Nothing here is serving anyone, so waiting for a maintenance window to see a
# parameter change is just waiting.
database_apply_immediately = true


# --- image and service ------------------------------------------------------

image_tag = "latest"

ecr_force_delete      = true
ecr_max_tagged_images = 10

container_port = 8000

# The Fargate floor: 0.25 vCPU, 512 MiB. The application is one uvicorn worker
# whose time goes on waiting for an EHR, so this is mostly about having room for
# the interpreter and the connection pool.
service_cpu    = 256
service_memory = 512

service_cpu_architecture = "X86_64"

service_desired_count = 1
service_min_size      = 1
service_max_size      = 2

# ~70% off, and a task can be reclaimed with two minutes' notice. In dev the
# interruption costs a restart nobody is watching.
use_fargate_spot = true

# Nothing here is worth protecting from a destroy.
alb_deletion_protection = false

# The only way to reach Aurora. `terraform output exec_command` prints the
# invocation; there is no bastion and no public database endpoint.
enable_execute_command = true

# Off in dev. Container Insights bills as custom CloudWatch metrics per task, and
# in an environment with one task and no users there is nothing to see.
enable_container_insights = false

log_retention_days = 7

# The API has no certificate, so it is HTTP on the load balancer's own hostname —
# usable with curl, and *not* usable from the frontend, because a browser will not
# let an HTTPS page call an HTTP origin. Nothing works end to end until a domain
# and a regional certificate exist.
#
# api_certificate_arn = "arn:aws:acm:us-east-1:<account>:certificate/<id>"
# api_domain          = "api-dev.example.org"


# --- frontend ---------------------------------------------------------------

frontend_price_class           = "PriceClass_100"
frontend_enable_access_logging = false


# --- identity ---------------------------------------------------------------

enable_cognito                  = true
cognito_mfa_configuration       = "OPTIONAL"
cognito_allow_self_registration = false

# The one thing about the pool that is not disposable: the application identifies
# an account by (issuer, subject), so a rebuilt pool orphans every record its
# users had connected. Cheap to keep, annoying to lose.
cognito_deletion_protection = true

# Left off so the current frontend, which does not sign anyone in yet, keeps
# working against this environment. Turn it on here first, once the frontend has
# adopted login, and see what breaks before prod does.
require_user_login = false


# --- EHR registrations ------------------------------------------------------
#
# The sandboxes only. Production Epic and Cerner registrations belong to prod, and
# pointing a dev deployment at them would put real records behind a stack that can
# be destroyed by design.
#
# The matching secrets are Secrets Manager placeholders — `terraform output
# secrets_awaiting_values` lists them.

# epic_sandbox_client_id   = ""
# smart_launcher_client_id = ""


# --- deployment -------------------------------------------------------------

# github_repository           = "chicagopcdc/smart-on-fhir-backend"
# github_deploy_ref_patterns  = ["ref:refs/heads/main"]
# create_github_oidc_provider = true


# --- secrets ----------------------------------------------------------------

generate_token_encryption_key = true

# Zero, so a destroyed environment can be re-applied immediately. A non-zero
# window keeps the *names* reserved for that many days, and the next apply fails on
# every secret with InvalidRequestException.
secret_recovery_window_days = 0
