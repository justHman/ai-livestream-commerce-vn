"""Ponytail self-check for the Task 8.8 sequential-continuity contract.

Run: ``uv run --project services/product/backend_service python -m backend.application.script_authoring.generation``

Proves: K segments -> exactly K semantic calls; every prompt carries a
bounded tail only (no full prior script, no summary call).
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from backend.application.contracts.llm_engines import LLMEngine, LLMRequest, LLMResponse  # noqa: E402
from backend.application.script_authoring.generation.continuity import (  # noqa: E402
    TAIL_LIMIT_CHARS,
    ContinuityState,
)
from backend.application.script_authoring.generation.segment_generator import (  # noqa: E402
    ProductSegmentGenerator,
    run_segment_step,
)


class _EchoLLM(LLMEngine):
    name = "echo"

    @classmethod
    def from_config(cls, cfg):  # pragma: no cover
        return cls()

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def generate(self, req: LLMRequest) -> LLMResponse:
        self.calls += 1
        self.prompts.append(req.messages[-1]["content"])
        return LLMResponse(text=f"Segment {self.calls} text.")


def _self_check() -> None:
    llm = _EchoLLM()
    generator = ProductSegmentGenerator(llm, system_prompt="persona")
    state = ContinuityState()
    for index in range(3):
        state = run_segment_step(generator, index, state).state
    assert llm.calls == 3, f"expected exactly 3 calls, got {llm.calls}"
    assert state.cta_count == 0
    for prompt in llm.prompts:
        assert "bounded to" in prompt
        assert len(prompt) < 1000, "prompt grew unbounded"
    print(f"OK: 3 segments -> {llm.calls} calls; prompts bounded <= {TAIL_LIMIT_CHARS} tail")


if __name__ == "__main__":
    _self_check()
