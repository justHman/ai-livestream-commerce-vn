data "aws_region" "current" {}
data "aws_caller_identity" "current" {}
data "aws_ssm_parameter" "ecs_gpu_ami" {
  count = var.create_ec2_capacity ? 1 : 0
  # ECS-optimized GPU AMI (x86_64) for g6/g4dn
  name = "/aws/service/ecs/optimized-ami/amazon-linux-2/gpu/recommended/image_id"
}
data "aws_ssm_parameter" "ecs_arm_ami" {
  count = var.create_ec2_capacity ? 1 : 0
  # ECS-optimized ARM AMI for c7g lmcache
  name = "/aws/service/ecs/optimized-ami/amazon-linux-2/arm64/recommended/image_id"
}

locals {
  common_tags = merge(
    {
      Project = var.project
      Env     = var.env
      Module  = "compute"
    },
    var.tags,
  )

  name_prefix = "${var.project}-${var.env}"
  log_prefix  = var.log_group_prefix != "" ? var.log_group_prefix : "/ecs/${var.project}-${var.env}"

  lmcache_desired = var.lmcache_enabled ? var.desired_lmcache : 0

  # Capacity provider names (created when create_ec2_capacity=true)
  cp_llm     = "${local.name_prefix}-cp-llm"
  cp_avatar  = "${local.name_prefix}-cp-avatar"
  cp_lmcache = "${local.name_prefix}-cp-lmcache"
}

# ---------------------------------------------------------------------------
# IAM — minimal execution + task roles (security module has SGs only)
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_execution" {
  name               = "${local.name_prefix}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# SSM SecureString read for future secrets injection
