"""P0-FB-017: durable signed Runtime usage evidence (C-USAGE-EVIDENCE-001).

Evidence, not billing. Default off; fail closed. The Runtime never computes Live
Credits, prices, balances or billable durations (BR-PRICING-001/002).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Sequence

from backend.application.execution_contract import (
    CommandOutcome,
    Evidence,
    ExecutionIdentity,
    ExecutionState,
)

from . import envelope
from .outbox import Staged, StageBlocked, UsageOutbox
from .sender import (  # noqa: F401
    CLEANUP_KEY,
    COMMITS_KEY,
    EVIDENCE_TTL,
    MAX_PENDING_TOKENS,
    UNSTAGED_KEY,
    UsageSender,
)
from .settings import CogsBuffer, CogsSample, UsageEvidenceSettings

__all__ = [
    "UsageEvidence",
    "UsageEvidenceRejected",
    "UsageEvidenceUnavailable",
    "UsageEvidenceSettings",
    "UsageSender",
]

logger = logging.getLogger(__name__)


class UsageEvidenceUnavailable(Exception):
    """The durable row could not be staged; the fact must not be applied unreported."""


class UsageEvidenceRejected(Exception):
    """The fact can never be reported (undeliverable identity): refuse it, never apply it."""


class UsageEvidence:
    """Control-plane facade used by ``api/v1/execution.py`` (never by the speech path)."""

    def __init__(self, outbox: UsageOutbox, settings: UsageEvidenceSettings) -> None:
        self._outbox = outbox
        self.settings = settings
        self.cogs = CogsBuffer(settings.cogs_buffer)
        self.rejected_invalid_identity = 0
        # Sessions whose meta holds deferred (unstaged) evidence; drained by the sender.
        self.unstaged_sessions: set[str] = set()

    @staticmethod
    def _proof(meta: dict[str, Any]) -> dict[str, Any]:
        raw = meta.get(COMMITS_KEY) or {}
        return {"version": int(raw.get("version", 0)), "tokens": dict(raw.get("tokens") or {})}

    async def _stage(
        self, updated: ExecutionState, drafts: list[envelope.Draft], meta: dict[str, Any]
    ) -> list[Staged]:
        """Stage one attempt per draft. ``meta`` carries the session's committed proof.

        The proof is ``{"version": V, "tokens": {stage_token: commit_version}}``. It is
        written with the state in one atomic save and never evicts an unresolved token:
        a token is dropped only once its row is no longer ``staged`` (ready, deleted).
        If more than ``MAX_PENDING_TOKENS`` are unresolved, staging fails closed.
        """
        if not drafts:
            return []
        try:
            proof = self._proof(meta)
            if proof["tokens"]:
                live = await self._outbox.still_staged(list(proof["tokens"]))
                proof["tokens"] = {t: v for t, v in proof["tokens"].items() if t in live}
            if len(proof["tokens"]) + len(drafts) > MAX_PENDING_TOKENS:
                raise StageBlocked("proof_full")
            staged = await self._outbox.stage(
                envelope.identity_of(updated),
                drafts,
                version=proof["version"] + 1,
                committed_tokens=set(proof["tokens"]),
            )
            meta[COMMITS_KEY] = proof  # pruned; stamp() adds this attempt's tokens
            return staged
        except envelope.InvalidIdentity as exc:
            # Can never be delivered. Fail closed: state must not advance without a row.
            self.rejected_invalid_identity += 1
            logger.error("usage evidence refused undeliverable identity: %s", exc)
            raise UsageEvidenceRejected(str(exc)) from exc
        except Exception as exc:
            logger.error("usage evidence stage failed error_type=%s", type(exc).__name__)
            raise UsageEvidenceUnavailable(type(exc).__name__) from exc

    async def stage_evidence(
        self, prior: ExecutionState, updated: ExecutionState, ev: Evidence, meta: dict[str, Any]
    ) -> list[Staged]:
        """Call only after ``apply_evidence`` succeeded and before the state is saved."""
        drafts = envelope.derive_from_evidence(prior, updated, ev, self.settings.gates)
        return await self._stage(updated, drafts, meta)

    async def stage_command(
        self,
        prior: ExecutionState,
        updated: ExecutionState,
        outcome: CommandOutcome,
        meta: dict[str, Any],
    ) -> list[Staged]:
        """Call only for an applied outcome, before the state is saved."""
        drafts = envelope.derive_from_command(prior, updated, outcome, self.settings.gates)
        return await self._stage(updated, drafts, meta)

    async def commit(self, staged: Sequence[Staged]) -> None:
        """After the save: ``staged`` -> ``ready``. Best effort; the sweeper converges."""
        items = [(s.event_id, s.token) for s in staged if s.token]
        if not items:
            return
        try:
            await self._outbox.mark_ready(items)
        except Exception as exc:
            logger.warning(
                "usage evidence ready flip deferred to sweeper error_type=%s", type(exc).__name__
            )

    def defer_entries(
        self, prior: ExecutionState, updated: ExecutionState, cause: Any
    ) -> list[dict[str, Any]]:
        """Serializable facts for a safety command that could not be staged (see UNSTAGED_KEY)."""
        if isinstance(cause, Evidence):
            drafts = envelope.derive_from_evidence(prior, updated, cause, self.settings.gates)
        else:
            drafts = envelope.derive_from_command(prior, updated, cause, self.settings.gates)
        envelope.validate_identity(
            envelope.identity_of(updated)
        )  # InvalidIdentity: never reportable
        identity = envelope.identity_of(updated).model_dump()
        return [{"identity": identity, "draft": envelope.draft_to_dict(d)} for d in drafts]

    @staticmethod
    def stamp(meta: dict[str, Any], staged: Sequence[Staged]) -> None:
        """Record the attempts this save commits, in the SAME atomic save as the state.

        The sweeper marks a staged row ready only if ITS ``stage_token`` is listed.
        """
        attempts = [s for s in staged if s.token]
        if not attempts:
            return
        proof = UsageEvidence._proof(meta)
        version = max(s.version for s in attempts)
        for s in attempts:
            proof["tokens"][s.token] = s.version
        proof["version"] = max(proof["version"], version)
        meta[COMMITS_KEY] = proof

    async def abort(self, staged: Sequence[Staged]) -> None:
        """The save DEFINITELY did not land: remove the staged rows. Best effort.

        Never call this for an ambiguous outcome; leave the rows for the sweeper.
        """
        items = [(s.event_id, s.token) for s in staged if s.token]
        if not items:
            return
        try:
            await self._outbox.release(items, delete=True)
        except Exception as exc:
            logger.warning(
                "usage evidence abort deferred to sweeper error_type=%s", type(exc).__name__
            )

    def record_cogs(
        self,
        identity: ExecutionIdentity,
        *,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        sample_id: str | None = None,
    ) -> None:
        """Speech-path safe: append to a bounded in-memory buffer. No await, no I/O."""
        self.cogs.append(
            CogsSample(
                identity,
                sample_id or uuid.uuid4().hex,
                datetime.now(timezone.utc),
                model_id,
                input_tokens,
                output_tokens,
            )
        )

    async def health(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "cogs_buffered": len(self.cogs),
            "cogs_dropped": self.cogs.dropped,
        }
        try:
            out.update(await self._outbox.backlog())
        except Exception as exc:
            out["backlog_error"] = type(exc).__name__
        return out
