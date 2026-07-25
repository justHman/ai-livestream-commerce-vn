# Root module wiring — docs/terraform-layout.md §3
# Order: network → security → storage → secrets → database → loadbalancer → compute → monitoring

data "aws_caller_identity" "current" {}

locals {
  # ALB + RDS need >=2 AZ subnets. Merge network output with optional extra subnets.
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

  env               = var.env
  project           = var.project
  vpc_id            = module.network.vpc_id
  alb_ingress_cidrs = var.alb_ingress_cidrs
  # :80 always open in dev: forward when no cert, redirect→443 when cert set.
  alb_http_ingress_cidrs = ["0.0.0.0/0"]
  certificate_arn        = var.certificate_arn
  enable_oidc            = false # OIDC lives in environments/global
  tags                   = var.tags
}

module "storage" {
  source = "../../modules/storage"

  env           = var.env
  project       = var.project
  force_destroy = true # dev only
  tags          = var.tags
}

module "secrets" {
  source = "../../modules/secrets"

  env               = var.env
  project           = var.project
  tags              = var.tags
  backend_api_token = var.backend_api_token
  admin_api_token   = var.admin_api_token
  # other values set out-of-band: aws ssm put-parameter --overwrite
}

module "database" {
  source = "../../modules/database"

  env                  = var.env
  project              = var.project
  subnet_ids           = local.public_subnet_ids
  rds_sg_id            = module.security.sg_rds_id
  redis_sg_id          = module.security.sg_redis_id
  db_password          = var.db_password
  publicly_accessible  = false
  instance_class       = var.db_instance_class
  allocated_storage_gb = var.db_allocated_storage_gb
  multi_az             = false
  redis_node_type      = var.redis_node_type
  # Iron rule (no backup pile-up): DEV keeps no multi-day RDS backup pile.
  # backup_retention_days=0 disables automated backups; skip_final_snapshot=true
  # leaves no final snapshot on destroy; deletion_protection=false lets teardown
  # proceed. No manual RDS snapshots for DEV stages.
  backup_retention_days = 0
  skip_final_snapshot   = true
  deletion_protection   = false
  tags                  = var.tags
}

module "loadbalancer" {
  source = "../../modules/loadbalancer"

  env                        = var.env
  project                    = var.project
  vpc_id                     = module.network.vpc_id
  subnet_ids                 = local.public_subnet_ids
  sg_alb_id                  = module.security.sg_alb_id
  certificate_arn            = var.certificate_arn
  enable_http_redirect       = var.certificate_arn != ""
  enable_deletion_protection = false
  tags                       = var.tags
}

module "compute" {
  source = "../../modules/compute"

  env             = var.env
  project         = var.project
  vpc_id          = module.network.vpc_id
  subnet_ids      = module.network.public_subnet_ids
  sg_map          = module.security.sg_map
  image_backend   = var.image_backend
  image_llm       = var.image_llm
  image_tts       = var.image_tts
  image_avatar    = var.image_avatar
  image_lmcache   = var.image_lmcache
  image_livekit   = var.image_livekit
  lmcache_enabled = var.lmcache_enabled
  desired_backend = var.desired_backend
  desired_llm_tts = var.desired_llm_tts
  desired_avatar  = var.desired_avatar
  desired_livekit = var.desired_livekit
  desired_lmcache = var.desired_lmcache
  weights_s3_uri  = module.storage.weights_uri
  secrets_arns = merge(module.secrets.parameter_arns,
    # Stage 2: LiveAvatar cloud API key (backend-only secret, put out-of-band
    # in SSM /dev/liveavatar/api_key). Injected into backend task as
    # LIVEAVATAR_API_KEY when present.
    { "liveavatar/api_key" = "arn:aws:ssm:ap-northeast-2:${data.aws_caller_identity.current.account_id}:parameter/dev/liveavatar/api_key" },
    # Remote OpenAI-compat LLM API key (optional, when llm_engine=openai_compat
    # and base_url is a remote endpoint needing auth). Put in SSM
    # /dev/llm/api_key out-of-band.
    { "llm/api_key" = "arn:aws:ssm:ap-northeast-2:${data.aws_caller_identity.current.account_id}:parameter/dev/llm/api_key" },
    # Stage 2 ship-fast: ElevenLabs remote TTS API key (backend-only secret).
    { "tts/api_key" = "arn:aws:ssm:ap-northeast-2:${data.aws_caller_identity.current.account_id}:parameter/dev/tts/api_key" },
    var.enable_database_url ? {
      "backend/database_url" = var.database_url_parameter_arn
  } : {})
  backend_target_group_arn = module.loadbalancer.backend_target_group_arn
  assign_public_ip         = true
  create_ec2_capacity      = var.create_ec2_capacity
  spot_capacity_percentage = var.spot_capacity_percentage
  log_group_prefix         = "/ecs/${var.project}-${var.env}"
  cors_origins             = var.cors_origins
  debug_enabled            = var.debug_enabled
  session_store            = var.session_store
  redis_url                = var.redis_url != "" ? var.redis_url : "redis://${module.database.redis_connection_string}"
  app_env                  = var.app_env
  render_backend           = var.render_backend
  llm_engine               = var.llm_engine
  llm_base_url             = var.llm_base_url
  llm_model                = var.llm_model
  tts_engine               = var.tts_engine
  tts_base_url             = var.tts_base_url
  tts_voice_id             = var.tts_voice_id
  tags                     = var.tags
}

module "monitoring" {
  source = "../../modules/monitoring"

  env                   = var.env
  project               = var.project
  alert_email           = var.alert_email
  enable_billing_alarms = var.enable_billing_alarms
  tags                  = var.tags
}
