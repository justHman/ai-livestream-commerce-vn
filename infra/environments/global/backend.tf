# Account-wide state (OIDC + shared tfstate primitives).
# Bootstrap once with local state, then migrate with:
#   terraform init -migrate-state -force-copy

terraform {
  backend "s3" {
    bucket       = "ai-livestream-tfstate-191918535424"
    key          = "global/terraform.tfstate"
    region       = "ap-northeast-2"
    encrypt      = true
    use_lockfile = true
  }
}
