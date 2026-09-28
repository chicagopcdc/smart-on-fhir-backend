output "vpc_id" {
  description = "Id of the VPC."
  value       = aws_vpc.this.id
}

output "vpc_cidr" {
  description = "Address space of the VPC, for anything that needs to write a rule against the whole of it."
  value       = aws_vpc.this.cidr_block
}

output "public_subnet_ids" {
  description = "Public subnet ids, in AZ order. The load balancer always spans these."
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "Private subnet ids, in AZ order. The database subnet group draws from these, and so do the tasks when NAT is enabled."
  value       = aws_subnet.private[*].id
}

output "ecs_task_subnet_ids" {
  description = <<-DESC
    Where the application's tasks run: the public subnets by default, the private
    ones when NAT is enabled. Paired with assign_public_ip, which has to agree
    with it — which is why both are decided here rather than in the ECS module.
  DESC
  value       = local.ecs_task_subnet_ids
}

output "assign_public_ip" {
  description = <<-DESC
    Whether the tasks should be given a public address.

    True when there is no NAT gateway, and it is not optional in that case: a
    Fargate task in a public subnet with no public IP cannot reach the internet,
    cannot pull its own image from ECR, and cannot read its secrets. This is the
    capability App Runner's VPC connector lacked, and the reason this stack needs
    no NAT.
  DESC
  value       = local.assign_public_ip
}

output "availability_zones" {
  description = "The AZs the subnets were placed in."
  value       = local.azs
}

output "alb_security_group_id" {
  description = "Security group on the load balancer; admits 80 and 443 from alb_ingress_cidrs."
  value       = aws_security_group.alb.id
}

output "ecs_tasks_security_group_id" {
  description = "Security group on the tasks; admits the load balancer on the application port and nothing else."
  value       = aws_security_group.ecs_tasks.id
}

output "rds_security_group_id" {
  description = "Security group the database attaches to; admits Postgres from the tasks only."
  value       = aws_security_group.rds.id
}

output "has_internet_egress" {
  description = <<-DESC
    Whether the tasks can reach the internet at all, by either route — a public
    IP through the internet gateway, or NAT. Always true as the module is
    written; it exists so the ECS module can refuse to deploy a service that
    would come up and then be unable to reach any EHR, if that ever stops being
    the case.
  DESC
  value       = local.assign_public_ip || var.enable_nat_gateway
}
