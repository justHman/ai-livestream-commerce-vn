"""Approved-script runtime handoff to the canonical Change A speech path
(tasks 12.3-12.9).

Change B ends at an immutable, human-approved ``spoken_text`` and a session
binding. At runtime the backend resolves the exact approved ``spoken_text``
and hands the COMPLETE text to the SAME source-agnostic Change A
``backend.application.text_chunker.TextChunker`` path used for arbitrary
incremental text — via the existing runtime speech service
(``StreamOrchestrator.speak_verbatim``, which is the canonical full-script
path: ``TextChunker.feed(full_text)`` + ``finalize()`` internally).

Architectural invariants this module enforces (Decision 17, MUST-NOT list):

- NO import of ``backend.application.speech_chunking``;
- NO ``TextChunk`` from ``backend.application.render.windows``;
- NO direct ``TextChunk(...)`` construction for a whole script (the
  orchestrator's verbatim path owns chunk construction via the chunker);
- NO script-specific chunker / ``mode="script"`` — policy selection stays
  Change A's (fixed / adaptive_vi behind the same TextChunker capability);
- NO ``check_timeout`` / ``flush_timeout_ms`` / streaming deadlines — a
  complete approved script has no upstream token wait; realtime deadline
  ownership remains Change A orchestration;
- NO ``is_final`` stamping — Change A's exactly-once finalization protocol
  owns normal completion and error/cancel semantics.

The binding snapshot (task 12.3) is a plain dict stored in session/runtime
state: ``script_set_id`` plus ``product_id -> {approved_version_id,
spoken_text}``. It never mutates authoring artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

# Re-exported for a single API import point (task 12.2).
from backend.application.script_authoring.session_binding import (
    BindingCheck,
    BindingSource,
    RuntimeCatalogProxy,
    RuntimePlan,
    validate_binding,
)

__all__ = [
    "ApprovedScriptStore",
    "BindingCheck",
    "BindingSnapshot",
    "BindingSource",
    "ResolvedApprovedScript",
    "RuntimeCatalogProxy",
    "RuntimePlan",
    "ScriptSelectionPolicy",
    "build_binding_snapshot",
    "resolve_approved_script",
    "select_next_script_product",
    "speak_approved_script",
    "validate_binding",
]


@dataclass(frozen=True)
class ResolvedApprovedScript:
    """Exact approved artifact resolved for one product (task 12.4).

    ``spoken_text`` is the immutable, human-approved string. Nothing
    rephrases or rewrites it between here and the canonical chunker
    ingestion (task 12.10).
    """

    product_id: str
    approved_version_id: str
    spoken_text: str


@dataclass(frozen=True)
class BindingSnapshot:
    """Session-state snapshot of a bound ScriptSet (task 12.3).

    Stored in session/runtime state as a JSON-serializable dict; authoring
    artifacts (ScriptSet/ScriptItem/Approval rows) are never mutated.
    """

    script_set_id: str
    products: tuple[ResolvedApprovedScript, ...]

    def by_product(self, product_id: str) -> Optional[ResolvedApprovedScript]:
        for entry in self.products:
            if entry.product_id == product_id:
                return entry
        return None

    def as_dict(self) -> dict[str, object]:
        """Stable session-store payload (``script_set_id`` + products)."""
        return {
            "script_set_id": self.script_set_id,
            "products": [
                {
                    "product_id": entry.product_id,
                    "approved_version_id": entry.approved_version_id,
                    "spoken_text": entry.spoken_text,
                }
                for entry in self.products
            ],
        }


class ApprovedScriptStore(Protocol):
    """Source of approved script versions (repository or in-memory fake)."""

    def get_approved_version(
        self, *, script_set_id: str, product_id: str
    ) -> Optional[ResolvedApprovedScript]: ...


class ScriptSelectionPolicy(Protocol):
    """Runtime product selection/reordering (task 12.13).

    Implementations decide which product speaks next under
    ``ORDER_AGNOSTIC`` (e.g. demand-pivot in the Director); the handoff
    layer stays policy-agnostic and only resolves the selected product's
    approved script.
    """

    def select_product(
        self, current: Optional[str], candidates: Sequence[str]
    ) -> Optional[str]: ...


class _SpeechService(Protocol):
    """The canonical runtime speech entry (``StreamOrchestrator``)."""

    async def speak_verbatim(self, session_id: str, text: str) -> str: ...


def build_binding_snapshot(
    script_set_id: str,
    entries: Sequence[ResolvedApprovedScript],
) -> BindingSnapshot:
    """Build the session-state snapshot for a successful binding.

    The snapshot captures the EXACT approved version id + spoken_text per
    product at bind time. It is stored in session/runtime state only —
    authoring artifacts are never mutated (task 12.3).
    """
    return BindingSnapshot(script_set_id=script_set_id, products=tuple(entries))


async def resolve_approved_script(
    store: ApprovedScriptStore,
    *,
    script_set_id: str,
    product_id: str,
) -> Optional[ResolvedApprovedScript]:
    """Resolve the exact approved ``spoken_text`` for ``product_id``.

    Async because the authoring service's approved-version lookup is an
    async repository read; sync in-memory fakes are awaited transparently.
    Returns None when the product has no approved version in the store. The
    returned text is passed VERBATIM to the canonical chunker path — no
    post-approval LLM rewrite, no source-specific transformation
    (task 12.10).
    """
    import inspect

    result = store.get_approved_version(script_set_id=script_set_id, product_id=product_id)
    if inspect.isawaitable(result):
        result = await result
    return result


def select_next_script_product(
    policy: ScriptSelectionPolicy,
    current: Optional[str],
    candidates: Sequence[str],
) -> Optional[str]:
    """Delegate runtime product selection/reordering to ``policy``.

    Under ``ORDER_AGNOSTIC`` the Director may reorder products; the handoff
    layer simply asks the policy which product speaks next and then resolves
    that product's approved script through the canonical path (task 12.13).
    """
    return policy.select_product(current, list(candidates))


async def speak_approved_script(
    speech_service: _SpeechService,
    *,
    session_id: str,
    script: ResolvedApprovedScript,
) -> str:
    """Speak one approved script through the canonical Change A path.

    The full approved ``spoken_text`` is supplied to the existing runtime
    speech service (``speak_verbatim``), which feeds the complete text into
    the SAME source-agnostic ``TextChunker`` (``feed`` + ``finalize``) and
    lets Change A own policy selection, chunk creation, and exactly-once
    finality. No giant ``TextChunk`` is constructed here; no deadlines or
    finality flags are owned here (tasks 12.5/12.7-12.9).
    """
    return await speech_service.speak_verbatim(session_id, script.spoken_text)
