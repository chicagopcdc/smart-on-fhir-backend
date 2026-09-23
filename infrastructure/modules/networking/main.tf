# The network the backend runs in.
#
# Three things live here that could each argue for their own module, and are
# together because they are meaningless apart: the VPC and its subnets, the two
# security groups that describe who may talk to whom, and the App Runner VPC
# connector that is the only thing ever attached to the private subnets. The
# connector is the seam between this module and the service — the App Runner
# module takes its ARN and knows nothing else about the network.

data "aws_availability_zones" "available" {
  state = "available"

  # Opt-in zones (Local Zones, Wavelength) are returned by this call even when
  # the account has never enabled them, and a subnet cannot be created in one.
  # Without this filter the module works in most regions and fails in the ones
  # that happen to list an opt-in zone early.
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)

  # Public subnets take the low /24s, private subnets start at .10, which leaves
  # an unallocated gap between the tiers. The gap is the point: a tier added
  # later (an ECS cluster, an isolated subnet with no route out at all) lands in
  # its own contiguous block instead of being interleaved with these, and the
  # third octet still says at a glance which tier an address belongs to.
  public_subnet_cidrs  = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 8, i)]
  private_subnet_cidrs = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 8, i + 10)]

  # One NAT gateway per AZ, or one shared by all of them. Computed once here so
  # the gateway, its EIP and the route that points at it cannot disagree.
  nat_gateway_count = var.enable_nat_gateway ? (var.single_nat_gateway ? 1 : var.az_count) : 0
}


# --- the VPC itself ---------------------------------------------------------

resource "aws_vpc" "this" {
  cidr_block = var.vpc_cidr

  # Both are required for an RDS endpoint to resolve. Aurora hands out a DNS
  # name, not an address, and the writer/reader split is expressed entirely
  # through DNS, so a VPC without hostname support has a database nothing in it
  # can find.
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(var.tags, { Name = var.name_prefix })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-igw" })
}


# --- public tier ------------------------------------------------------------
#
# Nothing the application runs lives out here. The public subnets exist to hold
# the NAT gateways and to give the route table something to attach the internet
# gateway to; if the frontend ever moves off S3+CloudFront onto something with
# a load balancer, this is where that load balancer goes.

resource "aws_subnet" "public" {
  count = var.az_count

  vpc_id            = aws_vpc.this.id
  cidr_block        = local.public_subnet_cidrs[count.index]
  availability_zone = local.azs[count.index]

  # Off deliberately. Only NAT gateways are placed here, and they carry their
  # own Elastic IP; anything else that lands in a public subnet should have to
  # ask for a public address rather than receive one because of where it sits.
  map_public_ip_on_launch = false

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-public-${local.azs[count.index]}"
    Tier = "public"
  })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-public" })
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  count = var.az_count

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}


# --- egress -----------------------------------------------------------------

resource "aws_eip" "nat" {
  count = local.nat_gateway_count

  domain = "vpc"

  tags = merge(var.tags, { Name = "${var.name_prefix}-nat-${count.index}" })
}

resource "aws_nat_gateway" "this" {
  count = local.nat_gateway_count

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = merge(var.tags, { Name = "${var.name_prefix}-nat-${local.azs[count.index]}" })

  # A NAT gateway in a subnet whose route table has no internet gateway route
  # comes up and silently routes nothing. The dependency is on the route rather
  # than the gateway because attaching the IGW is not what makes the path work.
  depends_on = [aws_route.public_internet]
}


# --- private tier -----------------------------------------------------------
#
# The database and the App Runner connector's ENIs. Reachable from inside the
# VPC only; outbound traffic leaves through NAT if there is one.

resource "aws_subnet" "private" {
  count = var.az_count

  vpc_id            = aws_vpc.this.id
  cidr_block        = local.private_subnet_cidrs[count.index]
  availability_zone = local.azs[count.index]

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-private-${local.azs[count.index]}"
    Tier = "private"
  })
}

# One route table per private subnet even when they would all be identical.
# With single_nat_gateway they do point at the same gateway, but a shared table
# would have to be split — and every subnet re-associated — the moment that
# setting is flipped, which is exactly when the change should be least eventful.
resource "aws_route_table" "private" {
  count = var.az_count

  vpc_id = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-private-${local.azs[count.index]}" })
}