resource "aws_iam_role_policy" "ecs_execution_ssm" {
  name = "${local.name_prefix}-ecs-execution-ssm"
  role = aws_iam_role.ecs_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameters"]
        Resource = values(var.secrets_arns)
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "ssm.${data.aws_region.current.region}.amazonaws.com"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role" "ecs_task" {
  name               = "${local.name_prefix}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy" "ecs_task_s3_weights" {
  name = "${local.name_prefix}-ecs-task-s3"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket",
        ]
        Resource = ["*"]
      },
      {
        Effect = "Allow"
        Action = [
          "ssmmessages:CreateControlChannel",
          "ssmmessages:CreateDataChannel",
          "ssmmessages:OpenControlChannel",
          "ssmmessages:OpenDataChannel",
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_role" "ecs_instance" {
  count = var.create_ec2_capacity ? 1 : 0

  name               = "${local.name_prefix}-ecs-instance"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "ecs_instance" {
  count = var.create_ec2_capacity ? 1 : 0

  role       = aws_iam_role.ecs_instance[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_role_policy_attachment" "ecs_instance_ssm" {
  count = var.create_ec2_capacity ? 1 : 0

  role       = aws_iam_role.ecs_instance[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ecs" {
  count = var.create_ec2_capacity ? 1 : 0

  name = "${local.name_prefix}-ecs-instance"
  role = aws_iam_role.ecs_instance[0].name
  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# ECS cluster + capacity providers
# ---------------------------------------------------------------------------

resource "aws_ecs_cluster" "this" {
  name = "${local.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = var.enable_container_insights ? "enabled" : "disabled"
  }

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-cluster"
  })
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name = aws_ecs_cluster.this.name

  capacity_providers = compact(concat(
    ["FARGATE", "FARGATE_SPOT"],
    var.create_ec2_capacity ? [
      aws_ecs_capacity_provider.llm[0].name,
      aws_ecs_capacity_provider.avatar[0].name,
      aws_ecs_capacity_provider.lmcache[0].name,
    ] : [],
  ))

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 1
    base              = 0
  }
}

# ---------------------------------------------------------------------------
# Cloud Map service discovery (internal DNS for backend → LLM/TTS)
# 1 namespace + 1 SD service registering the llm_tts task ENI.
# Both LLM (8001) and TTS (8002) resolve via the same A record;
# backend calls llm-tts.<env>.ai-live.local:8001 and :8002 respectively.
# ---------------------------------------------------------------------------
resource "aws_service_discovery_private_dns_namespace" "this" {
  count = var.create_ec2_capacity ? 1 : 0

  name        = "${var.env}.ai-live.local"
  description = "Internal DNS for ${var.env} GPU services"
  vpc         = var.vpc_id
  tags        = local.common_tags
}

resource "aws_service_discovery_service" "llm_tts" {
  count = var.create_ec2_capacity ? 1 : 0

  name = "llm-tts"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.this[0].id
    dns_records {
      ttl  = 10
      type = "A"
    }
    routing_policy = "MULTIVALUE"
  }

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# EC2 Spot capacity — g6 (LLM+TTS), g4dn (Avatar), c7g (LMCache)
# Placeholders: min=0 so create does not launch expensive Spot until desired>0
# ---------------------------------------------------------------------------

resource "aws_launch_template" "llm" {
  count = var.create_ec2_capacity ? 1 : 0

  name_prefix   = "${local.name_prefix}-lt-llm-"
  image_id      = data.aws_ssm_parameter.ecs_gpu_ami[0].value
  instance_type = var.instance_type_llm

  # GPU images are large (vLLM ~9GB + vllm-omni ~9GB + triton); the default
  # ECS-optimized AMI root volume is ~30GB and runs out of space on pull.
  # 200GB: docker images 18GB + extract layers ~36GB + /models weights ~7GB +
  # SSM agent logs + headroom for image updates without prune.
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
    try(var.sg_map["tts"], ""),
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
      Role = "llm-tts"
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

resource "aws_launch_template" "lmcache" {
  count = var.create_ec2_capacity ? 1 : 0

  name_prefix   = "${local.name_prefix}-lt-lmcache-"
  image_id      = data.aws_ssm_parameter.ecs_arm_ami[0].value
  instance_type = var.instance_type_lmcache

  iam_instance_profile {
    arn = aws_iam_instance_profile.ecs[0].arn
  }

  vpc_security_group_ids = compact([try(var.sg_map["lmcache"], "")])

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  user_data = base64encode(<<-EOT
    #!/bin/bash
    echo ECS_CLUSTER=${aws_ecs_cluster.this.name} >> /etc/ecs/ecs.config
    echo ECS_ENABLE_SPOT_INSTANCE_DRAINING=true >> /etc/ecs/ecs.config
  EOT
  )

  tag_specifications {
    resource_type = "instance"
    tags = merge(local.common_tags, {
      Name = "${local.name_prefix}-ecs-lmcache"
      Role = "lmcache"
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

resource "aws_autoscaling_group" "lmcache" {
  count = var.create_ec2_capacity ? 1 : 0

  name                      = "${local.name_prefix}-asg-lmcache"
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
        launch_template_id = aws_launch_template.lmcache[0].id
        version            = "$Latest"
      }
    }
  }

  tag {
    key                 = "Name"
    value               = "${local.name_prefix}-asg-lmcache"
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

resource "aws_ecs_capacity_provider" "lmcache" {
  count = var.create_ec2_capacity ? 1 : 0

  name = local.cp_lmcache

  auto_scaling_group_provider {
    auto_scaling_group_arn         = aws_autoscaling_group.lmcache[0].arn
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

# Backend — Fargate ARM64
resource "aws_ecs_task_definition" "backend" {
  family                   = "${local.name_prefix}-backend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.backend_cpu
  memory                   = var.backend_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name      = "backend"
      image     = var.image_backend
      essential = true
      portMappings = [
        {
          containerPort = 8800
          hostPort      = 8800
          protocol      = "tcp"
        }
      ]
      environment = [
        { name = "ENV", value = var.env },
        { name = "PORT", value = "8800" },
        { name = "APP_ENV", value = var.app_env != "" ? var.app_env : var.env },
        # API-only smoke: mock render + tone TTS + no LLM.
        # SESSION_STORE: memory (single-task) or redis (multi-task via redis_url).
        { name = "RENDER_BACKEND", value = var.render_backend },
        { name = "LLM_ENGINE", value = var.llm_engine },
        { name = "LLM_BASE_URL", value = var.llm_base_url },
        { name = "LLM_MODEL", value = var.llm_model },
        # DeepSeek reasoning model uses tokens for reasoning then content; default
        # max_tokens=128 -> all tokens consumed by reasoning -> content="" +
        # finish=length. Raise to 8192 so reasoning (2-4k) + content (1-2k) fit.
        { name = "LLM_MAX_TOKENS", value = "8192" },
        { name = "TTS_ENGINE", value = var.tts_engine },
        { name = "TTS_BASE_URL", value = var.tts_base_url },
        { name = "TTS_VOICE_ID", value = var.tts_voice_id },
        { name = "TTS_MODEL_ID", value = "eleven_turbo_v2_5" },
        { name = "SESSION_STORE", value = var.session_store },
        { name = "DIRECTOR_ENABLED", value = "1" },
        { name = "DIRECTOR_EMBEDDER", value = "semantic-required" },
        { name = "DIRECTOR_EMBEDDER_MODEL", value = "bkai-foundation-models/vietnamese-bi-encoder" },
        { name = "LMCACHE_ENABLED", value = tostring(var.lmcache_enabled) },
        { name = "PIPECAT_ENABLED", value = "0" },
        { name = "LIVEKIT_PUBLISH", value = "0" },
        { name = "DEBUG_ENABLED", value = var.debug_enabled ? "1" : "0" },
        { name = "CORS_ORIGINS", value = var.cors_origins },
        { name = "REDIS_URL", value = var.redis_url },
      ]
      secrets = concat(
        [
          {
            name      = "BACKEND_API_TOKEN"
            valueFrom = var.secrets_arns["backend/api_token"]
          },
          {
            name      = "ADMIN_API_TOKEN"
            valueFrom = var.secrets_arns["admin/api_token"]
          },
        ],
        lookup(var.secrets_arns, "backend/database_url", "") != "" ? [
          {
            name      = "DATABASE_URL"
            valueFrom = var.secrets_arns["backend/database_url"]
          },
        ] : [],
        # Stage 2 only: LiveAvatar cloud API key (backend-only secret).
        # Injected when the secrets module exposes liveavatar/api_key;
        # absent in Stage 1 (mock) and Stage 3 (self-host avatar).
        lookup(var.secrets_arns, "liveavatar/api_key", "") != "" ? [
          {
            name      = "LIVEAVATAR_API_KEY"
            valueFrom = var.secrets_arns["liveavatar/api_key"]
          },
        ] : [],
        # Remote OpenAI-compat LLM API key (optional, when llm_engine=openai_compat
        # and base_url points to a remote endpoint needing auth).
        lookup(var.secrets_arns, "llm/api_key", "") != "" ? [
          {
            name      = "LLM_API_KEY"
            valueFrom = var.secrets_arns["llm/api_key"]
          },
        ] : [],
        # Stage 2 ship-fast: ElevenLabs remote TTS API key (optional, when
        # tts_engine=elevenlabs). Put in SSM /dev/tts/api_key out-of-band.
        lookup(var.secrets_arns, "tts/api_key", "") != "" ? [
          {
            name      = "TTS_API_KEY"
            valueFrom = var.secrets_arns["tts/api_key"]
          },
        ] : [],
      )
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = "${local.log_prefix}/backend"
          awslogs-region        = data.aws_region.current.region
          awslogs-stream-prefix = "backend"
        }
      }
    }
  ])

  tags = merge(local.common_tags, { Role = "backend" })
}

# LLM + TTS — EC2 GPU, same task family / host; ONLY llm declares GPU resource
# TTS shares the GPU process-level (0.25 fraction) — no resourceRequirements on tts.
resource "aws_ecs_task_definition" "llm_tts" {
  family                   = "${local.name_prefix}-llm-tts"
  requires_compatibilities = ["EC2"]
  network_mode             = "awsvpc"
  # Host resources come from the EC2 instance; cpu/memory are soft limits here.
  # memory 14336 -> 24576: vLLM init + fetch_weights sync + CUDA context + Qwen3-4B-AWQ
  # (2.68GB) + vllm-omni TTS + KV cache can exceed 14GB container limit -> OOM kill (137).
  cpu                = 4096
  memory             = 24576
  execution_role_arn = aws_iam_role.ecs_execution.arn
  task_role_arn      = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "llm"
      image     = var.image_llm
      essential = true
      # ONLY llm container requests GPU — TTS shares the same device.
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
    },
    {
      name      = "tts"
      image     = var.image_tts
      essential = true
      # No GPU resourceRequirements — shares GPU 0 with llm via process fractions.
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

  tags = merge(local.common_tags, { Role = "llm-tts" })
}

# Avatar — EC2 GPU g4dn
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
resource "aws_ecs_task_definition" "lmcache" {
  family                   = "${local.name_prefix}-lmcache"
  requires_compatibilities = ["EC2"]
  network_mode             = "awsvpc"
  cpu                      = 4096
  memory                   = 14336
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name      = "lmcache"
      image     = var.image_lmcache
      essential = true
      portMappings = [
        {
          containerPort = 5555
          hostPort      = 5555
          protocol      = "tcp"
        }
      ]
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
  ])

  tags = merge(local.common_tags, { Role = "lmcache" })
}

