# network

VPC foundation for ai-livestream MVP (public-subnet only).

## Resources

- VPC + DNS hostnames/support
- 1 public subnet (default AZ `ap-northeast-2a`)
- Internet Gateway + public route table (`0.0.0.0/0` → IGW)
- S3 Gateway VPC Endpoint (free private S3 path)
- Optional VPC Flow Logs

## Explicit non-goals

- No NAT Gateway
- No private subnets
- No Interface VPC Endpoints

## Inputs

| Name | Default | Notes |
|------|---------|-------|
| `env` | required | `dev` / `prod` |
| `cidr_block` | `10.20.0.0/16` | VPC CIDR |
| `public_subnet_cidr` | `10.20.1.0/24` | single public subnet |
| `az` | `ap-northeast-2a` | single-AZ MVP |
| `enable_flow_logs` | `false` | opt-in |

## Outputs

`vpc_id`, `public_subnet_id`, `public_subnet_ids`, `public_route_table_id`, `s3_gateway_endpoint_id`, `internet_gateway_id`, `availability_zone`

## Usage

```hcl
module "network" {
  source     = "../../modules/network"
  env        = var.env
  cidr_block = var.vpc_cidr
  az         = "ap-northeast-2a"
}
```
