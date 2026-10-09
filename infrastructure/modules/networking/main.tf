# The network the backend runs in.
#
# The VPC and its subnets, and the three security groups that describe who may
# talk to whom: the load balancer, the ECS tasks behind it, and the database
# behind them. They are in one module because they are meaningless apart — a
# subnet with no rule about what may enter it describes nothing.
#
# Where the tasks actually run is this module's decision, not the ECS module's,
# because it is a routing question: with no NAT gateway they go in the public
# subnets and carry public IPs so they can reach the EHRs directly; with NAT
# they go in the private subnets and carry none. `ecs_task_subnet_ids` and
# `assign_public_ip` are the two outputs that carry that decision forward.

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

  # One NAT gateway per AZ, or one shared by all of them, or none — which is the
  # default. Computed once here so the gateway, its EIP and the route that points
  # at it cannot disagree.
  nat_gateway_count = var.enable_nat_gateway ? (var.single_nat_gateway ? 1 : var.az_count) : 0

  # Where the tasks run, and whether they get a public address. The two always
  # move together: assign_public_ip only works in a subnet with a route to the
  # internet gateway, and a task in a private subnet without NAT can reach
  # nothing outside the VPC. Deciding both here is what stops them diverging.
  ecs_task_subnet_ids = var.enable_nat_gateway ? aws_subnet.private[*].id : aws_subnet.public[*].id
  assign_public_ip    = !var.enable_nat_gateway
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
# The load balancer always lives here, and by default so do the ECS tasks — with
# public IPs, which is what lets them reach Epic without a NAT gateway. When NAT
# is enabled the tasks move to the private subnets and these hold the load
# balancer and the gateways only.
#
# "Public subnet" names a routing property, not a security posture. Nothing out
# here is reachable from the internet because of where it sits; it is reachable
# because a security group says so, and the tasks' group admits the load balancer
# and nothing else.

resource "aws_subnet" "public" {
  count = var.az_count

  vpc_id            = aws_vpc.this.id
  cidr_block        = local.public_subnet_cidrs[count.index]
  availability_zone = local.azs[count.index]

  # Off, and it is not the setting that gives the tasks their public addresses —
  # that is `assign_public_ip` on the ECS service's network configuration, which
  # is a property of the task rather than of the subnet. This one only affects
  # EC2 instances launched here, of which there are none. Leaving it off means
  # anything that does land here has to ask for a public address rather than
  # receive one for sitting in the wrong place.
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
# Always the database; the ECS tasks too when NAT is enabled. Reachable from
# inside the VPC only, and with no route out at all unless a NAT gateway exists.

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
# Three groups, in a chain: the internet may reach the load balancer, the load
# balancer may reach the tasks, the tasks may reach the database. Nothing skips
# a link.
#
# The rules are separate resources rather than inline ingress/egress blocks.
# Inline, each group's definition would depend on the next one's id, which is a
# cycle; split out, the groups are created first and the rules reference both.

resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-alb"
  description = "Public entry point: HTTP and HTTPS from the internet, forwarding to the tasks."
  vpc_id      = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-alb" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "ecs_tasks" {
  name        = "${var.name_prefix}-ecs-tasks"
  description = "The application containers: in from the load balancer, out to the database and to the EHR and IdP endpoints."
  vpc_id      = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-ecs-tasks" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "rds" {
  name        = "${var.name_prefix}-rds"
  description = "Database ingress: the ECS tasks, and nothing else."
  vpc_id      = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-rds" })

  lifecycle {
    create_before_destroy = true
  }
}


# Load balancer, inbound.

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  for_each = toset(var.alb_ingress_cidrs)

  security_group_id = aws_security_group.alb.id

  # Open even when a certificate is configured and this port does nothing but
  # redirect. A browser that is sent to http:// has to be able to arrive
  # somewhere in order to be told to go to https:// instead; closing 80 turns a
  # redirect into a connection timeout.
  description = "HTTP, redirected to HTTPS when a certificate is configured"
  cidr_ipv4   = each.value
  from_port   = 80
  to_port     = 80
  ip_protocol = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  for_each = toset(var.alb_ingress_cidrs)

  security_group_id = aws_security_group.alb.id
  description       = "HTTPS"
  cidr_ipv4         = each.value
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_tasks" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Forward to the application containers"
  referenced_security_group_id = aws_security_group.ecs_tasks.id
  from_port                    = var.app_port
  to_port                      = var.app_port
  ip_protocol                  = "tcp"
}


# Tasks.

resource "aws_vpc_security_group_ingress_rule" "tasks_from_alb" {
  security_group_id = aws_security_group.ecs_tasks.id

  # The only ingress rule on the tasks, and the reason a public subnet is not a
  # public container. With a public IP and no rule admitting the internet, the
  # address is routable and the port is not.
  description                  = "Application traffic from the load balancer"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = var.app_port
  to_port                      = var.app_port
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "tasks_to_database" {
  security_group_id            = aws_security_group.ecs_tasks.id
  description                  = "Postgres to the Aurora cluster"
  referenced_security_group_id = aws_security_group.rds.id
  from_port                    = var.database_port
  to_port                      = var.database_port
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "tasks_to_internet" {
  security_group_id = aws_security_group.ecs_tasks.id

  # Four kinds of destination share this rule, and all four are HTTPS to a host
  # this stack does not choose: the EHRs a provider registration names, the OIDC
  # issuer's discovery and JWKS documents, and — because the tasks reach AWS's
  # own public endpoints rather than going through interface endpoints — ECR,
  # Secrets Manager and CloudWatch Logs.
  #
  # So this is a destination CIDR rather than a prefix list: a provider added in
  # configuration must not need a security group change to become reachable. The
  # port is the bound worth keeping, and 443 is the only one anything here uses.
  description = "HTTPS to EHR FHIR endpoints, the OIDC issuer, and the AWS APIs the task depends on"
  cidr_ipv4   = "0.0.0.0/0"
  from_port   = 443
  to_port     = 443
  ip_protocol = "tcp"
}


# Database.

resource "aws_vpc_security_group_ingress_rule" "database_from_tasks" {
  security_group_id            = aws_security_group.rds.id
  description                  = "Postgres from the ECS tasks"
  referenced_security_group_id = aws_security_group.ecs_tasks.id
  from_port                    = var.database_port
  to_port                      = var.database_port
  ip_protocol                  = "tcp"
}

# The database group gets no egress rule. A default security group allows all
# outbound; one created here starts with none, and Aurora has nowhere it needs
# to reach on its own — backups, logs and monitoring all go through paths the
# service manages rather than through this group.
