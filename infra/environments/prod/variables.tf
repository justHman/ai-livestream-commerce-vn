variable "env" {
  description = "Environment name"
  type        = string
  default     = "prod"
}

variable "cors_origins" {
  description = "Comma-separated allowed browser origins"
  type        = string
  default     = ""

  validation {
    condition     = !strcontains(var.cors_origins, "*")
    error_message = "cors_origins must not contain wildcard origins in prod."
  }
}

variable "debug_enabled" {
  description = "Enable verbose backend logging"
  type        = bool
  default     = false
}

variable "session_store" {
  description = "Session store backend: memory or redis"
  type        = string
  default     = "redis"

  validation {
    condition     = var.session_store == "redis"
    error_message = "prod session_store must be 'redis' (multi-replica production requires shared durable state; memory is dev-only)."
  }
}

variable "redis_url" {
  description = "Redis URL for SESSION_STORE=redis; empty derives from ElastiCache"
  type        = string
  default     = ""
}

variable "redis_auth_token" {
  description = "Managed Redis AUTH secret passed to the database module (empty = no auth)."
  type        = string
  sensitive   = true
  default     = ""
}

variable "app_env" {
  description = "APP_ENV runtime flag; empty falls back to env"
  type        = string
  default     = ""
}

variable "spot_capacity_percentage" {
  description = "Percentage of EC2 capacity supplied by Spot"
  type        = number
  default     = 100

  validation {
    condition     = var.spot_capacity_percentage >= 0 && var.spot_capacity_percentage <= 100 && floor(var.spot_capacity_percentage) == var.spot_capacity_percentage
    error_message = "spot_capacity_percentage must be an integer from 0 to 100."
  }
}

variable "project" {
  description = "Project tag / name prefix"
  type        = string
  default     = "ai-livestream"
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "ap-northeast-2"
}

variable "vpc_cidr" {
  description = "VPC CIDR"
  type        = string
  default     = "10.30.0.0/16"
}

variable "public_subnet_cidr" {
  description = "Primary public subnet CIDR (compute pin AZ)"
  type        = string
  default     = "10.30.1.0/24"
}

variable "public_subnet_cidr_b" {
  description = "Second public subnet CIDR (ALB/RDS multi-AZ span; still public, no NAT)"
  type        = string
  default     = "10.30.2.0/24"
}

variable "az" {
  description = "Primary AZ"
  type        = string
  default     = "ap-northeast-2a"
}

variable "az_b" {
  description = "Second AZ for ALB/RDS subnet groups"
  type        = string
  default     = "ap-northeast-2b"
}

variable "extra_public_subnet_ids" {
  description = "Optional additional public subnet IDs beyond network module"
  type        = list(string)
  default     = []
}

