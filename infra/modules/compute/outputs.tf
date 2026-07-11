output "cluster_id" {
  description = "ECS cluster ID"
  value       = aws_ecs_cluster.this.id
}

output "cluster_name" {
  description = "ECS cluster name"
  value       = aws_ecs_cluster.this.name
}

output "cluster_arn" {
  description = "ECS cluster ARN"
  value       = aws_ecs_cluster.this.arn
}

output "task_definition_arns" {
  description = "Map of service → task definition ARN"
  value = {
    backend = aws_ecs_task_definition.backend.arn
    llm_tts = aws_ecs_task_definition.llm_tts.arn
    avatar  = aws_ecs_task_definition.avatar.arn
    lmcache = aws_ecs_task_definition.lmcache.arn
    livekit = aws_ecs_task_definition.livekit.arn
  }
}

output "service_names" {
  description = "Map of role → ECS service name"
  value = {
    backend = aws_ecs_service.backend.name
    llm_tts = aws_ecs_service.llm_tts.name
    avatar  = aws_ecs_service.avatar.name
    lmcache = aws_ecs_service.lmcache.name
    livekit = aws_ecs_service.livekit.name
  }
}

output "execution_role_arn" {
  description = "ECS task execution role ARN"
  value       = aws_iam_role.ecs_execution.arn
}

output "task_role_arn" {
  description = "ECS task role ARN"
  value       = aws_iam_role.ecs_task.arn
}

output "capacity_provider_names" {
  description = "EC2 capacity provider names (empty if create_ec2_capacity=false)"
  value = var.create_ec2_capacity ? {
    llm     = aws_ecs_capacity_provider.llm[0].name
    avatar  = aws_ecs_capacity_provider.avatar[0].name
    lmcache = aws_ecs_capacity_provider.lmcache[0].name
  } : {}
}

output "asg_names" {
  description = "ASG names for GPU/ARM capacity"
  value = var.create_ec2_capacity ? {
    llm     = aws_autoscaling_group.llm[0].name
    avatar  = aws_autoscaling_group.avatar[0].name
    lmcache = aws_autoscaling_group.lmcache[0].name
  } : {}
}
