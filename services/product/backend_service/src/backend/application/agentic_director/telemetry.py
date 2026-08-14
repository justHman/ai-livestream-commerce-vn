"""Content-safe execution telemetry for the bounded agentic director (C12).

Requirement "Agent execution is observable": every Q&A exposes content-safe
metadata — execution path, evidence cache hits/misses, evidence rounds, LLM
call counts, token/latency metrics, and terminal state. Content-safe means
identifiers and counts ONLY: no viewer text, no model text, no evidence text
ever enters a telemetry record.

Metrics are recorded as flat ``(name, value)`` pairs by the executors
(``fast_path`` / ``complex_path``, built by sibling tasks) and aggregated
here into one typed record by exact metric name. ``latency_ms`` degrades to
0 when absent (the executor measures wall time itself in a later cluster).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "ExecutionTelemetry",
    "MetricSink",
    "InMemoryMetricSink",
    "build_execution_telemetry",
]

# Executors record exactly these metric names; anything else is ignored
# (unknown metrics are neither counted nor forwarded to metadata).
_METRIC_FIELDS: dict[str, str] = {
    "llm_calls": "llm_calls",
    "prompt_tokens": "prompt_tokens",
    "generated_tokens": "generated_tokens",
    "evidence_rounds": "evidence_rounds",
    "evidence_cache_hits": "evidence_cache_hits",
    "evidence_cache_misses": "evidence_cache_misses",
    "latency_ms": "latency_ms",
}
_INT_METRICS: frozenset[str] = frozenset(
    {
        "llm_calls",
        "prompt_tokens",
        "generated_tokens",
        "evidence_rounds",
        "evidence_cache_hits",
        "evidence_cache_misses",
    }
)


@dataclass(frozen=True, slots=True)
class ExecutionTelemetry:
    """One content-safe telemetry record for one executed plan.

    ``path`` is the execution path discriminator ("factual_fast" | "complex"
    | "unavailable" | "budget_exceeded"); ``terminal`` is the ``PlanKind``
    value the plan finished in. Counts and identifiers only — no text.
    """

    path: str
    evidence_cache_hits: int = 0
    evidence_cache_misses: int = 0
    evidence_rounds: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    generated_tokens: int = 0
    latency_ms: int = 0
    terminal: str = ""

    def to_metadata(self) -> dict[str, int | str]:
        """Content-safe serialization: identifiers and counts only.

        Every key/value is safe for observability pipelines and the browser
        session — no text content is ever included.
        """
        return {
            "execution_path": self.path,
            "evidence_cache_hits": self.evidence_cache_hits,
            "evidence_cache_misses": self.evidence_cache_misses,
            "evidence_rounds": self.evidence_rounds,
            "llm_calls": self.llm_calls,
            "prompt_tokens": self.prompt_tokens,
            "generated_tokens": self.generated_tokens,
            "latency_ms": self.latency_ms,
            "terminal": self.terminal,
        }


@runtime_checkable
class MetricSink(Protocol):
    """Duck-typed metric sink the executors record into.

    ``fast_path`` / ``complex_path`` executors accept
    ``Callable[[str, int | float], None]`` sinks (a later cluster wires them
    together); this Protocol matches that callable shape so the executor is
    testable against the same surface without importing it here.
    """

    def record(self, name: str, value: int | float) -> None: ...


class InMemoryMetricSink:
    """Records ordered ``(name, value)`` pairs for tests and diagnostics."""

    def __init__(self) -> None:
        self._records: list[tuple[str, int | float]] = []

    def record(self, name: str, value: int | float) -> None:
        self._records.append((name, value))

    @property
    def records(self) -> list[tuple[str, int | float]]:
        return list(self._records)


def build_execution_telemetry(
    path: str, metrics: Mapping[str, int | float], terminal: str
) -> ExecutionTelemetry:
    """Aggregate executor metrics into one typed telemetry record.

    Exactly the allowlisted metric names are read; unknown metric names are
    ignored. Count metrics truncate to int; latency is rounded to int ms.
    """
    fields: dict[str, int] = {}
    for metric, field in _METRIC_FIELDS.items():
        value = metrics.get(metric)
        if value is None:
            continue
        if field in _INT_METRICS:
            fields[field] = int(value)
        else:
            fields[field] = round(float(value))
    return ExecutionTelemetry(path=path, terminal=terminal, **fields)
