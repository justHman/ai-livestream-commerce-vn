locals {
  common_tags = merge(
    {
      Project = var.project
      Env     = var.env
      Module  = "security"
    },
    var.tags,
  )
}

# ---------------------------------------------------------------------------
# Security groups - matrix from docs/aws-architecture.md s3
# Iron rules: no port 22; no 0.0.0.0/0 on DB/Redis/GPU control ports
# ---------------------------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "${var.project}-${var.env}-sg-alb"
  description = "ALB public HTTPS ingress"
  vpc_id      = var.vpc_id

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.env}-sg-alb"
    Role = "alb"
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  for_each = toset(var.alb_ingress_cidrs)

  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from edge (${each.value})"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = each.value
}

# HTTP:80 — either forward (no cert) or redirect-to-HTTPS (cert set).
# Caller passes alb_http_ingress_cidrs; empty list = close :80.
resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  for_each = toset(var.alb_http_ingress_cidrs)

  security_group_id = aws_security_group.alb.id
  description       = "HTTP from edge (${each.value})"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = each.value
}

resource "aws_vpc_security_group_egress_rule" "alb_to_backend" {
  security_group_id            = aws_security_group.alb.id
  description                  = "HTTP to backend tasks"
  ip_protocol                  = "tcp"
  from_port                    = 8800
  to_port                      = 8800
  referenced_security_group_id = aws_security_group.backend.id
}

resource "aws_vpc_security_group_egress_rule" "alb_to_livekit" {
  security_group_id            = aws_security_group.alb.id
  description                  = "HTTPS signaling to LiveKit"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.livekit.id
}

# --- backend ---

resource "aws_security_group" "backend" {
  name        = "${var.project}-${var.env}-sg-backend"
  description = "Backend Fargate API"
  vpc_id      = var.vpc_id

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.env}-sg-backend"
    Role = "backend"
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "backend_from_alb" {
  security_group_id            = aws_security_group.backend.id
  description                  = "App port from ALB"
  ip_protocol                  = "tcp"
  from_port                    = 8800
  to_port                      = 8800
  referenced_security_group_id = aws_security_group.alb.id
}

resource "aws_vpc_security_group_egress_rule" "backend_to_rds" {
  security_group_id            = aws_security_group.backend.id
  description                  = "Postgres"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.rds.id
}

resource "aws_vpc_security_group_egress_rule" "backend_to_redis" {
  security_group_id            = aws_security_group.backend.id
  description                  = "Redis"
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  referenced_security_group_id = aws_security_group.redis.id
}

resource "aws_vpc_security_group_egress_rule" "backend_to_llm" {
  security_group_id            = aws_security_group.backend.id
  description                  = "vLLM"
  ip_protocol                  = "tcp"
  from_port                    = 8001
  to_port                      = 8001
  referenced_security_group_id = aws_security_group.llm.id
}

resource "aws_vpc_security_group_egress_rule" "backend_to_tts" {
  security_group_id            = aws_security_group.backend.id
  description                  = "TTS"
  ip_protocol                  = "tcp"
  from_port                    = 8002
  to_port                      = 8002
  referenced_security_group_id = aws_security_group.tts.id
}

resource "aws_vpc_security_group_egress_rule" "backend_to_avatar" {
  security_group_id            = aws_security_group.backend.id
  description                  = "Avatar"
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
  referenced_security_group_id = aws_security_group.avatar.id
}

