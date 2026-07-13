locals {
  common_tags = merge(
    {
      Project = var.project
      Env     = var.env
      Module  = "secrets"
    },
    var.tags,
  )

  prefix = var.parameter_prefix != "" ? trimsuffix(var.parameter_prefix, "/") : "/${var.env}"
}

resource "aws_ssm_parameter" "this" {
  for_each = var.parameters

  name        = "${local.prefix}/${each.key}"
  description = "SecureString placeholder for ${each.key} (${var.env})"
  type        = "SecureString"
  value       = each.value
  overwrite   = false
  tier        = "Standard"

  tags = merge(local.common_tags, {
    Name = "${local.prefix}/${each.key}"
  })

  lifecycle {
    # Values are set out-of-band: aws ssm put-parameter --overwrite
    ignore_changes = [value]
  }
}
