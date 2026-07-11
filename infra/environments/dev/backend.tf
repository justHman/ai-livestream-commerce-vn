# Remote state — bootstrap once (global env or manual):
#   1. Create S3 bucket: ai-livestream-tfstate (versioning ON, SSE AES256, public access blocked)
#   2. Create DynamoDB table: ai-livestream-tf-lock (partition key LockID String)
#   3. Uncomment the backend block below and re-run: terraform init -migrate-state
#
# Until bootstrap exists, local state is fine for dry-runs.

terraform {
  # backend "s3" {
  #   bucket         = "ai-livestream-tfstate"
  #   key            = "env:/dev/terraform.tfstate"
  #   region         = "ap-northeast-2"
  #   dynamodb_table = "ai-livestream-tf-lock"
  #   encrypt        = true
  # }
}