resource "aws_vpc_security_group_egress_rule" "backend_https" {
  security_group_id = aws_security_group.backend.id
  description       = "HTTPS egress (Hub/S3/HF/SSM)"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "backend_http" {
  security_group_id = aws_security_group.backend.id
  description       = "HTTP egress (package mirrors)"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = "0.0.0.0/0"
}

# --- llm ---

resource "aws_security_group" "llm" {
  name        = "${var.project}-${var.env}-sg-llm"
  description = "LLM vLLM GPU service"
  vpc_id      = var.vpc_id

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.env}-sg-llm"
    Role = "llm"
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "llm_from_backend" {
  security_group_id            = aws_security_group.llm.id
  description                  = "vLLM from backend"
  ip_protocol                  = "tcp"
  from_port                    = 8001
  to_port                      = 8001
  referenced_security_group_id = aws_security_group.backend.id
}

resource "aws_vpc_security_group_egress_rule" "llm_https" {
  security_group_id = aws_security_group.llm.id
  description       = "HTTPS egress (weights/HF)"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

# --- tts ---

resource "aws_security_group" "tts" {
  name        = "${var.project}-${var.env}-sg-tts"
  description = "TTS GPU service"
  vpc_id      = var.vpc_id

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.env}-sg-tts"
    Role = "tts"
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "tts_from_backend" {
  security_group_id            = aws_security_group.tts.id
  description                  = "TTS from backend"
  ip_protocol                  = "tcp"
  from_port                    = 8002
  to_port                      = 8002
  referenced_security_group_id = aws_security_group.backend.id
}

resource "aws_vpc_security_group_egress_rule" "tts_https" {
  security_group_id = aws_security_group.tts.id
  description       = "HTTPS egress"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

# --- avatar ---

resource "aws_security_group" "avatar" {
  name        = "${var.project}-${var.env}-sg-avatar"
  description = "Avatar GPU service"
  vpc_id      = var.vpc_id

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.env}-sg-avatar"
    Role = "avatar"
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "avatar_from_backend" {
  security_group_id            = aws_security_group.avatar.id
  description                  = "Avatar control from backend"
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
  referenced_security_group_id = aws_security_group.backend.id
}

resource "aws_vpc_security_group_egress_rule" "avatar_https" {
  security_group_id = aws_security_group.avatar.id
  description       = "HTTPS egress"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "avatar_livekit_tcp" {
  security_group_id            = aws_security_group.avatar.id
  description                  = "LiveKit signaling/media TCP"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.livekit.id
}

resource "aws_vpc_security_group_egress_rule" "avatar_livekit_udp" {
  security_group_id            = aws_security_group.avatar.id
  description                  = "LiveKit media UDP"
  ip_protocol                  = "udp"
  from_port                    = 50000
  to_port                      = 60000
  referenced_security_group_id = aws_security_group.livekit.id
}

# --- rds ---

resource "aws_security_group" "rds" {
  name        = "${var.project}-${var.env}-sg-rds"
  description = "RDS Postgres - backend only"
  vpc_id      = var.vpc_id

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.env}-sg-rds"
    Role = "rds"
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_backend" {
  security_group_id            = aws_security_group.rds.id
  description                  = "Postgres from backend only"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.backend.id
}

# no egress rules - default deny all outbound for data plane

# --- redis ---

resource "aws_security_group" "redis" {
  name        = "${var.project}-${var.env}-sg-redis"
  description = "ElastiCache Redis - backend only"
  vpc_id      = var.vpc_id

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.env}-sg-redis"
    Role = "redis"
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "redis_from_backend" {
  security_group_id            = aws_security_group.redis.id
  description                  = "Redis from backend only"
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  referenced_security_group_id = aws_security_group.backend.id
}

# --- lmcache ---

# --- livekit ---

resource "aws_security_group" "livekit" {
  name        = "${var.project}-${var.env}-sg-livekit"
  description = "LiveKit SFU - signaling + public media UDP"
  vpc_id      = var.vpc_id

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.env}-sg-livekit"
    Role = "livekit"
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "livekit_https_from_alb" {
  security_group_id            = aws_security_group.livekit.id
  description                  = "Signaling HTTPS from ALB"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.alb.id
}

resource "aws_vpc_security_group_ingress_rule" "livekit_media_udp" {
  security_group_id = aws_security_group.livekit.id
  description       = "WebRTC media UDP public"
  ip_protocol       = "udp"
  from_port         = 50000
  to_port           = 60000
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "livekit_media_udp" {
  security_group_id = aws_security_group.livekit.id
  description       = "WebRTC media UDP egress"
  ip_protocol       = "udp"
  from_port         = 50000
  to_port           = 60000
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "livekit_https" {
  security_group_id = aws_security_group.livekit.id
  description       = "HTTPS egress"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

# ---------------------------------------------------------------------------
# Optional GitHub Actions OIDC provider (account-wide; enable once)
# ---------------------------------------------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  count = var.enable_oidc ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [var.github_oidc_thumbprint]

  tags = merge(local.common_tags, {
    Name = "${var.project}-github-oidc"
  })
}
