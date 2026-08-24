# Terraform native tests: compute-module capacity invariants (R2.4).
# A hosted/provider adapter selection must force unused self-host capacity to
# zero (fail plan, not silently allow); self-host Avatar requires the test-only
# escape while only a stub engine exists. Plan-only, no cloud credentials,
# deterministic. Mirrors runtime_matrix.tftest.hcl (fake digest images +
# dummy subnet_ids/sg_map/secrets_arns).

# ── hosted Avatar must zero unused self-host Avatar capacity ────────────────
run "hosted_avatar_rejects_unused_selfhost_capacity" {
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
    avatar_adapter      = "liveavatar"
    desired_avatar      = 1
    secrets_arns        = { "backend/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/backend/api_token", "admin/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/admin/api_token" }
  }

  expect_failures = [
    terraform_data.capacity_invariants,
  ]
}

# ── hosted TTS must zero unused self-host TTS capacity ──────────────────────
run "hosted_tts_rejects_unused_selfhost_capacity" {
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
    tts_adapter         = "elevenlabs"
    desired_tts         = 1
    secrets_arns        = { "backend/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/backend/api_token", "admin/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/admin/api_token" }
  }

  expect_failures = [
    terraform_data.capacity_invariants,
  ]
}

# ── hosted LLM URL override must zero unused self-host LLM capacity ─────────
run "hosted_llm_url_rejects_unused_selfhost_capacity" {
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
    llm_base_url        = "https://llm.example.com/v1"
    desired_llm         = 1
    secrets_arns        = { "backend/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/backend/api_token", "admin/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/admin/api_token" }
  }

  expect_failures = [
    terraform_data.capacity_invariants,
  ]
}

# ── self-host Avatar without a real engine must fail (only a stub exists) ───
run "selfhost_avatar_without_real_engine_fails" {
  command = plan

  module {
    source = "../modules/compute"
  }

  variables {
    env                         = "test"
    subnet_ids                  = ["subnet-a", "subnet-b"]
    sg_map                      = { llm = "sg-llm", tts = "sg-tts", avatar = "sg-av", backend = "sg-be" }
    image_backend               = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_llm                   = "imjusthman/ai-live-llm@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_tts                   = "imjusthman/ai-live-tts@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_avatar                = "imjusthman/ai-live-avatar@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_lmcache               = "imjusthman/ai-live-lmcache@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    create_ec2_capacity         = false
    avatar_adapter              = "self_hosted"
    allow_stub_avatar_test_only = false
    desired_avatar              = 0
    secrets_arns                = { "backend/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/backend/api_token", "admin/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/admin/api_token" }
  }

  expect_failures = [
    terraform_data.capacity_invariants,
  ]
}

# ── explicit test-only escape: self-host Avatar allowed when flagged ────────
run "selfhost_avatar_test_only_escape" {
  command = plan

  module {
    source = "../modules/compute"
  }

  variables {
    env                         = "test"
    subnet_ids                  = ["subnet-a", "subnet-b"]
    sg_map                      = { llm = "sg-llm", tts = "sg-tts", avatar = "sg-av", backend = "sg-be" }
    image_backend               = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_llm                   = "imjusthman/ai-live-llm@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_tts                   = "imjusthman/ai-live-tts@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_avatar                = "imjusthman/ai-live-avatar@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_lmcache               = "imjusthman/ai-live-lmcache@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    create_ec2_capacity         = false
    avatar_adapter              = "self_hosted"
    allow_stub_avatar_test_only = true
    desired_avatar              = 0
    secrets_arns                = { "backend/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/backend/api_token", "admin/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/admin/api_token" }
  }

  assert {
    condition     = aws_ecs_task_definition.avatar.family == "ai-livestream-test-avatar"
    error_message = "self-host Avatar task definition must exist in test-only escape topology"
  }
}
