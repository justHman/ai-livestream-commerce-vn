# Postgres platform ownership

Postgres is used by the control plane through `DATABASE_URL` when configured. Managed RDS provisioning remains in `infra/modules/database/`; this directory contains no application SQL or product package.

The runtime schema remains owned by the backend at `core/sql/runtime_schema.sql`. Apply it through the backend's existing database lifecycle, not by copying it into this platform directory.
