# The identities around the service.
#
# Three, and they are separate on purpose:
#
#   access role    App Runner assumes it to pull the image. Used at deploy time,
#                  by the service, before any of the application's code runs.
#   instance role  The running container's identity. This is what a compromise
#                  of the application gets, so it holds exactly the secrets the
#                  process reads and nothing else.
#   deploy role    CI assumes it to push an image and start a deployment. It can
#                  replace what runs; it cannot read what runs with.
#
# Collapsing any pair of these would hand one of them the other's reach — most
# obviously, giving the instance role ECR write would let a compromised request
# handler publish the image its own next deployment pulls.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}


# --- access role: pulling the image -----------------------------------------

data "aws_iam_policy_document" "access_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type = "Service"

      # build.apprunner, not tasks.apprunner. The pull happens in App Runner's
      # build/deploy plane; the running container is a different principal.
      identifiers = ["build.apprunner.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "access" {
  name               = "${var.name_prefix}-apprunner-access"
  description        = "Assumed by App Runner to pull the backend image from ECR"
  assume_role_policy = data.aws_iam_policy_document.access_assume.json

  tags = var.tags
}

data "aws_iam_policy_document" "access" {
  statement {
    sid    = "PullImage"
    effect = "Allow"

    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
    ]

    resources = [var.ecr_repository_arn]
  }

  statement {
    sid    = "AuthenticateToRegistry"
    effect = "Allow"

    actions = ["ecr:GetAuthorizationToken"]

    # Not scopable. GetAuthorizationToken acts on the registry as a whole and
    # IAM rejects a policy that names a repository for it. The token it returns
    # is still bounded by the statement above.
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "access" {
  name   = "${var.name_prefix}-apprunner-access"
  role   = aws_iam_role.access.id
  policy = data.aws_iam_policy_document.access.json
}


# --- instance role: what the running container may do -----------------------

data "aws_iam_policy_document" "instance_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["tasks.apprunner.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "${var.name_prefix}-apprunner-instance"
  description        = "Identity of the running backend container"
  assume_role_policy = data.aws_iam_policy_document.instance_assume.json

  tags = var.tags
}

data "aws_iam_policy_document" "instance" {
  # App Runner resolves runtime_environment_secrets using the *instance* role,
  # so this grant is what makes the container start at all rather than something
  # the application calls for later.
  dynamic "statement" {
    for_each = length(var.secret_arns) > 0 ? [1] : []

    content {
      sid       = "ReadOwnSecrets"
      effect    = "Allow"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = var.secret_arns
    }
  }

  # Only needed for a customer-managed key. The aws/secretsmanager key grants
  # decrypt through its own key policy to any principal in the account that is
  # already allowed GetSecretValue.
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
        values   = ["secretsmanager.${data.aws_region.current.name}.amazonaws.com"]
      }
    }
  }

  # Nothing else. The application writes its logs to stdout and App Runner ships
  # them, which needs no permission on this role; it makes no AWS API call of
  # its own. If that changes — an S3 export, an SES send — it is added here and
  # the diff is the record of the service's reach growing.
  # Role chaining, denied explicitly. Nothing grants the container sts:AssumeRole
  # today, so this changes no behaviour — it is here so that a later grant, on
  # this role or on a resource policy elsewhere, cannot turn the service's
  # identity into a step towards a different one without someone removing this
  # statement first. It also guarantees the document has a statement when a
  # deployment configures no secrets at all, which IAM would otherwise reject.
  statement {
    sid       = "DenyRoleChaining"
    effect    = "Deny"
    actions   = ["sts:AssumeRole"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "instance" {
  name   = "${var.name_prefix}-apprunner-instance"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.instance.json
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

  # IAM has verified GitHub's certificate against the trust store rather than
  # this list since 2023, but the field is still required. The value is the
  # thumbprint AWS documents; it is not load-bearing.
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
    # the subject claim is the only thing distinguishing this repo's workflows
    # from every other GitHub repository in the world.
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
  description        = "Assumed by ${var.github_repository} workflows to publish an image and start a deployment"
  assume_role_policy = data.aws_iam_policy_document.deploy_assume[0].json

  # An hour. Long enough for a build and push, short enough that a token that
  # escapes the runner is not much use by the time anyone could act on it.
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

  # Enough to roll the service and watch it, and not enough to change what it
  # runs *as*: UpdateService is absent, so CI cannot repoint the service at a
  # different image repository or swap its instance role.
  statement {
    sid    = "DeployService"
    effect = "Allow"

    actions = [
      "apprunner:StartDeployment",
      "apprunner:DescribeService",
      "apprunner:ListOperations",
    ]

    resources = [
      "arn:${data.aws_partition.current.partition}:apprunner:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:service/${var.service_name}/*",
    ]
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

      # Both ARNs: the bucket itself for ListBucket, and everything in it for
      # the object actions. `aws s3 sync --delete` needs all four.
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
