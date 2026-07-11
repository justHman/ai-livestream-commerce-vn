output "parameter_names" {
  description = "List of SSM parameter names"
  value       = [for p in aws_ssm_parameter.this : p.name]
}

output "parameter_arns" {
  description = "Map of relative path → SSM parameter ARN"
  value       = { for k, p in aws_ssm_parameter.this : k => p.arn }
}

output "parameter_name_map" {
  description = "Map of relative path → full SSM parameter name"
  value       = { for k, p in aws_ssm_parameter.this : k => p.name }
}

output "prefix" {
  description = "SSM path prefix used for this env"
  value       = local.prefix
}
