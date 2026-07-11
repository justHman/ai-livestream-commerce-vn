locals {
  common_tags = merge(
    {
      Project = var.project
      Env     = var.env
      Module  = "monitoring"
    },
    var.tags,
  )
}

# ---------------------------------------------------------------------------
# CloudWatch log groups for ECS services
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "services" {
  for_each = toset(var.service_log_groups)

  name              = "/ecs/${var.project}-${var.env}/${each.value}"
  retention_in_days = var.log_retention_days

  tags = merge(local.common_tags, {
    Name    = "/ecs/${var.project}-${var.env}/${each.value}"
    Service = each.value
  })
}

# ---------------------------------------------------------------------------
# SNS topic for ops alerts
# ---------------------------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name = "${var.project}-${var.env}-alerts"

  tags = merge(local.common_tags, {
    Name = "${var.project}-${var.env}-alerts"
  })
}

resource "aws_sns_topic_subscription" "email" {
  count = var.alert_email != "" ? 1 : 0

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ---------------------------------------------------------------------------
# Billing alarms (EstimatedCharges)
#
# AWS Billing metrics are published ONLY in us-east-1. Root must either:
#   1) pass a provider alias with region=us-east-1 into this module, or
#   2) apply billing alarms from environments/global in us-east-1.
# If the root provider is ap-northeast-2 only, these resources will fail
# until billing is enabled and the metric is queried from us-east-1.
# Enable "Receive Billing Alerts" in Account billing preferences first.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "billing" {
  for_each = var.enable_billing_alarms ? toset([for t in var.billing_alarm_thresholds : tostring(t)]) : toset([])

  alarm_name          = "${var.project}-${var.env}-billing-${each.value}"
  alarm_description   = "EstimatedCharges >= ${each.value} ${var.billing_currency} (${var.env})"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "EstimatedCharges"
  namespace           = "AWS/Billing"
  period              = 21600
  statistic           = "Maximum"
  threshold           = tonumber(each.value)
  treat_missing_data  = "notBreaching"

  dimensions = {
    Currency = var.billing_currency
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = merge(local.common_tags, {
    Name      = "${var.project}-${var.env}-billing-${each.value}"
    Threshold = each.value
  })
}
