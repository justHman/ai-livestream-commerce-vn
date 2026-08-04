# Backend Fargate task + service (was main.tf).
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
        # Cloud Map private DNS for self-host adapters; var override for hosted providers.
        { name = "TTS_BASE_URL", value = var.tts_base_url != "" ? var.tts_base_url : "http://tts.${var.env}.ai-live.local:8002" },
        { name = "TTS_VOICE_ID", value = var.tts_voice_id },
        { name = "TTS_MODEL_ID", value = "eleven_turbo_v2_5" },
        { name = "SESSION_STORE", value = var.session_store },
        { name = "DIRECTOR_ENABLED", value = "1" },
        { name = "DIRECTOR_EMBEDDER", value = "semantic-required" },
        { name = "DIRECTOR_EMBEDDER_MODEL", value = "bkai-foundation-models/vietnamese-bi-encoder" },
        { name = "LMCACHE_ENABLED", value = tostring(var.lmcache_enabled) },
        { name = "PIPECAT_ENABLED", value = "0" },
        { name = "LIVEKIT_URL", value = var.livekit_url },
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
        # LiveKit Cloud (no self-host): URL is plaintext env; key/secret are
        # server-side SSM SecureString refs — never sent to the browser.
        lookup(var.secrets_arns, "livekit/api_key", "") != "" ? [
          {
            name      = "LIVEKIT_API_KEY"
            valueFrom = var.secrets_arns["livekit/api_key"]
          },
        ] : [],
        lookup(var.secrets_arns, "livekit/api_secret", "") != "" ? [
          {
            name      = "LIVEKIT_API_SECRET"
            valueFrom = var.secrets_arns["livekit/api_secret"]
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
