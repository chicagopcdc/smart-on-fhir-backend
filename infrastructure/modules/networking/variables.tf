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
    minimum Aurora's DB subnet group accepts, and the minimum that lets a
    single AZ failure leave a writer somewhere to fail over to.
  DESC
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 4
    error_message = "az_count must be between 2 and 4; Aurora requires at least two and this stack has no use for more than four."
  }
}

variable "enable_nat_gateway" {
  description = <<-DESC
    Whether to give the private subnets a route to the internet.

    This has to be on for the backend to work. App Runner's VPC connector puts
    the service's egress inside the VPC *wholesale* — once a connector is
    attached, outbound traffic no longer leaves through App Runner's own managed
    network, and an ENI in a private subnet with no NAT route can reach nothing
    outside the VPC. The service talks to Epic, Cerner and the OIDC issuer over
    the public internet on every authorization and every token refresh, so
    turning this off is only useful for a plan that provisions the network
    without intending to run the service against it.

    Putting the connector in a *public* subnet does not avoid the NAT: the ENI
    it creates there is never assigned a public IP, so it has no path out either.
  DESC
  type        = bool
  default     = true
}

variable "single_nat_gateway" {
  description = <<-DESC
    Route every private subnet through one NAT gateway instead of one per AZ.

    A NAT gateway is billed hourly whether or not anything crosses it, so this
    is the difference between one standing charge and az_count of them. The
    trade is a single point of failure and a cross-AZ data path: if that AZ
    goes, the backend loses its route to the EHRs even though App Runner is
    still up. Reasonable in dev, wrong in prod.
  DESC
  type        = bool
  default     = false
}

variable "database_port" {
  description = "TCP port the database listens on, opened from the connector's security group to the database's."
  type        = number
  default     = 5432
}

variable "tags" {
  description = "Tags merged onto every resource this module creates."
  type        = map(string)
  default     = {}
}
