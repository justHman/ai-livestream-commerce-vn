# LLM/TTS/LMCache/LiveKit compute: launch templates, ASGs, capacity providers,
# task defs, internal NLB, services. LLM and TTS are independent services —
# no shared task definition or fractional GPU.
#
# Moved blocks migrate state for resources whose address changed in the
# combined-task split; lmcache/livekit/avatar addresses are unchanged.

moved {
  from = aws_service_discovery_service.llm_tts
  to   = aws_service_discovery_service.llm
}

moved {
  from = aws_ecs_task_definition.llm_tts
  to   = aws_ecs_task_definition.llm
}

moved {
  from = aws_ecs_service.llm_tts
  to   = aws_ecs_service.llm
}

# ---------------------------------------------------------------------------
# EC2 Spot capacity — g6 (LLM), g6 (TTS), g4dn (Avatar), c7g (LMCache)
# Placeholders: min=0 so create does not launch expensive Spot until desired>0
# ---------------------------------------------------------------------------

resource "aws_launch_template" "llm" {
  count = var.create_ec2_capacity ? 1 : 0

  name_prefix   = "${local.name_prefix}-lt-llm-"
  image_id      = data.aws_ssm_parameter.ecs_gpu_ami[0].value
  instance_type = var.instance_type_llm

  # GPU images are large (vLLM ~9GB + triton); the default ECS-optimized AMI
  # root volume is ~30GB and runs out of space on pull.
  # 200GB: docker images + extract layers + /models weights + headroom.
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 200
      volume_type           = "gp3"
      delete_on_termination = true
      encrypted             = true
    }
  }

  iam_instance_profile {
    arn = aws_iam_instance_profile.ecs[0].arn
  }

  vpc_security_group_ids = compact([
    try(var.sg_map["llm"], ""),
  ])

  # IMDSv2 required — block SSRF metadata theft
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  user_data = base64encode(<<-EOT
    #!/bin/bash
    echo ECS_CLUSTER=${aws_ecs_cluster.this.name} >> /etc/ecs/ecs.config
    echo ECS_ENABLE_GPU_SUPPORT=true >> /etc/ecs/ecs.config
    echo ECS_ENABLE_SPOT_INSTANCE_DRAINING=true >> /etc/ecs/ecs.config
  EOT
  )

  tag_specifications {
    resource_type = "instance"
    tags = merge(local.common_tags, {
      Name = "${local.name_prefix}-ecs-llm"
      Role = "llm"
    })
  }

  tags = local.common_tags

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_launch_template" "tts" {
  count = var.create_ec2_capacity ? 1 : 0

  name_prefix   = "${local.name_prefix}-lt-tts-"
  image_id      = data.aws_ssm_parameter.ecs_gpu_ami[0].value
  instance_type = var.instance_type_llm

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 200
      volume_type           = "gp3"
      delete_on_termination = true
      encrypted             = true
    }
  }

  iam_instance_profile {
    arn = aws_iam_instance_profile.ecs[0].arn
  }

  vpc_security_group_ids = compact([
    try(var.sg_map["tts"], ""),
  ])

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  user_data = base64encode(<<-EOT
    #!/bin/bash
    echo ECS_CLUSTER=${aws_ecs_cluster.this.name} >> /etc/ecs/ecs.config
    echo ECS_ENABLE_GPU_SUPPORT=true >> /etc/ecs/ecs.config
    echo ECS_ENABLE_SPOT_INSTANCE_DRAINING=true >> /etc/ecs/ecs.config
  EOT
  )

  tag_specifications {
    resource_type = "instance"
    tags = merge(local.common_tags, {
      Name = "${local.name_prefix}-ecs-tts"
      Role = "tts"
    })
  }

  tags = local.common_tags

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_launch_template" "avatar" {
  count = var.create_ec2_capacity ? 1 : 0

  name_prefix   = "${local.name_prefix}-lt-avatar-"
  image_id      = data.aws_ssm_parameter.ecs_gpu_ami[0].value
  instance_type = var.instance_type_avatar

  # Avatar GPU image is large; default AMI root volume too small for pull.
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 200
      volume_type           = "gp3"
      delete_on_termination = true
      encrypted             = true
    }
  }

  iam_instance_profile {
    arn = aws_iam_instance_profile.ecs[0].arn
  }

  vpc_security_group_ids = compact([try(var.sg_map["avatar"], "")])

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  user_data = base64encode(<<-EOT
    #!/bin/bash
    echo ECS_CLUSTER=${aws_ecs_cluster.this.name} >> /etc/ecs/ecs.config
    echo ECS_ENABLE_GPU_SUPPORT=true >> /etc/ecs/ecs.config
    echo ECS_ENABLE_SPOT_INSTANCE_DRAINING=true >> /etc/ecs/ecs.config
  EOT
  )

  tag_specifications {
    resource_type = "instance"
    tags = merge(local.common_tags, {
      Name = "${local.name_prefix}-ecs-avatar"
      Role = "avatar"
    })
  }

  tags = local.common_tags

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_autoscaling_group" "llm" {
  count = var.create_ec2_capacity ? 1 : 0

  name                      = "${local.name_prefix}-asg-llm"
  vpc_zone_identifier       = var.subnet_ids
  min_size                  = 0
  max_size                  = 2
  desired_capacity          = 0
  health_check_type         = "EC2"
  health_check_grace_period = 120
  capacity_rebalance        = true

  mixed_instances_policy {
    instances_distribution {
      on_demand_base_capacity                  = 0
      on_demand_percentage_above_base_capacity = 100 - var.spot_capacity_percentage
      spot_allocation_strategy                 = "price-capacity-optimized"
    }

    launch_template {
      launch_template_specification {
        launch_template_id = aws_launch_template.llm[0].id
        version            = "$Latest"
      }
    }
  }

  tag {
    key                 = "Name"
    value               = "${local.name_prefix}-asg-llm"
    propagate_at_launch = true
  }
  tag {
    key                 = "AmazonECSManaged"
    value               = "true"
    propagate_at_launch = true
  }
  tag {
    key                 = "Env"
    value               = var.env
    propagate_at_launch = true
  }

  lifecycle {
    ignore_changes = [desired_capacity]
  }
}

