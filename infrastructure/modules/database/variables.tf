variable "name_prefix" {
  description = "Prefix applied to every resource name."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnet ids the DB subnet group spans. At least two AZs."
  type        = list(string)
}

variable "security_group_ids" {
  description = "Security groups attached to the cluster; the networking module's rds group belongs here."
  type        = list(string)
}

variable "engine_version" {
  description = <<-DESC
    Aurora PostgreSQL version, pinned rather than floating.

    A minor upgrade restarts the writer, so it should happen when someone chose
    it. The application only needs a version asyncpg and SQLAlchemy 2 are happy
    with, which is anything current; the constraint that actually matters is
    that Serverless v2 requires 13.6 or newer.
  DESC
  type        = string
  default     = "16.4"
}

variable "database_name" {
  description = "Name of the database created inside the cluster."
  type        = string
  default     = "smartfhir"
}

variable "master_username" {
  description = <<-DESC
    Master user. Not "admin", "postgres" or "root": Aurora reserves several
    names outright, and an account whose name is guessable is one less thing an
    attacker has to learn.
  DESC
  type        = string
  default     = "smartfhir_admin"
}

variable "port" {
  description = "Port the cluster listens on."
  type        = number
  default     = 5432
}

variable "min_capacity" {
  description = <<-DESC
    Floor for Serverless v2, in Aurora Capacity Units.

    0 lets the cluster pause entirely when nothing is connected, which is the
    cheapest thing available and costs a cold start — several seconds — on the
    first connection after it scales down. Fine for dev. In prod the floor
    should be high enough that a user's first request of the morning is not the
    one that pays for the resume.
  DESC
  type        = number
  default     = 0.5
}

variable "max_capacity" {
  description = "Ceiling for Serverless v2, in ACUs. This is the cost bound: it is what the cluster can be driven to."
  type        = number
  default     = 2
}

variable "instance_count" {
  description = <<-DESC
    Instances in the cluster. One is a writer with no failover target — an AZ
    failure is an outage until AWS rebuilds it. Two gives a reader that Aurora
    can promote, which is the difference between minutes and tens of seconds.

    The application does not split reads from writes, so a second instance buys
    availability and nothing else.
  DESC
  type        = number
  default     = 1
}

variable "backup_retention_period" {
  description = "Days of automated backups. Aurora's minimum is 1; the point of raising it is how far back a bad migration can be undone."
  type        = number
  default     = 7
}

variable "preferred_backup_window" {
  description = "UTC window for the daily backup, as hh:mm-hh:mm."
  type        = string
  default     = "07:00-08:00"
}

variable "preferred_maintenance_window" {
  description = "UTC window for patching, as ddd:hh:mm-ddd:hh:mm. Must not overlap the backup window."
  type        = string
  default     = "sun:08:30-sun:09:30"
}

variable "deletion_protection" {
  description = <<-DESC
    Refuse to delete the cluster until someone turns this off.

    It blocks `terraform destroy` too, which is the intent: in prod a destroy
    that reaches the database is a mistake being made quickly, and this turns it
    into a mistake that takes two steps.
  DESC
  type        = bool
  default     = true
}

variable "skip_final_snapshot" {
  description = "Delete the cluster without taking a last snapshot. Only ever reasonable in an environment whose data is disposable."
  type        = bool
  default     = false
}

variable "performance_insights_enabled" {
  description = "Collect Performance Insights. Free at 7 days' retention, and the only way to see which query is holding the pool."
  type        = bool
  default     = true
}

variable "monitoring_interval" {
  description = "Seconds between Enhanced Monitoring samples, or 0 to leave it off. Billed per instance."
  type        = number
  default     = 0

  validation {
    condition     = contains([0, 1, 5, 10, 15, 30, 60], var.monitoring_interval)
    error_message = "monitoring_interval must be one of 0, 1, 5, 10, 15, 30, 60."
  }
}

variable "apply_immediately" {
  description = "Apply modifications now rather than in the next maintenance window. True in dev, where waiting is just waiting; false in prod, where it means an unscheduled restart."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags merged onto every resource this module creates."
  type        = map(string)
  default     = {}
}
