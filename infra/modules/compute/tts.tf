# TTS — independent EC2 GPU service (was part of combined llm_tts task).
# Own task definition, capacity provider, desired count, health check, rollback.

resource "aws_ecs_task_definition" "tts" {
  family                   = "${local.name_prefix}-tts"
  requires_compatibilities = ["EC2"]
  network_mode             = "awsvpc"
  # Host resources come from the EC2 instance; cpu/memory are soft limits here.
  cpu                = 4096
  memory             = 14336
  execution_role_arn = aws_iam_role.ecs_execution.arn
  task_role_arn      = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "tts"
      image     = var.image_tts
      essential = true
      resourceRequirements = [
        {
          type  = "GPU"
          value = "1"
        }
      ]
      portMappings = [
        {
          containerPort = 8002
          hostPort      = 8002
          protocol      = "tcp"
        }
      ]
      environment = [
        { name = "ENV", value = var.env },
        { name = "WEIGHTS_S3_URI", value = "${var.weights_s3_uri}tts/" },
        # Local dir (vLLM 0.22 supports --model <local-dir> via Path.exists()).
        # fetch_weights.sh syncs S3 weights/tts/vieneu/* -> /models/vieneu/
        # (atomic, validated, .ready) before vllm-omni starts.
        { name = "MODEL_ID", value = "/models/vieneu" },
        { name = "MODEL_SUBDIR", value = "vieneu" },
        { name = "ROLE", value = "tts" },
        # Air-gapped + HF cache separated from model dir.
        { name = "HF_HUB_OFFLINE", value = "1" },
        { name = "TRANSFORMERS_OFFLINE", value = "1" },
        { name = "HF_HUB_DISABLE_TELEMETRY", value = "1" },
        { name = "VLLM_NO_USAGE_STATS", value = "1" },
        { name = "DO_NOT_TRACK", value = "1" },
        { name = "HF_HOME", value = "/var/cache/huggingface" },
        { name = "HF_HUB_CACHE", value = "/var/cache/huggingface/hub" },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = "${local.log_prefix}/tts"
          awslogs-region        = data.aws_region.current.region
          awslogs-stream-prefix = "tts"
        }
      }
    }
  ])

  tags = merge(local.common_tags, { Role = "tts" })
}

resource "aws_ecs_service" "tts" {
  count = var.create_ec2_capacity ? 1 : 0

  name                   = "${local.name_prefix}-tts"
  cluster                = aws_ecs_cluster.this.id
  task_definition        = aws_ecs_task_definition.tts.arn
  desired_count          = var.desired_tts
  enable_execute_command = var.enable_execute_command

  # Cloud Map: register the task ENI under tts.<env>.ai-live.local.
  service_registries {
    registry_arn   = aws_service_discovery_service.tts[0].arn
    container_name = "tts"
  }

  # Internal NLB target group: stable DNS endpoint for backend (Fargate).
  load_balancer {
    target_group_arn = aws_lb_target_group.tts[0].arn
    container_name   = "tts"
    container_port   = 8002
  }

  capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.tts[0].name
    weight            = 1
    base              = 0
  }

  network_configuration {
    subnets = var.subnet_ids
    security_groups = compact([
      try(var.sg_map["tts"], ""),
    ])
    # EC2 launch type: public IP is on the instance ENI, not the task ENI.
    assign_public_ip = false
  }

  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  tags = merge(local.common_tags, { Role = "tts" })

  lifecycle {
    # CI owns task-definition revisions; operators/autoscaling own desired count after initial create.
    ignore_changes = [desired_count, task_definition]
  }
}
