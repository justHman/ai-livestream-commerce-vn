"""SQL-backed repositories for Script Authoring (Change B, tasks 2.4-2.8).

Raw SQL + asyncpg, mirroring ``application/db/postgres_store.py`` conventions
(own pool, ``apply_schema`` owns the schema, parameterized ``$N`` queries).
Immutability: ``script_versions`` / ``script_segments`` rows are never
UPDATE'd — only inserted. The mutable current/approved pointers live on
``script_items`` and change via revision-guarded UPDATEs.

Transaction ownership: the service opens transactions for multi-write atomic
units; every repo method accepts ``conn`` and uses it when given, otherwise
acquires from the shared pool.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from backend.application.script_authoring.generation.batch import BatchState
from backend.application.script_authoring.models import (
    Approval,
    GateRun,
    GenerationBatch,
    GenerationJob,
    ProductScriptPlan,
    ScriptItem,
    ScriptSegment,
    ScriptSet,
    ScriptVersion,
)

_CONNECT_TIMEOUT_SECONDS = 5.0
_COMMAND_TIMEOUT_SECONDS = 5.0

# Columns shared by reads; every model's JSONB fields are round-tripped via
# json.dumps so asyncpg never has to guess a jsonb encode for a bare dict.
_SET_COLS = (
    "id, shop_id, title, brief::text AS brief, product_ids::text AS product_ids, "
    "session_id, revision, created_at, updated_at"
)
_ITEM_COLS = (
    "id, script_set_id, product_id, state, source, current_version_id, "
    "approved_version_id, intent, revision, created_at, updated_at"
)
_VERSION_COLS = (
    "id, script_item_id, version, state, source, display_text, spoken_text, "
    "text_hash, segment_version_ids::text AS segment_version_ids, plan_version, "
    "gate_run_id, fingerprint::text AS fingerprint, created_at"
)
_PLAN_COLS = (
    "id, script_item_id, version, product_id, target_duration_s, segment_count, "
    "fingerprint, created_at"
)
_SEGMENT_COLS = (
    "id, script_item_id, plan_id, segment_index, title, intent, target_duration_s, "
    "display_text, spoken_text, status, version, created_at"
)
_GATE_COLS = (
    "id, script_item_id, is_full AS full, passed, rule_set_fingerprint, "
    "script_version_id, violations::text AS violations, created_at"
)
_APPROVAL_COLS = (
    "id, script_item_id, script_version_id, actor, approval_hash, gate_run_id, "
    "dependencies::text AS dependencies, created_at"
)
_BATCH_COLS = (
    "id, script_set_id, status, product_ids::text AS product_ids, "
    "job_ids::text AS job_ids, estimated_semantic_calls, idempotency_key, "
    "revision, state::text AS state, created_at, updated_at"
)
_JOB_COLS = (
    "id, batch_id, script_item_id, product_id, intent, status, plan_id, "
    "plan_segment_count, current_segment_index, attempt_count, target_duration_s, "
    "fingerprint::text AS fingerprint, idempotency_key, created_at, updated_at"
)

# Plain column lists for INSERT (no casts / aliases).
_SET_INS = "id, shop_id, title, brief, product_ids, session_id, revision, created_at, updated_at"
_ITEM_INS = (
    "id, script_set_id, product_id, state, source, current_version_id, "
    "approved_version_id, intent, revision, created_at, updated_at"
)
_VERSION_INS = (
    "id, script_item_id, version, state, source, display_text, spoken_text, "
    "text_hash, segment_version_ids, plan_version, gate_run_id, fingerprint, "
    "created_at"
)
_PLAN_INS = (
    "id, script_item_id, version, product_id, target_duration_s, segment_count, "
    "fingerprint, created_at"
)
_SEGMENT_INS = (
    "id, script_item_id, plan_id, segment_index, title, intent, "
    "target_duration_s, display_text, spoken_text, status, version, created_at"
)
_GATE_INS = (
    "id, script_item_id, is_full, passed, rule_set_fingerprint, "
    "script_version_id, violations, created_at"
)
_APPROVAL_INS = (
    "id, script_item_id, script_version_id, actor, approval_hash, "
    "gate_run_id, dependencies, created_at"
)
_BATCH_INS = (
    "id, script_set_id, status, product_ids, job_ids, estimated_semantic_calls, "
    "idempotency_key, revision, state, created_at, updated_at"
)
_JOB_INS = (
    "id, batch_id, script_item_id, product_id, intent, status, plan_id, "
    "plan_segment_count, current_segment_index, attempt_count, target_duration_s, "
    "fingerprint, idempotency_key, created_at, updated_at"
)


class StaleRevisionError(Exception):
    """Raised when an optimistic-lock UPDATE matches zero rows."""


def _iso(ts: Any) -> str:
    return ts.isoformat() if isinstance(ts, datetime) else str(ts)


def _dts(iso_value: str | None) -> datetime | None:
    return datetime.fromisoformat(iso_value) if iso_value else None


def _json_rows(col_value: str | None, default: Any) -> Any:
    return json.loads(col_value) if col_value is not None else default


class _Repo:
    """Shared pool access for the per-aggregate repositories."""

    def __init__(self, parent: "PostgresAuthoringRepositories") -> None:
        self._parent = parent

    def _pool(self):
        if self._parent._pool is None:  # noqa: SLF001
            raise RuntimeError("PostgresAuthoringRepositories not connected")
        return self._parent._pool

    async def _acquire(self, conn):
        if conn is not None:
            return conn
        return await self._pool().acquire()

    async def _release(self, conn, acquired: bool) -> None:
        if acquired:
            await self._pool().release(conn)

    async def _command(self, coro):
        try:
            return await asyncio.wait_for(coro(), timeout=_COMMAND_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise exc

    async def _fetchone(self, sql: str, *args, conn=None):
        acquired = conn is None
        c = await self._acquire(conn)
        try:
            row = await self._command(lambda: c.fetchrow(sql, *args))
            return row
        finally:
            await self._release(c, acquired)

    async def _fetchall(self, sql: str, *args, conn=None):
        acquired = conn is None
        c = await self._acquire(conn)
        try:
            rows = await self._command(lambda: c.fetch(sql, *args))
            return rows
        finally:
            await self._release(c, acquired)

    async def _execute(self, sql: str, *args, conn=None):
        acquired = conn is None
        c = await self._acquire(conn)
        try:
            return await self._command(lambda: c.execute(sql, *args))
        finally:
            await self._release(c, acquired)


class ScriptSetRepository(_Repo):
    async def insert(self, set_: ScriptSet, *, conn=None) -> None:
        await self._execute(
            f"INSERT INTO script_sets ({_SET_INS}) "
            f"VALUES ($1,$2,$3,$4::jsonb,$5::jsonb,$6,$7,$8,$9)",
            set_.id,
            set_.shop_id,
            set_.title,
            json.dumps(set_.brief.model_dump()),
            json.dumps(set_.product_ids),
            set_.session_id,
            set_.revision,
            _dts(set_.created_at),
            _dts(set_.updated_at),
            conn=conn,
        )

    async def get(self, set_id: str, *, conn=None) -> ScriptSet | None:
        row = await self._fetchone(
            f"SELECT {_SET_COLS} FROM script_sets WHERE id = $1", set_id, conn=conn
        )
        if row is None:
            return None
        data = dict(row)
        data["brief"] = _json_rows(data["brief"], {})
        data["product_ids"] = _json_rows(data["product_ids"], [])
        data["created_at"] = _iso(data["created_at"])
        data["updated_at"] = _iso(data["updated_at"])
        return ScriptSet.model_validate(data)

    async def update(self, set_: ScriptSet, *, expected_revision: int, conn=None) -> None:
        acquired = conn is None
        c = await self._acquire(conn)
        try:
            status = await self._command(
                lambda: c.execute(
                    "UPDATE script_sets SET title=$2, brief=$3::jsonb, product_ids=$4::jsonb, "
                    "session_id=$5, revision=revision+1, updated_at=NOW() "
                    "WHERE id=$1 AND revision=$6 RETURNING id",
                    set_.id,
                    set_.title,
                    json.dumps(set_.brief.model_dump()),
                    json.dumps(set_.product_ids),
                    set_.session_id,
                    expected_revision,
                )
            )
        finally:
            await self._release(c, acquired)
        if status != "UPDATE 1":
            raise StaleRevisionError(
                f"script_set {set_.id}: revision {expected_revision} not current"
            )


class ScriptItemRepository(_Repo):
    async def insert(self, item: ScriptItem, *, conn=None) -> None:
        await self._execute(
            f"INSERT INTO script_items ({_ITEM_INS}) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
            item.id,
            item.script_set_id,
            item.product_id,
            item.state.value,
            item.source.value if item.source else None,
            item.current_version_id,
            item.approved_version_id,
            item.intent.value if item.intent else None,
            item.revision,
            _dts(item.created_at),
            _dts(item.updated_at),
            conn=conn,
        )

    async def get(self, item_id: str, *, conn=None) -> ScriptItem | None:
        row = await self._fetchone(
            f"SELECT {_ITEM_COLS} FROM script_items WHERE id = $1", item_id, conn=conn
        )
        return self._from_row(row)

    async def get_by_product(self, set_id: str, product_id: str, *, conn=None) -> ScriptItem | None:
        row = await self._fetchone(
            f"SELECT {_ITEM_COLS} FROM script_items WHERE script_set_id = $1 AND product_id = $2",
            set_id,
            product_id,
            conn=conn,
        )
        return self._from_row(row)

    async def list_by_set(self, set_id: str, *, conn=None) -> list[ScriptItem]:
        rows = await self._fetchall(
            f"SELECT {_ITEM_COLS} FROM script_items WHERE script_set_id = $1",
            set_id,
            conn=conn,
        )
        return [self._from_row(r) for r in rows]

    async def update(self, item: ScriptItem, *, expected_revision: int, conn=None) -> None:
        acquired = conn is None
        c = await self._acquire(conn)
        try:
            status = await self._command(
                lambda: c.execute(
                    "UPDATE script_items SET state=$2, source=$3, current_version_id=$4, "
                    "approved_version_id=$5, intent=$6, revision=revision+1, updated_at=NOW() "
                    "WHERE id=$1 AND revision=$7 RETURNING id",
                    item.id,
                    item.state.value,
                    item.source.value if item.source else None,
                    item.current_version_id,
                    item.approved_version_id,
                    item.intent.value if item.intent else None,
                    expected_revision,
                )
            )
        finally:
            await self._release(c, acquired)
        if status != "UPDATE 1":
            raise StaleRevisionError(
                f"script_item {item.id}: revision {expected_revision} not current"
            )

    @staticmethod
    def _from_row(row) -> ScriptItem | None:
        if row is None:
            return None
        data = dict(row)
        data["created_at"] = _iso(data["created_at"])
        data["updated_at"] = _iso(data["updated_at"])
        return ScriptItem.model_validate(data)


class PlanRepository(_Repo):
    async def insert(self, plan: ProductScriptPlan, *, conn=None) -> None:
        acquired = conn is None
        c = await self._acquire(conn)
        try:
            async with c.transaction() if acquired else _null_transaction():
                await c.execute(
                    f"INSERT INTO product_script_plans ({_PLAN_INS}) "
                    f"VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                    plan.id,
                    plan.script_item_id,
                    plan.version,
                    plan.product_id,
                    plan.target_duration_s,
                    plan.segment_count,
                    plan.fingerprint,
                    _dts(plan.created_at),
                )
                for segment in plan.segments:
                    await self._insert_segment(c, segment)
        finally:
            await self._release(c, acquired)

    @staticmethod
    async def _insert_segment(c, segment: ScriptSegment) -> None:
        await c.execute(
            f"INSERT INTO script_segments ({_SEGMENT_INS}) "
            f"VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
            segment.id,
            segment.script_item_id,
            segment.plan_id,
            segment.segment_index,
            segment.title,
            segment.intent,
            segment.target_duration_s,
            segment.display_text,
            segment.spoken_text,
            segment.status.value,
            segment.version,
            _dts(segment.created_at),
        )

    async def get(self, plan_id: str, *, conn=None) -> ProductScriptPlan | None:
        row = await self._fetchone(
            f"SELECT {_PLAN_COLS} FROM product_script_plans WHERE id = $1", plan_id, conn=conn
        )
        if row is None:
            return None
        return await self._with_segments(row, conn=conn)

    async def get_latest(self, item_id: str, *, conn=None) -> ProductScriptPlan | None:
        row = await self._fetchone(
            f"SELECT {_PLAN_COLS} FROM product_script_plans "
            "WHERE script_item_id = $1 ORDER BY version DESC LIMIT 1",
            item_id,
            conn=conn,
        )
        if row is None:
            return None
        return await self._with_segments(row, conn=conn)

    async def _with_segments(self, row, *, conn=None) -> ProductScriptPlan:
        rows = await self._fetchall(
            "SELECT id, script_item_id, plan_id, segment_index, title, intent, "
            "target_duration_s, display_text, spoken_text, status, version, created_at "
            "FROM script_segments WHERE plan_id = $1 ORDER BY segment_index",
            row["id"],
            conn=conn,
        )
        data = dict(row)
        data["created_at"] = _iso(data["created_at"])
        data["K"] = data.pop("segment_count")
        data["segments"] = [self._segment_from_row(r) for r in rows]
        return ProductScriptPlan.model_validate(data)

    @staticmethod
    def _segment_from_row(row) -> ScriptSegment:
        data = dict(row)
        data["created_at"] = _iso(data["created_at"])
        return ScriptSegment.model_validate(data)


class SegmentRepository(_Repo):
    async def insert(self, segment: ScriptSegment, *, conn=None) -> None:
        acquired = conn is None
        c = await self._acquire(conn)
        try:
            await self._insert_segment(c, segment)
        finally:
            await self._release(c, acquired)

    @staticmethod
    async def _insert_segment(c, segment: ScriptSegment) -> None:
        await c.execute(
            f"INSERT INTO script_segments ({_SEGMENT_INS}) "
            f"VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
            segment.id,
            segment.script_item_id,
            segment.plan_id,
            segment.segment_index,
            segment.title,
            segment.intent,
            segment.target_duration_s,
            segment.display_text,
            segment.spoken_text,
            segment.status.value,
            segment.version,
            _dts(segment.created_at),
        )

    async def get(self, segment_id: str, *, conn=None) -> ScriptSegment | None:
        row = await self._fetchone(
            f"SELECT {_SEGMENT_COLS} FROM script_segments WHERE id = $1", segment_id, conn=conn
        )
        return self._from_row(row)

    async def list_by_plan(self, plan_id: str, *, conn=None) -> list[ScriptSegment]:
        rows = await self._fetchall(
            f"SELECT {_SEGMENT_COLS} FROM script_segments WHERE plan_id = $1 ORDER BY segment_index",
            plan_id,
            conn=conn,
        )
        return [self._from_row(r) for r in rows]

    async def list_selected(self, segment_ids: list[str], *, conn=None) -> list[ScriptSegment]:
        if not segment_ids:
            return []
        rows = await self._fetchall(
            f"SELECT {_SEGMENT_COLS} FROM script_segments WHERE id = ANY($1::text[]) "
            "ORDER BY segment_index",
            segment_ids,
            conn=conn,
        )
        return [self._from_row(r) for r in rows]

    @staticmethod
    def _from_row(row) -> ScriptSegment | None:
        if row is None:
            return None
        data = dict(row)
        data["created_at"] = _iso(data["created_at"])
        return ScriptSegment.model_validate(data)


class VersionRepository(_Repo):
    async def insert(self, version: ScriptVersion, *, conn=None) -> None:
        await self._execute(
            f"INSERT INTO script_versions ({_VERSION_INS}) "
            f"VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12::jsonb,$13)",
            version.id,
            version.script_item_id,
            version.version,
            version.state.value,
            version.source.value,
            version.display_text,
            version.spoken_text,
            version.text_hash,
            json.dumps(version.segment_version_ids),
            version.plan_version,
            version.gate_run_id,
            json.dumps(version.fingerprint.model_dump()) if version.fingerprint else None,
            _dts(version.created_at),
            conn=conn,
        )

    async def get(self, version_id: str, *, conn=None) -> ScriptVersion | None:
        row = await self._fetchone(
            f"SELECT {_VERSION_COLS} FROM script_versions WHERE id = $1", version_id, conn=conn
        )
        return self._from_row(row)

    async def list_by_item(self, item_id: str, *, conn=None) -> list[ScriptVersion]:
        rows = await self._fetchall(
            f"SELECT {_VERSION_COLS} FROM script_versions WHERE script_item_id = $1 "
            "ORDER BY version",
            item_id,
            conn=conn,
        )
        return [self._from_row(r) for r in rows]

    async def get_approved(self, item_id: str, *, conn=None) -> ScriptVersion | None:
        # Qualified columns: the JOIN makes unqualified ids ambiguous.
        version_cols = ", ".join(f"v.{part.strip()}" for part in _VERSION_COLS.split(","))
        row = await self._fetchone(
            f"SELECT {version_cols} FROM script_versions v "
            "JOIN script_items i ON i.approved_version_id = v.id WHERE i.id = $1",
            item_id,
            conn=conn,
        )
        return self._from_row(row)

    @staticmethod
    def _from_row(row) -> ScriptVersion | None:
        if row is None:
            return None
        data = dict(row)
        data["segment_version_ids"] = _json_rows(data["segment_version_ids"], [])
        data["fingerprint"] = _json_rows(data["fingerprint"], None)
        data["created_at"] = _iso(data["created_at"])
        return ScriptVersion.model_validate(data)


class GateRunRepository(_Repo):
    async def insert(self, run: GateRun, *, conn=None) -> None:
        await self._execute(
            f"INSERT INTO script_gate_runs ({_GATE_INS}) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8)",
            run.id,
            run.script_item_id,
            run.full,
            run.passed,
            run.rule_set_fingerprint,
            run.script_version_id,
            json.dumps([v.model_dump() for v in run.violations]),
            _dts(run.created_at),
            conn=conn,
        )

    async def get(self, run_id: str, *, conn=None) -> GateRun | None:
        row = await self._fetchone(
            f"SELECT {_GATE_COLS} FROM script_gate_runs WHERE id = $1", run_id, conn=conn
        )
        return self._from_row(row)

    async def list_by_item(self, item_id: str, *, conn=None) -> list[GateRun]:
        rows = await self._fetchall(
            f"SELECT {_GATE_COLS} FROM script_gate_runs WHERE script_item_id = $1 "
            "ORDER BY created_at",
            item_id,
            conn=conn,
        )
        return [self._from_row(r) for r in rows]

    async def latest_for_version(self, version_id: str, *, conn=None) -> GateRun | None:
        row = await self._fetchone(
            f"SELECT {_GATE_COLS} FROM script_gate_runs WHERE script_version_id = $1 "
            "ORDER BY created_at DESC LIMIT 1",
            version_id,
            conn=conn,
        )
        return self._from_row(row)

    @staticmethod
    def _from_row(row) -> GateRun | None:
        if row is None:
            return None
        data = dict(row)
        data["violations"] = _json_rows(data["violations"], [])
        data["created_at"] = _iso(data["created_at"])
        return GateRun.model_validate(data)


class ApprovalRepository(_Repo):
    async def insert(self, approval: Approval, *, dependencies: dict, conn=None) -> None:
        await self._execute(
            f"INSERT INTO script_approvals ({_APPROVAL_INS}) "
            f"VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8)",
            approval.id,
            approval.script_item_id,
            approval.script_version_id,
            approval.actor,
            approval.approval_hash,
            approval.gate_run_id,
            json.dumps(dependencies),
            _dts(approval.created_at),
            conn=conn,
        )

    async def get_by_item(self, item_id: str, *, conn=None) -> Approval | None:
        row = await self._fetchone(
            f"SELECT {_APPROVAL_COLS} FROM script_approvals WHERE script_item_id = $1 "
            "ORDER BY created_at DESC LIMIT 1",
            item_id,
            conn=conn,
        )
        return self._from_row(row)

    async def get(self, approval_id: str, *, conn=None) -> Approval | None:
        row = await self._fetchone(
            f"SELECT {_APPROVAL_COLS} FROM script_approvals WHERE id = $1",
            approval_id,
            conn=conn,
        )
        return self._from_row(row)

    async def recorded_dependencies(self, item_id: str, *, conn=None) -> dict:
        row = await self._fetchone(
            "SELECT dependencies::text AS dependencies FROM script_approvals "
            "WHERE script_item_id = $1 ORDER BY created_at DESC LIMIT 1",
            item_id,
            conn=conn,
        )
        return _json_rows(row["dependencies"], {}) if row else {}

    @staticmethod
    def _from_row(row) -> Approval | None:
        if row is None:
            return None
        data = dict(row)
        # `dependencies` is stored on the row but is not part of the Approval
        # domain model — it is read separately via recorded_dependencies().
        data.pop("dependencies", None)
        data["created_at"] = _iso(data["created_at"])
        return Approval.model_validate(data)


class BatchRepository(_Repo):
    async def insert(self, batch: GenerationBatch, *, state: BatchState, conn=None) -> None:
        await self._execute(
            f"INSERT INTO script_generation_batches ({_BATCH_INS}) "
            f"VALUES ($1,$2,$3,$4::jsonb,$5::jsonb,$6,$7,$8,$9::jsonb,$10,$11)",
            batch.id,
            batch.script_set_id,
            batch.status.value,
            json.dumps(batch.product_ids),
            json.dumps(batch.job_ids),
            batch.estimated_semantic_calls,
            batch.idempotency_key,
            state.revision,
            json.dumps(state.model_dump(mode="json")),
            _dts(batch.created_at),
            _dts(batch.updated_at),
            conn=conn,
        )

    async def get(self, batch_id: str, *, conn=None) -> tuple[GenerationBatch, BatchState] | None:
        row = await self._fetchone(
            f"SELECT {_BATCH_COLS} FROM script_generation_batches WHERE id = $1",
            batch_id,
            conn=conn,
        )
        if row is None:
            return None
        data = dict(row)
        data["product_ids"] = _json_rows(data["product_ids"], [])
        data["job_ids"] = _json_rows(data["job_ids"], [])
        data["created_at"] = _iso(data["created_at"])
        data["updated_at"] = _iso(data["updated_at"])
        # `revision` and `state` are not GenerationBatch model fields — they are
        # the batch-level optimistic-lock and the persisted BatchState payload.
        row_revision = data.pop("revision", None)
        state_json = _json_rows(data.pop("state", None), {})
        batch = GenerationBatch.model_validate(data)
        state = BatchState.model_validate(state_json)
        if row_revision is not None:
            state.revision = row_revision
        return batch, state

    async def update_state(
        self, batch_id: str, *, state: BatchState, expected_revision: int, conn=None
    ) -> None:
        acquired = conn is None
        c = await self._acquire(conn)
        try:
            status = await self._command(
                lambda: c.execute(
                    "UPDATE script_generation_batches SET status=$2, state=$3::jsonb, "
                    "revision=revision+1, updated_at=NOW() WHERE id=$1 AND revision=$4 RETURNING id",
                    batch_id,
                    state.status,
                    json.dumps(state.model_dump(mode="json")),
                    expected_revision,
                )
            )
        finally:
            await self._release(c, acquired)
        if status != "UPDATE 1":
            raise StaleRevisionError(f"batch {batch_id}: revision {expected_revision} not current")

    async def find_by_idempotency(
        self, set_id: str, key: str, *, conn=None
    ) -> GenerationBatch | None:
        if not key:
            return None
        row = await self._fetchone(
            f"SELECT {_BATCH_COLS} FROM script_generation_batches "
            "WHERE script_set_id = $1 AND idempotency_key = $2",
            set_id,
            key,
            conn=conn,
        )
        if row is None:
            return None
        data = dict(row)
        data["product_ids"] = _json_rows(data["product_ids"], [])
        data["job_ids"] = _json_rows(data["job_ids"], [])
        data["created_at"] = _iso(data["created_at"])
        data["updated_at"] = _iso(data["updated_at"])
        # `revision` and `state` are batch-row columns, not GenerationBatch fields.
        data.pop("revision", None)
        data.pop("state", None)
        return GenerationBatch.model_validate(data)


class JobRepository(_Repo):
    async def insert(self, job: GenerationJob, *, conn=None) -> None:
        await self._execute(
            f"INSERT INTO script_generation_jobs ({_JOB_INS}) "
            f"VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13,$14,$15)",
            job.id,
            job.batch_id,
            job.script_item_id,
            job.product_id,
            job.intent.value,
            job.status.value,
            job.plan_id,
            job.plan_segment_count,
            job.current_segment_index,
            job.attempt_count,
            job.target_duration_s,
            json.dumps(job.fingerprint.model_dump()) if job.fingerprint else None,
            job.idempotency_key,
            _dts(job.created_at),
            _dts(job.updated_at),
            conn=conn,
        )

    async def get(self, job_id: str, *, conn=None) -> GenerationJob | None:
        row = await self._fetchone(
            f"SELECT {_JOB_COLS} FROM script_generation_jobs WHERE id = $1", job_id, conn=conn
        )
        return self._from_row(row)

    async def list_by_batch(self, batch_id: str, *, conn=None) -> list[GenerationJob]:
        rows = await self._fetchall(
            f"SELECT {_JOB_COLS} FROM script_generation_jobs WHERE batch_id = $1 ORDER BY created_at",
            batch_id,
            conn=conn,
        )
        return [self._from_row(r) for r in rows]

    async def find_by_idempotency(
        self, item_id: str, intent: str, key: str, *, conn=None
    ) -> GenerationJob | None:
        if not key:
            return None
        row = await self._fetchone(
            f"SELECT {_JOB_COLS} FROM script_generation_jobs "
            "WHERE script_item_id = $1 AND intent = $2 AND idempotency_key = $3",
            item_id,
            intent,
            key,
            conn=conn,
        )
        return self._from_row(row)

    async def update(self, job: GenerationJob, *, expected_revision: int = 0, conn=None) -> None:
        # The job row is owned by a single background worker; no optimistic
        # guard is needed (the signature keeps call-site parity).
        await self._execute(
            "UPDATE script_generation_jobs SET status=$2, plan_id=$3, "
            "plan_segment_count=$4, current_segment_index=$5, attempt_count=$6, "
            "updated_at=NOW() WHERE id=$1",
            job.id,
            job.status.value,
            job.plan_id,
            job.plan_segment_count,
            job.current_segment_index,
            job.attempt_count,
            conn=conn,
        )

    @staticmethod
    def _from_row(row) -> GenerationJob | None:
        if row is None:
            return None
        data = dict(row)
        data["fingerprint"] = _json_rows(data["fingerprint"], None)
        data["created_at"] = _iso(data["created_at"])
        data["updated_at"] = _iso(data["updated_at"])
        return GenerationJob.model_validate(data)


class IdempotencyRepository(_Repo):
    async def get(self, fingerprint: str, *, conn=None) -> str | None:
        row = await self._fetchone(
            "SELECT batch_id FROM script_idempotency WHERE fingerprint = $1",
            fingerprint,
            conn=conn,
        )
        return row["batch_id"] if row else None

    async def register(self, fingerprint: str, batch_id: str, *, conn=None) -> None:
        await self._execute(
            "INSERT INTO script_idempotency (fingerprint, batch_id) VALUES ($1, $2) "
            "ON CONFLICT (fingerprint) DO NOTHING",
            fingerprint,
            batch_id,
            conn=conn,
        )


class _NullTransaction:
    """Async context manager no-op used when a caller already owns a transaction."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _null_transaction():
    return _NullTransaction()