resource "aws_autoscaling_group" "tts" {
  count = var.create_ec2_capacity ? 1 : 0

  name                      = "${local.name_prefix}-asg-tts"
  vpc_zone_identifier       = var.subnet_ids
  min_size                  = 0
  max_size                  = 2
  desired_capacity          = 0
  health_check_type         = "EC2"
  health_check_grace_period = 120
  capacity_rebalance        = true

  mixed_instances_policy {
    instances_distribution {
      on_demand_base_capacity                  = 0
      on_demand_percentage_above_base_capacity = 100 - var.spot_capacity_percentage
      spot_allocation_strategy                 = "price-capacity-optimized"
    }

    launch_template {
      launch_template_specification {
        launch_template_id = aws_launch_template.tts[0].id
        version            = "$Latest"
      }
    }
  }

  tag {
    key                 = "Name"
    value               = "${local.name_prefix}-asg-tts"
    propagate_at_launch = true
  }
  tag {
    key                 = "AmazonECSManaged"
    value               = "true"
    propagate_at_launch = true
  }
  tag {
    key                 = "Env"
    value               = var.env
    propagate_at_launch = true
  }

  lifecycle {
    ignore_changes = [desired_capacity]
  }
}

resource "aws_autoscaling_group" "avatar" {
  count = var.create_ec2_capacity ? 1 : 0

  name                      = "${local.name_prefix}-asg-avatar"
  vpc_zone_identifier       = var.subnet_ids
  min_size                  = 0
  max_size                  = 2
  desired_capacity          = 0
  health_check_type         = "EC2"
  health_check_grace_period = 120
  capacity_rebalance        = true

  mixed_instances_policy {
    instances_distribution {
      on_demand_base_capacity                  = 0
      on_demand_percentage_above_base_capacity = 100 - var.spot_capacity_percentage
      spot_allocation_strategy                 = "price-capacity-optimized"
    }

    launch_template {
      launch_template_specification {
        launch_template_id = aws_launch_template.avatar[0].id
        version            = "$Latest"
      }
    }
  }

  tag {
    key                 = "Name"
    value               = "${local.name_prefix}-asg-avatar"
    propagate_at_launch = true
  }
  tag {
    key                 = "AmazonECSManaged"
    value               = "true"
    propagate_at_launch = true
  }
  tag {
    key                 = "Env"
    value               = var.env
    propagate_at_launch = true
  }

  lifecycle {
    ignore_changes = [desired_capacity]
  }
}

resource "aws_ecs_capacity_provider" "llm" {
  count = var.create_ec2_capacity ? 1 : 0

  name = local.cp_llm

  auto_scaling_group_provider {
    auto_scaling_group_arn         = aws_autoscaling_group.llm[0].arn
    managed_termination_protection = "DISABLED"

    managed_scaling {
      status                    = "ENABLED"
      target_capacity           = 100
      minimum_scaling_step_size = 1
      maximum_scaling_step_size = 1
    }
  }

  tags = local.common_tags
}

resource "aws_ecs_capacity_provider" "tts" {
  count = var.create_ec2_capacity ? 1 : 0

  name = local.cp_tts

  auto_scaling_group_provider {
    auto_scaling_group_arn         = aws_autoscaling_group.tts[0].arn
    managed_termination_protection = "DISABLED"

    managed_scaling {
      status                    = "ENABLED"
      target_capacity           = 100
      minimum_scaling_step_size = 1
      maximum_scaling_step_size = 1
    }
  }

  tags = local.common_tags
}

