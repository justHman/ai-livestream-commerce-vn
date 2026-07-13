# AWS Pricing Seoul — Final Validation Report

- Region: `ap-northeast-2` / `Asia Pacific (Seoul)`
- Validated (UTC): `2026-07-11T10:43:03.502121+00:00`
- Sources: `aws ec2 describe-spot-price-history` + `aws pricing get-products`
- Sheet: `aws-pricing-seoul.xlsx`
- Result: **PASS=45 FAIL=0**

## Live quotes

| Key | Live value |
|---|---:|
| `alb.hourly` | 0.0225 |
| `alb.lcu` | 0.008 |
| `cw.logs.ingest_gb` | 0.76 |
| `dt.out.first10tb` | 0.126 |
| `ec2.c7g.2xlarge.od` | 0.3264 |
| `ec2.c7g.2xlarge.ri_1yr_hourly_eff` | 0.20532557077625574 |
| `ec2.c7g.2xlarge.ri_1yr_monthly` | 149.8876666666667 |
| `ec2.c7g.2xlarge.ri_3yr_hourly_eff` | 0.13738340943683408 |
| `ec2.c7g.2xlarge.ri_3yr_monthly` | 100.28988888888888 |
| `ec2.g4dn.xlarge.od` | 0.647 |
| `ec2.g4dn.xlarge.ri_1yr_hourly_eff` | 0.38772146118721457 |
| `ec2.g4dn.xlarge.ri_1yr_monthly` | 283.03666666666663 |
| `ec2.g4dn.xlarge.ri_3yr_hourly_eff` | 0.2584140030441401 |
| `ec2.g4dn.xlarge.ri_3yr_monthly` | 188.64222222222224 |
| `ec2.g6.xlarge.od` | 0.9896 |
| `ec2.g6.xlarge.ri_1yr_hourly_eff` | 0.6135151598173516 |
| `ec2.g6.xlarge.ri_1yr_monthly` | 447.86606666666665 |
| `ec2.g6.xlarge.ri_3yr_hourly_eff` | 0.42056397260273976 |
| `ec2.g6.xlarge.ri_3yr_monthly` | 307.0117 |
| `ecr.storage_gbmo` | 0.1 |
| `fargate.arm.gb` | 0.00409 |
| `fargate.arm.vcpu` | 0.03725 |
| `fargate.backend.od` | 0.04543 |
| `fargate.backend.spot_est` | 0.013628999999999999 |
| `fargate.livekit.od` | 0.09086 |
| `fargate.livekit.spot_est` | 0.027257999999999998 |
| `nat.hourly` | 0.059 |
| `nat.per_gb` | 0.059 |
| `rds.gp3.ma_gbmo` | 0.262 |
| `rds.gp3.sa_gbmo` | 0.131 |
| `rds.t4g.medium.ma.od` | 0.203 |
| `rds.t4g.medium.ma.ri_1yr_hourly_eff` | 0.1504283105022831 |
| `rds.t4g.medium.ma.ri_1yr_monthly` | 109.81266666666667 |
| `rds.t4g.medium.ma.ri_3yr_hourly_eff` | 0.10159908675799087 |
| `rds.t4g.medium.ma.ri_3yr_monthly` | 74.16733333333333 |
| `rds.t4g.medium.sa.od` | 0.102 |
| `rds.t4g.medium.sa.ri_1yr_hourly_eff` | 0.07515707762557078 |
| `rds.t4g.medium.sa.ri_1yr_monthly` | 54.864666666666665 |
| `rds.t4g.medium.sa.ri_3yr_hourly_eff` | 0.050818569254185694 |
| `rds.t4g.medium.sa.ri_3yr_monthly` | 37.09755555555556 |
| `redis.t4g.small.od` | 0.047 |
| `route53.hosted_zone_mo` | 0.5 |
| `s3.standard.first50tb` | 0.025 |
| `secrets.per_secret_mo` | 0.4 |
| `spot.c7g.2xlarge.avg` | 0.1282047619047619 |
| `spot.c7g.2xlarge.max` | 0.1361 |
| `spot.c7g.2xlarge.median` | 0.1332 |
| `spot.c7g.2xlarge.min` | 0.1124 |
| `spot.c7g.2xlarge.n` | 21 |
| `spot.g4dn.xlarge.avg` | 0.24288125 |
| `spot.g4dn.xlarge.max` | 0.2506 |
| `spot.g4dn.xlarge.median` | 0.2403 |
| `spot.g4dn.xlarge.min` | 0.239 |
| `spot.g4dn.xlarge.n` | 16 |
| `spot.g6.xlarge.avg` | 0.3029625 |
| `spot.g6.xlarge.max` | 0.3581 |
| `spot.g6.xlarge.median` | 0.316 |
| `spot.g6.xlarge.min` | 0.2277 |
| `spot.g6.xlarge.n` | 16 |

## Checks

