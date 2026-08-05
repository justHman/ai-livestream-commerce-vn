# Postgres platform ownership

Postgres is used by the control plane through `DATABASE_URL` when configured.
Managed RDS provisioning remains in `infra/modules/database/`; this directory
contains no application SQL or product package.

## Official local image (dev + ephemeral smoke)

Use the official upstream image only — no custom production DB image:

```bash
docker run --rm --name ailive-pg \
  -e POSTGRES_DB=ailive -e POSTGRES_USER=ailive -e POSTGRES_PASSWORD=dev \
  -p 5432:5432 \
  postgres:16.4-alpine@sha256:5660c2cbfea50c7a9127d17dc4e48543eedd3d7a41a595a2dfa572471e37e64c
```

## Local smoke

```bash
# Requires a running local Postgres (above). Validates TCP readiness only.
python - <<'PY'
import socket, sys
s = socket.socket()
s.settimeout(3)
try:
    s.connect(("127.0.0.1", 5432))
except OSError as error:
    sys.exit(f"postgres smoke FAIL: {error}")
finally:
    s.close()
print("postgres smoke OK")
PY
```

## Managed RDS (staging/prod) and dev defaults

- Dev: `create_rds=false` by default; memory sessions default; opt-in test
  paths explicit.
- Staging/prod: managed RDS with `publicly_accessible=false`, SG ingress from
  backend only, `storage_encrypted=true`, auth via master password (SSM/TF_VAR,
  never committed), `backup_retention_days` per env, `deletion_protection`
  true in prod.
- No public address exposure; `DATABASE_URL` is injected as an ECS secret from
  SSM, never plaintext in Terraform.

## SQL ownership

Backend owns `services/product/backend_service/src/backend/db/sql/runtime_schema.sql` and applies it through its existing
lifecycle. Do not copy it into this platform directory.