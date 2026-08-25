# Terraform native tests: VieNeu TTS model-source engine-specificity (R0.5/Decision 6).
# Default "sdk" mode must NOT inject WEIGHTS_S3_URI or forced offline flags so the
# VieNeu SDK/provider download path is used; "s3_bootstrap" is a dormant opt-in that
# keeps the object-backed weights + air-gapped flags. Plan-only, no cloud credentials,
# deterministic. Mirrors runtime_matrix.tftest.hcl (fake digest images + dummy
# subnet_ids/sg_map/secrets_arns; create_ec2_capacity=true so the TTS task def exists).

# ── SDK path: no S3 URI, no forced offline (default tts_model_source) ────────
run "vieneu_sdk_model_source_has_no_s3_or_offline" {
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
    tts_engine          = "vieneu"
    tts_model_source    = "sdk"
    secrets_arns        = { "backend/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/backend/api_token", "admin/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/admin/api_token" }
  }

  assert {
    condition     = alltrue([for e in jsondecode(aws_ecs_task_definition.tts.container_definitions)[0].environment : !contains(["WEIGHTS_S3_URI", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"], e.name)])
    error_message = "sdk model source must not set WEIGHTS_S3_URI or forced offline flags"
  }
}

# ── Dormant S3-bootstrap path: keeps S3 URI + air-gapped flags ──────────────
run "s3_bootstrap_dormant_path_keeps_s3_and_offline" {
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
    tts_model_source    = "s3_bootstrap"
    weights_s3_uri      = "s3://bucket/weights"
    secrets_arns        = { "backend/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/backend/api_token", "admin/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/admin/api_token" }
  }

  assert {
    condition     = lookup({ for e in jsondecode(aws_ecs_task_definition.tts.container_definitions)[0].environment : e.name => e.value }, "WEIGHTS_S3_URI", "") == "s3://bucket/weights/tts/"
    error_message = "s3_bootstrap model source must set WEIGHTS_S3_URI to the tts/ object prefix"
  }
  assert {
    condition     = lookup({ for e in jsondecode(aws_ecs_task_definition.tts.container_definitions)[0].environment : e.name => e.value }, "HF_HUB_OFFLINE", "") == "1"
    error_message = "s3_bootstrap model source must force HF_HUB_OFFLINE=1"
  }
  assert {
    condition     = lookup({ for e in jsondecode(aws_ecs_task_definition.tts.container_definitions)[0].environment : e.name => e.value }, "TRANSFORMERS_OFFLINE", "") == "1"
    error_message = "s3_bootstrap model source must force TRANSFORMERS_OFFLINE=1"
  }
}
