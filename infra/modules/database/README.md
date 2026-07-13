# database

RDS Postgres 16 + ElastiCache Redis 7 for ai-livestream MVP.

## Resources

- `aws_db_subnet_group` on provided subnet IDs (public subnets in MVP)
- RDS Postgres 16 `db.t4g.medium` single-AZ, gp3 100GB, encrypted, `publicly_accessible=false`
- ElastiCache Redis 7 `cache.t4g.small` single-node

## Explicit non-goals

- No Multi-AZ RDS
- No Redis cluster mode / replicas
- No Secrets Manager (password via var / SSM out-of-band)

## Inputs

| Name | Default | Notes |
|------|---------|-------|
| `env` | required | `dev` / `prod` |
| `subnet_ids` | required | from network `public_subnet_ids` |
| `rds_sg_id` | required | security module |
| `redis_sg_id` | required | security module |
| `db_password` | required sensitive | `TF_VAR_db_password` or `*.tfvars.local` |
| `instance_class` | `db.t4g.medium` | |
| `allocated_storage_gb` | `100` | gp3 |
| `publicly_accessible` | `false` | IRON rule |
| `redis_node_type` | `cache.t4g.small` | |

## Outputs

`rds_endpoint`, `rds_address`, `rds_port`, `rds_db_name`, `redis_endpoint`, `redis_port`, `redis_connection_string`

## Usage

```hcl
module "database" {
  source               = "../../modules/database"
  env                  = var.env
  subnet_ids           = module.network.public_subnet_ids
  rds_sg_id            = module.security.sg_rds_id
  redis_sg_id          = module.security.sg_redis_id
  db_password          = var.db_password
  publicly_accessible  = false
  instance_class       = "db.t4g.medium"
  allocated_storage_gb = 100
  multi_az             = false
}
```

## Notes

- RDS subnet groups require subnets in **>=2 AZs**. Network MVP ships 1 public subnet — add a second AZ subnet before `apply`, or apply will fail with `DBSubnetGroupDoesNotCoverEnoughAZs`.
- Password lifecycle ignores drift so out-of-band rotation via SSM/CLI does not force replace.
