variable "name_prefix" {
  description = "Prefix applied to every resource name."
  type        = string
}

variable "service_name" {
  description = "Name of the ECS service, and of the task definition family. Must match what the IAM module was told, since the deploy role's policy is written against it."
  type        = string
}


# --- what runs --------------------------------------------------------------

variable "image_repository_url" {
  description = "ECR repository URL without a tag."
  type        = string
}

variable "image_tag" {
  description = <<-DESC
    Tag for the task definition Terraform registers.

    This is the *baseline* revision, not necessarily what is running. The service
    ignores changes to its task definition so CI can register revisions carrying
    newer images — see the `lifecycle` block on aws_ecs_service. Terraform still
    owns everything else about the task: a change to an environment variable or a
    secret here produces a new revision, and the next CI deploy picks it up
    because it renders from the latest revision rather than from a file.
  DESC
  type        = string
  default     = "latest"
}

variable "cpu" {
  description = <<-DESC
    Task CPU in Fargate units: 256 (.25 vCPU), 512, 1024, 2048, 4096.

    Valid memory depends on it — 256 allows only 512/1024/2048 MiB — and Fargate
    rejects an invalid pair at apply rather than at plan.
  DESC
  type        = number
  default     = 256
}

variable "memory" {
  description = "Task memory in MiB. Must be a pairing Fargate allows with cpu."
  type        = number
  default     = 512
}

variable "cpu_architecture" {
  description = <<-DESC
    X86_64 or ARM64.

    ARM64 is about 20% cheaper for the same allocation and the image can be built
    for it — the Dockerfile notes that every compiled dependency ships a manylinux
    wheel for both architectures, so no compiler is needed either way. The default
    is X86_64 only because that is what a `docker build` on a developer's machine
    produces without being told otherwise, and an image of the wrong architecture
    fails at task start with an exec format error that surfaces nowhere but the
    container log.

    Switching is this variable plus `--platform linux/arm64` in whatever builds
    the image. Do both or neither.
  DESC
  type        = string
  default     = "X86_64"

  validation {
    condition     = contains(["X86_64", "ARM64"], var.cpu_architecture)
    error_message = "cpu_architecture must be X86_64 or ARM64."
  }
}

variable "port" {
  description = "Port the container listens on. The Dockerfile's uvicorn binds 8000."
  type        = number
  default     = 8000
}

variable "environment_variables" {
  description = "Plain environment variables. Anything secret belongs in environment_secrets; these are visible to anyone who can describe the task definition."
  type        = map(string)
  default     = {}
}

variable "environment_secrets" {
  description = "Environment variable name to Secrets Manager ARN. Resolved by the *execution* role when the task starts, not by the application."
  type        = map(string)
  default     = {}
}

variable "execution_role_arn" {
  description = "Role the ECS agent uses to pull the image, read the secrets and write to the log group. Used before the container starts."
  type        = string
}

variable "task_role_arn" {
  description = "Role the running container assumes. This is what a compromise of the application reaches."
  type        = string
}


# --- where it runs ----------------------------------------------------------

variable "subnet_ids" {
  description = "Subnets the tasks run in. Public when there is no NAT gateway, private when there is; the networking module decides and outputs ecs_task_subnet_ids."
  type        = list(string)
}

variable "assign_public_ip" {
  description = <<-DESC
    Give each task a public address.

    Has to be true when the subnets are public and there is no NAT: without it a
    Fargate task cannot reach the internet, which means it cannot pull its own
    image, read its secrets, or call a single FHIR endpoint. Has to be false when
    the subnets are private. The networking module outputs the right value.
  DESC
  type        = bool
}

variable "security_group_ids" {
  description = "Security groups on the tasks."
  type        = list(string)
}

variable "alb_subnet_ids" {
  description = "Public subnets the load balancer spans. At least two AZs."
  type        = list(string)
}

variable "alb_security_group_ids" {
  description = "Security groups on the load balancer."
  type        = list(string)
}

variable "has_internet_egress" {
  description = "Whether the tasks can reach the internet by either route. Passed through from the networking module so this module can refuse to build a service that could not reach any EHR."
  type        = bool
  default     = true

  validation {
    condition     = var.has_internet_egress
    error_message = "The tasks have no route to the internet: no public IP and no NAT gateway. The service would come up unable to pull its image, read its secrets, or reach any FHIR endpoint."
  }
}


# --- how many, and for how long ---------------------------------------------

variable "desired_count" {
  description = "Tasks to run. Autoscaling moves this at runtime, so it is the starting point rather than a fixed number."
  type        = number
  default     = 1
}

variable "min_capacity" {
  description = <<-DESC
    Floor for autoscaling.

    Two things bound how low this should go. The application runs one uvicorn
    worker and counts rate limits in process, so N tasks means N times the
    configured limit — which is an argument for keeping this small. And zero is
    not a useful setting: unlike App Runner, ECS has no scale-to-zero with a cold
    start on the next request; zero tasks is simply an outage.
  DESC
  type        = number
  default     = 1
}

variable "max_capacity" {
  description = "Ceiling for autoscaling. This is the cost bound under load."
  type        = number
  default     = 2
}

