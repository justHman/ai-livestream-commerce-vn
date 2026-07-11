output "bucket_id" {
  description = "Assets S3 bucket name"
  value       = aws_s3_bucket.assets.id
}

output "bucket_arn" {
  description = "Assets S3 bucket ARN"
  value       = aws_s3_bucket.assets.arn
}

output "bucket_regional_domain_name" {
  description = "Regional domain name"
  value       = aws_s3_bucket.assets.bucket_regional_domain_name
}

output "weights_uri" {
  description = "s3 URI for model weights prefix"
  value       = "s3://${aws_s3_bucket.assets.id}/weights/"
}

output "idle_frames_uri" {
  description = "s3 URI for idle frames prefix"
  value       = "s3://${aws_s3_bucket.assets.id}/idle-frames/"
}

output "replays_uri" {
  description = "s3 URI for replays prefix"
  value       = "s3://${aws_s3_bucket.assets.id}/replays/"
}

output "prefixes" {
  description = "Logical object prefixes in the assets bucket"
  value       = local.prefixes
}
