locals {
  common_tags = merge(
    {
      Project = var.project
      Env     = var.env
      Module  = "database"
    },
    var.tags,
  )

  name_prefix = "${var.project}-${var.env}"

  # TLS Redis URI (rediss://). When AUTH is set the token is embedded here —
  # this value is never rendered as a plaintext task-definition environment
  # value; it is delivered to the app only via the redis/url SSM SecureString
  # (secrets valueFrom). When unauthenticated it stays a host:port URI that
  # dev/staging may pass through plain ECS environment.
  redis_uri = var.create_redis ? (
    var.redis_auth_token != ""
    ? "rediss://${var.redis_auth_token}@${aws_elasticache_replication_group.redis[0].primary_endpoint_address}:${aws_elasticache_replication_group.redis[0].port}"
    : "rediss://${aws_elasticache_replication_group.redis[0].primary_endpoint_address}:${aws_elasticache_replication_group.redis[0].port}"
  ) : ""
}

# ---------------------------------------------------------------------------
# RDS Postgres 16 — single-AZ, gp3, no public IP
# ---------------------------------------------------------------------------

resource "aws_db_subnet_group" "this" {
  count = var.create_rds ? 1 : 0

  name       = "${local.name_prefix}-db"
  subnet_ids = var.subnet_ids

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-db-subnets"
  })
}

resource "aws_db_parameter_group" "postgres16" {
  count = var.create_rds ? 1 : 0

  name   = "${local.name_prefix}-pg16"
  family = "postgres16"

  # Server-side TLS policy (R7.4): rds.force_ssl=1 makes the engine reject
  # non-SSL connections even if the app-side contract is ever missed.
  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-pg16"
  })
}

resource "aws_db_instance" "postgres" {
  count = var.create_rds ? 1 : 0

  identifier = "${local.name_prefix}-postgres"

  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class
  db_name        = var.db_name
  username       = var.db_username
  password       = var.db_password
  port           = 5432

  allocated_storage     = var.allocated_storage_gb
  max_allocated_storage = var.max_allocated_storage_gb > 0 ? var.max_allocated_storage_gb : null
  storage_type          = "gp3"
  storage_encrypted     = var.storage_encrypted

  db_subnet_group_name   = aws_db_subnet_group.this[0].name
  vpc_security_group_ids = [var.rds_sg_id]
  publicly_accessible    = var.publicly_accessible
  multi_az               = var.multi_az

  parameter_group_name = aws_db_parameter_group.postgres16[0].name

  backup_retention_period   = var.backup_retention_days
  deletion_protection       = var.deletion_protection
  skip_final_snapshot       = var.skip_final_snapshot
  final_snapshot_identifier = var.skip_final_snapshot ? null : "${local.name_prefix}-postgres-final"

  # No public admin surface; ops via ECS Exec / SSM only.
  apply_immediately     = var.env != "prod"
  copy_tags_to_snapshot = true

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-postgres"
  })

  lifecycle {
    ignore_changes = [password]
  }
}

# ---------------------------------------------------------------------------
# ElastiCache Redis 7 — single node MVP
# ---------------------------------------------------------------------------

resource "aws_elasticache_subnet_group" "this" {
  count = var.create_redis ? 1 : 0

  name       = "${local.name_prefix}-redis"
  subnet_ids = var.subnet_ids

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-redis-subnets"
  })
}

resource "aws_elasticache_parameter_group" "redis7" {
  count = var.create_redis ? 1 : 0

  name   = "${local.name_prefix}-redis7"
  family = "redis7"

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-redis7"
  })
}

resource "aws_elasticache_replication_group" "redis" {
  count = var.create_redis ? 1 : 0

  replication_group_id = "${local.name_prefix}-redis"
  description          = "Redis 7 managed cache (TLS+auth)"
  engine               = "redis"
  engine_version       = var.redis_engine_version
  node_type            = var.redis_node_type
  port                 = var.redis_port
  parameter_group_name = aws_elasticache_parameter_group.redis7[0].name
  subnet_group_name    = aws_elasticache_subnet_group.this[0].name
  security_group_ids   = [var.redis_sg_id]

  # Single-node MVP unchanged: no sharding, no replicas, no failover, no snapshots.
  num_node_groups            = 1
  replicas_per_node_group    = 0
  automatic_failover_enabled = false
  snapshot_retention_limit   = 0

  # Managed-Redis production contract (R7.1): transit + at-rest encryption and
  # optional AUTH are only available on a replication group, not a cluster.
  transit_encryption_enabled = true
  at_rest_encryption_enabled = true
  auth_token                 = var.redis_auth_token != "" ? var.redis_auth_token : null

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-redis"
  })
}

# Fail plan/apply when managed production Redis would silently run without
# AUTH. R7.1 requires auth for production; dev/staging keep their unauth mode
# by leaving require_redis_auth=false.
resource "terraform_data" "redis_auth_required" {
  input = format("%s|%s", var.create_redis, var.require_redis_auth)
  lifecycle {
    precondition {
      condition     = !var.create_redis || !var.require_redis_auth || var.redis_auth_token != ""
      error_message = <<-EOT
        Managed Redis with require_redis_auth=true must set a non-empty
        redis_auth_token. Refusing to run production Redis unauthenticated
        (R7.1). Pass the token via TF_VAR_redis_auth_token / tfvars.local.
      EOT
    }
  }
}

# Secure delivery of the credential-bearing URI: the computed rediss:// URI
# (which embeds the AUTH token) is stored as an SSM SecureString so the ECS
# task can reference it via `secrets = [{ name = "REDIS_URL", valueFrom = ... }]`
# instead of a plaintext task-definition environment value. Only provisioned
# when AUTH is actually set.
resource "aws_ssm_parameter" "redis_uri" {
  count = var.create_redis && var.redis_auth_token != "" ? 1 : 0

  name        = "/${var.env}/redis/url"
  description = "Credential-bearing Redis URI (rediss://) for ECS secrets injection; never a plaintext task-definition env value."
  type        = "SecureString"
  value       = local.redis_uri

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-redis-url"
  })
}
