# Terraform native tests: B6 production managed Redis cannot silently run
# unauthenticated (R7.1). The database module refuses to plan create_redis=true
# + require_redis_auth=true with an empty token, and provisions the credential-
# bearing URI as an SSM SecureString (redis/url) so ECS can reference it via
# secrets valueFrom. Plan-only, no cloud credentials, deterministic.

# ── prod managed Redis without AUTH fails loudly ─────────────────────────────
run "prod_managed_redis_requires_auth" {
  command = plan

  module {
    source = "../modules/database"
  }

  variables {
    env                = "prod"
    subnet_ids         = ["subnet-a", "subnet-b"]
    rds_sg_id          = "sg-rds"
    redis_sg_id        = "sg-redis"
    db_password        = "dummy-password"
    create_rds         = false
    create_redis       = true
    require_redis_auth = true
    redis_auth_token   = ""
  }

  expect_failures = [
    terraform_data.redis_auth_required,
  ]
}

# ── prod managed Redis with AUTH is accepted and provisions the SecureString ─
run "prod_managed_redis_with_auth_provisions_securestring" {
  command = plan

  module {
    source = "../modules/database"
  }

  variables {
    env                = "prod"
    subnet_ids         = ["subnet-a", "subnet-b"]
    rds_sg_id          = "sg-rds"
    redis_sg_id        = "sg-redis"
    db_password        = "dummy-password"
    create_rds         = false
    create_redis       = true
    require_redis_auth = true
    redis_auth_token   = "correct-horse-battery-staple"
  }

  assert {
    condition     = length(aws_ssm_parameter.redis_uri) == 1
    error_message = "authenticated managed Redis must provision the redis/url SSM parameter"
  }
  assert {
    condition     = aws_ssm_parameter.redis_uri[0].name == "/prod/redis/url"
    error_message = "redis/url parameter must use the /<env>/redis/url name"
  }
  assert {
    condition     = aws_ssm_parameter.redis_uri[0].type == "SecureString"
    error_message = "redis/url parameter must be a SecureString"
  }
  assert {
    condition     = aws_ssm_parameter.redis_uri[0].description != ""
    error_message = "redis/url parameter must carry an explanatory description"
  }
}

# ── dev/staging unauth managed Redis keeps its existing mode (no regression) ─
run "dev_unauthenticated_redis_still_allowed" {
  command = plan

  module {
    source = "../modules/database"
  }

  variables {
    env                = "dev"
    subnet_ids         = ["subnet-a", "subnet-b"]
    rds_sg_id          = "sg-rds"
    redis_sg_id        = "sg-redis"
    db_password        = "dummy-password"
    create_rds         = false
    create_redis       = true
    require_redis_auth = false
    redis_auth_token   = ""
  }

  assert {
    condition     = length(aws_ssm_parameter.redis_uri) == 0
    error_message = "unauthenticated Redis must not provision a redis/url SecureString"
  }
  assert {
    condition     = aws_elasticache_replication_group.redis[0].auth_token == null
    error_message = "unauthenticated dev Redis must leave AUTH unset (existing mode)"
  }
}
