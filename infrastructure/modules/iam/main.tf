# The identities around the service.
#
# Three, and they are separate on purpose:
#
#   execution role  The ECS agent's. Pulls the image, resolves the secrets, opens
#                   the log stream — all before any application code runs.
#   task role       The running container's own identity. This is what a
#                   compromise of the application gets.
#   deploy role     CI's. Can replace what runs; cannot read what it runs with.
#
# The first two being separate is the useful part, and it is a property ECS has
# that App Runner did not expose as cleanly: the secrets are granted to the agent,
# so the application itself has no permission to read them. An attacker who
# reaches the request handler already has the values in its environment, but
# cannot enumerate Secrets Manager for anything it was not given.
#
# Collapsing execution and deploy would be worse still — it would let a
# compromised request handler publish the image its own next deployment pulls.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

locals {
  # Built from names rather than taken from the ecs module's outputs, which would
  # be a cycle: that module needs these roles before it can define the resources
  # these ARNs describe. Both modules take the same names from the same root local,
  # so they cannot disagree.
  arn_prefix = "arn:${data.aws_partition.current.partition}"
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name

  log_group_arn = "${local.arn_prefix}:logs:${local.region}:${local.account_id}:log-group:/aws/ecs/${var.service_name}"

  service_arn = "${local.arn_prefix}:ecs:${local.region}:${local.account_id}:service/${var.cluster_name}/${var.service_name}"

  deploy_manifest_arn = "${local.arn_prefix}:ssm:${local.region}:${local.account_id}:parameter/${var.service_name}/deploy"

  task_definition_arns = [
    "${local.arn_prefix}:ecs:${local.region}:${local.account_id}:task-definition/${var.service_name}:*",
    "${local.arn_prefix}:ecs:${local.region}:${local.account_id}:task-definition/${var.service_name}-migrate:*",
  ]

  ecs_tasks_assume_principal = "ecs-tasks.amazonaws.com"
}

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = [local.ecs_tasks_assume_principal]
    }

    # Both conditions narrow a role that would otherwise be assumable on behalf of
    # any task in the account. SourceAccount alone still allows any account the
    # service acts for; SourceArn pins it to tasks in this account's ECS.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["${local.arn_prefix}:ecs:${local.region}:${local.account_id}:*"]
    }
  }
}


# --- execution role: starting the task --------------------------------------

resource "aws_iam_role" "execution" {
  name               = "${var.name_prefix}-ecs-execution"
  description        = "Assumed by the ECS agent to pull the image, read the secrets and write logs"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = var.tags
}

data "aws_iam_policy_document" "execution" {
  statement {
    sid    = "PullImage"
    effect = "Allow"

    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]

    resources = [var.ecr_repository_arn]
  }

  statement {
    sid    = "AuthenticateToRegistry"
    effect = "Allow"

    actions = ["ecr:GetAuthorizationToken"]

    # Not scopable. GetAuthorizationToken acts on the registry as a whole and IAM
    # rejects a policy that names a repository for it. The token it returns is
    # still bounded by the statement above.
    resources = ["*"]
  }

  statement {
    sid    = "WriteLogs"
    effect = "Allow"

    # CreateLogGroup is deliberately absent. Terraform creates the group with a
    # retention policy; if the agent could create it too, a typo in the group name
    # would silently produce a second group that keeps logs forever.
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]

    resources = ["${local.log_group_arn}:*"]
  }

  # The reason the application needs no Secrets Manager permission of its own.
  dynamic "statement" {
    for_each = length(var.secret_arns) > 0 ? [1] : []

    content {
      sid       = "ResolveTaskSecrets"
      effect    = "Allow"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = var.secret_arns
    }
  }

  # Only needed for a customer-managed key. The aws/secretsmanager key grants
  # decrypt through its own key policy to any principal in the account already
  # allowed GetSecretValue.
  dynamic "statement" {
    for_each = var.secrets_kms_key_arn != null ? [1] : []

    content {
      sid       = "DecryptSecrets"
      effect    = "Allow"
      actions   = ["kms:Decrypt"]
      resources = [var.secrets_kms_key_arn]

      condition {
        test     = "StringEquals"
        variable = "kms:ViaService"
        values   = ["secretsmanager.${local.region}.amazonaws.com"]
      }
    }
  }
}

