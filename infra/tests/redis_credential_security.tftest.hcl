# Terraform native tests: B6 Redis credential security at the compute module.
# A credential-bearing REDIS_URL must be delivered via the redis/url SSM
# SecureString secret (secrets valueFrom) at the AWS edge — never a plaintext
# task-definition environment value. Unauthenticated dev/staging Redis may keep
# the plaintext env delivery (no credential in the URI). Plan-only, no cloud
# credentials, deterministic. Mirrors runtime_matrix.tftest.hcl (fake digest
# images + dummy subnet_ids/sg_map).

# ── credential-bearing URI delivered as a secret, not environment ────────────
run "credential_redis_url_is_secret_not_environment" {
  command = plan

  module {
    source = "../modules/compute"
  }

  variables {
    env                 = "prod"
    subnet_ids          = ["subnet-a", "subnet-b"]
    sg_map              = { llm = "sg-llm", tts = "sg-tts", avatar = "sg-av", backend = "sg-be" }
    image_backend       = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_llm           = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_tts           = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_avatar        = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_lmcache       = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    create_ec2_capacity = false
    desired_backend     = 1
    session_store       = "redis"
    redis_url           = "" # prod: delivered via the redis/url SSM secret
    secrets_arns = {
      "backend/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/prod/backend/api_token"
      "admin/api_token"   = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/prod/admin/api_token"
      "redis/url"         = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/prod/redis/url"
    }
  }

  assert {
    condition = !contains(
      [for e in jsondecode(aws_ecs_task_definition.backend.container_definitions)[0].environment : e.name],
      "REDIS_URL"
    )
    error_message = "when the redis/url secret is wired, REDIS_URL must not be a plaintext environment value"
  }
  assert {
    condition = contains(
      [for s in jsondecode(aws_ecs_task_definition.backend.container_definitions)[0].secrets : s.valueFrom if s.name == "REDIS_URL"],
      "arn:aws:ssm:ap-northeast-2:111111111111:parameter/prod/redis/url"
    )
    error_message = "credential-bearing REDIS_URL must be injected via the redis/url SSM secret (valueFrom) at the AWS edge"
  }
}

# ── unauthenticated dev/staging Redis may remain a plaintext env value ───────
run "unauthenticated_redis_url_plain_env_ok" {
  command = plan

  module {
    source = "../modules/compute"
  }

  variables {
    env                 = "dev"
    subnet_ids          = ["subnet-a", "subnet-b"]
    sg_map              = { llm = "sg-llm", tts = "sg-tts", avatar = "sg-av", backend = "sg-be" }
    image_backend       = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_llm           = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_tts           = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_avatar        = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_lmcache       = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    create_ec2_capacity = false
    desired_backend     = 1
    session_store       = "redis"
    redis_url           = "rediss://cache.example.com:6379"
    secrets_arns = {
      "backend/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/dev/backend/api_token"
      "admin/api_token"   = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/dev/admin/api_token"
    }
  }

  assert {
    condition = contains(
      [for e in jsondecode(aws_ecs_task_definition.backend.container_definitions)[0].environment : e.name],
      "REDIS_URL"
    )
    error_message = "unauthenticated Redis URL may remain a plaintext environment value (dev/staging mode)"
  }
  assert {
    condition = !contains(
      [for s in jsondecode(aws_ecs_task_definition.backend.container_definitions)[0].secrets : s.name],
      "REDIS_URL"
    )
    error_message = "no REDIS_URL secret when the URI carries no credential"
  }
}

# ── credential-bearing URI WITHOUT the secret fails loudly ───────────────────
run "credential_redis_url_without_secret_fails" {
  command = plan

  module {
    source = "../modules/compute"
  }

  variables {
    env                 = "dev"
    subnet_ids          = ["subnet-a", "subnet-b"]
    sg_map              = { llm = "sg-llm", tts = "sg-tts", avatar = "sg-av", backend = "sg-be" }
    image_backend       = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_llm           = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_tts           = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_avatar        = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_lmcache       = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    create_ec2_capacity = false
    desired_backend     = 1
    session_store       = "redis"
    redis_url           = "rediss://supersecrettoken@cache.example.com:6379"
    secrets_arns = {
      "backend/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/dev/backend/api_token"
      "admin/api_token"   = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/dev/admin/api_token"
    }
  }

  expect_failures = [
    terraform_data.redis_credential_security,
  ]
}

# ── credential-bearing URI alongside the secret also fails (double render) ───
run "credential_redis_url_with_secret_fails" {
  command = plan

  module {
    source = "../modules/compute"
  }

  variables {
    env                 = "prod"
    subnet_ids          = ["subnet-a", "subnet-b"]
    sg_map              = { llm = "sg-llm", tts = "sg-tts", avatar = "sg-av", backend = "sg-be" }
    image_backend       = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_llm           = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_tts           = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_avatar        = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    image_lmcache       = "imjusthman/ai-live-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    create_ec2_capacity = false
    desired_backend     = 1
    session_store       = "redis"
    redis_url           = "rediss://supersecrettoken@cache.example.com:6379"
    secrets_arns = {
      "backend/api_token" = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/prod/backend/api_token"
      "admin/api_token"   = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/prod/admin/api_token"
      "redis/url"         = "arn:aws:ssm:ap-northeast-2:111111111111:parameter/prod/redis/url"
    }
  }

  expect_failures = [
    terraform_data.redis_credential_security,
  ]
}
