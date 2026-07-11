locals {
  common_tags = merge(
    {
      Project = var.project
      Env     = var.env
      Module  = "loadbalancer"
    },
    var.tags,
  )

  name_prefix  = "${var.project}-${var.env}"
  use_https    = var.certificate_arn != ""
  listener_arn = local.use_https ? aws_lb_listener.https[0].arn : aws_lb_listener.http[0].arn
}

# ---------------------------------------------------------------------------
# ALB — internet-facing origin for Cloudflare (no AWS WAF)
# ---------------------------------------------------------------------------

resource "aws_lb" "this" {
  name               = "${local.name_prefix}-alb"
  load_balancer_type = "application"
  internal           = false
  security_groups    = [var.sg_alb_id]
  subnets            = var.subnet_ids

  idle_timeout               = var.idle_timeout
  enable_deletion_protection = var.enable_deletion_protection
  drop_invalid_header_fields = true

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-alb"
  })
}

resource "aws_lb_target_group" "backend" {
  name        = "${local.name_prefix}-backend"
  port        = var.backend_port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    enabled             = true
    path                = var.health_check_path
    protocol            = "HTTP"
    matcher             = "200-399"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # Sticky for WS/SSE sessions through ALB.
  stickiness {
    type            = "lb_cookie"
    cookie_duration = 86400
    enabled         = true
  }

  deregistration_delay = 30

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-tg-backend"
    Role = "backend"
  })

  lifecycle {
    create_before_destroy = true
  }
}

# Dev / no-cert: HTTP:80 → backend TG
resource "aws_lb_listener" "http" {
  count = local.use_https ? 0 : 1

  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.backend.arn
  }

  tags = local.common_tags
}

# Prod / cert set: HTTPS:443 → backend TG
resource "aws_lb_listener" "https" {
  count = local.use_https ? 1 : 0

  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.backend.arn
  }

  tags = local.common_tags
}

# Optional HTTP → HTTPS redirect when cert is present
resource "aws_lb_listener" "http_redirect" {
  count = local.use_https && var.enable_http_redirect ? 1 : 0

  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }

  tags = local.common_tags
}

# Optional path-pattern stubs (future /v1/chat etc.). Empty target_group_arn → backend TG.
resource "aws_lb_listener_rule" "path" {
  for_each = { for r in var.path_rules : r.name => r }

  listener_arn = local.listener_arn
  priority     = each.value.priority

  action {
    type             = "forward"
    target_group_arn = each.value.target_group_arn != "" ? each.value.target_group_arn : aws_lb_target_group.backend.arn
  }

  condition {
    path_pattern {
      values = [each.value.path_pattern]
    }
  }

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-rule-${each.key}"
  })
}
