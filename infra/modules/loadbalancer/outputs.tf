output "alb_arn" {
  description = "ALB ARN"
  value       = aws_lb.this.arn
}

output "alb_id" {
  description = "ALB ID"
  value       = aws_lb.this.id
}

output "alb_dns_name" {
  description = "ALB DNS name (Cloudflare CNAME target)"
  value       = aws_lb.this.dns_name
}

output "alb_zone_id" {
  description = "ALB hosted zone ID"
  value       = aws_lb.this.zone_id
}

output "backend_target_group_arn" {
  description = "Backend target group ARN (attach ECS service)"
  value       = aws_lb_target_group.backend.arn
}

output "backend_target_group_name" {
  description = "Backend target group name"
  value       = aws_lb_target_group.backend.name
}

output "listener_arn" {
  description = "Active default listener ARN (HTTP or HTTPS)"
  value       = local.listener_arn
}

output "https_enabled" {
  description = "True when certificate_arn was set"
  value       = local.use_https
}
