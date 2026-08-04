# ECS cluster + capacity providers + AMI lookups (was main.tf).
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}
data "aws_ssm_parameter" "ecs_gpu_ami" {
  count = var.create_ec2_capacity ? 1 : 0
  # ECS-optimized GPU AMI (x86_64) for g6/g4dn
  name = "/aws/service/ecs/optimized-ami/amazon-linux-2/gpu/recommended/image_id"
}

resource "aws_ecs_cluster" "this" {
  name = "${local.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = var.enable_container_insights ? "enabled" : "disabled"
  }

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-cluster"
  })
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name = aws_ecs_cluster.this.name

  capacity_providers = compact(concat(
    ["FARGATE", "FARGATE_SPOT"],
    var.create_ec2_capacity ? [
      aws_ecs_capacity_provider.llm[0].name,
      aws_ecs_capacity_provider.tts[0].name,
      aws_ecs_capacity_provider.avatar[0].name,
    ] : [],
  ))

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 1
    base              = 0
  }
}

# ---------------------------------------------------------------------------
# Cloud Map service discovery (internal DNS for backend → LLM/TTS)
# 1 namespace + 1 SD service registering the llm_tts task ENI.
# Both LLM (8001) and TTS (8002) resolve via the same A record;
# backend calls llm-tts.<env>.ai-live.local:8001 and :8002 respectively.
# ---------------------------------------------------------------------------
