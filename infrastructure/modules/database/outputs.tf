output "cluster_identifier" {
  description = "Identifier of the Aurora cluster."
  value       = aws_rds_cluster.this.cluster_identifier
}

output "cluster_arn" {
  description = "ARN of the Aurora cluster."
  value       = aws_rds_cluster.this.arn
}

output "endpoint" {
  description = "Writer endpoint. The application does not split reads, so this is the only one it uses."
  value       = aws_rds_cluster.this.endpoint
}

output "reader_endpoint" {
  description = "Reader endpoint. Unused today; here so a read-heavy report does not have to reach into the cluster resource to find it."
  value       = aws_rds_cluster.this.reader_endpoint
}

output "port" {
  description = "Port the cluster listens on."
  value       = aws_rds_cluster.this.port
}

output "database_name" {
  description = "Name of the database inside the cluster."
  value       = aws_rds_cluster.this.database_name
}

output "master_username" {
  description = "Master user."
  value       = aws_rds_cluster.this.master_username
}

output "master_password" {
  description = "Generated master password. Consumed by the secrets module, which is what puts it somewhere a human can read it."
  value       = random_password.master.result
  sensitive   = true
}

output "database_url" {
  description = <<-DESC
    The connection string the application reads as DATABASE_URL, assembled here
    because this module is the only place that knows every part of it.

    The asyncpg driver is not incidental: app/core/db.py builds an async engine,
    and SQLAlchemy picks the driver from this scheme. A plain postgresql:// URL
    resolves to psycopg2 and fails at startup with a sync-driver error.
  DESC
  value       = "postgresql+asyncpg://${aws_rds_cluster.this.master_username}:${random_password.master.result}@${aws_rds_cluster.this.endpoint}:${aws_rds_cluster.this.port}/${aws_rds_cluster.this.database_name}"
  sensitive   = true
}
