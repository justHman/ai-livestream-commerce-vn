output "sg_alb_id" {
  description = "ALB security group ID"
  value       = aws_security_group.alb.id
}

output "sg_backend_id" {
  description = "Backend security group ID"
  value       = aws_security_group.backend.id
}

output "sg_llm_id" {
  description = "LLM security group ID"
  value       = aws_security_group.llm.id
}

output "sg_tts_id" {
  description = "TTS security group ID"
  value       = aws_security_group.tts.id
}

output "sg_avatar_id" {
  description = "Avatar security group ID"
  value       = aws_security_group.avatar.id
}

output "sg_rds_id" {
  description = "RDS security group ID"
  value       = aws_security_group.rds.id
}

output "sg_redis_id" {
  description = "Redis security group ID"
  value       = aws_security_group.redis.id
}

output "sg_livekit_id" {
  description = "LiveKit security group ID"
  value       = aws_security_group.livekit.id
}

output "sg_map" {
  description = "Map of role → security group ID for compute module"
  value = {
    alb     = aws_security_group.alb.id
    backend = aws_security_group.backend.id
    llm     = aws_security_group.llm.id
    tts     = aws_security_group.tts.id
    avatar  = aws_security_group.avatar.id
    rds     = aws_security_group.rds.id
    redis   = aws_security_group.redis.id
    livekit = aws_security_group.livekit.id
  }
}

output "github_oidc_provider_arn" {
  description = "GitHub OIDC provider ARN (null if enable_oidc=false)"
  value       = try(aws_iam_openid_connect_provider.github[0].arn, null)
}
