# Redis platform ownership

Redis is an optional session-store backend selected with `SESSION_STORE=redis` and configured through `REDIS_URL`. Managed ElastiCache provisioning remains in `infra/modules/database/`; local development uses an official Redis image.

The default local URL is `redis://localhost:6379/0`. No product code or application data schema belongs here.
