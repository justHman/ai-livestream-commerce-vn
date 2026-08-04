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
  description = "Backend image URI"
  type        = string
  default     = "imjusthman/ai-live-backend:latest"
}

variable "image_llm" {
  description = "LLM image URI (vLLM + Qwen3.5-4B-AWQ)"
  type        = string
  default     = "imjusthman/ai-live-llm:latest"
}

variable "image_tts" {
  description = "TTS image URI (vllm-omni + VieNeu-TTS-v2)"
  type        = string
  default     = "imjusthman/ai-live-tts:latest"
}

variable "image_avatar" {
  description = "Avatar image URI"
  type        = string
  default     = "imjusthman/ai-live-avatar:latest"
}

variable "image_lmcache" {
  description = "LMCache sidecar image URI (colocated in LLM task; evidence-gated)"
  type        = string
  default     = "imjusthman/ai-live-lmcache:latest"
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
variable "render_backend" {
  description = "Backend render backend: mock (no GPU) or cloud/self_host (GPU)"
  type        = string
  default     = "mock"
}

variable "llm_engine" {
  description = "Backend LLM engine: none (stub), openai_compat (remote vLLM), vllm"
  type        = string
  default     = "none"
}

variable "llm_base_url" {
  description = "Remote LLM OpenAI-compat base URL (service discovery). Empty when llm_engine=none."
  type        = string
  default     = ""
}

variable "llm_model" {
  description = "LLM model id passed to OpenAI-compat endpoint (e.g. oc/deepseek-v4-flash-free). Empty = default."
  type        = string
  default     = ""
}

variable "tts_engine" {
  description = "Backend TTS engine: tone (stub), remote_http (remote vllm-omni), vieneu"
  type        = string
  default     = "tone"
}

variable "tts_base_url" {
  description = "Remote TTS base URL (service discovery). Empty when tts_engine=tone."
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