resource "aws_route" "private_nat" {
  count = var.enable_nat_gateway ? var.az_count : 0

  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"

  # With one gateway every subnet points at it; with one per AZ each points at
  # its own, so traffic never crosses a zone boundary to get out.
  nat_gateway_id = aws_nat_gateway.this[var.single_nat_gateway ? 0 : count.index].id
}

resource "aws_route_table_association" "private" {
  count = var.az_count

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}


# --- who may talk to whom ---------------------------------------------------
#
# Two groups, and the rules between them are written as separate resources
# rather than inline blocks. Inline ingress/egress on aws_security_group would
# make each group's definition depend on the other's id, which is a cycle;
# split out, the groups are created first and the rules reference both.

resource "aws_security_group" "app_runner_vpc_connector" {
  name        = "${var.name_prefix}-apprunner-vpc-connector"
  description = "Egress for the App Runner service: the database, and the EHR and IdP endpoints it calls."
  vpc_id      = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-apprunner-vpc-connector" })

  # The connector holds a reference to this group, and AWS refuses to delete a
  # group that is still attached. Creating the replacement first lets the
  # connector move onto it before the old one is removed.
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "rds" {
  name        = "${var.name_prefix}-rds"
  description = "Database ingress: the App Runner VPC connector, and nothing else."
  vpc_id      = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-rds" })

  lifecycle {
    create_before_destroy = true
  }
}

# No ingress rule on the connector's group at all. App Runner terminates
# requests on its own managed endpoint and never routes inbound traffic through
# the connector, so an ingress rule here would open a path nothing arrives on.

resource "aws_vpc_security_group_egress_rule" "connector_to_database" {
  security_group_id            = aws_security_group.app_runner_vpc_connector.id
  description                  = "Postgres to the Aurora cluster"
  referenced_security_group_id = aws_security_group.rds.id
  from_port                    = var.database_port
  to_port                      = var.database_port
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "connector_to_internet" {
  security_group_id = aws_security_group.app_runner_vpc_connector.id

  # Every SMART authorization, token refresh and FHIR read goes to a host the
  # provider registration names, and the OIDC issuer's discovery and JWKS
  # documents are fetched the same way. The set of destinations is open by
  # construction — a provider added in configuration must not need a security
  # group change to be reachable — so this is a destination CIDR rather than a
  # prefix list. The port is the bound that is worth keeping: 443 only.
  description = "HTTPS to EHR FHIR endpoints and the OIDC issuer"
  cidr_ipv4   = "0.0.0.0/0"
  from_port   = 443
  to_port     = 443
  ip_protocol = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "database_from_connector" {
  security_group_id            = aws_security_group.rds.id
  description                  = "Postgres from the App Runner VPC connector"
  referenced_security_group_id = aws_security_group.app_runner_vpc_connector.id
  from_port                    = var.database_port
  to_port                      = var.database_port
  ip_protocol                  = "tcp"
}

# The database group gets no egress rule. A default security group allows all
# outbound; one created here starts with none, and Aurora has nowhere it needs
# to reach on its own — backups, logs and monitoring all go through paths the
# service manages rather than through this group.


# --- the seam to App Runner -------------------------------------------------

resource "aws_apprunner_vpc_connector" "this" {
  # A connector is immutable: changing its subnets or security groups cannot be
  # done in place, so Terraform replaces it. App Runner keeps the name and
  # increments a revision, which is why the same name can be reused across
  # replacements — and why create_before_destroy below is enough to move a live
  # service onto the new revision before the old one goes.
  vpc_connector_name = substr("${var.name_prefix}-connector", 0, 40)

  # Private subnets, per AWS's own constraint: the connector's ENIs get no
  # public IP, so a public subnet would leave them with no route out. Everything
  # they reach outside the VPC goes through NAT — see enable_nat_gateway.
  subnets         = aws_subnet.private[*].id
  security_groups = [aws_security_group.app_runner_vpc_connector.id]

  tags = merge(var.tags, { Name = "${var.name_prefix}-connector" })

  # Same reason as the security groups: a service still pointing at this
  # connector pins it, so the replacement has to exist before the old one goes.
  lifecycle {
    create_before_destroy = true
  }
}