# LiveKit — Fargate ARM64
resource "aws_ecs_task_definition" "livekit" {
  family                   = "${local.name_prefix}-livekit"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.livekit_cpu
  memory                   = var.livekit_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name      = "livekit"
      image     = var.image_livekit
      essential = true
      portMappings = [
        {
          containerPort = 7880
          hostPort      = 7880
          protocol      = "tcp"
        },
        {
          containerPort = 7881
          hostPort      = 7881
          protocol      = "tcp"
        },
        {
          containerPort = 50000
          hostPort      = 50000
          protocol      = "udp"
        }
      ]
      environment = [
        { name = "ENV", value = var.env },
      ]
      secrets = [
        {
          name      = "LIVEKIT_API_KEY",
          valueFrom = var.secrets_arns["livekit/api_key"]
        },
        {
          name      = "LIVEKIT_API_SECRET",
          valueFrom = var.secrets_arns["livekit/api_secret"]
        },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = "${local.log_prefix}/livekit"
          awslogs-region        = data.aws_region.current.region
          awslogs-stream-prefix = "livekit"
        }
      }
    }
  ])

  tags = merge(local.common_tags, { Role = "livekit" })
}

# ---------------------------------------------------------------------------
# Internal NLB for llm_tts (stable DNS for backend Fargate → GPU engines)
# Fargate backend cannot resolve Cloud Map private namespace reliably; NLB
# internal DNS (.elb.amazonaws.com) resolves from Fargate tasks. Two target
# groups: llm:8001 (vLLM OpenAI-compat), tts:8002 (vllm-omni /v1/audio/speech).
# ---------------------------------------------------------------------------
resource "aws_lb" "llm_tts_internal" {
  count = var.create_ec2_capacity ? 1 : 0

  name               = "${var.project}-stg-llm-tts"
  internal           = true
  load_balancer_type = "network"
  subnets            = var.subnet_ids

  tags = merge(local.common_tags, { Name = "${local.name_prefix}-llm-tts-internal" })
}

