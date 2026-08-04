# Avatar GPU task + service (was main.tf).
resource "aws_ecs_task_definition" "avatar" {
  family                   = "${local.name_prefix}-avatar"
  requires_compatibilities = ["EC2"]
  network_mode             = "awsvpc"
  cpu                      = 2048
  memory                   = 14336
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "avatar"
      image     = var.image_avatar
      essential = true
      resourceRequirements = [
        {
          type  = "GPU"
          value = "1"
        }
      ]
      portMappings = [
        {
          containerPort = 8080
          hostPort      = 8080
          protocol      = "tcp"
        }
      ]
      environment = [
        { name = "ENV", value = var.env },
        # Service-local engine selector; adapters live on the backend.
        { name = "AVATAR_ENGINE", value = var.avatar_engine },
        { name = "WEIGHTS_S3_URI", value = var.weights_s3_uri },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = "${local.log_prefix}/avatar"
          awslogs-region        = data.aws_region.current.region
          awslogs-stream-prefix = "avatar"
        }
      }
    }
  ])

  tags = merge(local.common_tags, { Role = "avatar" })
}

# LMCache — EC2 ARM (no GPU)
resource "aws_ecs_service" "avatar" {
  count = var.create_ec2_capacity ? 1 : 0

  name                   = "${local.name_prefix}-avatar"
  cluster                = aws_ecs_cluster.this.id
  task_definition        = aws_ecs_task_definition.avatar.arn
  desired_count          = var.desired_avatar
  enable_execute_command = var.enable_execute_command

  capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.avatar[0].name
    weight            = 1
  }

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = compact([try(var.sg_map["avatar"], "")])
    assign_public_ip = false
  }

  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  tags = merge(local.common_tags, { Role = "avatar" })

  lifecycle {
    # CI owns task-definition revisions; operators/autoscaling own desired count after initial create.
    ignore_changes = [desired_count, task_definition]
  }
}

