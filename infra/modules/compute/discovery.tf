# Cloud Map service discovery (internal DNS backend -> LLM/TTS).
resource "aws_service_discovery_private_dns_namespace" "this" {
  count = var.create_ec2_capacity ? 1 : 0

  name        = "${var.env}.ai-live.local"
  description = "Internal DNS for ${var.env} GPU services"
  vpc         = var.vpc_id
  tags        = local.common_tags
}

resource "aws_service_discovery_service" "llm_tts" {
  count = var.create_ec2_capacity ? 1 : 0

  name = "llm-tts"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.this[0].id
    dns_records {
      ttl  = 10
      type = "A"
    }
    routing_policy = "MULTIVALUE"
  }

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# EC2 Spot capacity — g6 (LLM+TTS), g4dn (Avatar), c7g (LMCache)
# Placeholders: min=0 so create does not launch expensive Spot until desired>0
# ---------------------------------------------------------------------------