resource "aws_iam_role_policy" "execution" {
  name   = "${var.name_prefix}-ecs-execution"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}


# --- task role: what the running container may do ---------------------------

resource "aws_iam_role" "task" {
  name               = "${var.name_prefix}-ecs-task"
  description        = "Identity of the running backend container"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json

  tags = var.tags
}

data "aws_iam_policy_document" "task" {
  # What `aws ecs execute-command` runs on. The channels are opened by the SSM
  # agent inside the container, so they have to be on the task role rather than
  # the execution role — and this is the only thing the task role grants.
  dynamic "statement" {
    for_each = var.enable_execute_command ? [1] : []

    content {
      sid    = "ExecuteCommandChannels"
      effect = "Allow"

      actions = [
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
      ]

      # Session Manager's channels are not resource-scopable; the session is
      # bounded by which task the caller may exec into, which is decided by IAM on
      # the caller rather than here.
      resources = ["*"]
    }
  }

  # Role chaining, denied explicitly. Nothing grants the container sts:AssumeRole
  # today, so this changes no behaviour — it is here so that a later grant, on
  # this role or on a resource policy elsewhere, cannot turn the service's
  # identity into a step towards a different one without someone removing this
  # statement first. It also guarantees the document has a statement when exec is
  # disabled, which IAM would otherwise reject.
  statement {
    sid       = "DenyRoleChaining"
    effect    = "Deny"
    actions   = ["sts:AssumeRole"]
    resources = ["*"]
  }

  # Nothing else. The application writes its logs to stdout and the agent ships
  # them, which needs no permission here; it makes no AWS API call of its own. If
  # that changes — an S3 export, an SES send — it is added here and the diff is
  # the record of the service's reach growing.
}

resource "aws_iam_role_policy" "task" {
  name   = "${var.name_prefix}-ecs-task"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}


# --- deploy role: what CI may do --------------------------------------------

locals {
  create_deploy_role = var.github_repository != null

  github_oidc_provider_arn = local.create_deploy_role ? (
    var.create_github_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
    : var.github_oidc_provider_arn
  ) : null
}

resource "aws_iam_openid_connect_provider" "github" {
  count = local.create_deploy_role && var.create_github_oidc_provider ? 1 : 0

  url = "https://token.actions.githubusercontent.com"

  # The audience GitHub's official configure-aws-credentials action requests.
  client_id_list = ["sts.amazonaws.com"]

  # IAM has verified GitHub's certificate against the trust store rather than this
  # list since 2023, but the field is still required. The value is the thumbprint
  # AWS documents; it is not load-bearing.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]

  tags = var.tags
}

data "aws_iam_policy_document" "deploy_assume" {
  count = local.create_deploy_role ? 1 : 0

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # StringLike rather than StringEquals so an environment or ref pattern can
    # carry a wildcard, and scoped to this repository's subjects in both halves:
    # the subject claim is the only thing distinguishing this repo's workflows from
    # every other GitHub repository in the world.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [for pattern in var.github_deploy_ref_patterns : "repo:${var.github_repository}:${pattern}"]
    }
  }
}

resource "aws_iam_role" "deploy" {
  count = local.create_deploy_role ? 1 : 0

  name               = "${var.name_prefix}-deploy"
  description        = "Assumed by ${var.github_repository} workflows to publish an image, migrate and deploy"
  assume_role_policy = data.aws_iam_policy_document.deploy_assume[0].json

  # An hour. Long enough for a build, a migration and a rolling deploy to reach
  # steady state; short enough that a token which escapes the runner is not much
  # use by the time anyone could act on it.
  max_session_duration = 3600

  tags = var.tags
}

