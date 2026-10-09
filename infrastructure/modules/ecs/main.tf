# The backend service.
#
# ECS on Fargate behind an Application Load Balancer, which replaced App Runner
# for one reason: a Fargate task can be given a public IP and App Runner's VPC
# connector cannot. That single difference removes the NAT gateway — the tasks
# reach Epic and Cerner straight through the internet gateway and Aurora over the
# VPC's local route — and a NAT gateway was about $32/month per AZ for traffic
# amounting to a few hundred API calls a day.
#
# Three things came free with the move, each of which was a gap under App Runner:
#
#   - Migrations are a real task. `aws ecs run-task` against the migrate task
#     definition below runs `alembic upgrade head` once, before the service is
#     updated, instead of racing N containers starting at once.
#   - Log retention is set here, because the awslogs driver writes to a group
#     this module creates rather than one the platform invents afterwards.
#   - `aws ecs execute-command` opens a shell inside the VPC, which is how
#     anybody reaches the database without a bastion.
#
# What it cost: the load balancer, at roughly $16/month plus capacity units. Still
# cheaper than the NAT gateway it replaced, and it is the thing that terminates
# TLS now that nothing else does.

locals {
  # ALB target group names are capped at 32 characters and cannot end in a
  # hyphen. Truncating rather than hashing keeps it readable in the console.
  target_group_name = substr("${var.service_name}-tg", 0, 32)

  log_group_name = "/aws/ecs/${var.service_name}"

  # Container definitions are the same for the service and the migration except
  # for the command and the port, so the shared parts are built once.
  environment = [
    for key in sort(keys(var.environment_variables)) : {
      name  = key
      value = var.environment_variables[key]
    }
  ]

  secrets = [
    for key in sort(keys(var.environment_secrets)) : {
      name      = key
      valueFrom = var.environment_secrets[key]
    }
  ]

  image = "${var.image_repository_url}:${var.image_tag}"

  log_configuration = {
    logDriver = "awslogs"
    options = {
      awslogs-group         = local.log_group_name
      awslogs-region        = data.aws_region.current.name
      awslogs-stream-prefix = "ecs"
    }
  }
}

data "aws_region" "current" {}


# --- cluster and logs -------------------------------------------------------

resource "aws_ecs_cluster" "this" {
  name = var.name_prefix

  setting {
    name  = "containerInsights"
    value = var.enable_container_insights ? "enabled" : "disabled"
  }

  tags = merge(var.tags, { Name = var.name_prefix })
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name = aws_ecs_cluster.this.name

  # Both are declared whichever one is in use, so flipping use_fargate_spot does
  # not have to add a provider to the cluster before it can add it to the service.
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = var.use_fargate_spot ? "FARGATE_SPOT" : "FARGATE"
    weight            = 100
  }
}

resource "aws_cloudwatch_log_group" "app" {
  name              = local.log_group_name
  retention_in_days = var.log_retention_days

  tags = merge(var.tags, { Name = local.log_group_name })
}


# --- task definitions -------------------------------------------------------

resource "aws_ecs_task_definition" "app" {
  family = var.service_name

  requires_compatibilities = ["FARGATE"]

  # awsvpc is the only network mode Fargate supports, and it is what gives each
  # task its own interface in the subnet — which is what makes a security group
  # on the task meaningful rather than a property of a shared host.
  network_mode = "awsvpc"

  cpu    = var.cpu
  memory = var.memory

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  # Two roles, and the split matters. The execution role is the ECS agent's: it
  # pulls the image, resolves the secrets and opens the log stream, all before any
  # application code runs. The task role is the container's own identity. A
  # compromise of the application reaches the second and not the first, which is
  # why the secrets are granted to the execution role rather than to the app.
  execution_role_arn = var.execution_role_arn
  task_role_arn      = var.task_role_arn

  container_definitions = jsonencode([
    {
      name      = "app"
      image     = local.image
      essential = true

      portMappings = [
        {
          containerPort = var.port
          protocol      = "tcp"
        }
      ]

      environment = local.environment
      secrets     = local.secrets

      logConfiguration = local.log_configuration

      # The load balancer's probe is the one that decides whether a task receives
      # traffic; this one decides whether ECS replaces it. Both are worth having:
      # a process that has wedged answers neither, but only this one gets the task
      # killed rather than merely drained.
      #
      # The runtime image is slim and carries no curl, so the check is the
      # interpreter — the same approach the compose file takes, and for the same
      # reason. The empty ProxyHandler is load-bearing: urlopen otherwise builds
      # one from HTTP_PROXY and nothing exempts loopback from it.
      healthCheck = {
        command = [
          "CMD-SHELL",
          "python -c 'import urllib.request as u; u.build_opener(u.ProxyHandler({})).open(\"http://127.0.0.1:${var.port}/health\", timeout=3)'"
        ]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }

      # Uvicorn stops accepting connections on SIGTERM and finishes what it is
      # holding. 30 seconds is the room it gets; a FHIR fan-out in flight is the
      # thing being waited for, and it should agree with deregistration_delay.
      stopTimeout = 30
    }
  ])

  tags = merge(var.tags, { Name = var.service_name })
}