class PostgresAuthoringRepositories:
    """Aggregate root owning the pool + one repository per domain aggregate."""

    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = (database_url or "").strip()
        self._pool = None
        self.script_sets = ScriptSetRepository(self)
        self.items = ScriptItemRepository(self)
        self.plans = PlanRepository(self)
        self.segments = SegmentRepository(self)
        self.versions = VersionRepository(self)
        self.gate_runs = GateRunRepository(self)
        self.approvals = ApprovalRepository(self)
        self.batches = BatchRepository(self)
        self.jobs = JobRepository(self)
        self.idempotency = IdempotencyRepository(self)

    @property
    def enabled(self) -> bool:
        return bool(self.database_url)

    async def connect(self) -> None:
        if not self.enabled:
            return
        try:
            import asyncpg  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dep
            raise RuntimeError("asyncpg is required when DATABASE_URL is set") from exc
        self._pool = await asyncio.wait_for(
            asyncpg.create_pool(
                self.database_url,
                min_size=1,
                max_size=5,
                command_timeout=_COMMAND_TIMEOUT_SECONDS,
            ),
            timeout=_CONNECT_TIMEOUT_SECONDS,
        )

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    @asynccontextmanager
    async def transaction(self):
        """Open a transaction over a pool connection (caller commits/rolls back)."""
        if self._pool is None:
            raise RuntimeError("PostgresAuthoringRepositories not connected")
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                yield conn
