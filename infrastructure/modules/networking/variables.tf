variable "name_prefix" {
  description = "Prefix applied to every resource name, e.g. \"smart-on-fhir-dev\"."
  type        = string
}

variable "vpc_cidr" {
  description = <<-DESC
    Address space for the VPC. A /16 leaves room for the /24s carved below and
    for tiers this stack does not have yet. Keep it out of the range any EHR VPN
    or peered network already uses: the FHIR endpoints this service calls are
    reached over the internet today, but a customer that insists on private
    connectivity is a peering away, and overlapping CIDRs cannot be peered.
  DESC
  type        = string
  default     = "10.0.0.0/16"

  validation {
    condition     = can(cidrnetmask(var.vpc_cidr)) && tonumber(split("/", var.vpc_cidr)[1]) <= 20
    error_message = "vpc_cidr must be valid CIDR notation and no smaller than a /20, or the subnets carved from it will not fit."
  }
}

variable "az_count" {
  description = <<-DESC
    How many availability zones to spread the subnets across. Two is the
    minimum Aurora's DB subnet group accepts, the minimum an ALB accepts, and
    the minimum that lets a single AZ failure leave a writer somewhere to fail
    over to.
  DESC
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 4
    error_message = "az_count must be between 2 and 4; Aurora and the load balancer both require at least two, and this stack has no use for more than four."
  }
}

variable "enable_nat_gateway" {
  description = <<-DESC
    Give the private subnets a route to the internet, and move the ECS tasks
    into them.

    **Off by default, and that default is the whole reason this stack runs on
    ECS rather than App Runner.**

    A Fargate task can be given a public IP. That one capability is what removes
    the NAT gateway: a task in a public subnet with `assign_public_ip = true`
    reaches Epic, Cerner and the identity provider straight through the internet
    gateway, and reaches Aurora in the private subnets over the VPC's local
    route. Both destinations work, and neither needs a NAT.

    App Runner could not do this. Its VPC connector's network interfaces are
    never assigned a public address, and an interface with no public address has
    nothing an internet gateway can translate — so attaching a connector to
    reach Aurora took away the internet access the service previously had, and
    the only way back was a NAT gateway at about $32/month per AZ.

    Turning this on buys the classic layout instead: tasks in private subnets
    with no public address at all, reaching the internet through NAT. The
    security boundary is the same either way — it is the task security group,
    which admits nothing but the load balancer — but a task without a public IP
    cannot be exposed by a later mistake in that group. That is what the money
    buys. See `ecs_task_subnet_ids` and `assign_public_ip` in the outputs.
  DESC
  type        = bool
  default     = false
}

variable "single_nat_gateway" {
  description = <<-DESC
    When NAT is enabled, route every private subnet through one gateway instead
    of one per AZ. Halves a standing charge and accepts a single point of failure
    for the route to every EHR. Ignored when enable_nat_gateway is false.
  DESC
  type        = bool
  default     = true
}

variable "app_port" {
  description = "Port the application container listens on, opened from the load balancer's security group to the tasks'."
  type        = number
  default     = 8000
}

variable "database_port" {
  description = "TCP port the database listens on, opened from the tasks' security group to the database's."
  type        = number
  default     = 5432
}

variable "alb_ingress_cidrs" {
  description = <<-DESC
    Who may reach the load balancer. The whole internet by default, because the
    frontend is served from CloudFront and a clinician's browser calls the API
    directly from wherever they are.

    Narrowing it is the one lever that makes the API not-public without a
    rewrite — a hospital's egress ranges, or a VPN's — and it is worth
    remembering that it exists.
  DESC
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "tags" {
  description = "Tags merged onto every resource this module creates."
  type        = map(string)
  default     = {}
}
