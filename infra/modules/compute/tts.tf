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
      environment = concat(
        # Always-present base: the VieNeu SDK/provider model-init path
        # (Decision 6 / R0.5-V3). No WEIGHTS_S3_URI and no forced offline flags
        # here so entrypoint.sh skips fetch_weights.sh and HF init stays online.
        [
          { name = "ENV", value = var.env },
          # Service-local engine selector; adapters live on the backend.
          { name = "TTS_ENGINE", value = var.tts_engine },
          { name = "TTS_PROVIDER", value = "vieneu_v3" },
          { name = "TTS_MODEL_REVISION", value = "pnnbao-ump/VieNeu-TTS-v3-Turbo" },
          { name = "TTS_ACCELERATOR", value = "auto" },
          { name = "ROLE", value = "tts" },
          { name = "HF_HUB_DISABLE_TELEMETRY", value = "1" },
          { name = "DO_NOT_TRACK", value = "1" },
          { name = "HF_HOME", value = "/var/cache/huggingface" },
          { name = "HF_HUB_CACHE", value = "/var/cache/huggingface/hub" },
        ],
        # Dormant S3/offline bootstrap for a future object-backed engine:
        # explicit opt-in via tts_model_source="s3_bootstrap" only.
        var.tts_model_source == "s3_bootstrap" ? [
          { name = "WEIGHTS_S3_URI", value = "${trim(var.weights_s3_uri, "/")}/tts/" },
          # fetch_weights.sh syncs S3 weights/tts/vieneu/* -> /models/vieneu/
          # (atomic, validated, .ready) before the uvicorn service starts.
          { name = "MODEL_SUBDIR", value = "vieneu" },
          # Air-gapped + HF cache separated from model dir.
          { name = "HF_HUB_OFFLINE", value = "1" },
          { name = "TRANSFORMERS_OFFLINE", value = "1" },
        ] : [],
        # Durable voice-profile store (B5): provider-neutral URI injected
        # whenever the operator supplies one. Production self-host TTS is
        # required to set tts_voice_store_uri (see tts_voice_store_durability
        # precondition) so voice profiles never land on task-local file://.
        var.tts_voice_store_uri != "" ? [
          { name = "TTS_VOICE_STORE_URI", value = var.tts_voice_store_uri },
        ] : [],
      )
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

  # Real health endpoint; circuit breaker rolls back on repeated failures.
  health_check_grace_period_seconds = 60
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  tags = merge(local.common_tags, { Role = "tts" })

  lifecycle {
    # CI owns task-definition revisions; operators/autoscaling own desired count after initial create.
    ignore_changes = [desired_count, task_definition]
  }
}

# B5 durable voice-store invariant: production self-host TTS must never fall
# back to task-local file:// voice profiles. When tts_require_durable_voice_store
# is set and self-host TTS is the active adapter, tts_voice_store_uri is required
# and the task above injects it as TTS_VOICE_STORE_URI.
resource "terraform_data" "tts_voice_store_durability" {
  input = format("%s|%s|%s", var.tts_adapter, var.tts_require_durable_voice_store, var.tts_voice_store_uri)
  lifecycle {
    precondition {
      condition = (
        !var.tts_require_durable_voice_store
        || var.tts_adapter != "self_hosted"
        || var.tts_voice_store_uri != ""
      )
      error_message = <<-EOT
        Self-host TTS in production requires a durable provider-neutral
        TTS_VOICE_STORE_URI (e.g. s3://<bucket>/voice-profiles). Refusing to let
        voice profiles land on a task-local filesystem (V3 acceptance: local
        filesystem is explicit dev/test only). Set tts_voice_store_uri, or select
        a hosted TTS adapter.
      EOT
    }
  }
}
