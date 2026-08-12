"""Task 8.8 tests: sequential continuity, exactly-K calls, bounded prompts.

These tests prove the sequential segment generation design:

- ``run_segment_step``/``ProductSegmentGenerator.run_one`` performs exactly
  ONE semantic LLM call per segment index (K segments -> K calls), so no
  extra summary LLM call is made between segments.
- Each prompt contains only a bounded tail of the previous segment, never
  the full prior script — prompt context stays bounded.
"""

from __future__ import annotations

from backend.application.contracts.llm_engines import LLMEngine, LLMRequest, LLMResponse
from backend.application.script_authoring.generation.continuity import (
    TAIL_LIMIT_CHARS,
    ContinuityState,
    build_tail,
)
from backend.application.script_authoring.generation.segment_generator import (
    ProductSegmentGenerator,
    run_segment_step,
)


class _CountingLLM(LLMEngine):
    """In-memory LLM stub that counts ``generate`` calls and records prompts."""

    name = "counting"

    def __init__(
        self,
        texts: list[str],
        *,
        fact_ids: frozenset[str] = frozenset(),
        objection_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._texts = list(texts)
        self._fact_ids = fact_ids
        self._objection_ids = objection_ids
        self.calls = 0
        self.prompts: list[str] = []

    @classmethod
    def from_config(cls, cfg: dict) -> "_CountingLLM":  # pragma: no cover
        raise NotImplementedError

    def generate(self, req: LLMRequest) -> LLMResponse:
        self.calls += 1
        self.prompts.append(req.messages[-1]["content"])
        text = self._texts[(self.calls - 1) % len(self._texts)]
        response = LLMResponse(text=text, num_generated_tokens=4)
        # Carry the stub's validated continuity IDs to the generator.
        response.fact_ids = self._fact_ids  # type: ignore[attr-defined]
        response.objection_ids = self._objection_ids  # type: ignore[attr-defined]
        return response


def _run_k_segments(
    texts: list[str],
    *,
    valid_fact_ids: frozenset[str] = frozenset(),
    valid_objection_ids: frozenset[str] = frozenset(),
) -> tuple[_CountingLLM, list]:
    """Run one generator over ``len(texts)`` segments; return (llm, outcomes)."""
    llm = _CountingLLM(texts)
    generator = ProductSegmentGenerator(llm, system_prompt="persona")
    state = ContinuityState()
    outcomes: list = []
    for index in range(len(texts)):
        outcome = run_segment_step(
            generator,
            index,
            state,
            valid_fact_ids=valid_fact_ids,
            valid_objection_ids=valid_objection_ids,
        )
        outcomes.append(outcome)
        state = outcome.state
    return llm, outcomes


def test_k_segments_call_llm_exactly_k_times() -> None:
    """K segments -> exactly K semantic calls (no extra summary call)."""
    texts = ["Seg zero.", "Seg one.", "Seg two.", "Seg three."]
    llm, outcomes = _run_k_segments(texts)

    assert llm.calls == 4
    assert [o.result.spoken_text for o in outcomes] == texts


def test_prompt_contains_only_bounded_previous_tail() -> None:
    """Each prompt's continuity section is bounded, never the full prior script."""
    long_prior = ("Chi tiết sản phẩm rất dài. " * 200).strip()  # >> 300 chars
    llm, _ = _run_k_segments(["First short segment.", long_prior, "Third segment."])

    # The first prompt has no previous segment.
    assert "(no previous segment)" in llm.prompts[0]

    # The second prompt's tail is the BOUNDED tail, not the full prior text.
    prompt_1 = llm.prompts[1]
    assert long_prior not in prompt_1
    tail_1 = _extract_tail(prompt_1)
    assert len(tail_1) <= TAIL_LIMIT_CHARS

    # The third prompt is bounded too.
    prompt_2 = llm.prompts[2]
    assert long_prior not in prompt_2
    tail_2 = _extract_tail(prompt_2)
    assert len(tail_2) <= TAIL_LIMIT_CHARS


def _extract_tail(prompt: str) -> str:
    """Return the previous-segment tail section of a prompt (bounded)."""
    marker = "Previous-segment tail (bounded to"
    after_marker = prompt.split(marker, 1)[1]
    # Drop the "... chars):" header line; the tail is the next line.
    _, _, rest = after_marker.partition("\n")
    # The tail runs until the next known section header.
    for stop in ("Covered fact IDs", "Handled objection IDs", "CTA count so far"):
        if stop in rest:
            return rest.split(stop, 1)[0].strip()
    return rest.strip()


def test_tail_never_exceeds_limit() -> None:
    """Even a pathological single segment text cannot blow the tail limit."""
    for n in (1, 300, 301, 1000, 10000):
        text = "x" * n
        tail = build_tail(text)
        assert len(tail) <= TAIL_LIMIT_CHARS


def test_every_prompt_contains_continuity_state_not_full_history() -> None:
    """K prompts each carry compact state; no prompt contains earlier prose."""
    llm, _ = _run_k_segments(["Segment A text.", "Segment B text.", "Segment C text."])

    assert llm.calls == 3
    for prompt in llm.prompts:
        # Bounded-tail marker and the bounded-tail constant are present.
        assert "Previous-segment tail (bounded to" in prompt
        assert f"bounded to {TAIL_LIMIT_CHARS} chars" in prompt
        # Every prompt tail is bounded.
        assert len(_extract_tail(prompt)) <= TAIL_LIMIT_CHARS

    # A LONG prior segment's full prose never reappears in a later prompt
    # (a short segment legitimately fits inside the bounded tail).
    long_segment = "Đây là đoạn văn rất dài. " * 100  # >> 300 chars
    llm2, _ = _run_k_segments(["A.", long_segment, "C."])
    for prompt in llm2.prompts[1:]:
        assert long_segment not in prompt
        assert len(_extract_tail(prompt)) <= TAIL_LIMIT_CHARS


def test_continuity_advances_with_validated_ids() -> None:
    """Covered IDs merge only when they exist in the authoritative registry."""
    llm = _CountingLLM(
        ["First.", "Second."],
        fact_ids=frozenset({"fact-1", "fact-2", "fact-unknown"}),
    )
    generator = ProductSegmentGenerator(llm, system_prompt="persona")

    state = ContinuityState()
    outcome = run_segment_step(
        generator,
        0,
        state,
        valid_fact_ids=frozenset({"fact-1", "fact-2"}),
    )
    # fact-unknown is not in the authoritative registry -> dropped.
    assert outcome.state.covered_fact_ids == frozenset({"fact-1", "fact-2"})

    outcome = run_segment_step(
        generator,
        1,
        outcome.state,
        valid_fact_ids=frozenset({"fact-1", "fact-2"}),
    )
    # Second segment's IDs intersect the registry again -> idempotent merge.
    assert outcome.state.covered_fact_ids == frozenset({"fact-1", "fact-2"})
    assert llm.calls == 2
