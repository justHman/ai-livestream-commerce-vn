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

  env               = var.env
  project           = var.project
  vpc_id            = module.network.vpc_id
  alb_ingress_cidrs = var.alb_ingress_cidrs
  enable_oidc       = false
  tags              = var.tags
}

module "storage" {
  source = "../../modules/storage"

  env               = var.env
  project           = var.project
  force_destroy     = false
  enable_versioning = true
  tags              = var.tags
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
  rds_sg_id            = module.security.sg_rds_id
  redis_sg_id          = module.security.sg_redis_id
  db_password          = var.db_password
  publicly_accessible  = false
  instance_class       = var.db_instance_class
  allocated_storage_gb = var.db_allocated_storage_gb
  multi_az             = false
  redis_node_type      = var.redis_node_type
  skip_final_snapshot  = false
  deletion_protection  = true
  tags                 = var.tags
}

module "loadbalancer" {
  source = "../../modules/loadbalancer"

  env                        = var.env
  project                    = var.project
  vpc_id                     = module.network.vpc_id
  subnet_ids                 = local.public_subnet_ids
  sg_alb_id                  = module.security.sg_alb_id
  certificate_arn            = var.certificate_arn
  enable_deletion_protection = true
  tags                       = var.tags
}

module "compute" {
  source = "../../modules/compute"

  env                      = var.env
  project                  = var.project
  subnet_ids               = module.network.public_subnet_ids
  sg_map                   = module.security.sg_map
  image_backend            = var.image_backend
  image_llm_tts            = var.image_llm_tts
  image_avatar             = var.image_avatar
  image_lmcache            = var.image_lmcache
  lmcache_enabled          = var.lmcache_enabled
  desired_backend          = var.desired_backend
  desired_llm_tts          = var.desired_llm_tts
  desired_avatar           = var.desired_avatar
  desired_livekit          = var.desired_livekit
  desired_lmcache          = var.desired_lmcache
  weights_s3_uri           = module.storage.weights_uri
  secrets_arns             = module.secrets.parameter_arns
  backend_target_group_arn = module.loadbalancer.backend_target_group_arn
  assign_public_ip         = true
  create_ec2_capacity      = var.create_ec2_capacity
  log_group_prefix         = "/ecs/${var.project}-${var.env}"
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
