output "rds_instance_id" {
  description = "RDS instance identifier"
  value       = var.create_rds ? aws_db_instance.postgres[0].id : ""
}

output "rds_endpoint" {
  description = "RDS hostname:port"
  value       = var.create_rds ? aws_db_instance.postgres[0].endpoint : ""
}

output "rds_address" {
  description = "RDS hostname only"
  value       = var.create_rds ? aws_db_instance.postgres[0].address : ""
}

output "rds_port" {
  description = "RDS port"
  value       = var.create_rds ? aws_db_instance.postgres[0].port : ""
}

output "rds_db_name" {
  description = "Initial database name"
  value       = var.create_rds ? aws_db_instance.postgres[0].db_name : ""
}

output "rds_username" {
  description = "Master username"
  value       = var.create_rds ? aws_db_instance.postgres[0].username : ""
  sensitive   = true
}

output "rds_resource_id" {
  description = "RDS resource ID (for IAM auth / monitoring)"
  value       = var.create_rds ? aws_db_instance.postgres[0].resource_id : ""
}

output "db_subnet_group_name" {
  description = "DB subnet group name"
  value       = var.create_rds ? aws_db_subnet_group.this[0].name : ""
}

output "redis_cluster_id" {
  description = "ElastiCache replication group ID"
  value       = var.create_redis ? aws_elasticache_replication_group.redis[0].id : ""
}

output "redis_endpoint" {
  description = "Redis primary endpoint address"
  value       = var.create_redis ? aws_elasticache_replication_group.redis[0].primary_endpoint_address : ""
}

output "redis_port" {
  description = "Redis port"
  value       = var.create_redis ? aws_elasticache_replication_group.redis[0].port : ""
}

output "redis_uri" {
  description = "TLS Redis URI (rediss://) for app config; embeds the AUTH token when set. Sensitive — deliver via redis_uri_parameter_arn, never a plaintext env value."
  value       = local.redis_uri
  sensitive   = true
}

output "redis_uri_parameter_arn" {
  description = "SSM SecureString ARN holding the credential-bearing Redis URI (empty when Redis disabled or unauthenticated)"
  value       = var.create_redis && var.redis_auth_token != "" ? aws_ssm_parameter.redis_uri[0].arn : ""
  # The ARN is transitively sensitive (the parameter's value embeds the AUTH
  # token); Terraform requires explicit sensitive on outputs that reference it.
  sensitive = true
}