resource "aws_ecs_capacity_provider" "avatar" {
  count = var.create_ec2_capacity ? 1 : 0

  name = local.cp_avatar

  auto_scaling_group_provider {
    auto_scaling_group_arn         = aws_autoscaling_group.avatar[0].arn
    managed_termination_protection = "DISABLED"

    managed_scaling {
      status                    = "ENABLED"
      target_capacity           = 100
      minimum_scaling_step_size = 1
      maximum_scaling_step_size = 1
    }
  }

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------

# LLM — EC2 GPU g6, one vLLM container. Own task, own capacity, own rollback.
resource "aws_ecs_task_definition" "llm" {
  family                   = "${local.name_prefix}-llm"
  requires_compatibilities = ["EC2"]
  network_mode             = "awsvpc"
  # Host resources come from the EC2 instance; cpu/memory are soft limits here.
  cpu                = 4096
  memory             = 14336
  execution_role_arn = aws_iam_role.ecs_execution.arn
  task_role_arn      = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode(concat([
    {
      name      = "llm"
      image     = var.image_llm
      essential = true
      resourceRequirements = [
        {
          type  = "GPU"
          value = "1"
        }
      ]
      portMappings = [
        {
          containerPort = 8001
          hostPort      = 8001
          protocol      = "tcp"
        }
      ]
      environment = [
        { name = "ENV", value = var.env },
        # Service-local engine selector; adapters live on the backend.
        { name = "LLM_ENGINE", value = var.llm_engine },
        { name = "WEIGHTS_S3_URI", value = "${var.weights_s3_uri}llm/" },
        # Local dir (vLLM 0.22 supports --model <local-dir> when Path exists + config.json).
        # HF repo ID would phone home to HF (throttle VN -> hang -> SIGINT -> crash).
        # fetch_weights.sh syncs S3 weights/llm/* -> /models/qwen3-4b-awq/ (atomic,
        # validated, .ready marker) before vLLM starts.
        { name = "MODEL_ID", value = "/models/qwen3-4b-awq" },
        { name = "MODEL_SUBDIR", value = "qwen3-4b-awq" },
        { name = "ROLE", value = "llm" },
        { name = "LMCACHE_ENABLED", value = tostring(var.lmcache_enabled) },
        # Air-gapped: vLLM must NOT phone home (HF throttle VN -> connect hang -> SIGINT).
        # HF_HOME separated from model dir (do NOT mix).
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
          awslogs-group         = "${local.log_prefix}/llm"
          awslogs-region        = data.aws_region.current.region
          awslogs-stream-prefix = "llm"
        }
      }
    }
    ],
    # LMCache sidecar — colocated in the LLM task, no standalone capacity.
    # Disabled by default; enabled only when lmcache_enabled=true AND verified
    # benchmark evidence exists (ponytail: evidence-gated, see design §15).
    var.lmcache_enabled ? [
      {
        name      = "lmcache"
        image     = var.image_lmcache
        essential = false
        environment = [
          { name = "ENV", value = var.env },
        ]
        logConfiguration = {
          logDriver = "awslogs"
          options = {
            awslogs-group         = "${local.log_prefix}/lmcache"
            awslogs-region        = data.aws_region.current.region
            awslogs-stream-prefix = "lmcache"
          }
        }
      }
    ] : [])
  )

  tags = merge(local.common_tags, { Role = "llm" })
}

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

resource "aws_ecs_service" "llm" {
  count = var.create_ec2_capacity ? 1 : 0

  name                   = "${local.name_prefix}-llm"
  cluster                = aws_ecs_cluster.this.id
  task_definition        = aws_ecs_task_definition.llm.arn
  desired_count          = var.desired_llm
  enable_execute_command = var.enable_execute_command

  # Cloud Map: register the task ENI under llm.<env>.ai-live.local.
  service_registries {
    registry_arn   = aws_service_discovery_service.llm[0].arn
    container_name = "llm"
  }

  capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.llm[0].name
    weight            = 1
    base              = 0
  }

  network_configuration {
    subnets = var.subnet_ids
    security_groups = compact([
      try(var.sg_map["llm"], ""),
    ])
    # EC2 launch type: public IP is on the instance ENI, not the task ENI.
    assign_public_ip = false
  }

  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  # Rollback: a failed rollout restores the previous task definition.
  deployment_controller {
    type = "ECS"
  }

  tags = merge(local.common_tags, { Role = "llm" })

  lifecycle {
    # CI owns task-definition revisions; operators/autoscaling own desired count after initial create.
    ignore_changes = [desired_count, task_definition]
  }
}

