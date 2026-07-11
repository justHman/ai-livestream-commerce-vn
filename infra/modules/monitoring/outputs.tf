output "log_group_names" {
  description = "Map of service → CloudWatch log group name"
  value       = { for k, g in aws_cloudwatch_log_group.services : k => g.name }
}

output "log_group_arns" {
  description = "Map of service → CloudWatch log group ARN"
  value       = { for k, g in aws_cloudwatch_log_group.services : k => g.arn }
}

output "sns_topic_arn" {
  description = "SNS alerts topic ARN"
  value       = aws_sns_topic.alerts.arn
}

output "sns_topic_name" {
  description = "SNS alerts topic name"
  value       = aws_sns_topic.alerts.name
}

output "billing_alarm_names" {
  description = "Billing alarm names (empty if disabled)"
  value       = [for a in aws_cloudwatch_metric_alarm.billing : a.alarm_name]
}
