# Terraform native tests: B5 deployment-side durable voice-store URI.
# Production self-host TTS must never fall back to task-local file:// voice
# profiles: when tts_require_durable_voice_store is set and tts_adapter is
# self_hosted, a non-empty tts_voice_store_uri is required and injected as
# TTS_VOICE_STORE_URI into the TTS task definition. Plan-only, no cloud
# credentials, deterministic. Mirrors vieneu_model_source.tftest.hcl
# (fake digest images + dummy subnet_ids/sg_map; create_ec2_capacity=true so
# the TTS task definition exists).

# ── production self-host TTS without a durable URI fails loudly ──────────────
run "prod_selfhost_tts_without_durable_voice_store_fails" {
  command = plan

  module {
    source = "../modules/compute"
  }

  variables {
    env                             = "prod"
    subnet_ids                      = ["subnet-a", "subnet-b"]
    sg_map                          = { llm = "sg-llm", tts = "sg-tts", avatar = "sg-av", backend = "sg-be" }
    image_backend                   = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_llm                       = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_tts                       = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_avatar                    = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_lmcache                   = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    create_ec2_capacity             = true
    tts_adapter                     = "self_hosted"
    tts_require_durable_voice_store = true
    tts_voice_store_uri             = ""
    secrets_arns = {
      "backend/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/prod/backend/api_token"
      "admin/api_token"   = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/prod/admin/api_token"
    }
  }

  expect_failures = [
    terraform_data.tts_voice_store_durability,
  ]
}

# ── production self-host TTS with a durable URI is accepted and injected ─────
run "prod_selfhost_tts_with_durable_voice_store_ok" {
  command = plan

  module {
    source = "../modules/compute"
  }

  variables {
    env                             = "prod"
    subnet_ids                      = ["subnet-a", "subnet-b"]
    sg_map                          = { llm = "sg-llm", tts = "sg-tts", avatar = "sg-av", backend = "sg-be" }
    image_backend                   = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_llm                       = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_tts                       = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_avatar                    = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_lmcache                   = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    create_ec2_capacity             = true
    tts_adapter                     = "self_hosted"
    tts_require_durable_voice_store = true
    tts_voice_store_uri             = "s3://ai-livestream-assets/voice-profiles"
    secrets_arns = {
      "backend/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/prod/backend/api_token"
      "admin/api_token"   = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/prod/admin/api_token"
    }
  }

  assert {
    condition = lookup(
      { for e in jsondecode(aws_ecs_task_definition.tts.container_definitions)[0].environment : e.name => e.value },
      "TTS_VOICE_STORE_URI",
      "",
    ) == "s3://ai-livestream-assets/voice-profiles"
    error_message = "self-host TTS task must inject the durable TTS_VOICE_STORE_URI"
  }
}

# ── dev/test self-host TTS without a durable URI stays allowed (no regression) ─
run "dev_selfhost_tts_without_durable_uri_ok" {
  command = plan

  module {
    source = "../modules/compute"
  }

  variables {
    env                             = "dev"
    subnet_ids                      = ["subnet-a", "subnet-b"]
    sg_map                          = { llm = "sg-llm", tts = "sg-tts", avatar = "sg-av", backend = "sg-be" }
    image_backend                   = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_llm                       = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_tts                       = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_avatar                    = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_lmcache                   = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    create_ec2_capacity             = true
    tts_adapter                     = "self_hosted"
    tts_require_durable_voice_store = false
    tts_voice_store_uri             = ""
    secrets_arns = {
      "backend/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/dev/backend/api_token"
      "admin/api_token"   = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/dev/admin/api_token"
    }
  }

  assert {
    condition = !contains(
      [for e in jsondecode(aws_ecs_task_definition.tts.container_definitions)[0].environment : e.name],
      "TTS_VOICE_STORE_URI",
    )
    error_message = "dev/test self-host TTS may omit TTS_VOICE_STORE_URI (file:// is dev/test only)"
  }
}