| Status | Item | Sheet | Live | Delta | Note |
|---|---|---:|---:|---:|---|
| PASS | MVP g6 Spot 1h | 0.30204 | 0.3029625 | -0.000923 | Spot volatile; 5% band |
| PASS | MVP g4dn Spot 1h | 0.242881 | 0.24288125 | -0.000000 | Spot volatile; 5% band |
| PASS | MVP c7g Spot 1h | 0.129364 | 0.1282047619047619 | +0.001159 | Spot volatile; 5% band |
| PASS | PROD g6 OD 1h | 0.9896 | 0.9896 | +0.000000 |  |
| PASS | PROD g4dn OD 1h | 0.647 | 0.647 | +0.000000 |  |
| PASS | PROD c7g OD 1h | 0.3264 | 0.3264 | +0.000000 |  |
| PASS | RI g6 1y monthly | 447.87 | 447.86606666666665 | +0.003933 |  |
| PASS | RI g6 3y monthly | 307.01 | 307.0117 | -0.001700 |  |
| PASS | RI g4dn 1y monthly | 283.04 | 283.03666666666663 | +0.003333 |  |
| PASS | RI g4dn 3y monthly | 188.64 | 188.64222222222224 | -0.002222 |  |
| PASS | RI c7g 1y monthly | 149.89 | 149.8876666666667 | +0.002333 |  |
| PASS | RI c7g 3y monthly | 100.29 | 100.28988888888888 | +0.000111 |  |
| PASS | Backend Fargate OD | 0.04543 | 0.04543 | +0.000000 |  |
| PASS | LiveKit Fargate OD | 0.09086 | 0.09086 | +0.000000 |  |
| PASS | Backend Fargate Spot est | 0.013629 | 0.013628999999999999 | +0.000000 | 0.3*OD assumption |
| PASS | LiveKit Fargate Spot est | 0.027258 | 0.027257999999999998 | +0.000000 | 0.3*OD assumption |
| PASS | RDS SA OD | 0.102 | 0.102 | +0.000000 |  |
| PASS | RDS MA OD | 0.203 | 0.203 | +0.000000 |  |
| PASS | RDS SA RI1y monthly | 54.86 | 54.864666666666665 | -0.004667 |  |
| PASS | RDS SA RI3y monthly | 37.1 | 37.09755555555556 | +0.002444 |  |
| PASS | RDS MA RI1y monthly | 109.81 | 109.81266666666667 | -0.002667 |  |
| PASS | RDS MA RI3y monthly | 74.17 | 74.16733333333333 | +0.002667 |  |
| PASS | RDS gp3 SA /GB-mo | 0.131 | 0.131 | +0.000000 |  |
| PASS | RDS gp3 MA /GB-mo | 0.262 | 0.262 | +0.000000 |  |
| PASS | gp3 SA 100GB monthly | 13.1 | 13.100000000000001 | -0.000000 |  |
| PASS | gp3 MA 100GB monthly | 26.2 | 26.200000000000003 | -0.000000 |  |
| PASS | Redis t4g.small OD | 0.047 | 0.047 | +0.000000 |  |
| PASS | Redis MultiAZ x2 | 0.094 | 0.094 | +0.000000 |  |
| PASS | ALB hourly | 0.0225 | 0.0225 | +0.000000 |  |
| PASS | ALB LCU | 0.008 | 0.008 | +0.000000 |  |
| PASS | ALB LCU*5 | 0.04 | 0.04 | +0.000000 |  |
| PASS | ALB LCU*15 | 0.12 | 0.12 | +0.000000 |  |
| PASS | S3 first50TB | 0.025 | 0.025 | +0.000000 |  |
| PASS | S3 50GB monthly | 1.25 | 1.25 | +0.000000 |  |
| PASS | S3 150GB monthly | 3.75 | 3.75 | +0.000000 |  |
| PASS | DT out first10TB | 0.126 | 0.126 | +0.000000 |  |
| PASS | DT 50GB monthly | 6.3 | 6.3 | +0.000000 |  |
| PASS | DT 200GB monthly | 25.2 | 25.2 | +0.000000 |  |
| PASS | CW logs ingest /GB | 0.76 | 0.76 | +0.000000 |  |
| PASS | CW 3GB monthly | 2.28 | 2.2800000000000002 | -0.000000 |  |
| PASS | CW 10GB monthly | 7.6 | 7.6 | +0.000000 |  |
| PASS | NAT hourly | 0.059 | 0.059 | +0.000000 |  |
| PASS | ECR storage /GB-mo | 0.1 | 0.1 | +0.000000 |  |
| PASS | Secrets Manager /secret-mo | 0.4 | 0.4 | +0.000000 |  |
| PASS | Route53 hosted zone | 0.5 | 0.5 | +0.000000 |  |

## Notes

1. Spot prices are market-dynamic; sheet uses 24h average. Tolerance band = 5%.
2. Fargate Spot is estimated as 0.3 * Fargate OD (AWS publishes 'up to 70% off'; exact Spot rate is dynamic and not in Pricing API the same way).
3. reserved_* for non-RI services equals operating 1h/1d/1m by design.
4. Month factor = 730 hours.
