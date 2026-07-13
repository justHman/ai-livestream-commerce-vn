# Remote state created by environments/global bootstrap.

terraform {
  backend "s3" {
    bucket         = "ai-livestream-tfstate-191918535424"
    key            = "dev/terraform.tfstate"
    region         = "ap-northeast-2"
    dynamodb_table = "ai-livestream-tf-lock"
    encrypt        = true
  }
}
