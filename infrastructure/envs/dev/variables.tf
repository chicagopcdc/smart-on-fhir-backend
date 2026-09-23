variable "aws_region" {
  description = "Region everything in this environment is created in."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project name, the first half of every resource name."
  type        = string
  default     = "smart-on-fhir"
}

variable "environment" {
  description = "Environment name, the second half of every resource name."
  type        = string
}


# --- network ----------------------------------------------------------------

variable "vpc_cidr" {
  description = "Address space for the VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "az_count" {
  description = "Availability zones to spread across."
  type        = number
  default     = 2
}

variable "single_nat_gateway" {
  description = "One NAT gateway for the whole VPC instead of one per AZ. Cheaper, and a single point of failure for the service's route to the EHRs."
  type        = bool
  default     = true
}


# --- database ---------------------------------------------------------------

variable "database_engine_version" {
  description = "Aurora PostgreSQL version."
  type        = string
  default     = "16.4"
}

variable "database_min_capacity" {
  description = "Serverless v2 floor, in ACUs."
  type        = number
  default     = 0.5
}

variable "database_max_capacity" {
  description = "Serverless v2 ceiling, in ACUs."
  type        = number
  default     = 2
}

variable "database_instance_count" {
  description = "Instances in the cluster. One writer, or a writer and a failover target."
  type        = number
  default     = 1
}

variable "database_backup_retention_days" {
  description = "Days of automated backups."
  type        = number
  default     = 7
}

variable "database_deletion_protection" {
  description = "Refuse to delete the cluster."
  type        = bool
  default     = true
}

variable "database_skip_final_snapshot" {
  description = "Delete the cluster without a final snapshot."
  type        = bool
  default     = false
}

variable "database_apply_immediately" {
  description = "Apply cluster modifications now rather than in the maintenance window."
  type        = bool
  default     = false
}


# --- image and service ------------------------------------------------------

variable "image_tag" {
  description = <<-DESC
    Tag of the backend image to run.

    This is the deployment lever: changing it and applying is what rolls the
    service. Against an IMMUTABLE repository it has to be a tag that was
    actually pushed, so a git sha rather than \"latest\".
  DESC
  type        = string
  default     = "latest"
}

variable "ecr_force_delete" {
  description = "Allow the image repository to be destroyed with images still in it."
  type        = bool
  default     = false
}

variable "ecr_max_tagged_images" {
  description = "How many tagged images to keep, which is how far back a rollback can reach."
  type        = number
  default     = 30
}

variable "service_cpu" {
  description = "App Runner vCPU allocation."
  type        = string
  default     = "0.25 vCPU"
}

variable "service_memory" {
  description = "App Runner memory allocation."
  type        = string
  default     = "1 GB"
}

variable "service_min_size" {
  description = "Instances kept warm."
  type        = number
  default     = 1
}

variable "service_max_size" {
  description = "Ceiling on instances."
  type        = number
  default     = 2
}

variable "service_custom_domain" {
  description = "Domain for the API, or null for the default *.awsapprunner.com hostname."
  type        = string
  default     = null
}

variable "forwarded_allow_ips" {
  description = <<-DESC
    Peers whose X-Forwarded-For header uvicorn will trust, as FORWARDED_ALLOW_IPS.

    It has to be set to something. App Runner terminates TLS and forwards to the
    container over plain HTTP, so without this every request arrives from App
    Runner's forwarder as far as the application is concerned — and the rate
    limiter keys on the client address, which would make one global bucket out of
    what is meant to be a per-client throttle. Every log line's client field
    becomes the same address too.

    "*" — which the repository's README warns against in general, correctly —
    is the right answer *here specifically*, for a reason particular to App
    Runner: the forwarder's address is inside AWS's managed network, is not
    published, and changes. There is no address to name. What makes trusting it
    safe is that the container has no reachable listener except App Runner's
    ingress: the service's egress is the VPC connector, its ingress is App
    Runner's own endpoint, and nothing in the VPC can route to the container. So
    the only peer that can set the header is the one whose header we want.

    That argument stops holding the moment the service is fronted by something
    else, or moved onto ECS behind a load balancer whose address *is* knowable.
    Then this becomes that address, and not a wildcard.
  DESC
  type        = string
  default     = "*"
}

variable "log_retention_days" {
  description = <<-DESC
    Retention to put on the service's CloudWatch log groups.

    Not applied by `terraform apply`: App Runner creates those groups itself,
    named after a service id that does not exist until the service does, and it
    will not adopt a group Terraform made first. The value is here because it is
    a per-environment decision that belongs in version control; the
    log_retention_commands output turns it into the two commands that apply it.
  DESC
  type        = number
  default     = 30
}


