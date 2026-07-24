variable "env" {
  description = "Environment name"
  type        = string
  default     = "dev"
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
  default     = "10.20.0.0/16"
}

variable "public_subnet_cidr" {
  description = "Primary public subnet CIDR (compute pin AZ)"
  type        = string
  default     = "10.20.1.0/24"
}

variable "public_subnet_cidr_b" {
  description = "Second public subnet CIDR (ALB/RDS multi-AZ span; still public, no NAT)"
  type        = string
  default     = "10.20.2.0/24"
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

# Optional third+ subnets if ever needed. Network module already creates 2 public AZs.
variable "extra_public_subnet_ids" {
  description = "Optional additional public subnet IDs beyond network module"
  type        = list(string)
  default     = []
}

variable "alb_ingress_cidrs" {
  description = "CIDRs allowed to ALB:443 (dev: 0.0.0.0/0; prod: Cloudflare)"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "certificate_arn" {
  description = "ACM cert ARN for ALB. Empty → HTTP:80 (dev default)"
  type        = string
  default     = ""
}

variable "db_password" {
  description = "RDS master password. Set via TF_VAR_db_password or terraform.tfvars.local — never commit."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.db_password) >= 8 && length(var.db_password) <= 128 && var.db_password != "CHANGE_ME"
    error_message = "db_password must be 8-128 chars and not a placeholder."
  }
}

variable "backend_api_token" {
  description = "Backend API bearer token. Set via TF_VAR_backend_api_token — never commit."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.backend_api_token) >= 32 && var.backend_api_token != "CHANGE_ME"
    error_message = "backend_api_token must be at least 32 chars and not a placeholder."
  }
}

variable "admin_api_token" {
  description = "Admin API bearer token. Set via TF_VAR_admin_api_token — never commit."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.admin_api_token) >= 32 && var.admin_api_token != "CHANGE_ME"
    error_message = "admin_api_token must be at least 32 chars and not a placeholder."
  }
}

variable "enable_database_url" {
  description = "Inject DATABASE_URL from an existing SSM SecureString parameter."
  type        = bool
  default     = false
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
  description = "Enable LMCache service (desired_count)"
  type        = bool
  default     = false
}

variable "desired_backend" {
  type    = number
  default = 1

  validation {
    condition     = var.desired_backend >= 0 && floor(var.desired_backend) == var.desired_backend
    error_message = "desired_backend must be a nonnegative integer."
  }
}

variable "desired_llm_tts" {
  type    = number
  default = 0

  validation {
    condition     = var.desired_llm_tts >= 0 && floor(var.desired_llm_tts) == var.desired_llm_tts
    error_message = "desired_llm_tts must be a nonnegative integer."
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

variable "desired_livekit" {
  type    = number
  default = 0

  validation {
    condition     = var.desired_livekit >= 0 && floor(var.desired_livekit) == var.desired_livekit
    error_message = "desired_livekit must be a nonnegative integer."
  }
}

variable "desired_lmcache" {
  type    = number
  default = 0

  validation {
    condition     = var.desired_lmcache >= 0 && floor(var.desired_lmcache) == var.desired_lmcache
    error_message = "desired_lmcache must be a nonnegative integer."
  }
}

variable "image_backend" {
  type    = string
  default = "imjusthman/ai-live-backend:latest"
}

variable "image_llm" {
  type    = string
  default = "imjusthman/ai-live-llm:dev"
}

variable "image_tts" {
  type    = string
  default = "imjusthman/ai-live-tts:dev"
}

variable "render_backend" {
  description = "Render backend: mock (no GPU), cloud (LiveAvatar), self_host (MuseTalk future)"
  type        = string
  default     = "mock"
}

variable "llm_engine" {
  description = "LLM engine: none (stub), openai_compat (remote vLLM GPU)"
  type        = string
  default     = "none"
}

variable "llm_base_url" {
  description = "Remote LLM base URL via service discovery. Empty when llm_engine=none."
  type        = string
  default     = ""
}

variable "tts_engine" {
  description = "TTS engine: tone (stub), remote_http (remote vllm-omni GPU)"
  type        = string
  default     = "tone"
}

variable "tts_base_url" {
  description = "Remote TTS base URL via service discovery. Empty when tts_engine=tone."
  type        = string
  default     = ""
}

variable "image_avatar" {
  type    = string
  default = "imjusthman/ai-live-avatar:latest"
}

variable "image_lmcache" {
  type    = string
  default = "imjusthman/ai-live-lmcache:latest"
}

variable "image_livekit" {
  type    = string
  default = "imjusthman/ai-live-livekit:dev"
}

variable "alert_email" {
  description = "SNS alert email (empty = topic only)"
  type        = string
  default     = ""
}

variable "enable_billing_alarms" {
  description = "Billing alarms need us-east-1 metrics; keep false for Seoul-only provider"
  type        = bool
  default     = false
}

variable "create_ec2_capacity" {
  description = "Create GPU/ARM ASG capacity providers"
  type        = bool
  default     = false
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "debug_enabled" {
  description = "Enable verbose backend logging (dev smoke only)"
  type        = bool
  default     = true
}

variable "cors_origins" {
  description = "CORS origins. Dev default *; tighten for prod-like."
  type        = string
  default     = "*"
}
variable "session_store" {
  description = "Session store: memory (single-task) or redis (multi-task)."
  type        = string
  default     = "memory"
}

variable "redis_url" {
  description = "Redis URL. Empty → derive from database module output."
  type        = string
  default     = ""
}

variable "app_env" {
  description = "APP_ENV runtime flag. Empty → falls back to env (dev)."
  type        = string
  default     = ""
}
variable "spot_capacity_percentage" {
  description = "Spot capacity % (0=On-Demand for smoke when Spot quota=0, 100=Spot prod)"
  type        = number
  default     = 100

  validation {
    condition     = var.spot_capacity_percentage >= 0 && var.spot_capacity_percentage <= 100 && floor(var.spot_capacity_percentage) == var.spot_capacity_percentage
    error_message = "spot_capacity_percentage must be an integer from 0 to 100."
  }
}
