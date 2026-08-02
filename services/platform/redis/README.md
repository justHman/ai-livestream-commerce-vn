# Redis platform ownership

Redis is an optional session-store backend selected with `SESSION_STORE=redis` and configured through `REDIS_URL`. Managed ElastiCache provisioning remains in `infra/modules/database/`; local development uses an official Redis image.

Local smoke target:

```bash
REDIS_URL=redis://127.0.0.1:6379/0 services/platform/redis/smoke.sh
```

The default local URL is `redis://localhost:6379/0`. No product code or application data schema belongs here.
