# One environment of the SMART on FHIR stack.
#
# This file is identical in envs/dev and envs/prod on purpose. Everything that
# differs between them — instance sizes, whether the database may be deleted,
# how many NAT gateways stand up, whether login is required — is a variable, and
# the difference lives in each environment's terraform.tfvars. Two copies of the
# same composition is the price of `terraform apply` meaning one environment;
# two copies that have quietly drifted apart is the thing to avoid, so a change
# here belongs in both files in the same commit.

locals {
  name_prefix = "${var.project}-${var.environment}"

  # The cluster takes the environment's name and the service adds "-backend", so a
  # second service in the same environment has somewhere to go. Both are passed to
  # the iam module as strings rather than read back from the ecs module's outputs:
  # ECS needs the roles before it can create the resources those policies name, so
  # taking the names from here is what keeps it from being a cycle.
  cluster_name = "${var.project}-${var.environment}"
  service_name = "${var.project}-${var.environment}-backend"

  # Cognito when this environment creates one, otherwise whatever was configured
  # by hand. Both null leaves the application's login routes answering 503 and
  # the anonymous connect flow unchanged, which is a legitimate state — login is
  # an opt-in layer.
  oidc_issuer    = var.enable_cognito ? module.cognito[0].issuer : var.oidc_issuer
  oidc_client_id = var.enable_cognito ? module.cognito[0].client_id : var.oidc_client_id

  # Plain configuration, in the shape app/core/config.py reads. Nulls are
  # stripped below rather than passed as empty strings: pydantic-settings treats
  # an empty string as a value, so EPIC_ISSUER="" would mean "allowlist the
  # empty issuer" instead of "this provider is not configured".
  environment_variables_raw = {
    FRONTEND_HOSTNAME    = module.frontend.url
    CORS_ALLOWED_ORIGINS = module.frontend.url

    # The awslogs driver ships stdout to CloudWatch, where one object per line is
    # what Logs Insights can query. The text format is for a developer's terminal.
    LOG_FORMAT = "json"

    # Read by uvicorn, not by the application, which is why it is not in
    # .env.example alongside the application's own settings. See the variable's own
    # description for why a wildcard is the working value behind the load balancer,
    # and for the one thing about it worth verifying.
    FORWARDED_ALLOW_IPS = var.forwarded_allow_ips

    REQUIRE_USER_LOGIN = var.require_user_login ? "true" : "false"

    OIDC_ISSUER    = local.oidc_issuer
    OIDC_CLIENT_ID = local.oidc_client_id

    EPIC_ISSUER              = var.epic_issuer
    EPIC_CLIENT_ID           = var.epic_client_id
    EPIC_SANDBOX_CLIENT_ID   = var.epic_sandbox_client_id
    CERNER_CLIENT_ID         = var.cerner_client_id
    SMART_LAUNCHER_CLIENT_ID = var.smart_launcher_client_id
  }

  environment_variables = {
    for key, value in local.environment_variables_raw : key => value if value != null
  }

  # Values that must not be readable from the service description. Each is
  # created as an empty container here and filled out of band — see the
  # secrets module — because none of them is this stack's to generate.
  placeholder_secrets = {
    OIDC_CLIENT_SECRET = {
      description = "Client secret at the identity provider users sign in with"
    }
    EPIC_CLIENT_SECRET = {
      description = "OAuth client secret registered with production Epic"
    }
    EPIC_SANDBOX_CLIENT_SECRET = {
      description = "OAuth client secret registered with the Epic sandbox"
    }
    CERNER_CLIENT_SECRET = {
      description = "OAuth client secret from the Cerner Central system account"
    }
  }
}


# --- network ----------------------------------------------------------------

module "networking" {
  source = "../../modules/networking"

  name_prefix        = local.name_prefix
  vpc_cidr           = var.vpc_cidr
  az_count           = var.az_count
  enable_nat_gateway = var.enable_nat_gateway
  single_nat_gateway = var.single_nat_gateway
  alb_ingress_cidrs  = var.alb_ingress_cidrs
  app_port           = var.container_port
}


# --- image ------------------------------------------------------------------

module "ecr" {
  source = "../../modules/ecr"

  name_prefix       = local.name_prefix
  force_delete      = var.ecr_force_delete
  max_tagged_images = var.ecr_max_tagged_images
}


# --- data -------------------------------------------------------------------

module "database" {
  source = "../../modules/database"

  name_prefix        = local.name_prefix
  subnet_ids         = module.networking.private_subnet_ids
  security_group_ids = [module.networking.rds_security_group_id]

