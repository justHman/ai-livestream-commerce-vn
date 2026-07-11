variable "env" {
  description = "Environment name (dev|prod)"
  type        = string
}

variable "project" {
  description = "Project tag value"
  type        = string
  default     = "ai-livestream"
}

variable "log_retention_days" {
  description = "CloudWatch log group retention"
  type        = number
  default     = 14
}

variable "service_log_groups" {
  description = "Service names that get /ecs/{project}-{env}/{name} log groups"
  type        = list(string)
  default = [
    "backend",
    "llm",
    "tts",
    "avatar",
    "livekit",
    "lmcache",
  ]
}

variable "alert_email" {
  description = "Email for SNS alert subscription (empty = topic only, no subscription)"
  type        = string
  default     = ""
}

variable "billing_alarm_thresholds" {
  description = "EstimatedCharges USD thresholds for billing alarms"
  type        = list(number)
  default     = [50, 100]
}

variable "enable_billing_alarms" {
  description = "Create billing EstimatedCharges alarms (requires us-east-1 metrics enabled)"
  type        = bool
  default     = true
}

variable "billing_currency" {
  description = "Currency dimension for AWS/Billing EstimatedCharges"
  type        = string
  default     = "USD"
}

variable "tags" {
  description = "Extra tags merged onto all resources"
  type        = map(string)
  default     = {}
}
