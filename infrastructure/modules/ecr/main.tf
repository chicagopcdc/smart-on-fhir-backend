# Where the backend image lives.
#
# One repository, private, in the same region as the ECS service that pulls from
# it. Region is not a variable here: it comes from the provider, and the provider
# is the environment's region. A cross-region pull works but is billed as
# inter-region transfer on every task start, which on a service that scales is a
# cost with no upside.

resource "aws_ecr_repository" "this" {
  name                 = "${var.name_prefix}-${var.repository_name}"
  image_tag_mutability = var.image_tag_mutability
  force_delete         = var.force_delete

  image_scanning_configuration {
    scan_on_push = var.scan_on_push
  }

  # AES256 with an AWS-owned key. KMS is the alternative and costs a decrypt
  # call per layer pull; the image is public code and a lock file, so the thing
  # worth protecting is the ability to *push* to this repository, which is IAM's
  # job rather than encryption's.
  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-${var.repository_name}" })
}

resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name

  # Rules are evaluated in priority order and an image is matched by the first
  # rule that applies, so the untagged sweep has to come first: the countMore
  # rule below would otherwise keep untagged layers alive as part of its count.
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after ${var.untagged_image_expiry_days} days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = var.untagged_image_expiry_days
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the ${var.max_tagged_images} most recent tagged images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = var.max_tagged_images
        }
        action = { type = "expire" }
      },
    ]
  })
}

data "aws_iam_policy_document" "repository" {
  count = length(var.pull_principal_arns) > 0 ? 1 : 0

  statement {
    sid    = "AllowPull"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = var.pull_principal_arns
    }

    # GetAuthorizationToken is deliberately absent: it is an account-level
    # action that cannot be granted by a repository policy, so a principal
    # listed here still needs it from its own IAM policy.
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:DescribeImages",
    ]
  }
}

resource "aws_ecr_repository_policy" "this" {
  count = length(var.pull_principal_arns) > 0 ? 1 : 0

  repository = aws_ecr_repository.this.name
  policy     = data.aws_iam_policy_document.repository[0].json
}
