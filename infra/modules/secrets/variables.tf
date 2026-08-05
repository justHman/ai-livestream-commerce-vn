variable "env" {
  description = "Environment name (dev|prod)"
  type        = string
}

variable "project" {
  description = "Project tag value"
  type        = string
  default     = "ai-livestream"
}

variable "parameter_prefix" {
  description = "SSM path prefix. Empty → /{env}"
  type        = string
  default     = ""
}

variable "parameters" {
  description = "Map of relative path → placeholder SecureString value"
  type        = map(string)
  default = {
    "db/password" = "CHANGE_ME"
    "redis/auth"  = "CHANGE_ME"
    # Rotate both values before enabling the LiveKit service.
    "livekit/api_key"    = "CHANGE_ME"
    "livekit/api_secret" = "CHANGE_ME"
    "jwt/secret"         = "CHANGE_ME"
    "backend/api_token"  = "CHANGE_ME"
    "admin/api_token"    = "CHANGE_ME"
  }
}

variable "ignore_value_changes" {
  description = "Ignore value drift after out-of-band put-parameter"
  type        = bool
  default     = true
}

variable "tags" {
  description = "Extra tags merged onto all resources"
  type        = map(string)
  default     = {}
}
