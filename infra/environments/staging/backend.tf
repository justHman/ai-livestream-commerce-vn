# Remote state is created by environments/global bootstrap.
# Staging has its own state key — no Terraform workspaces.

terraform {
  backend "s3" {
    bucket       = "ai-livestream-tfstate-191918535424"
    key          = "staging/terraform.tfstate"
    region       = "ap-northeast-2"
    encrypt      = true
    use_lockfile = true
  }
}
