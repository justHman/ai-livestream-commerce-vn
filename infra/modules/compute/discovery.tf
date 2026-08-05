# Cloud Map service discovery (internal DNS backend → LLM/TTS).
# 1 namespace + 1 SD service per model service: llm.<env>.ai-live.local:8001,
# tts.<env>.ai-live.local:8002. Backend reaches each engine over its own
# environment-specific private DNS name.

resource "aws_service_discovery_private_dns_namespace" "this" {
  count = var.create_ec2_capacity ? 1 : 0

  name        = "${var.env}.ai-live.local"
  description = "Internal DNS for ${var.env} GPU services"
  vpc         = var.vpc_id
  tags        = local.common_tags
}

resource "aws_service_discovery_service" "llm" {
  count = var.create_ec2_capacity ? 1 : 0

  name = "llm"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.this[0].id
    dns_records {
      ttl  = 10
      type = "A"
    }
    routing_policy = "MULTIVALUE"
  }

  tags = merge(local.common_tags, { Role = "llm" })
}

resource "aws_service_discovery_service" "tts" {
  count = var.create_ec2_capacity ? 1 : 0

  name = "tts"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.this[0].id
    dns_records {
      ttl  = 10
      type = "A"
    }
    routing_policy = "MULTIVALUE"
  }

  tags = merge(local.common_tags, { Role = "tts" })
}
