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
  description = "Public subnet IDs for tasks/ASG (no NAT MVP)"
  type        = list(string)
}

variable "sg_map" {
  description = "Map of role → security group ID from security module"
  type        = map(string)
}

variable "assign_public_ip" {
  description = "Assign public IP to Fargate tasks (required without NAT)"
  type        = bool
  default     = true
}

# --- images (Docker Hub public) ---

variable "image_backend" {
  description = "Backend image URI (immutable digest only: registry/repo@sha256:...)"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9./_-]+@sha256:[a-f0-9]{64}$", var.image_backend))
    error_message = "image_backend must reference an immutable digest (repo@sha256:64hex). No mutable tags."
  }
}

variable "image_llm" {
  description = "LLM image URI (vLLM + Qwen3.5-4B-AWQ) (immutable digest only: registry/repo@sha256:...)"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9./_-]+@sha256:[a-f0-9]{64}$", var.image_llm))
    error_message = "image_llm must reference an immutable digest (repo@sha256:64hex). No mutable tags."
  }
}

variable "image_tts" {
  description = "TTS image URI (provider-neutral FastAPI + VieNeu-TTS-v3-Turbo) (immutable digest only: registry/repo@sha256:...)"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9./_-]+@sha256:[a-f0-9]{64}$", var.image_tts))
    error_message = "image_tts must reference an immutable digest (repo@sha256:64hex). No mutable tags."
  }
}

variable "image_avatar" {
  description = "Avatar image URI (immutable digest only: registry/repo@sha256:...)"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9./_-]+@sha256:[a-f0-9]{64}$", var.image_avatar))
    error_message = "image_avatar must reference an immutable digest (repo@sha256:64hex). No mutable tags."
  }
}

variable "image_lmcache" {
  description = "LMCache sidecar image URI (immutable digest only: registry/repo@sha256:...)"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9./_-]+@sha256:[a-f0-9]{64}$", var.image_lmcache))
    error_message = "image_lmcache must reference an immutable digest (repo@sha256:64hex). No mutable tags."
  }
}

variable "livekit_url" {
  description = "LiveKit Cloud WebSocket URL (wss://...). Empty = LiveKit disabled."
  type        = string
  default     = ""
}

# --- desired counts ---

variable "desired_backend" {
  description = "Backend Fargate service desired count"
  type        = number
  default     = 1
}

variable "backend_capacity_provider" {
  description = "Backend capacity provider: FARGATE_SPOT (dev) or FARGATE (staging/prod)"
  type        = string
  default     = "FARGATE_SPOT"

  validation {
    condition     = contains(["FARGATE", "FARGATE_SPOT"], var.backend_capacity_provider)
    error_message = "backend_capacity_provider must be FARGATE or FARGATE_SPOT."
  }
}

variable "desired_llm" {
  description = "LLM EC2 GPU service desired count"
  type        = number
  default     = 0
}

variable "desired_tts" {
  description = "TTS EC2 GPU service desired count"
  type        = number
  default     = 0
}

variable "desired_avatar" {
  description = "Avatar EC2 GPU service desired count"
  type        = number
  default     = 0
}

variable "lmcache_enabled" {
  description = "When false, force lmcache desired_count=0"
  type        = bool
  default     = false
}

# --- capacity / instance types ---

variable "instance_type_llm" {
  description = "EC2 Spot type for LLM and TTS (g6 L4)"
  type        = string
  default     = "g6.xlarge"
}

variable "instance_type_avatar" {
  description = "EC2 Spot type for Avatar (g4dn T4)"
  type        = string
  default     = "g4dn.xlarge"
}

variable "backend_cpu" {
  description = "Fargate CPU units for backend"
  type        = number
  default     = 1024
}

variable "backend_memory" {
  description = "Fargate memory MiB for backend"
  type        = number
  default     = 2048
}

variable "backend_target_group_arn" {
  description = "ALB backend target group ARN (empty = no LB attachment)"
  type        = string
  default     = ""
}

variable "weights_s3_uri" {
  description = "S3 URI for model weights (passed as env to GPU tasks)"
  type        = string
  default     = ""
}

