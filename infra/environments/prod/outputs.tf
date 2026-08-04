output "vpc_id" {
  value = module.network.vpc_id
}

output "public_subnet_ids" {
  value = local.public_subnet_ids
}

output "alb_dns_name" {
  description = "Cloudflare CNAME target"
  value       = module.loadbalancer.alb_dns_name
}

output "backend_target_group_arn" {
  value = module.loadbalancer.backend_target_group_arn
}

output "rds_endpoint" {
  value = module.database.rds_endpoint
}

output "redis_connection_string" {
  value = module.database.redis_connection_string
}

output "ecs_cluster_name" {
  value = module.compute.cluster_name
}

output "ecs_service_names" {
  value = module.compute.service_names
}

output "desired_optional_services" {
  description = "Effective desired counts: EC2-gated services are 0 when capacity is absent."
  value = {
    llm     = var.create_ec2_capacity ? var.desired_llm : 0
    tts     = var.create_ec2_capacity ? var.desired_tts : 0
    avatar  = var.create_ec2_capacity ? var.desired_avatar : 0
    livekit = var.desired_livekit
    lmcache = var.create_ec2_capacity && var.lmcache_enabled ? var.desired_lmcache : 0
  }
}

output "alb_url_scheme" {
  value = "https"
}

output "weights_uri" {
  value = module.storage.weights_uri
}

output "ssm_parameter_arns" {
  value = module.secrets.parameter_arns
}

output "sns_topic_arn" {
  value = module.monitoring.sns_topic_arn
}
