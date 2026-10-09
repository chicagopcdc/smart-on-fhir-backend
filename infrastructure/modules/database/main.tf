# The Postgres the backend stores connections, tokens and accounts in.
#
# Aurora Serverless v2 rather than a provisioned instance: the load is a handful
# of clinicians' sessions, which is nothing most of the day and a burst when
# someone opens a record. Serverless v2 bills the floor and scales to the burst;
# a provisioned instance large enough for the burst bills for it overnight.

resource "random_password" "master" {
  length = 40

  # Alphanumerics plus hyphen and underscore, and nothing else.
  #
  # The password ends up inside a URL — postgresql+asyncpg://user:pass@host/db —
  # and the characters excluded here are the ones that would have to be
  # percent-encoded to survive that. Doing it this way rather than encoding on
  # the way in means there is one representation of the password, so a human who
  # copies it out of Secrets Manager into psql gets the same string the
  # application uses. Aurora separately rejects '/', '@', '"' and space.
  override_special = "-_"

  # Guarantee at least one of each class rather than trusting 40 draws to
  # produce them: Aurora will reject a password that happens to miss a class.
  min_upper   = 1
  min_lower   = 1
  min_numeric = 1
  min_special = 1
}

resource "aws_db_subnet_group" "this" {
  name        = "${var.name_prefix}-db"
  description = "Private subnets for the ${var.name_prefix} Aurora cluster"
  subnet_ids  = var.subnet_ids

  tags = merge(var.tags, { Name = "${var.name_prefix}-db" })
}

# Logging and a slow-query threshold, set on the cluster rather than per
# instance so a second instance inherits them.
resource "aws_rds_cluster_parameter_group" "this" {
  name_prefix = "${var.name_prefix}-aurora-pg-"
  family      = "aurora-postgresql${split(".", var.engine_version)[0]}"
  description = "Cluster parameters for ${var.name_prefix}"

  # Ship every connection and disconnection. On a service this size the volume
  # is negligible, and it is the only record of who reached the database that
  # survives the instance being replaced.
  parameter {
    name  = "log_connections"
    value = "1"
  }

  parameter {
    name  = "log_disconnections"
    value = "1"
  }

  # Anything over a second is worth a line. The application fans out FHIR calls
  # per read and holds a connection across them; a query that slow is either a
  # missing index or a lock being waited on, and both are invisible otherwise.
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }

  tags = var.tags

  # name_prefix plus this: a parameter change that forces replacement cannot
  # destroy the group the cluster is still attached to.
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_rds_cluster" "this" {
  cluster_identifier = "${var.name_prefix}-aurora"

  engine = "aurora-postgresql"

  # Serverless v2 is the provisioned engine mode with a scaling configuration
  # attached, not engine_mode = "serverless" — that is v1, which is a different
  # and now legacy product.
  engine_mode    = "provisioned"
  engine_version = var.engine_version

  database_name   = var.database_name
  master_username = var.master_username
  master_password = random_password.master.result
  port            = var.port

  db_subnet_group_name            = aws_db_subnet_group.this.name
  vpc_security_group_ids          = var.security_group_ids
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.this.name

  serverlessv2_scaling_configuration {
    min_capacity = var.min_capacity
    max_capacity = var.max_capacity
  }

  # Encrypted with the account's default RDS key. The data at rest here is
  # refresh tokens for EHR accounts and the identities they belong to; the
  # tokens are separately encrypted by the application with the Fernet key, so
  # this is the layer under that one, not instead of it.
  storage_encrypted = true

  backup_retention_period      = var.backup_retention_period
  preferred_backup_window      = var.preferred_backup_window
  preferred_maintenance_window = var.preferred_maintenance_window
  copy_tags_to_snapshot        = true

  # Postgres logs to CloudWatch. Without this the log_* parameters above are
  # written to a disk nobody can read.
  enabled_cloudwatch_logs_exports = ["postgresql"]

  deletion_protection = var.deletion_protection
  skip_final_snapshot = var.skip_final_snapshot
  final_snapshot_identifier = var.skip_final_snapshot ? null : (
    "${var.name_prefix}-aurora-final-${formatdate("YYYYMMDDhhmmss", timestamp())}"
  )

  apply_immediately = var.apply_immediately

  tags = merge(var.tags, { Name = "${var.name_prefix}-aurora" })

  lifecycle {
    ignore_changes = [
      # timestamp() is evaluated on every plan, so without this the snapshot
      # name alone would show a diff every time. It is only read when the
      # cluster is actually being destroyed, which is when a fresh value is
      # exactly what is wanted.
      final_snapshot_identifier,

      # The password is rotated in Secrets Manager, not here. Leaving this
      # unignored would mean any out-of-band rotation gets reverted on the next
      # apply — silently, and with the application still holding the new one.
      master_password,
    ]
  }
}

resource "aws_rds_cluster_instance" "this" {
  count = var.instance_count

  identifier         = "${var.name_prefix}-aurora-${count.index}"
  cluster_identifier = aws_rds_cluster.this.id

  # The literal string that means "take capacity from the cluster's
  # serverlessv2_scaling_configuration" rather than a fixed instance size.
  instance_class = "db.serverless"

  engine         = aws_rds_cluster.this.engine
  engine_version = aws_rds_cluster.this.engine_version

  # No public address, ever. The cluster is reachable from the VPC connector's
  # security group and from nothing else; this is the setting that would undo
  # that in one line.
  publicly_accessible = false

  db_subnet_group_name = aws_db_subnet_group.this.name

  performance_insights_enabled = var.performance_insights_enabled

  # 7 days is the free tier. Anything longer is billed, so it is a choice rather
  # than a default worth inheriting.
  performance_insights_retention_period = var.performance_insights_enabled ? 7 : null

  monitoring_interval = var.monitoring_interval
  monitoring_role_arn = var.monitoring_interval > 0 ? aws_iam_role.enhanced_monitoring[0].arn : null

  # Minor versions are applied in the maintenance window, not whenever AWS
  # publishes one. engine_version is pinned above; this is about what happens
  # between the times someone changes it.
  auto_minor_version_upgrade = true

  apply_immediately = var.apply_immediately

  tags = merge(var.tags, { Name = "${var.name_prefix}-aurora-${count.index}" })
}


# --- enhanced monitoring role -----------------------------------------------
#
# Created here rather than in the IAM module because it is not the application's
# identity: it is a role RDS assumes on its own behalf, it exists only when
# monitoring_interval is non-zero, and nothing outside this module can use it.

data "aws_iam_policy_document" "enhanced_monitoring_assume" {
  count = var.monitoring_interval > 0 ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["monitoring.rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "enhanced_monitoring" {
  count = var.monitoring_interval > 0 ? 1 : 0

  name               = "${var.name_prefix}-rds-enhanced-monitoring"
  assume_role_policy = data.aws_iam_policy_document.enhanced_monitoring_assume[0].json

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "enhanced_monitoring" {
  count = var.monitoring_interval > 0 ? 1 : 0

  role       = aws_iam_role.enhanced_monitoring[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}
