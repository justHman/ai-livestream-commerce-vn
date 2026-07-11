output "github_oidc_provider_arn" {
  description = "GitHub OIDC provider ARN (null if disabled)"
  value       = try(aws_iam_openid_connect_provider.github[0].arn, null)
}

output "tfstate_bucket_id" {
  value = try(aws_s3_bucket.tfstate[0].id, null)
}

output "tf_lock_table_name" {
  value = try(aws_dynamodb_table.tf_lock[0].name, null)
}