resource "aws_lb_target_group" "llm" {
  count = var.create_ec2_capacity ? 1 : 0

  name        = "${var.project}-stg-llm"
  port        = 8001
  protocol    = "TCP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    protocol            = "HTTP"
    path                = "/health"
    port                = "8001"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
  }

  tags = merge(local.common_tags, { Role = "llm" })
}

resource "aws_lb_target_group" "tts" {
  count = var.create_ec2_capacity ? 1 : 0

  name        = "${var.project}-stg-tts"
  port        = 8002
  protocol    = "TCP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    protocol            = "HTTP"
    path                = "/health"
    port                = "8002"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
  }

  tags = merge(local.common_tags, { Role = "tts" })
}

resource "aws_lb_listener" "llm" {
  count = var.create_ec2_capacity ? 1 : 0

  load_balancer_arn = aws_lb.llm_tts_internal[0].arn
  port              = 8001
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.llm[0].arn
  }

  tags = local.common_tags
}

resource "aws_lb_listener" "tts" {
  count = var.create_ec2_capacity ? 1 : 0

  load_balancer_arn = aws_lb.llm_tts_internal[0].arn
  port              = 8002
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.tts[0].arn
  }

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

resource "aws_ecs_service" "backend" {
  name                   = "${local.name_prefix}-backend"
  cluster                = aws_ecs_cluster.this.id
  task_definition        = aws_ecs_task_definition.backend.arn
  desired_count          = var.desired_backend
  enable_execute_command = var.enable_execute_command

  capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 1
    base              = 0
  }

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = compact([try(var.sg_map["backend"], "")])
    assign_public_ip = var.assign_public_ip
  }

  dynamic "load_balancer" {
    for_each = var.backend_target_group_arn != "" ? [1] : []
    content {
      target_group_arn = var.backend_target_group_arn
      container_name   = "backend"
      container_port   = 8800
    }
  }

  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 200

  tags = merge(local.common_tags, { Role = "backend" })

  lifecycle {
    # CI owns task-definition revisions; operators/autoscaling own desired count after initial create.
    ignore_changes = [desired_count, task_definition]
  }
}

