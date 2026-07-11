# Account-wide state (OIDC, shared tfstate bucket resources if managed here).

terraform {
  # backend "s3" {
  #   bucket         = "ai-livestream-tfstate"
  #   key            = "env:/global/terraform.tfstate"
  #   region         = "ap-northeast-2"
  #   dynamodb_table = "ai-livestream-tf-lock"
  #   encrypt        = true
  # }
}
