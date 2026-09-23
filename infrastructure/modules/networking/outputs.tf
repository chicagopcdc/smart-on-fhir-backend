output "vpc_id" {
  description = "Id of the VPC."
  value       = aws_vpc.this.id
}

output "vpc_cidr" {
  description = "Address space of the VPC, for anything that needs to write a rule against the whole of it."
  value       = aws_vpc.this.cidr_block
}

output "public_subnet_ids" {
  description = "Public subnet ids, in AZ order."
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "Private subnet ids, in AZ order. The database subnet group and the VPC connector both draw from these."
  value       = aws_subnet.private[*].id
}

output "availability_zones" {
  description = "The AZs the subnets were placed in."
  value       = local.azs
}

output "rds_security_group_id" {
  description = "Security group the database attaches to; admits Postgres from the VPC connector only."
  value       = aws_security_group.rds.id
}

output "app_runner_vpc_connector_security_group_id" {
  description = "Security group on the VPC connector's ENIs."
  value       = aws_security_group.app_runner_vpc_connector.id
}

output "vpc_connector_arn" {
  description = "ARN of the App Runner VPC connector, for the App Runner service's egress configuration."
  value       = aws_apprunner_vpc_connector.this.arn
}

output "has_internet_egress" {
  description = <<-DESC
    Whether the private subnets can reach the internet. The App Runner module
    reads this to fail the plan rather than deploy a service that would come up
    and then be unable to reach any EHR.
  DESC
  value       = var.enable_nat_gateway
}
