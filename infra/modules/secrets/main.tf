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
  # Values provisioned out-of-band from protected GitHub Environment secrets
  # (aws ssm put-parameter --overwrite). Terraform only creates/keeps the
  # parameter name and never holds plaintext values.
  value     = each.value
  overwrite = true
  tier      = "Standard"

  tags = merge(local.common_tags, {
    Name = "${local.prefix}/${each.key}"
  })

  lifecycle {
    # Operators rotate SecureStrings out-of-band without Terraform reverting them.
    ignore_changes = [value]
  }
}