variable "alb_ingress_cidrs" {
  description = "CIDRs allowed to ALB:443 — set Cloudflare IP ranges in prod"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "certificate_arn" {
  description = "ACM cert ARN for ALB HTTPS (required for prod). Set via TF_VAR_certificate_arn."
  type        = string

  validation {
    condition     = can(regex("^arn:aws:acm:ap-northeast-2:[0-9]{12}:certificate/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.certificate_arn))
    error_message = "certificate_arn must be a valid ACM certificate ARN in ap-northeast-2."
  }
}

variable "db_password" {
  description = "RDS master password via TF_VAR / tfvars.local — never commit"
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.db_password) >= 8 && length(var.db_password) <= 128 && var.db_password != "CHANGE_ME"
    error_message = "db_password must be 8-128 chars and not a placeholder."
  }
}

variable "enable_database_url" {
  description = "Explicitly enable DATABASE_URL injection from an existing SSM SecureString parameter."
  type        = bool
}

variable "database_url_parameter_arn" {
  description = "Existing SSM SecureString ARN containing DATABASE_URL; provide the ARN only."
  type        = string
  default     = ""

  validation {
    condition = (trimspace(var.database_url_parameter_arn) == "" && !var.enable_database_url) || can(regex(
      "^arn:[a-z0-9-]+:ssm:[a-z0-9-]+:[0-9]{12}:parameter/[A-Za-z0-9_.+=,@-]+(/[A-Za-z0-9_.+=,@-]+)*$",
      trimspace(var.database_url_parameter_arn),
    ))
    error_message = "database_url_parameter_arn must be empty or an existing SSM Parameter Store ARN."
  }
}

variable "create_rds" {
  description = "Create RDS Postgres (false = memory sessions, no DB cost)"
  type        = bool
  default     = true
}

variable "create_redis" {
  description = "Create ElastiCache Redis (false = no cache cost)"
  type        = bool
  default     = true
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "db_allocated_storage_gb" {
  type    = number
  default = 100
}

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.small"
}

variable "lmcache_enabled" {
  type    = bool
  default = false
}

variable "backend_capacity_provider" {
  description = "Backend capacity provider: FARGATE only in prod — no Spot for production backend."
  type        = string
  default     = "FARGATE"

  validation {
    condition     = var.backend_capacity_provider == "FARGATE"
    error_message = "prod backend must use FARGATE (on-demand); Spot is not allowed in production."
  }
}

variable "desired_backend" {
  type    = number
  default = 2

  validation {
    condition     = var.desired_backend >= 2 && floor(var.desired_backend) == var.desired_backend
    error_message = "prod desired_backend must be at least 2 (multi-replica production minimum)."
  }
}

variable "desired_llm" {
  type    = number
  default = 0

  validation {
    condition     = var.desired_llm >= 0 && floor(var.desired_llm) == var.desired_llm
    error_message = "desired_llm must be a nonnegative integer."
  }
}

variable "desired_tts" {
  type    = number
  default = 0

  validation {
    condition     = var.desired_tts >= 0 && floor(var.desired_tts) == var.desired_tts
    error_message = "desired_tts must be a nonnegative integer."
  }
}

variable "desired_avatar" {
  type    = number
  default = 0

  validation {
    condition     = var.desired_avatar >= 0 && floor(var.desired_avatar) == var.desired_avatar
    error_message = "desired_avatar must be a nonnegative integer."
  }
}

variable "image_backend" {
  type = string

  validation {
    condition     = can(regex("^[a-z0-9./_-]+@sha256:[a-f0-9]{64}$", var.image_backend))
    error_message = "prod image_backend must be an immutable digest (repo@sha256:64hex); mutable tags are not a valid production release identity."
  }
}

variable "image_llm" {
  type = string

  validation {
    condition     = can(regex("^[a-z0-9./_-]+@sha256:[a-f0-9]{64}$", var.image_llm))
    error_message = "prod image_llm must be an immutable digest (repo@sha256:64hex); mutable tags are not a valid production release identity."
  }
}

variable "image_tts" {
  type = string

  validation {
    condition     = can(regex("^[a-z0-9./_-]+@sha256:[a-f0-9]{64}$", var.image_tts))
    error_message = "prod image_tts must be an immutable digest (repo@sha256:64hex); mutable tags are not a valid production release identity."
  }
}

variable "image_lmcache" {
  description = "LMCache sidecar image URI (colocated in LLM task; evidence-gated)"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9./_-]+@sha256:[a-f0-9]{64}$", var.image_lmcache))
    error_message = "prod image_lmcache must be an immutable digest (repo@sha256:64hex); mutable tags are not a valid production release identity."
  }
}

variable "image_avatar" {
  type = string

  validation {
    condition     = can(regex("^[a-z0-9./_-]+@sha256:[a-f0-9]{64}$", var.image_avatar))
    error_message = "prod image_avatar must be an immutable digest (repo@sha256:64hex); mutable tags are not a valid production release identity."
  }
}

variable "livekit_url" {
  description = "LiveKit Cloud WebSocket URL (wss://...). Empty = LiveKit disabled."
  type        = string
  default     = ""
}

variable "avatar_adapter" {
  description = "Backend avatar adapter: self_hosted|liveavatar|baidu_xiling"
  type        = string
  default     = "liveavatar"

  validation {
    condition     = contains(["self_hosted", "liveavatar", "baidu_xiling"], var.avatar_adapter)
    error_message = "avatar_adapter must be one of: self_hosted, liveavatar, baidu_xiling."
  }
}

variable "llm_adapter" {
  description = "Backend LLM adapter (always openai_compatible)"
  type        = string
  default     = "openai_compatible"

  validation {
    condition     = var.llm_adapter == "openai_compatible"
    error_message = "llm_adapter must be openai_compatible."
  }
}

variable "llm_base_url" {
  type    = string
  default = ""
}

variable "tts_adapter" {
  description = "Backend TTS adapter: self_hosted|elevenlabs|openai_speech"
  type        = string
  default     = "self_hosted"

  validation {
    condition     = contains(["self_hosted", "elevenlabs", "openai_speech"], var.tts_adapter)
    error_message = "tts_adapter must be one of: self_hosted, elevenlabs, openai_speech."
  }
}

variable "tts_base_url" {
  type    = string
  default = ""
}

variable "tts_voice_store_uri" {
  description = "Durable provider-neutral voice-store URI for self-host TTS (e.g. s3://<bucket>/voice-profiles). Required when tts_adapter=self_hosted (enforced by the tts_voice_store_durability precondition) so voice profiles never land on task-local file://."
  type        = string
  default     = ""
}

variable "alert_email" {
  type    = string
  default = ""
}

variable "enable_billing_alarms" {
  type    = bool
  default = false
}

variable "create_ec2_capacity" {
  type    = bool
  default = false
}

variable "tags" {
  type    = map(string)
  default = {}
}