data "aws_iam_policy_document" "deploy" {
  count = local.create_deploy_role ? 1 : 0

  statement {
    sid       = "AuthenticateToRegistry"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "PushImage"
    effect = "Allow"

    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]

    resources = [var.ecr_repository_arn]
  }

  statement {
    sid    = "ReadTaskDefinitions"
    effect = "Allow"

    # Describe is how the deploy renders its new revision: it reads the latest
    # revision of the family — the one Terraform registered, carrying whatever
    # environment and secrets Terraform last set — and swaps only the image. That
    # is what keeps Terraform the source of truth for configuration while CI owns
    # which image runs.
    actions   = ["ecs:DescribeTaskDefinition"]
    resources = ["*"]
  }

  statement {
    sid    = "RegisterTaskDefinition"
    effect = "Allow"

    # Not resource-scopable: a task definition that does not exist yet has no ARN
    # to name. The PassRole statement below is what actually bounds this — a
    # revision can only be registered naming roles CI is allowed to pass, so it
    # cannot register one that runs as something more privileged.
    actions   = ["ecs:RegisterTaskDefinition"]
    resources = ["*"]
  }

  statement {
    sid    = "PassTaskRoles"
    effect = "Allow"

    actions = ["iam:PassRole"]

    resources = [
      aws_iam_role.execution.arn,
      aws_iam_role.task.arn,
    ]

    # Without this, PassRole on these two roles would let CI hand them to any
    # service that accepts a role — Lambda, EC2 — and run arbitrary code as the
    # task. Pinned to ECS, it can only be used for what it is for.
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }

  statement {
    sid    = "RunMigration"
    effect = "Allow"

    actions   = ["ecs:RunTask"]
    resources = local.task_definition_arns

    # RunTask against any cluster would let CI start this task definition
    # somewhere with a different network configuration.
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = ["${local.arn_prefix}:ecs:${local.region}:${local.account_id}:cluster/${var.cluster_name}"]
    }
  }

  statement {
    sid    = "WatchTasks"
    effect = "Allow"

    # How the workflow learns whether the migration exited 0.
    actions = [
      "ecs:DescribeTasks",
      "ecs:ListTasks",
    ]

    resources = ["*"]

    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = ["${local.arn_prefix}:ecs:${local.region}:${local.account_id}:cluster/${var.cluster_name}"]
    }
  }

  statement {
    sid    = "DeployService"
    effect = "Allow"

    # UpdateService is scoped to this one service, so CI can roll it but cannot
    # create another, delete it, or point a different service at these roles.
    actions = [
      "ecs:UpdateService",
      "ecs:DescribeServices",
    ]

    resources = [local.service_arn]
  }

  statement {
    sid    = "ReadDeployManifest"
    effect = "Allow"

    # How the workflow finds the cluster, the service and the network configuration
    # for the migration task, without being given access to Terraform state.
    actions   = ["ssm:GetParameter"]
    resources = [local.deploy_manifest_arn]
  }

  statement {
    sid    = "ReadMigrationLogs"
    effect = "Allow"

    # So a failed migration's traceback appears in the workflow output rather than
    # only in CloudWatch, where whoever is watching the deploy has to go looking.
    actions = [
      "logs:GetLogEvents",
      "logs:FilterLogEvents",
      "logs:DescribeLogStreams",
    ]

    resources = ["${local.log_group_arn}:*"]
  }

  dynamic "statement" {
    for_each = var.frontend_bucket_arn != null ? [1] : []

    content {
      sid    = "SyncFrontend"
      effect = "Allow"

      actions = [
        "s3:ListBucket",
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
      ]

      # Both ARNs: the bucket itself for ListBucket, and everything in it for the
      # object actions. `aws s3 sync --delete` needs all four.
      resources = [
        var.frontend_bucket_arn,
        "${var.frontend_bucket_arn}/*",
      ]
    }
  }

  dynamic "statement" {
    for_each = var.frontend_distribution_arn != null ? [1] : []

    content {
      sid       = "InvalidateFrontend"
      effect    = "Allow"
      actions   = ["cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"]
      resources = [var.frontend_distribution_arn]
    }
  }
}

resource "aws_iam_role_policy" "deploy" {
  count = local.create_deploy_role ? 1 : 0

  name   = "${var.name_prefix}-deploy"
  role   = aws_iam_role.deploy[0].id
  policy = data.aws_iam_policy_document.deploy[0].json
}
