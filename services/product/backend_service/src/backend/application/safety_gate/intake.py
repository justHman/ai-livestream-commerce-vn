"""Session-owned state for the existing gate; no upstream approval semantics."""

from collections import deque
import hashlib
from typing import Any

from .checks import ReplayWindow
from .decision import ReasonCode
from .engine import SafetyGate
from .injection_patterns import detect_injection
from .resources import load_all_curated_patterns, match_curated


class FingerprintReplayWindow(ReplayWindow):
    """Keep replay fingerprints, never raw rejected text, in session metadata."""

    def normalized(self, text: str) -> str:
        normalized = super().normalized(text)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


class IntakeSafety:
    def __init__(self, gate: SafetyGate) -> None:
        self.gate = gate
        self.patterns = load_all_curated_patterns()

    def _curated(self, text: str) -> tuple[ReasonCode, ...]:
        return tuple(ReasonCode(kind) for kind in match_curated(text, self.patterns))

    def evaluate(
        self,
        meta: dict[str, Any],
        text: str,
        *,
        now: float,
        route: str,
        event_id: str | None = None,
        moderation_ref: str | None = None,
    ) -> dict[str, Any]:
        # The store and lock are already scoped to the Runtime session. Take
        # tenant/business identity only from server-owned session metadata.
        binding = meta.get("platform_event_binding") or meta.get("execution_contract") or {}
        scope = [binding.get("tenant_id"), binding.get("business_session_id")]
        state = dict[str, Any](meta.get("runtime_safety") or {})
        if state.get("scope") != scope:
            state = dict[str, Any](scope=scope)
        # Epoch timestamps survive workers/restarts; clamp rollback to keep the
        # gate's clock monotonic within this persisted session window.
        now = max(now, state.get("last_ts", now))
        window = FingerprintReplayWindow(recent=deque(tuple(e) for e in state.get("recent", [])))
        decision = self.gate.evaluate(
            text,
            replay_window=window,
            ts=now,
            extra_checks=(detect_injection, self._curated),
        )
        evidence = {
            "accepted": decision.accepted,
            "reason_codes": [str(code) for code in decision.reason_codes] or ["safe_input"],
            "policy_version": decision.policy_version,
            "composition_version": "p0-fb-006.v1",
            "resource_versions": {kind: value.version for kind, value in self.patterns.items()},
            "route": route,
            "event_id": event_id,
            "moderation_ref": moderation_ref,
            "tenant_id": scope[0],
            "business_session_id": scope[1],
        }
        # A rejection must not poison the replay history of eligible inputs.
        if decision.accepted:
            state["recent"] = list(window.recent)[-1000:]
        state["last_ts"] = now
        state["decisions"] = [*state.get("decisions", []), evidence][-100:]
        meta["runtime_safety"] = state
        return evidence
