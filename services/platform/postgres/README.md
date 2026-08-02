# Postgres platform ownership

Postgres is used by the control plane through `DATABASE_URL` when configured. Managed RDS provisioning remains in `infra/modules/database/`; this directory contains no application SQL or product package.

The runtime schema's canonical target is the backend `db/sql/runtime_schema.sql` package. Apply the current staged compatibility schema through the backend lifecycle; do not copy it into this platform directory.
