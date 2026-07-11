output "vpc_id" {
  description = "VPC ID"
  value       = aws_vpc.this.id
}

output "vpc_cidr_block" {
  description = "VPC CIDR"
  value       = aws_vpc.this.cidr_block
}

output "public_subnet_id" {
  description = "Single public subnet ID"
  value       = aws_subnet.public.id
}

output "public_subnet_ids" {
  description = "Public subnet IDs (list for downstream modules)"
  value       = [aws_subnet.public.id]
}

output "public_route_table_id" {
  description = "Public route table ID"
  value       = aws_route_table.public.id
}

output "internet_gateway_id" {
  description = "Internet gateway ID"
  value       = aws_internet_gateway.this.id
}

output "s3_gateway_endpoint_id" {
  description = "S3 Gateway VPC endpoint ID"
  value       = aws_vpc_endpoint.s3.id
}

output "availability_zone" {
  description = "AZ of the public subnet"
  value       = aws_subnet.public.availability_zone
}