# --- frontend ---------------------------------------------------------------

variable "frontend_aliases" {
  description = "Domain names the CloudFront distribution answers on."
  type        = list(string)
  default     = []
}

variable "frontend_acm_certificate_arn" {
  description = "Certificate for frontend_aliases. Must be in us-east-1."
  type        = string
  default     = null
}

variable "frontend_price_class" {
  description = "CloudFront price class."
  type        = string
  default     = "PriceClass_100"
}

variable "frontend_content_security_policy" {
  description = "CSP header for the frontend, or null to send none."
  type        = string
  default     = null
}

variable "frontend_enable_access_logging" {
  description = "Write CloudFront access logs to a bucket."
  type        = bool
  default     = false
}


# --- identity ---------------------------------------------------------------

variable "enable_cognito" {
  description = <<-DESC
    Create a Cognito user pool and point the service at it.

    False leaves the application's login layer unconfigured unless oidc_issuer
    and oidc_client_id are set by hand — which is the path a deployment takes if
    it uses Keycloak, Okta or Google instead. The application treats all of them
    identically; only these two settings change.
  DESC
  type        = bool
  default     = true
}

variable "cognito_mfa_configuration" {
  description = "OFF, OPTIONAL or ON."
  type        = string
  default     = "OPTIONAL"
}

variable "cognito_allow_self_registration" {
  description = "Let anyone with the link create an account, rather than an administrator creating them."
  type        = bool
  default     = false
}

variable "cognito_deletion_protection" {
  description = "Refuse to delete the user pool. Deleting it changes every subject, and the application identifies accounts by (issuer, subject)."
  type        = bool
  default     = true
}

variable "oidc_issuer" {
  description = "Identity provider issuer URL, when not using Cognito. Ignored when enable_cognito is true."
  type        = string
  default     = null
}

variable "oidc_client_id" {
  description = "Client id at that issuer. Ignored when enable_cognito is true."
  type        = string
  default     = null
}

variable "require_user_login" {
  description = <<-DESC
    Require a signed-in user before a provider may be connected.

    False is the application's default, which keeps a frontend that has not
    adopted login working. Turning it on is what makes a patient record reliably
    reachable on the next sign-in, because it stops anonymous records — which
    can never be claimed by an account later — from being created at all.
  DESC
  type        = bool
  default     = false
}


# --- EHR registrations ------------------------------------------------------
#
# Client ids are plain environment variables and client secrets are Secrets
# Manager placeholders. The split is not cosmetic: a client id is public by
# construction — it travels in the authorization URL the browser follows — while
# the secret is what proves the backend is the registered client. Paying $0.40 a
# month to store a value that appears in a query string buys nothing.

variable "epic_issuer" {
  description = "FHIR base URL of the production Epic deployment, which is the issuer an authorization may be started against. Unset leaves that provider rejecting every request."
  type        = string
  default     = null
}

variable "epic_client_id" {
  description = "Client id registered with production Epic."
  type        = string
  default     = null
}

variable "epic_sandbox_client_id" {
  description = "Client id registered with the Epic sandbox."
  type        = string
  default     = null
}

variable "cerner_client_id" {
  description = "Client id registered with Cerner / Oracle Health."
  type        = string
  default     = null
}

variable "smart_launcher_client_id" {
  description = "Client id for the public SMART App Launcher, which does not validate it."
  type        = string
  default     = null
}


# --- deployment -------------------------------------------------------------

variable "github_repository" {
  description = "\"owner/repo\" whose workflows may assume the deploy role, or null to create no deploy role."
  type        = string
  default     = null
}

variable "github_deploy_ref_patterns" {
  description = "GitHub OIDC subject patterns allowed to assume the deploy role."
  type        = list(string)
  default     = ["ref:refs/heads/main"]
}

variable "create_github_oidc_provider" {
  description = "Create the account's GitHub OIDC provider here. At most one environment per account may do this."
  type        = bool
  default     = false
}

variable "github_oidc_provider_arn" {
  description = "ARN of an existing GitHub OIDC provider, when this environment is not creating it."
  type        = string
  default     = null
}


# --- secrets ----------------------------------------------------------------

variable "generate_token_encryption_key" {
  description = "Generate TOKEN_ENCRYPTION_KEY in Terraform. False leaves a placeholder to be written by hand, keeping the key out of state."
  type        = bool
  default     = true
}

variable "secret_recovery_window_days" {
  description = "Days a deleted secret stays recoverable. 0 is what lets an environment be destroyed and re-applied under the same names."
  type        = number
  default     = 30
}

variable "secrets_kms_key_id" {
  description = "Customer-managed KMS key for the secrets, or null for the account's aws/secretsmanager key."
  type        = string
  default     = null
}
