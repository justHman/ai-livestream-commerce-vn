# Terraform native tests: compute module invariants (OpenSpec 1.75).
# Plan-only, no cloud credentials, deterministic.

# ── Zero-cost hosted-adapter topology ────────────────────────────────────────
run "zero_cost_hosted_topology" {
  command = plan

  module {
    source = "../modules/compute"
  }

  variables {
    env                       = "test"
    subnet_ids                = ["subnet-a", "subnet-b"]
    sg_map                    = { llm = "sg-llm", tts = "sg-tts", avatar = "sg-av", backend = "sg-be" }
    image_backend             = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_llm                 = "imjusthman/ai-live-llm@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_tts                 = "imjusthman/ai-live-tts@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_avatar              = "imjusthman/ai-live-avatar@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_lmcache             = "imjusthman/ai-live-lmcache@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    create_ec2_capacity       = false
    desired_backend           = 1
    desired_llm               = 0
    desired_tts               = 0
    desired_avatar            = 0
    lmcache_enabled           = false
    backend_capacity_provider = "FARGATE_SPOT"
    secrets_arns              = { "backend/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/backend/api_token", "admin/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/admin/api_token" }
  }

  # No GPU capacity when hosted adapters selected (zero-cost topology).
  assert {
    condition     = length(aws_autoscaling_group.llm) == 0
    error_message = "no LLM ASG when create_ec2_capacity=false"
  }
  assert {
    condition     = length(aws_ecs_service.llm) == 0
    error_message = "no LLM service when create_ec2_capacity=false"
  }
  assert {
    condition     = length(aws_ecs_task_definition.migrate) == 0
    error_message = "no migration task without DATABASE_URL secret"
  }
}

# ── Independent services: llm/tts never share a task definition ─────────────
run "independent_llm_tts" {
  command = plan

  module {
    source = "../modules/compute"
  }

  variables {
    env                 = "test"
    subnet_ids          = ["subnet-a", "subnet-b"]
    sg_map              = { llm = "sg-llm", tts = "sg-tts", avatar = "sg-av", backend = "sg-be" }
    image_backend       = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_llm           = "imjusthman/ai-live-llm@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_tts           = "imjusthman/ai-live-tts@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_avatar        = "imjusthman/ai-live-avatar@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_lmcache       = "imjusthman/ai-live-lmcache@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    create_ec2_capacity = true
    desired_llm         = 1
    desired_tts         = 1
    desired_avatar      = 0
    lmcache_enabled     = false
    secrets_arns        = { "backend/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/backend/api_token", "admin/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/admin/api_token" }
  }

  assert {
    condition     = aws_ecs_task_definition.llm.family == "ai-livestream-test-llm"
    error_message = "LLM task family independent"
  }
  assert {
    condition     = aws_ecs_task_definition.tts.family == "ai-livestream-test-tts"
    error_message = "TTS task family independent"
  }
  assert {
    condition     = aws_ecs_task_definition.llm.family != aws_ecs_task_definition.tts.family
    error_message = "LLM and TTS must not share a task definition"
  }
}

# ── No self-host LiveKit / no standalone LMCache ─────────────────────────────
run "no_forbidden_topology" {
  command = plan

  module {
    source = "../modules/compute"
  }

  variables {
    env                 = "test"
    subnet_ids          = ["subnet-a", "subnet-b"]
    sg_map              = { llm = "sg-llm", tts = "sg-tts", avatar = "sg-av", backend = "sg-be" }
    image_backend       = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_llm           = "imjusthman/ai-live-llm@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_tts           = "imjusthman/ai-live-tts@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_avatar        = "imjusthman/ai-live-avatar@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_lmcache       = "imjusthman/ai-live-lmcache@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    create_ec2_capacity = false
    lmcache_enabled     = false
    secrets_arns        = { "backend/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/backend/api_token", "admin/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/admin/api_token" }
  }

  # Zero-cost topology: no GPU/EC2 services when create_ec2_capacity=false.
  # Self-host LiveKit and standalone LMCache resources are absent by design;
  # reintroducing them fails compile and is covered by check script.
  assert {
    condition     = length(aws_ecs_service.llm) == 0
    error_message = "no LLM service in zero-cost topology"
  }
  # No standalone LMCache service (resource absent by design; presence of a
  # lmcache launch template/ASG/CP would fail compile and is covered by
  # scripts/ci/check_infra_module_boundaries.py).
}

# ── Immutable digests + circuit breakers ─────────────────────────────────────
run "digest_and_circuit_breaker" {
  command = plan

  module {
    source = "../modules/compute"
  }

  variables {
    env                 = "test"
    subnet_ids          = ["subnet-a", "subnet-b"]
    sg_map              = { llm = "sg-llm", tts = "sg-tts", avatar = "sg-av", backend = "sg-be" }
    image_backend       = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_llm           = "imjusthman/ai-live-llm@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_tts           = "imjusthman/ai-live-tts@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_avatar        = "imjusthman/ai-live-avatar@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_lmcache       = "imjusthman/ai-live-lmcache@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    create_ec2_capacity = true
    desired_backend     = 1
    lmcache_enabled     = false
    secrets_arns        = { "backend/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/backend/api_token", "admin/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/admin/api_token" }
  }

  assert {
    condition     = can(regex("sha256:[a-f0-9]{64}", aws_ecs_task_definition.backend.container_definitions))
    error_message = "backend image must be an immutable digest"
  }
  assert {
    condition     = aws_ecs_service.backend.deployment_circuit_breaker[0].rollback == true
    error_message = "backend service circuit breaker must roll back"
  }
}

# Prod Spot rejection is enforced by env-root variable validation and covered
# by scripts/ci/check_infra_module_boundaries.py (runtime matrix assert).