resource "aws_ecs_task_definition" "migrate" {
  family = "${var.service_name}-migrate"

  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"

  # The migration is a handful of DDL statements against Aurora and spends its
  # life waiting on the network. It gets the floor.
  cpu    = 256
  memory = 512

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  execution_role_arn = var.execution_role_arn
  task_role_arn      = var.task_role_arn

  container_definitions = jsonencode([
    {
      name      = "migrate"
      image     = local.image
      essential = true

      # The whole point of this file existing separately. Under App Runner there
      # was nowhere to run a migration from, so it was folded into the container's
      # start command and every task that started ran it — which meant a
      # deployment of several tasks had several processes racing the
      # alembic_version update. Here CI runs this once and waits for it to exit 0
      # before touching the service, so a failed migration stops the deploy
      # without ever having replaced a healthy task.
      command = ["alembic", "upgrade", "head"]

      # No portMappings: nothing connects to it, and no healthCheck: it is meant
      # to exit. ECS reports the container's exit code on the stopped task, which
      # is what CI reads.

      environment = local.environment
      secrets     = local.secrets

      logConfiguration = merge(local.log_configuration, {
        options = merge(local.log_configuration.options, {
          awslogs-stream-prefix = "migrate"
        })
      })
    }
  ])

  tags = merge(var.tags, { Name = "${var.service_name}-migrate" })
}


# --- load balancer ----------------------------------------------------------

resource "aws_lb" "this" {
  name               = substr(var.service_name, 0, 32)
  load_balancer_type = "application"
  internal           = false

  subnets         = var.alb_subnet_ids
  security_groups = var.alb_security_group_ids

  idle_timeout               = var.alb_idle_timeout
  enable_deletion_protection = var.alb_deletion_protection

  # Drop requests whose headers do not parse rather than normalising them. The
  # permissive alternative is how a request can be made to look like two.
  drop_invalid_header_fields = true

  # HTTP/2 to the browser; the connection to the task is HTTP/1.1 either way,
  # which is what uvicorn speaks.
  enable_http2 = true

  tags = merge(var.tags, { Name = var.service_name })
}

resource "aws_lb_target_group" "this" {
  name = local.target_group_name

  # "ip", not "instance". An awsvpc task is registered by the address of its own
  # interface; there is no instance to register.
  target_type = "ip"

  port     = var.port
  protocol = "HTTP"
  vpc_id   = data.aws_subnet.first.vpc_id

  deregistration_delay = var.deregistration_delay

  health_check {
    enabled             = true
    path                = var.health_check_path
    protocol            = "HTTP"
    interval            = var.health_check_interval
    timeout             = var.health_check_timeout
    healthy_threshold   = var.healthy_threshold
    unhealthy_threshold = var.unhealthy_threshold

    # 200 only. /health answers 503 when it cannot reach Postgres, and that is a
    # task that should not receive traffic — so the default 200-399 range would
    # be wrong here in a way that only shows up during a database outage.
    matcher = "200"
  }

  tags = merge(var.tags, { Name = local.target_group_name })

  # The listener holds a reference to this, and a target group in use cannot be
  # deleted. Any change that forces replacement — the name, the target type —
  # needs the new one to exist first.
  lifecycle {
    create_before_destroy = true
  }
}

# The VPC is not passed in as its own variable; it is read from a subnet the
# caller already had to supply, so there is no way to hand this module a target
# group in one VPC and tasks in another.
data "aws_subnet" "first" {
  id = var.subnet_ids[0]
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  # With a certificate, port 80 exists only to send browsers to 443. Without one,
  # it is the service — which does not work from the frontend, because an HTTPS
  # page may not call an HTTP origin. See certificate_arn.
  dynamic "default_action" {
    for_each = var.certificate_arn != null ? [1] : []

    content {
      type = "redirect"

      redirect {
        port     = "443"
        protocol = "HTTPS"

        # 301, not 302: it is a permanent property of the deployment, and a
        # permanent redirect is cached by the browser so the second request goes
        # straight to HTTPS.
        status_code = "HTTP_301"
      }
    }
  }

  dynamic "default_action" {
    for_each = var.certificate_arn == null ? [1] : []

    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.this.arn
    }
  }
}

