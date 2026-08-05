---
paths:
  - "services/product/backend_service/src/backend/application/db/**"
  - "services/product/backend_service/src/backend/db/**"
  - "infra/modules/database/**"
---

# Database — runtime DB (Postgres, raw SQL + asyncpg, no ORM/migration framework)

- This project uses raw SQL (`services/product/backend_service/src/backend/db/sql/runtime_schema.sql`) + asyncpg, NOT an ORM/migration tool. Treat that file as the schema source of truth.
- Schema changes are additive and reviewed — no migration framework tracks versions, so coordinate schema edits explicitly and re-`apply_schema` on dev.
- Never concatenate user input into SQL. Use asyncpg parameterized queries (`$1`, `$2`).
- Runtime DB (`/sessions/*`, `/admin/*`) is separate from business DB (`/user/*`, `/shop/*` — team SE owns those; we never touch business DB).
- `session_products` snapshot is frozen at `/attach`; never mutate it mid-livestream (replay correctness + price integrity).
- pgvector for product embeddings (Director scorer), pg_trgm for VN fuzzy. Don't add extensions ad hoc.

# Database Migrations

- **Never modify an existing migration.** Always create a new migration for changes. Existing migrations may have already run in production.
- Every migration must be reversible. Implement both up/forward and down/rollback.
- Test migrations in both directions before committing.
- Migration filenames are ordered by timestamp prefix. New migrations go at the end.
- Never use raw SQL when the ORM or migration tool provides a method for the operation.
- Never seed production data in migration files. Use dedicated seed files.
- Never drop columns or tables without first confirming the data is no longer needed.
- Add indexes in their own migration, not bundled with schema changes. Easier to roll back independently.
