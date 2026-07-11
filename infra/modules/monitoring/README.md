# monitoring

CloudWatch log groups, SNS alerts, and billing alarms for ai-livestream MVP.

## Resources

- Log groups: `/ecs/{project}-{env}/{backend,llm,tts,avatar,livekit,lmcache}`
- SNS topic `{project}-{env}-alerts` (+ optional email subscription)
- Billing alarms at $50 and $100 EstimatedCharges (toggle with `enable_billing_alarms`)

## Billing alarms caveat

`AWS/Billing` metrics exist **only in us-east-1**. Before alarms work:

1. Account → Billing preferences → enable **Receive Billing Alerts**
2. Root must call this module with a provider in `us-east-1`, **or** create billing alarms from `environments/global` in us-east-1

If root provider is only `ap-northeast-2`, either set `enable_billing_alarms = false` here and create alarms in global, or pass a provider alias.

## Inputs

| Name | Default |
|------|---------|
| `env` | required |
| `log_retention_days` | `14` |
| `service_log_groups` | backend/llm/tts/avatar/livekit/lmcache |
| `alert_email` | `""` (no subscription) |
| `billing_alarm_thresholds` | `[50, 100]` |
| `enable_billing_alarms` | `true` |

## Outputs

`log_group_names`, `log_group_arns`, `sns_topic_arn`, `billing_alarm_names`

## Usage

```hcl
module "monitoring" {
  source      = "../../modules/monitoring"
  env         = var.env
  alert_email = var.alert_email
  # providers = { aws = aws.us_east_1 }  # if billing alarms enabled from Seoul root
}
```
