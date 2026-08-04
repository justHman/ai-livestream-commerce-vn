# Shared locals for compute module (was main.tf).
locals {
  common_tags = merge(
    {
      Project = var.project
      Env     = var.env
      Module  = "compute"
    },
    var.tags,
  )

  name_prefix = "${var.project}-${var.env}"
  log_prefix  = var.log_group_prefix != "" ? var.log_group_prefix : "/ecs/${var.project}-${var.env}"

  lmcache_desired = var.lmcache_enabled ? var.desired_lmcache : 0

  # Capacity provider names (created when create_ec2_capacity=true)
  cp_llm     = "${local.name_prefix}-cp-llm"
  cp_tts     = "${local.name_prefix}-cp-tts"
  cp_avatar  = "${local.name_prefix}-cp-avatar"
  cp_lmcache = "${local.name_prefix}-cp-lmcache"
}

# ---------------------------------------------------------------------------
# IAM — minimal execution + task roles (security module has SGs only)
# ---------------------------------------------------------------------------

