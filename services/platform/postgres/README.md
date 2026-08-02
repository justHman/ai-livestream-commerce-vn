# Postgres platform ownership

Postgres is used by the control plane through `DATABASE_URL` when configured. Managed RDS provisioning remains in `infra/modules/database/`; this directory contains no application SQL or product package.

The current compatibility schema source is `core/sql/runtime_schema.sql`. Task 1.23 moves its ownership to the backend `db/sql/runtime_schema.sql` package. Apply the current schema through the backend lifecycle; do not copy it into this platform directory.
