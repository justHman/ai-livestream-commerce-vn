variable "env" {
  description = "Environment name (dev|prod)"
  type        = string
}

variable "project" {
  description = "Project tag value"
  type        = string
  default     = "ai-livestream"
}

variable "vpc_id" {
  description = "VPC ID"
  type        = string
}

variable "subnet_ids" {
  description = "Public subnet IDs for ALB (AWS requires >=2 AZs)"
  type        = list(string)
}

variable "sg_alb_id" {
  description = "ALB security group ID"
  type        = string
}

variable "certificate_arn" {
  description = "ACM cert ARN. Empty → HTTP:80 listener (dev); set → HTTPS:443"
  type        = string
  default     = ""
}

variable "backend_port" {
  description = "Backend target port"
  type        = number
  default     = 8800
}

variable "health_check_path" {
  description = "Backend health check path (router is /api/v1)"
  type        = string
  default     = "/api/v1/health/live"
}

variable "idle_timeout" {
  description = "ALB idle timeout seconds (WS/SSE friendly)"
  type        = number
  default     = 120
}

variable "enable_deletion_protection" {
  description = "ALB deletion protection"
  type        = bool
  default     = false
}

variable "enable_http_redirect" {
  description = "When HTTPS is enabled, also create HTTP:80 → HTTPS redirect"
  type        = bool
  default     = true
}

variable "path_rules" {
  description = "Optional extra path-pattern rules on the default listener (stubs for future services)"
  type = list(object({
    name         = string
    priority     = number
    path_pattern = string
    # target_group_arn reserved for future non-backend TGs; empty uses backend TG
    target_group_arn = optional(string, "")
  }))
  default = []
}

variable "tags" {
  description = "Extra tags merged onto all resources"
  type        = map(string)
  default     = {}
}
