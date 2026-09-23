# The backend service.
#
# App Runner rather than ECS or EKS: it is one resource instead of a cluster, a
# task definition, a service, a load balancer and a target group, and it bills
# for what it runs. The trade is that everything it does not do — a one-off
# migration task, a sidecar, more than one container — has to be done some other
# way, which is why the migration is folded into the start command below.

locals {
  # `&&` rather than two commands, so uvicorn never starts against a schema the
  # migration failed to apply: the container exits, the deployment fails, and
  # App Runner keeps the previous revision serving.
  default_start_command = var.run_migrations_on_start ? (
    "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${var.port}"
  ) : null

  start_command = coalesce(var.start_command, local.default_start_command, "")
}

resource "aws_apprunner_auto_scaling_configuration_version" "this" {
  auto_scaling_configuration_name = substr(replace(var.service_name, "/[^a-zA-Z0-9-_]/", "-"), 0, 32)

  max_concurrency = var.max_concurrency
  min_size        = var.min_size
  max_size        = var.max_size

  tags = merge(var.tags, { Name = var.service_name })

  # These are versions, not mutable resources: changing max_concurrency creates
  # revision N+1 rather than editing N, and App Runner refuses to delete a
  # revision a service still references. Creating the new one first lets the
  # service move onto it before the old one is removed.
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_apprunner_service" "this" {
  service_name = var.service_name

  source_configuration {
    # False with IMMUTABLE tags, which is the pairing that makes a deployment an
    # explicit act: Terraform moves image_tag, or CI calls StartDeployment.
    auto_deployments_enabled = var.auto_deployments_enabled

    authentication_configuration {
      access_role_arn = var.access_role_arn
    }

    image_repository {
      image_identifier      = "${var.image_repository_url}:${var.image_tag}"
      image_repository_type = "ECR"

      image_configuration {
        # A string, because that is what the App Runner API takes.
        port = tostring(var.port)

        runtime_environment_variables = var.environment_variables

        # Resolved by App Runner at container start using the *instance* role,
        # and injected as ordinary environment variables — so pydantic-settings
        # reads them exactly as it reads the plain ones, and the values never
        # appear in the service description.
        runtime_environment_secrets = var.environment_secrets

        start_command = local.start_command != "" ? local.start_command : null
      }
    }
  }

  instance_configuration {
    cpu               = var.cpu
    memory            = var.memory
    instance_role_arn = var.instance_role_arn
  }

  health_check_configuration {
    # HTTP, not TCP. A TCP check passes as soon as uvicorn binds, which it does
    # before it can reach the database; /health is the only probe that
    # distinguishes a process that is up from a service that works.
    protocol            = "HTTP"
    path                = var.health_check_path
    interval            = var.health_check_interval
    timeout             = var.health_check_timeout
    healthy_threshold   = var.health_check_healthy_threshold
    unhealthy_threshold = var.health_check_unhealthy_threshold
  }

  network_configuration {
    # Attaching a connector moves *all* outbound traffic into the VPC, not just
    # traffic addressed to it. That is what puts the database in reach, and it
    # is also why the private subnets need a NAT route before the service can
    # reach Epic, Cerner or the identity provider — see has_internet_egress.
    egress_configuration {
      egress_type       = "VPC"
      vpc_connector_arn = var.vpc_connector_arn
    }

    ingress_configuration {
      is_publicly_accessible = var.is_publicly_accessible
    }
  }

  auto_scaling_configuration_arn = aws_apprunner_auto_scaling_configuration_version.this.arn

  tags = merge(var.tags, { Name = var.service_name })
}

resource "aws_apprunner_custom_domain_association" "this" {
  count = var.custom_domain != null ? 1 : 0

  domain_name = var.custom_domain
  service_arn = aws_apprunner_service.this.arn

  # App Runner issues and renews the certificate itself once the validation
  # records resolve. www is included because it costs nothing to add and cannot
  # be added later without recreating the association.
  enable_www_subdomain = true
}
