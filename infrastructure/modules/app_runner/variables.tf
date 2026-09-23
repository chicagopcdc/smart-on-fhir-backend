variable "service_name" {
  description = "Name of the App Runner service. Must match what the IAM module was told, since the deploy role's policy is written against it."
  type        = string
}

variable "image_repository_url" {
  description = "ECR repository URL without a tag."
  type        = string
}

variable "image_tag" {
  description = <<-DESC
    Tag to deploy.

    \"latest\" against an IMMUTABLE repository is a contradiction — the tag can
    never move, so the service would be pinned to whatever was pushed first.
    Use a git sha or a release tag, and let CI call StartDeployment.
  DESC
  type        = string
  default     = "latest"
}

variable "access_role_arn" {
  description = "Role App Runner assumes to pull the image."
  type        = string
}

variable "instance_role_arn" {
  description = "Role the running container runs as."
  type        = string
}

variable "vpc_connector_arn" {
  description = "VPC connector the service's egress is routed through."
  type        = string
}

variable "port" {
  description = "Port the container listens on. The Dockerfile's uvicorn binds 8000."
  type        = number
  default     = 8000
}

variable "cpu" {
  description = "vCPU allocation, as App Runner spells it: \"0.25 vCPU\", \"0.5 vCPU\", \"1 vCPU\", \"2 vCPU\", \"4 vCPU\"."
  type        = string
  default     = "0.25 vCPU"
}

variable "memory" {
  description = <<-DESC
    Memory allocation: \"0.5 GB\", \"1 GB\", \"2 GB\", \"3 GB\", \"4 GB\"...

    Not every pairing with cpu is legal — 0.25 vCPU takes 0.5 or 1 GB only —
    and App Runner rejects an invalid pair at apply rather than at plan.
  DESC
  type        = string
  default     = "1 GB"
}

variable "min_size" {
  description = <<-DESC
    Instances kept warm.

    Below this App Runner scales to zero and the next request pays a cold start.
    It also interacts with the start command: every instance that comes up runs
    the migration, so a deployment that starts several at once has several
    processes calling `alembic upgrade head` against one database. See
    run_migrations_on_start.
  DESC
  type        = number
  default     = 1
}

variable "max_size" {
  description = "Ceiling on instances. This is the cost bound under load."
  type        = number
  default     = 2
}

variable "max_concurrency" {
  description = <<-DESC
    Requests one instance handles before App Runner adds another.

    App Runner's default is 100. The application deliberately runs a single
    uvicorn worker — token refreshes are coalesced per connection in-process and
    the rate limiter counts in-process, both of which a second worker would
    weaken — so concurrency here is asyncio's, bounded by the database pool and
    by how long the EHR takes to answer. 50 leaves headroom under that.
  DESC
  type        = number
  default     = 50
}

variable "run_migrations_on_start" {
  description = <<-DESC
    Run `alembic upgrade head` before uvicorn, as the container's start command.

    This is the cheapest thing that works on App Runner, which has no notion of
    a one-off task: there is nowhere else inside the VPC to run a migration from
    without standing up a second compute service for it.

    Two things to know before leaving it on:

    - Concurrency. `upgrade head` is a no-op against a current schema, so extra
      instances are usually harmless, but two containers starting at once *can*
      race the alembic_version update; one fails, its container exits, and App
      Runner restarts it — by which time the other has finished. Noisy, not
      unsafe. It stops being acceptable the moment a migration is long enough
      that the health check's patience runs out.
    - A failed migration takes the deployment down rather than leaving the old
      schema and the old code running, because App Runner sees a container that
      never became healthy and rolls back to the previous revision.

    The alternative, once this stops being a first iteration, is to run the
    migration from CI against the cluster and leave the service's start command
    alone.
  DESC
  type        = bool
  default     = true
}

variable "start_command" {
  description = <<-DESC
    Explicit start command, overriding both the image's CMD and whatever
    run_migrations_on_start would compose.

    Set this if App Runner turns out not to interpret the composed command
    through a shell in your account — the `&&` is the part that depends on it.
    The image's own CMD is:

        uvicorn app.main:app --host 0.0.0.0 --port 8000
  DESC
  type        = string
  default     = null
}

variable "environment_variables" {
  description = "Plain environment variables. Anything secret belongs in environment_secrets instead; these are visible to anyone who can describe the service."
  type        = map(string)
  default     = {}
}

variable "environment_secrets" {
  description = "Environment variable name to Secrets Manager ARN. App Runner resolves these with the instance role at container start."
  type        = map(string)
  default     = {}
}

variable "health_check_path" {
  description = "Path App Runner probes. /health answers 503 when Postgres is unreachable, so a database outage fails the probe rather than being served through."
  type        = string
  default     = "/health"
}

variable "health_check_interval" {
  description = "Seconds between probes (1-20)."
  type        = number
  default     = 10
}

variable "health_check_timeout" {
  description = "Seconds to wait for a probe to answer (1-20). The handler opens its own session and runs SELECT 1, so this has to cover a round trip to Aurora."
  type        = number
  default     = 5
}

variable "health_check_healthy_threshold" {
  description = "Consecutive successes before an instance is in service (1-20)."
  type        = number
  default     = 1
}

variable "health_check_unhealthy_threshold" {
  description = <<-DESC
    Consecutive failures before an instance is replaced (1-20).

    With run_migrations_on_start this is also the startup budget: uvicorn does
    not bind until the migration finishes, so the port is closed until then and
    every probe before it fails. interval x this is how long a migration may
    take before the deployment is rolled back.
  DESC
  type        = number
  default     = 5
}

variable "is_publicly_accessible" {
  description = "Whether the service gets a public endpoint. False puts it behind a VPC endpoint instead, which the S3+CloudFront frontend could not reach."
  type        = bool
  default     = true
}

variable "auto_deployments_enabled" {
  description = <<-DESC
    Deploy automatically when a new image lands on the configured tag.

    Only meaningful against a mutable tag; with IMMUTABLE tags nothing ever
    replaces what image_tag points at, and deployment is CI calling
    StartDeployment after Terraform has been pointed at the new tag.
  DESC
  type        = bool
  default     = false
}

variable "custom_domain" {
  description = "Domain to associate with the service, e.g. \"api.example.org\", or null for the *.awsapprunner.com default. The DNS records to create are returned as an output; the association stays pending until they exist."
  type        = string
  default     = null
}

variable "has_internet_egress" {
  description = "Whether the VPC connector's subnets can reach the internet. Passed through from the networking module so this module can refuse to build a service that could not reach any EHR."
  type        = bool
  default     = true

  validation {
    condition     = var.has_internet_egress
    error_message = "The VPC connector's subnets have no internet route (enable_nat_gateway is false). A service attached to that connector can reach the database and nothing else: every SMART authorization, token refresh and FHIR read would fail. Enable NAT, or deploy the network without the service."
  }
}

variable "tags" {
  description = "Tags merged onto every resource this module creates."
  type        = map(string)
  default     = {}
}
