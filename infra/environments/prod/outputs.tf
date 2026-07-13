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

output "weights_uri" {
  value = module.storage.weights_uri
}

output "ssm_parameter_arns" {
  value = module.secrets.parameter_arns
}

output "sns_topic_arn" {
  value = module.monitoring.sns_topic_arn
}
