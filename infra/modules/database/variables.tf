variable "create_rds" {
  description = "Create RDS Postgres instance (false = memory sessions, no DB cost)"
  type        = bool
  default     = true
}

variable "create_redis" {
  description = "Create ElastiCache Redis cluster (false = no cache cost)"
  type        = bool
  default     = true
}

variable "env" {
  description = "Environment name (dev|prod)"
  type        = string
}

variable "project" {
  description = "Project tag value"
  type        = string
  default     = "ai-livestream"
}

variable "subnet_ids" {
  description = "Subnet IDs for DB/Redis subnet groups (public subnets in MVP). RDS needs >=2 AZs."
  type        = list(string)
}

variable "rds_sg_id" {
  description = "Security group ID for RDS (from security module)"
  type        = string
}

variable "redis_sg_id" {
  description = "Security group ID for ElastiCache Redis (from security module)"
  type        = string
}

variable "db_name" {
  description = "Initial Postgres database name"
  type        = string
  default     = "ailive"
}

variable "db_username" {
  description = "Master username"
  type        = string
  default     = "ailive"
}

variable "db_password" {
  description = "Master password. Pass via TF_VAR_db_password or *.tfvars.local — never commit."
  type        = string
  sensitive   = true
}

variable "instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.t4g.medium"
}

variable "engine_version" {
  description = "Postgres major/minor version"
  type        = string
  default     = "16"
}

variable "allocated_storage_gb" {
  description = "gp3 allocated storage (GB)"
  type        = number
  default     = 100
}

variable "max_allocated_storage_gb" {
  description = "Autoscaling storage ceiling (0 = disabled)"
  type        = number
  default     = 0
}

variable "storage_encrypted" {
  description = "Encrypt RDS storage"
  type        = bool
  default     = true
}

variable "publicly_accessible" {
  description = "IRON: keep false even when subnet is public"
  type        = bool
  default     = false
}

variable "multi_az" {
  description = "Multi-AZ RDS (rejected for MVP cost)"
  type        = bool
  default     = false
}

variable "backup_retention_days" {
  description = "Automated backup retention"
  type        = number
  default     = 7
}

variable "skip_final_snapshot" {
  description = "Skip final snapshot on destroy (dev true, prod false)"
  type        = bool
  default     = true
}

variable "deletion_protection" {
  description = "Protect RDS from accidental destroy"
  type        = bool
  default     = false
}

variable "redis_node_type" {
  description = "ElastiCache node type"
  type        = string
  default     = "cache.t4g.small"
}

variable "redis_engine_version" {
  description = "Redis engine version"
  type        = string
  default     = "7.1"
}

variable "redis_port" {
  description = "Redis port"
  type        = number
  default     = 6379
}

variable "redis_auth_token" {
  description = "Managed Redis AUTH secret (empty = no auth). Prefer SSM out-of-band. Passed into the replication group and embedded in redis_uri."
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition     = var.redis_auth_token == "" || (length(var.redis_auth_token) >= 16 && !strcontains(var.redis_auth_token, " "))
    error_message = "redis_auth_token must be empty or at least 16 chars with no spaces."
  }
}

variable "tags" {
  description = "Extra tags merged onto all resources"
  type        = map(string)
  default     = {}
}