variable "cpu_target_utilization" {
  description = <<-DESC
    Average CPU percentage autoscaling aims to hold.

    CPU rather than request count, and this is a judgement call worth knowing
    about: most of a request's wall-clock time here is spent waiting on an EHR,
    which costs no CPU at all. So CPU is a lagging signal for this workload and a
    task can be saturated on concurrent awaits while looking idle. ALB request
    count per target would track the real constraint better; CPU is the default
    because it needs no tuning against a request rate nobody has measured yet.
  DESC
  type        = number
  default     = 70
}

variable "use_fargate_spot" {
  description = <<-DESC
    Run tasks on Fargate Spot, which is about 70% cheaper and can be reclaimed by
    AWS with two minutes' notice.

    Reasonable in dev. In prod it means a task can vanish mid-request; with more
    than one task and a load balancer that is a blip, with one task it is a short
    outage. Uvicorn handles SIGTERM, so in-flight requests drain rather than drop.
  DESC
  type        = bool
  default     = false
}


# --- ingress ----------------------------------------------------------------

variable "certificate_arn" {
  description = <<-DESC
    ACM certificate for the load balancer's HTTPS listener, in *this* region —
    not us-east-1, unlike CloudFront's.

    Null leaves the API on plain HTTP, and that is not a working configuration
    for anything but a curl from a terminal: the frontend is served from
    CloudFront over HTTPS, and a browser refuses to let an HTTPS page make
    requests to an HTTP origin. The API is unreachable from the application until
    this is set.

    It needs a domain name, because a certificate cannot be issued for an
    ALB's own *.elb.amazonaws.com hostname. See the README for the two ways out.
  DESC
  type        = string
  default     = null
}

variable "ssl_policy" {
  description = "TLS policy for the HTTPS listener. The default forbids TLS 1.0 and 1.1."
  type        = string
  default     = "ELBSecurityPolicy-TLS13-1-2-2021-06"
}

variable "alb_idle_timeout" {
  description = <<-DESC
    Seconds the load balancer holds an idle connection before closing it.

    Raised above the 60-second default because a single read fans out to several
    FHIR calls against an EHR whose latency this stack does not control. A
    timeout shorter than the slowest legitimate response turns a slow EHR into a
    502 with nothing in the application log to explain it.
  DESC
  type        = number
  default     = 120
}

variable "alb_deletion_protection" {
  description = "Refuse to delete the load balancer. Also blocks `terraform destroy`, which is the intent in prod."
  type        = bool
  default     = false
}

variable "deregistration_delay" {
  description = "Seconds the load balancer waits for in-flight requests to finish before removing a stopping task from rotation. Long enough for a slow FHIR fan-out to complete; too long makes every deploy drag."
  type        = number
  default     = 30
}


# --- health -----------------------------------------------------------------

variable "health_check_path" {
  description = "Path the load balancer probes. /health answers 503 when Postgres is unreachable, so a database outage takes the task out of rotation rather than being served through."
  type        = string
  default     = "/health"
}

variable "health_check_interval" {
  description = "Seconds between probes."
  type        = number
  default     = 15
}

variable "health_check_timeout" {
  description = "Seconds to wait for a probe. The handler opens its own session and runs SELECT 1, so this has to cover a round trip to Aurora."
  type        = number
  default     = 5
}

variable "healthy_threshold" {
  description = "Consecutive successes before a task receives traffic."
  type        = number
  default     = 2
}

variable "unhealthy_threshold" {
  description = "Consecutive failures before a task is taken out of rotation."
  type        = number
  default     = 3
}

variable "health_check_grace_period" {
  description = <<-DESC
    Seconds after a task starts during which a failing load balancer health check
    does not kill it.

    The image pre-compiles its bytecode, so importing the application costs about
    1.3s warm rather than 4.3s cold — but the pull, the secret resolution and the
    interpreter start all happen before uvicorn binds. Too short and a task that
    was going to become healthy is replaced by one that will take just as long.
  DESC
  type        = number
  default     = 60
}


# --- operations -------------------------------------------------------------

variable "log_retention_days" {
  description = <<-DESC
    Retention on the task's log group.

    Actually applied, unlike under App Runner: the awslogs driver writes to a
    group named here, so Terraform creates it and sets this. The manual
    put-retention-policy step that App Runner forced is gone.
  DESC
  type        = number
  default     = 30
}

variable "enable_execute_command" {
  description = <<-DESC
    Allow `aws ecs execute-command` into a running task.

    This is the answer to there being no other way into the VPC: it opens a shell
    in the container over SSM, from which psql reaches Aurora through the same
    security group the application uses. No bastion, no public database endpoint,
    no inbound port.

    It is also a way to read every secret the task holds, so it is an audited
    capability rather than a free one — every session is logged to CloudTrail, and
    who may use it is decided by IAM on the caller, not here. Worth leaving on in
    dev and thinking about in prod.
  DESC
  type        = bool
  default     = true
}

variable "enable_container_insights" {
  description = "Collect per-task CPU, memory and network metrics. Billed as custom CloudWatch metrics; the cluster is otherwise close to unobservable."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags merged onto every resource this module creates."
  type        = map(string)
  default     = {}
}