  engine_version          = var.database_engine_version
  min_capacity            = var.database_min_capacity
  max_capacity            = var.database_max_capacity
  instance_count          = var.database_instance_count
  backup_retention_period = var.database_backup_retention_days
  deletion_protection     = var.database_deletion_protection
  skip_final_snapshot     = var.database_skip_final_snapshot
  apply_immediately       = var.database_apply_immediately
}


# --- frontend ---------------------------------------------------------------
#
# Before the backend, because its URL is what the backend's FRONTEND_HOSTNAME,
# CORS_ALLOWED_ORIGINS and OIDC redirect URI are all derived from.

module "frontend" {
  source = "../../modules/frontend"

  name_prefix             = local.name_prefix
  aliases                 = var.frontend_aliases
  acm_certificate_arn     = var.frontend_acm_certificate_arn
  price_class             = var.frontend_price_class
  content_security_policy = var.frontend_content_security_policy
  enable_access_logging   = var.frontend_enable_access_logging
}


# --- identity ---------------------------------------------------------------

module "cognito" {
  source = "../../modules/cognito"
  count  = var.enable_cognito ? 1 : 0

  name_prefix = local.name_prefix

  # Exactly what app/core/config.py builds from FRONTEND_HOSTNAME. Written here
  # rather than passed in, so the two cannot be changed independently and left
  # to fail at the end of a login with a redirect_mismatch.
  callback_urls = ["${module.frontend.url}/auth/login/callback"]
  logout_urls   = [module.frontend.url]

  mfa_configuration       = var.cognito_mfa_configuration
  allow_self_registration = var.cognito_allow_self_registration
  deletion_protection     = var.cognito_deletion_protection
}


# --- secrets ----------------------------------------------------------------

module "secrets" {
  source = "../../modules/secrets"

  name_prefix = local.name_prefix

  managed_secrets = {
    DATABASE_URL = {
      description = "Async Postgres connection string for the Aurora cluster"
      value       = module.database.database_url
    }
  }

  placeholder_secrets           = local.placeholder_secrets
  generate_token_encryption_key = var.generate_token_encryption_key
  recovery_window_in_days       = var.secret_recovery_window_days
  kms_key_id                    = var.secrets_kms_key_id
}


# --- roles ------------------------------------------------------------------

module "iam" {
  source = "../../modules/iam"

  name_prefix  = local.name_prefix
  cluster_name = local.cluster_name
  service_name = local.service_name

  ecr_repository_arn     = module.ecr.repository_arn
  secret_arns            = values(module.secrets.secret_arns)
  secrets_kms_key_arn    = var.secrets_kms_key_id
  enable_execute_command = var.enable_execute_command

  github_repository           = var.github_repository
  github_deploy_ref_patterns  = var.github_deploy_ref_patterns
  create_github_oidc_provider = var.create_github_oidc_provider
  github_oidc_provider_arn    = var.github_oidc_provider_arn

  frontend_bucket_arn       = module.frontend.bucket_arn
  frontend_distribution_arn = module.frontend.distribution_arn
}


# --- the service ------------------------------------------------------------

module "ecs" {
  source = "../../modules/ecs"

  name_prefix  = local.cluster_name
  service_name = local.service_name

  image_repository_url = module.ecr.repository_url
  image_tag            = var.image_tag

  execution_role_arn = module.iam.execution_role_arn
  task_role_arn      = module.iam.task_role_arn

  # Where the tasks run and whether they get a public address are one decision,
  # made in the networking module because it is a routing question. Passing them
  # separately here would let them disagree.
  subnet_ids         = module.networking.ecs_task_subnet_ids
  assign_public_ip   = module.networking.assign_public_ip
  security_group_ids = [module.networking.ecs_tasks_security_group_id]

  alb_subnet_ids         = module.networking.public_subnet_ids
  alb_security_group_ids = [module.networking.alb_security_group_id]

  # Passed through so the service refuses to build against a network its tasks
  # cannot leave, rather than deploying and failing on the first image pull.
  has_internet_egress = module.networking.has_internet_egress

  port             = var.container_port
  cpu              = var.service_cpu
  memory           = var.service_memory
  cpu_architecture = var.service_cpu_architecture

  desired_count    = var.service_desired_count
  min_capacity     = var.service_min_size
  max_capacity     = var.service_max_size
  use_fargate_spot = var.use_fargate_spot

  certificate_arn         = var.api_certificate_arn
  alb_deletion_protection = var.alb_deletion_protection

  environment_variables = local.environment_variables
  environment_secrets   = module.secrets.secret_arns

  log_retention_days     = var.log_retention_days
  enable_execute_command = var.enable_execute_command

  enable_container_insights = var.enable_container_insights
}
