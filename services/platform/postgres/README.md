# Postgres platform ownership

Postgres is used by the control plane through `DATABASE_URL` when configured. Managed RDS provisioning remains in `infra/modules/database/`; this directory contains no application SQL or product package.

The backend currently owns `core/sql/runtime_schema.sql` and applies it through its existing lifecycle. Do not copy it into this platform directory.
