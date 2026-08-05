# Remote state is created by environments/global bootstrap.
# Do not initialize this backend until the bootstrap bucket and lock table exist.

terraform {
  backend "s3" {
    bucket       = "ai-livestream-tfstate-191918535424"
    key          = "prod/terraform.tfstate"
    region       = "ap-northeast-2"
    encrypt      = true
    use_lockfile = true
  }
}
