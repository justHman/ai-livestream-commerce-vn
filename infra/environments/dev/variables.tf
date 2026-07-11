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
  default     = "CHANGE_ME_via_TF_VAR"
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
}

variable "desired_llm_tts" {
  type    = number
  default = 0 # keep 0 until GPU Spot budget approved
}

variable "desired_avatar" {
  type    = number
  default = 0
}

variable "desired_livekit" {
  type    = number
  default = 0
}

variable "desired_lmcache" {
  type    = number
  default = 0
}

variable "image_backend" {
  type    = string
  default = "justhman/ai-live-backend:latest"
}

variable "image_llm_tts" {
  type    = string
  default = "justhman/ai-live-llm-tts:latest"
}

variable "image_avatar" {
  type    = string
  default = "justhman/ai-live-avatar:latest"
}

variable "image_lmcache" {
  type    = string
  default = "justhman/ai-live-lmcache:latest"
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
  default     = true
}

variable "tags" {
  type    = map(string)
  default = {}
}
