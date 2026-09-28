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

variable "enable_nat_gateway" {
  description = <<-DESC
    Put the tasks in private subnets behind a NAT gateway, instead of in public
    subnets with public IPs.

    Off by default, and that default is why this stack runs on ECS. A Fargate task
    can be given a public IP, so it reaches the EHRs through the internet gateway
    and Aurora over the VPC's local route — no NAT, and no ~$32/month per AZ.
    App Runner could not: its VPC connector's interfaces never get a public
    address, so reaching Aurora cost it the internet.

    Turning it on buys tasks with no public address at all, which cannot be
    exposed by a later mistake in a security group. The boundary is otherwise
    identical — the tasks admit the load balancer and nothing else.
  DESC
  type        = bool
  default     = false
}

variable "single_nat_gateway" {
  description = "When NAT is enabled, one gateway for the whole VPC instead of one per AZ. Ignored otherwise."
  type        = bool
  default     = true
}

variable "alb_ingress_cidrs" {
  description = "Who may reach the API's load balancer. The whole internet by default, because a clinician's browser calls it directly."
  type        = list(string)
  default     = ["0.0.0.0/0"]
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

variable "container_port" {
  description = "Port the application container listens on. The Dockerfile's uvicorn binds 8000; changing it here without changing the image breaks the health check."
  type        = number
  default     = 8000
}

variable "service_cpu" {
  description = "Task CPU in Fargate units: 256 (.25 vCPU), 512, 1024, 2048, 4096. Valid memory depends on it."
  type        = number
  default     = 256
}

variable "service_memory" {
  description = "Task memory in MiB. Must be a pairing Fargate allows with service_cpu; 256 CPU allows only 512, 1024 or 2048."
  type        = number
  default     = 512
}

variable "service_cpu_architecture" {
  description = "X86_64 or ARM64. ARM64 is ~20% cheaper and every dependency has an arm64 wheel, but the image has to be built for it."
  type        = string
  default     = "X86_64"
}

variable "service_desired_count" {
  description = "Tasks to start with. Autoscaling moves it from there."
  type        = number
  default     = 1
}

variable "service_min_size" {
  description = "Autoscaling floor. Zero is an outage, not a cold start — ECS has no scale-to-zero."
  type        = number
  default     = 1
}

variable "service_max_size" {
  description = "Autoscaling ceiling. This is the cost bound under load."
  type        = number
  default     = 2
}

variable "use_fargate_spot" {
  description = "Run tasks on Fargate Spot: ~70% cheaper, reclaimable with two minutes' notice."
  type        = bool
  default     = false
}

variable "api_certificate_arn" {
  description = <<-DESC
    ACM certificate for the API's HTTPS listener, in *this* region — not us-east-1
    like CloudFront's.

    Without it the API is HTTP only, and the frontend cannot call it: the frontend
    is served over HTTPS and a browser will not let an HTTPS page request an HTTP
    origin. Needs a domain, because no certificate can be issued for an ALB's own
    hostname.
  DESC
  type        = string
  default     = null
}

variable "api_domain" {
  description = "Domain pointed at the load balancer, used only to build the api_url output. The DNS record itself is not managed here."
  type        = string
  default     = null
}

variable "alb_deletion_protection" {
  description = "Refuse to delete the load balancer, which also blocks `terraform destroy`."
  type        = bool
  default     = false
}

variable "enable_execute_command" {
  description = <<-DESC
    Allow `aws ecs execute-command` into a running task.

    The only way into the VPC: a shell in the container, from which psql reaches
    Aurora through the same security group the application uses. No bastion, no
    public database endpoint. Also a way to read every secret the task holds, so
    every session is logged to CloudTrail and who may do it is decided by IAM on
    the caller.
  DESC
  type        = bool
  default     = true
}

variable "enable_container_insights" {
  description = "Per-task CPU, memory and network metrics. Billed as custom CloudWatch metrics; without it the cluster is close to unobservable."
  type        = bool
  default     = true
}

variable "forwarded_allow_ips" {
  description = <<-DESC
    Peers whose X-Forwarded-For header uvicorn will trust, as FORWARDED_ALLOW_IPS.

    It has to be set to something. The load balancer terminates TLS and forwards
    over plain HTTP, so without this every request arrives from the balancer as
    far as the application is concerned — and the rate limiter keys on the client
    address, which would make one global bucket out of what is meant to be a
    per-client throttle. Every log line's client field becomes the same address too.

    "*" — which the repository's README warns against in general, correctly — is
    the working answer here. The balancer's nodes take addresses from the public
    subnets and change as it scales, and uvicorn matches this list literally
    rather than by CIDR, so there is no stable set of addresses to name. What
    makes trusting the header safe is the tasks' security group: it admits the
    load balancer on the application port and nothing else, so the balancer is
    the only peer that can set the header.

    One thing worth verifying against the pinned uvicorn rather than taking on
    faith. An ALB *appends* the real peer to X-Forwarded-For and does not strip
    what the client sent, so a request carrying a forged header arrives as
    "1.2.3.4, <real client>". Reading the rightmost entry gives the truth;
    reading the leftmost gives the forgery, and with it a client-controlled rate
    limit bucket. Check which end uvicorn's ProxyHeadersMiddleware takes.
  DESC
  type        = string
  default     = "*"
}

variable "log_retention_days" {
  description = <<-DESC
    Retention on the task's log group.

    Actually applied at apply time, unlike under App Runner: the awslogs driver
    writes to a group this stack creates, so Terraform owns its retention. App
    Runner named its log groups after a service id that could not exist until the
    service did, which made this a manual step forever.
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
