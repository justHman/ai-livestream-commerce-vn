# Terraform native tests: ECS secret-name parity with backend client contract.
# The backend-owned outbound clients read LLM_AUTH_TOKEN / TTS_AUTH_TOKEN
# (application/clients/llm/openai_compatible.py, tts/self_hosted.py), so the
# backend task definition must inject secrets under those canonical names —
# never LLM_API_KEY / TTS_API_KEY, which the clients never read (R2.3).
# Plan-only, no cloud credentials, deterministic.

run "backend_injects_canonical_auth_token_secrets" {
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
    secrets_arns = {
      "backend/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/backend/api_token"
      "admin/api_token"   = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/admin/api_token"
      "llm/api_key"       = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/llm/api_key"
      "tts/api_key"       = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/tts/api_key"
    }
  }

  assert {
    condition = contains(
      [for s in jsondecode(aws_ecs_task_definition.backend.container_definitions)[0].secrets : s.name],
      "LLM_AUTH_TOKEN"
    )
    error_message = "backend must inject the LLM credential as LLM_AUTH_TOKEN (client contract)"
  }
  assert {
    condition = contains(
      [for s in jsondecode(aws_ecs_task_definition.backend.container_definitions)[0].secrets : s.valueFrom if s.name == "LLM_AUTH_TOKEN"],
      "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/llm/api_key"
    )
    error_message = "LLM_AUTH_TOKEN must resolve the llm/api_key SSM ARN"
  }
  assert {
    condition = contains(
      [for s in jsondecode(aws_ecs_task_definition.backend.container_definitions)[0].secrets : s.name],
      "TTS_AUTH_TOKEN"
    )
    error_message = "backend must inject the TTS credential as TTS_AUTH_TOKEN (client contract)"
  }
  assert {
    condition = contains(
      [for s in jsondecode(aws_ecs_task_definition.backend.container_definitions)[0].secrets : s.valueFrom if s.name == "TTS_AUTH_TOKEN"],
      "arn:aws:ssm:ap-northeast-2:111111111111:parameter/test/tts/api_key"
    )
    error_message = "TTS_AUTH_TOKEN must resolve the tts/api_key SSM ARN"
  }
  assert {
    condition = !contains(
      [for s in jsondecode(aws_ecs_task_definition.backend.container_definitions)[0].secrets : s.name],
      "LLM_API_KEY"
    )
    error_message = "backend must not inject LLM_API_KEY (clients read LLM_AUTH_TOKEN)"
  }
  assert {
    condition = !contains(
      [for s in jsondecode(aws_ecs_task_definition.backend.container_definitions)[0].secrets : s.name],
      "TTS_API_KEY"
    )
    error_message = "backend must not inject TTS_API_KEY (clients read TTS_AUTH_TOKEN)"
  }
}