resource "aws_lb_listener" "https" {
  count = var.certificate_arn != null ? 1 : 0

  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = var.ssl_policy
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }
}


# --- the service ------------------------------------------------------------

resource "aws_ecs_service" "this" {
  name            = var.service_name
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.desired_count

  capacity_provider_strategy {
    capacity_provider = var.use_fargate_spot ? "FARGATE_SPOT" : "FARGATE"
    weight            = 100
  }

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = var.security_group_ids
    assign_public_ip = var.assign_public_ip
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.this.arn
    container_name   = "app"
    container_port   = var.port
  }

  health_check_grace_period_seconds = var.health_check_grace_period

  # The rollback App Runner gave for free. Without the circuit breaker a
  # deployment whose tasks never become healthy retries until someone notices;
  # with it, ECS gives up and puts the previous task definition back.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # 100/200 is a rolling deploy that never dips below full capacity: the new task
  # comes up and passes its health check before the old one is drained. With a
  # single task and minimum_healthy_percent of 100, that briefly costs two.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  enable_execute_command = var.enable_execute_command

  # Propagate the task definition's tags to the running tasks, so a cost report
  # can attribute Fargate spend to an environment.
  propagate_tags = "TASK_DEFINITION"

  tags = merge(var.tags, { Name = var.service_name })

  lifecycle {
    ignore_changes = [
      # CI owns which revision is deployed. It reads the latest revision of the
      # family — which Terraform registers, so an environment or secret change
      # made here is picked up — swaps in the new image, registers the result and
      # points the service at it. Terraform re-asserting its own baseline revision
      # on the next apply would roll the service back to whatever image that
      # revision named.
      #
      # The consequence worth knowing: `terraform apply` alone does not deploy. It
      # produces the revision the next deploy will build on.
      task_definition,

      # Autoscaling owns this at runtime.
      desired_count,
    ]
  }

  depends_on = [
    # A service with a load balancer cannot be created before the listener that
    # will route to it exists, and Terraform cannot infer the ordering from the
    # target group alone.
    aws_lb_listener.http,
    aws_lb_listener.https,
  ]
}


# --- autoscaling ------------------------------------------------------------

resource "aws_appautoscaling_target" "this" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.this.name}"
  scalable_dimension = "ecs:service:DesiredCount"

  min_capacity = var.min_capacity
  max_capacity = var.max_capacity

  tags = var.tags
}

resource "aws_appautoscaling_policy" "cpu" {
  name               = "${var.service_name}-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.this.service_namespace
  resource_id        = aws_appautoscaling_target.this.resource_id
  scalable_dimension = aws_appautoscaling_target.this.scalable_dimension

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }

    target_value = var.cpu_target_utilization

    # Asymmetric on purpose. Scaling out after a minute is responsive; scaling
    # back in after five avoids flapping on a workload that is bursty by nature —
    # a clinician opening a record is a spike, not a trend.
    scale_out_cooldown = 60
    scale_in_cooldown  = 300
  }
}


# --- what CI needs to know --------------------------------------------------

resource "aws_ssm_parameter" "deploy_manifest" {
  name        = "/${var.service_name}/deploy"
  description = "Everything a deploy workflow needs to address this service, so CI does not need Terraform state"
  type        = "String"

  # The alternative is copying these into GitHub repository variables by hand,
  # where they are a second source of truth that drifts silently the first time a
  # subnet is replaced. Publishing them here means the workflow reads whatever the
  # last apply produced, and a network change needs no change to CI.
  #
  # Nothing in here is a secret: ids and names, all of them readable by anyone who
  # can already describe the resources.
  value = jsonencode({
    cluster                = aws_ecs_cluster.this.name
    service                = aws_ecs_service.this.name
    task_definition_family = aws_ecs_task_definition.app.family
    migrate_family         = aws_ecs_task_definition.migrate.family
    container_name         = "app"
    image_repository_url   = var.image_repository_url
    log_group              = aws_cloudwatch_log_group.app.name

    # The migration runs as a one-off task, so it needs the same network
    # configuration the service has — there is no service to inherit it from.
    network = {
      subnets          = var.subnet_ids
      security_groups  = var.security_group_ids
      assign_public_ip = var.assign_public_ip ? "ENABLED" : "DISABLED"
    }
  })

  tags = merge(var.tags, { Name = "/${var.service_name}/deploy" })
}
