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
  description = "ElastiCache cluster ID"
  value       = var.create_redis ? aws_elasticache_cluster.redis[0].id : ""
}

output "redis_endpoint" {
  description = "Redis primary endpoint address"
  value       = var.create_redis ? aws_elasticache_cluster.redis[0].cache_nodes[0].address : ""
}

output "redis_port" {
  description = "Redis port"
  value       = var.create_redis ? aws_elasticache_cluster.redis[0].port : ""
}

output "redis_connection_string" {
  description = "host:port for app config"
  value       = var.create_redis ? "${aws_elasticache_cluster.redis[0].cache_nodes[0].address}:${aws_elasticache_cluster.redis[0].port}" : ""
}
