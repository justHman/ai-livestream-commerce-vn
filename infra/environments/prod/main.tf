# Prod root — same wiring as dev; tighter defaults via tfvars.

locals {
  public_subnet_ids = concat(module.network.public_subnet_ids, var.extra_public_subnet_ids)
}

module "network" {
  source = "../../modules/network"

  env                  = var.env
  project              = var.project
  cidr_block           = var.vpc_cidr
  public_subnet_cidr   = var.public_subnet_cidr
  public_subnet_cidr_b = var.public_subnet_cidr_b
  az                   = var.az
  az_b                 = var.az_b
  tags                 = var.tags
}

module "security" {
  source = "../../modules/security"

  env                    = var.env
  project                = var.project
  vpc_id                 = module.network.vpc_id
  alb_ingress_cidrs      = var.alb_ingress_cidrs
  alb_http_ingress_cidrs = []
  certificate_arn        = var.certificate_arn
  enable_oidc            = false
  tags                   = var.tags
}

module "storage" {
  source = "../../modules/storage"

  env               = var.env
  project           = var.project
  force_destroy     = false
  enable_versioning = true
  # Iron rule (no backup pile-up): keep current object only; expire noncurrent
  # versions after 1 day so overwritten uploads do not pile up storage cost.
  lifecycle_noncurrent_days = 1
  tags                      = var.tags
}

module "secrets" {
  source = "../../modules/secrets"

  env     = var.env
  project = var.project
  tags    = var.tags
}

module "database" {
  source = "../../modules/database"

  env                  = var.env
  project              = var.project
  subnet_ids           = local.public_subnet_ids
  create_rds           = var.create_rds
  create_redis         = var.create_redis
  rds_sg_id            = module.security.sg_rds_id
  redis_sg_id          = module.security.sg_redis_id
  db_password          = var.db_password
  publicly_accessible  = false
  instance_class       = var.db_instance_class
  allocated_storage_gb = var.db_allocated_storage_gb
  multi_az             = false
  redis_node_type      = var.redis_node_type
  redis_auth_token     = var.redis_auth_token
  require_redis_auth   = true # B6: managed production Redis must not run unauthenticated (R7.1)
  skip_final_snapshot  = false
  deletion_protection  = true
  tags                 = var.tags
}

# R6.2 (HIGH-A parity): a prod backend that creates RDS but never injects
# DATABASE_URL into the backend container boots with Script Authoring silently
# disabled (HTTP 501) — the exact production failure the fail-fast gate exists
# to prevent. Fail plan/apply instead of silently building the broken config.
# The backend task injects DATABASE_URL only when `backend/database_url` is in
# `secrets_arns` (i.e. enable_database_url=true with a real parameter ARN), so
# create_rds=true MUST imply that wiring.
resource "terraform_data" "db_url_parity" {
  input = var.create_rds
  lifecycle {
    precondition {
      condition = (
        !var.create_rds
        || (var.enable_database_url && trimspace(var.database_url_parameter_arn) != "")
      )
      error_message = <<-EOT
        create_rds=true requires backend DB connectivity in prod: set
        enable_database_url=true and database_url_parameter_arn to the SSM
        SecureString ARN of the Postgres connection string, or set create_rds=false.
        Otherwise the backend container boots without DATABASE_URL and Script
        Authoring is silently disabled (501).
      EOT
    }
  }
}

# B6: prod delivers the credential-bearing REDIS_URL exclusively via the
# redis/url SSM SecureString secret. A directly-set redis_url would be a
# plaintext ECS task-definition value (or a silently-ignored override) — fail
# plan instead.
resource "terraform_data" "redis_url_no_plaintext" {
  input = format("%s|%s", var.create_redis, var.redis_url)
  lifecycle {
    precondition {
      condition     = !var.create_redis || var.redis_url == ""
      error_message = <<-EOT
        prod must not set redis_url directly. The credential-bearing REDIS_URL
        is delivered via the redis/url SSM SecureString secret
        (module.database.redis_uri_parameter_arn -> secrets valueFrom).
        Leave redis_url empty.
      EOT
    }
  }
}

module "loadbalancer" {
  source = "../../modules/loadbalancer"

  env                        = var.env
  project                    = var.project
  vpc_id                     = module.network.vpc_id
  subnet_ids                 = local.public_subnet_ids
  sg_alb_id                  = module.security.sg_alb_id
  certificate_arn            = var.certificate_arn
  enable_http_redirect       = false
  enable_deletion_protection = true
  tags                       = var.tags
}

module "compute" {
  source = "../../modules/compute"

  env                       = var.env
  project                   = var.project
  vpc_id                    = module.network.vpc_id
  subnet_ids                = module.network.public_subnet_ids
  sg_map                    = module.security.sg_map
  image_backend             = var.image_backend
  image_llm                 = var.image_llm
  image_tts                 = var.image_tts
  image_avatar              = var.image_avatar
  image_lmcache             = var.image_lmcache
  lmcache_enabled           = var.lmcache_enabled
  desired_backend           = var.desired_backend
  backend_capacity_provider = var.backend_capacity_provider
  desired_llm               = var.desired_llm
  desired_tts               = var.desired_tts
  desired_avatar            = var.desired_avatar
  weights_s3_uri            = module.storage.weights_uri
  secrets_arns = merge(module.secrets.parameter_arns, var.enable_database_url ? {
    "backend/database_url" = var.database_url_parameter_arn
    } : {},
    # B6: credential-bearing REDIS_URL is delivered via the redis/url SSM
    # SecureString (module.database.redis_uri_parameter_arn) — never a
    # plaintext task-definition environment value.
    var.create_redis && module.database.redis_uri_parameter_arn != "" ? {
      "redis/url" = module.database.redis_uri_parameter_arn
  } : {})
  backend_target_group_arn        = module.loadbalancer.backend_target_group_arn
  assign_public_ip                = true
  create_ec2_capacity             = var.create_ec2_capacity
  spot_capacity_percentage        = var.spot_capacity_percentage
  log_group_prefix                = "/ecs/${var.project}-${var.env}"
  cors_origins                    = var.cors_origins
  debug_enabled                   = var.debug_enabled
  session_store                   = var.session_store
  redis_url                       = "" # delivered via redis/url SSM secret (valueFrom)
  app_env                         = var.app_env
  avatar_adapter                  = var.avatar_adapter
  livekit_url                     = var.livekit_url
  llm_adapter                     = var.llm_adapter
  llm_base_url                    = var.llm_base_url
  tts_adapter                     = var.tts_adapter
  tts_base_url                    = var.tts_base_url
  tts_voice_store_uri             = var.tts_voice_store_uri
  tts_require_durable_voice_store = true # B5: prod self-host TTS needs a durable voice store
  tags                            = var.tags
}

module "monitoring" {
  source = "../../modules/monitoring"

  env                   = var.env
  project               = var.project
  alert_email           = var.alert_email
  enable_billing_alarms = var.enable_billing_alarms
  tags                  = var.tags
}
