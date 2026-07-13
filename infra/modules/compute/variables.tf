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

variable "image_llm_tts" {
  description = "Shared LLM+TTS image URI (two containers, one image family)"
  type        = string
  default     = "imjusthman/ai-live-llm-tts:latest"
}

variable "image_avatar" {
  description = "Avatar image URI"
  type        = string
  default     = "imjusthman/ai-live-avatar:latest"
}

variable "image_lmcache" {
  description = "LMCache image URI"
  type        = string
  default     = "imjusthman/ai-live-lmcache:latest"
}

variable "image_livekit" {
  description = "LiveKit server image URI"
  type        = string
  default     = "livekit/livekit-server:latest"
}

# --- desired counts ---

variable "desired_backend" {
  description = "Backend Fargate service desired count"
  type        = number
  default     = 1
}

variable "desired_llm_tts" {
  description = "LLM+TTS EC2 GPU service desired count"
  type        = number
  default     = 0
}

variable "desired_avatar" {
  description = "Avatar EC2 GPU service desired count"
  type        = number
  default     = 0
}

variable "desired_livekit" {
  description = "LiveKit Fargate service desired count"
  type        = number
  default     = 0
}

variable "desired_lmcache" {
  description = "LMCache EC2 service desired count (0 = off)"
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
  description = "EC2 Spot type for LLM+TTS (g6 L4)"
  type        = string
  default     = "g6.xlarge"
}

variable "instance_type_avatar" {
  description = "EC2 Spot type for Avatar (g4dn T4)"
  type        = string
  default     = "g4dn.xlarge"
}

variable "instance_type_lmcache" {
  description = "EC2 Spot type for LMCache (ARM)"
  type        = string
  default     = "c7g.2xlarge"
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

variable "livekit_cpu" {
  description = "Fargate CPU units for LiveKit"
  type        = number
  default     = 2048
}

variable "livekit_memory" {
  description = "Fargate memory MiB for LiveKit"
  type        = number
  default     = 4096
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
  description = "Map of secret key → SSM parameter ARN (for task secrets block later)"
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

variable "backend_api_token" {
  description = "Backend API bearer token (injected as env; rotate via SSM in prod)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "admin_api_token" {
  description = "Admin API bearer token (injected as env; rotate via SSM in prod)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "debug_enabled" {
  description = "Enable verbose debug logging in backend (dev only)"
  type        = bool
  default     = false
}
