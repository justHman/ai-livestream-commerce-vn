variable "env" {
  description = "Environment name (dev|prod)"
  type        = string
}

variable "project" {
  description = "Project tag value"
  type        = string
  default     = "ai-livestream"
}

variable "bucket_name" {
  description = "S3 bucket name. Leave empty to use project-env-assets-{account_id}"
  type        = string
  default     = ""
}

variable "enable_versioning" {
  description = "Enable S3 versioning"
  type        = bool
  default     = false
}

variable "force_destroy" {
  description = "Allow destroy with objects (dev only)"
  type        = bool
  default     = false
}

variable "lifecycle_noncurrent_days" {
  description = "Expire noncurrent versions after N days (0 = disabled)"
  type        = number
  default     = 30
}

variable "tags" {
  description = "Extra tags merged onto all resources"
  type        = map(string)
  default     = {}
}