variable "secrets_arns" {
  description = "Map of secret key → SSM parameter ARN for ECS secrets injection"
  type        = map(string)
  default     = {}
}

variable "log_group_prefix" {
  description = "CloudWatch log group prefix, e.g. /ecs/ai-livestream-dev"
  type        = string
  default     = ""
}

variable "enable_container_insights" {
  description = "Enable ECS Container Insights"
  type        = bool
  default     = false
}

variable "enable_execute_command" {
  description = "Enable ECS Exec on services"
  type        = bool
  default     = true
}

variable "create_ec2_capacity" {
  description = "Create EC2 launch templates / ASG / capacity providers (skeleton; set false for Fargate-only smoke)"
  type        = bool
  default     = true
}

variable "tags" {
  description = "Extra tags merged onto all resources"
  type        = map(string)
  default     = {}
}

variable "debug_enabled" {
  description = "Enable verbose debug logging in backend (dev only)"
  type        = bool
  default     = false
}

variable "cors_origins" {
  description = "Comma-separated CORS origins. Dev allows *; prod must be explicit."
  type        = string
  default     = "*"
}

variable "session_store" {
  description = "Session store backend: memory (single-task) or redis (multi-task)."
  type        = string
  default     = "memory"
}

variable "redis_url" {
  description = "Redis connection string for SESSION_STORE=redis. Empty = memory store."
  type        = string
  default     = ""
}

variable "app_env" {
  description = "APP_ENV runtime flag. Empty = falls back to var.env (dev)."
  type        = string
  default     = ""
}
variable "vpc_id" {
  description = "VPC ID for Cloud Map service discovery namespace"
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

variable "allow_stub_avatar_test_only" {
  description = "Explicit test-only escape for the Avatar stub; never enabled in production."
  type        = bool
  default     = false
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
  description = "LLM OpenAI-compat base URL. Empty = Cloud Map private DNS llm.<env>.ai-live.local for self-host adapters; override for hosted providers."
  type        = string
  default     = ""
}

variable "llm_model" {
  description = "LLM model id passed to OpenAI-compat endpoint (e.g. oc/deepseek-v4-flash-free). Empty = default."
  type        = string
  default     = ""
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

variable "llm_engine" {
  description = "Self-host LLM engine (service-local): vllm|sglang|transformers"
  type        = string
  default     = "vllm"

  validation {
    condition     = contains(["vllm", "sglang", "transformers"], var.llm_engine)
    error_message = "llm_engine must be one of: vllm, sglang, transformers."
  }
}

variable "tts_engine" {
  description = "Self-host TTS engine (service-local): vieneu|cosyvoice"
  type        = string
  default     = "vieneu"

  validation {
    condition     = contains(["vieneu", "cosyvoice"], var.tts_engine)
    error_message = "tts_engine must be one of: vieneu, cosyvoice."
  }
}

variable "tts_model_source" {
  description = "TTS model source: sdk (current VieNeu SDK/provider download; no S3 URI, no forced offline) | s3_bootstrap (dormant future engine object-backed weights)"
  type        = string
  default     = "sdk"

  validation {
    condition     = contains(["sdk", "s3_bootstrap"], var.tts_model_source)
    error_message = "tts_model_source must be sdk or s3_bootstrap."
  }
}

variable "avatar_engine" {
  description = "Self-host avatar engine (service-local): avatarforcing"
  type        = string
  default     = "avatarforcing"

  validation {
    condition     = var.avatar_engine == "avatarforcing"
    error_message = "avatar_engine must be avatarforcing."
  }
}

variable "tts_base_url" {
  description = "TTS base URL. Empty = Cloud Map private DNS tts.<env>.ai-live.local for self-host adapters; override for hosted providers."
  type        = string
  default     = ""
}

variable "tts_voice_id" {
  description = "ElevenLabs voice_id (when tts_engine=elevenlabs). Empty = default Rachel."
  type        = string
  default     = ""
}

variable "spot_capacity_percentage" {
  description = "Percentage of capacity from Spot (0-100). 0 = all On-Demand (smoke when Spot quota=0), 100 = all Spot (prod)."
  type        = number
  default     = 100
}