# EC2/GPU services only exist when capacity providers exist.
# Task defs require EC2 — never fall back to FARGATE (invalid launch type).
resource "aws_ecs_service" "llm_tts" {
  count = var.create_ec2_capacity ? 1 : 0

  name                   = "${local.name_prefix}-llm-tts"
  cluster                = aws_ecs_cluster.this.id
  task_definition        = aws_ecs_task_definition.llm_tts.arn
  desired_count          = var.desired_llm_tts
  enable_execute_command = var.enable_execute_command

  # Cloud Map: register the task ENI under llm-tts.<env>.ai-live.local.
  # Both LLM (:8001) and TTS (:8002) share this A record (same task ENI).
  service_registries {
    registry_arn   = aws_service_discovery_service.llm_tts[0].arn
    container_name = "llm"
  }

  # Internal NLB target groups: stable DNS endpoint for backend (Fargate) to
  # reach llm_tts without relying on Cloud Map private namespace resolution
  # (Fargate ENI DNS does not reliably resolve Cloud Map private hosted zones).
  # NLB DNS is AWS-internal and resolves from Fargate tasks.
  load_balancer {
    target_group_arn = aws_lb_target_group.llm[0].arn
    container_name   = "llm"
    container_port   = 8001
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.tts[0].arn
    container_name   = "tts"
    container_port   = 8002
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
      try(var.sg_map["tts"], ""),
    ])
    # EC2 launch type: public IP is on the instance ENI, not the task ENI.
    assign_public_ip = false
  }

  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  tags = merge(local.common_tags, { Role = "llm-tts" })

  lifecycle {
    # CI owns task-definition revisions; operators/autoscaling own desired count after initial create.
    ignore_changes = [desired_count, task_definition]
  }
}

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

resource "aws_ecs_service" "lmcache" {
  count = var.create_ec2_capacity ? 1 : 0

  name                   = "${local.name_prefix}-lmcache"
  cluster                = aws_ecs_cluster.this.id
  task_definition        = aws_ecs_task_definition.lmcache.arn
  desired_count          = local.lmcache_desired
  enable_execute_command = var.enable_execute_command

  capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.lmcache[0].name
    weight            = 1
  }

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = compact([try(var.sg_map["lmcache"], "")])
    assign_public_ip = false
  }

  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  tags = merge(local.common_tags, { Role = "lmcache" })

  lifecycle {
    # CI owns task-definition revisions; operators/autoscaling own desired count after initial create.
    ignore_changes = [desired_count, task_definition]
  }
}

resource "aws_ecs_service" "livekit" {
  name                   = "${local.name_prefix}-livekit"
  cluster                = aws_ecs_cluster.this.id
  task_definition        = aws_ecs_task_definition.livekit.arn
  desired_count          = var.desired_livekit
  enable_execute_command = var.enable_execute_command

  capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 1
  }

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = compact([try(var.sg_map["livekit"], "")])
    assign_public_ip = var.assign_public_ip
  }

  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 200

  tags = merge(local.common_tags, { Role = "livekit" })

  lifecycle {
    # CI owns task-definition revisions; operators/autoscaling own desired count after initial create.
    ignore_changes = [desired_count, task_definition]
  }
}
