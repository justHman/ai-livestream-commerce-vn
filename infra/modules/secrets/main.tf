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
  # Initial token values are passed by environment; legacy placeholders retain stable addresses.
  value = each.key == "backend/api_token" ? var.backend_api_token : (
    each.key == "admin/api_token" ? var.admin_api_token : each.value
  )
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
