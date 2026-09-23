variable "name_prefix" {
  description = "Prefix applied to every role and policy name."
  type        = string
}

variable "ecr_repository_arn" {
  description = "Repository the App Runner access role may pull from."
  type        = string
}

variable "secret_arns" {
  description = <<-DESC
    Secrets the running service may read, as a list of ARNs.

    Listed one by one rather than granted as a prefix wildcard. A wildcard on
    \"smart-on-fhir-prod/*\" would quietly extend to every secret added under
    that prefix later, including ones belonging to something that is not this
    service; naming them means a new secret is unreadable until someone says so.
  DESC
  type        = list(string)
  default     = []
}

variable "secrets_kms_key_arn" {
  description = "KMS key the secrets are encrypted with, when it is a customer-managed one. Null means the AWS-managed key, which needs no explicit grant."
  type        = string
  default     = null
}

variable "service_name" {
  description = "Name of the App Runner service, used to address the log groups it creates. Must match what the app_runner module is given."
  type        = string
}

variable "github_repository" {
  description = <<-DESC
    \"owner/repo\" whose Actions workflows may assume the deploy role, or null
    to create no deploy role at all.

    The role is assumed through GitHub's OIDC provider, so CI holds no long-
    lived AWS key. That is the reason to prefer it to an IAM user: a leaked
    access key is valid until someone notices, and a leaked OIDC token is valid
    for the length of one job.
  DESC
  type        = string
  default     = null

  validation {
    condition     = var.github_repository == null || can(regex("^[^/]+/[^/]+$", coalesce(var.github_repository, "x/y")))
    error_message = "github_repository must look like \"owner/repo\"."
  }
}

variable "github_deploy_ref_patterns" {
  description = <<-DESC
    Which git refs may assume the deploy role, as GitHub OIDC subject claims.

    This is the whole access control on deployment. \"repo:owner/repo:*\" would
    let a pull request from a fork — which runs workflow code the fork controls —
    assume the role, so the default names a branch. An environment claim
    (\"repo:owner/repo:environment:prod\") is stricter still, because it can be
    put behind a required reviewer in GitHub.
  DESC
  type        = list(string)
  default     = ["ref:refs/heads/main"]
}

variable "create_github_oidc_provider" {
  description = <<-DESC
    Create the GitHub OIDC provider in this account.

    There can only be one per account, and both environments are likely to be in
    the same account — so at most one of dev and prod may set this, and the other
    points at github_oidc_provider_arn. If some other stack already created it,
    neither should.
  DESC
  type        = bool
  default     = false
}

variable "github_oidc_provider_arn" {
  description = "ARN of an existing GitHub OIDC provider, when create_github_oidc_provider is false."
  type        = string
  default     = null
}

variable "frontend_bucket_arn" {
  description = "S3 bucket the deploy role may sync the built frontend into. Null leaves frontend deployment out of the role."
  type        = string
  default     = null
}

variable "frontend_distribution_arn" {
  description = "CloudFront distribution the deploy role may invalidate after a sync. Without it a deploy uploads new files that browsers keep the old copies of."
  type        = string
  default     = null
}

variable "tags" {
  description = "Tags merged onto every resource this module creates."
  type        = map(string)
  default     = {}
}
