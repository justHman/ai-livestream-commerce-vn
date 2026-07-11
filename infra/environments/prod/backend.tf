# Remote state — same bucket as dev, different key.
# Bootstrap: see environments/dev/README.md or environments/global.

terraform {
  # backend "s3" {
  #   bucket         = "ai-livestream-tfstate"
  #   key            = "env:/prod/terraform.tfstate"
  #   region         = "ap-northeast-2"
  #   dynamodb_table = "ai-livestream-tf-lock"
  #   encrypt        = true
  # }
}
