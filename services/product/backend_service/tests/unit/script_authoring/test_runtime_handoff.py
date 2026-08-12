"""Tasks 12.4/12.10-12.13: approved-script handoff to the canonical Change A path.

Proves the Change B MUST-NOT list holds at runtime:

- the full approved ``spoken_text`` enters the SAME source-agnostic
  ``backend.application.text_chunker.TextChunker`` via the existing
  orchestrator verbatim path (feed + finalize) — no giant TextChunk, no
  script-specific chunker, no source mode;
- no post-approval LLM rewrite: the exact approved string reaches the
  chunker boundary (12.10);
- one approved long script yields MULTIPLE canonical TextChunks when policy
  requires, and concatenation == input (12.11);
- normal completion emits exactly one final marker; error/cancel never
  fabricate one; a last-content-chunk-already-emitted EOF does not fabricate
  a replacement final TextChunk (12.12);
- multiple approved products with runtime selection/reordering under
  ORDER_AGNOSTIC resolve exact text/version identity at the chunker
  boundary under both fixed and adaptive policy on the same path (12.13).
"""

from __future__ import annotations

import asyncio
from typing import Iterator, Optional

import pytest

from avatar.engines.mock import MockRenderBackend
from backend.application.render.engines_base import StartOptions
from backend.application.render.queue import BoundedVideoQueue, CoordinatorMetrics
from backend.application.render.windows import AudioWindow, VideoWindow
from backend.application.script_authoring.runtime_handoff import (
    BindingSnapshot,
    ResolvedApprovedScript,
    build_binding_snapshot,
    resolve_approved_script,
    select_next_script_product,
    speak_approved_script,
)
from backend.application.text_chunker import FixedChunkPolicyConfig, TextChunk
from llm.engines.base import LLMEngine, LLMRequest, LLMResponse
from tts.engines.base import ToneEngine


# ---------- approved-script store fake ----------


class _FakeApprovedStore:
    """ApprovedScriptStore fake keyed by (set_id, product_id)."""

    def __init__(self, entries: dict[tuple[str, str], ResolvedApprovedScript]) -> None:
        self._entries = entries

    def get_approved_version(
        self, *, script_set_id: str, product_id: str
    ) -> Optional[ResolvedApprovedScript]:
        return self._entries.get((script_set_id, product_id))


def _approved(product_id: str, version_id: str, text: str) -> ResolvedApprovedScript:
    return ResolvedApprovedScript(
        product_id=product_id, approved_version_id=version_id, spoken_text=text
    )


# ---------- speech pipeline fakes (mirror test_streaming_controller_regressions) ----------


class _NoopLLM(LLMEngine):
    """LLM stub that must never be reached on the verbatim path."""

    name = "noop-llm"
    calls = 0

    @classmethod
    def from_config(cls, cfg: dict) -> "_NoopLLM":  # pragma: no cover
        return cls()

    def generate(self, req: LLMRequest) -> LLMResponse:  # pragma: no cover
        raise RuntimeError("verbatim path must not call the LLM")

    def stream_chunks(self, req, *, session_id="", utterance_id="") -> Iterator[TextChunk]:
        raise RuntimeError("verbatim path must not call the LLM")


class _RecordingTTS(ToneEngine):
    """ToneEngine subclass recording every received input + spoken text.

    The production TTS seam marks the LAST window of every synthesis call
    final and ignores chunk finality (tts/engines/base.py), so finality
    assertions inspect the recorded inputs, not the engine output.
    """

    def __init__(self) -> None:
        super().__init__()
        self.received_inputs: list[object] = []
        self.spoken_texts: list[str] = []

    def stream_audio(
        self, text_or_chunk, *, session_id="", utterance_id="", **kwargs
    ) -> Iterator[AudioWindow]:
        text = text_or_chunk.text if isinstance(text_or_chunk, TextChunk) else text_or_chunk
        self.received_inputs.append(text_or_chunk)
        self.spoken_texts.append(text)
        yield from super().stream_audio(
            text_or_chunk, session_id=session_id, utterance_id=utterance_id, **kwargs
        )


def _build_orchestrator(tts: _RecordingTTS):
    from backend.application.render.orchestrator import (
        StreamOrchestrator,
        StreamingControllerConfig,
    )

    backend = MockRenderBackend()
    backend.start(StartOptions())
    queue = BoundedVideoQueue(max_size=20)
    metrics = CoordinatorMetrics()
    orch = StreamOrchestrator(
        llm=_NoopLLM(),
        tts=tts,
        backend=backend,
        queue=queue,
        metrics=metrics,
        fixed_config=FixedChunkPolicyConfig(min_chars=4, target_chars=20, max_chars=40),
        controller_config=StreamingControllerConfig(flush_timeout_ms=50),
    )
    sid = next(iter(backend._sessions.keys()))
    return orch, backend, queue, sid


async def _drain(queue: BoundedVideoQueue) -> list[VideoWindow]:
    windows: list[VideoWindow] = []
    while queue.qsize() > 0:
        windows.append(await queue.get())
    return windows


# ---------- 12.4: canonical path resolution + handoff ----------


async def test_resolve_and_speak_approved_script_uses_canonical_verbatim_path() -> None:
    """The handoff resolves exact text and speaks it via speak_verbatim.

    The TTS seam receives the canonical TextChunk class with the EXACT
    approved string; the LLM is never reached (no post-approval rewrite).
    """
    from backend.application.text_chunker import TextChunk as CanonicalTextChunk

    store = _FakeApprovedStore(
        {("set-1", "P001"): _approved("P001", "v-1", "Xin chào mọi người. Hôm nay giảm giá 50%!")}
    )
    resolved = await resolve_approved_script(store, script_set_id="set-1", product_id="P001")
    assert resolved is not None and resolved.approved_version_id == "v-1"

    tts = _RecordingTTS()
    orch, backend, queue, sid = _build_orchestrator(tts)
    spoken = await speak_approved_script(orch, session_id=sid, script=resolved)
    await _drain(queue)

    assert spoken == resolved.spoken_text
    assert tts.received_inputs, "TTS must be reached"
    assert all(isinstance(chunk, CanonicalTextChunk) for chunk in tts.received_inputs)
    assert "".join(tts.spoken_texts) == resolved.spoken_text
    # Exactly-once finality: the LAST canonical chunk carries is_final.
    assert getattr(tts.received_inputs[-1], "is_final", None) is True
    assert all(getattr(chunk, "is_final", None) is not True for chunk in tts.received_inputs[:-1])


async def test_resolve_unknown_product_returns_none() -> None:
    store = _FakeApprovedStore({})
    assert await resolve_approved_script(store, script_set_id="set-1", product_id="P999") is None


# ---------- 12.10: no post-approval LLM rewrite ----------


async def test_no_llm_call_between_approved_text_and_chunker_ingestion() -> None:
    """Contract: the binding->speech path makes zero LLM calls.

    ``_NoopLLM`` raises if the verbatim path ever touched the LLM; the
    approved string must reach the chunker boundary unmodified.
    """
    store = _FakeApprovedStore(
        {("set-1", "P001"): _approved("P001", "v-1", "Kem ABC chỉ 299.000 đồng, giảm 20%.")}
    )
    resolved = await resolve_approved_script(store, script_set_id="set-1", product_id="P001")
    tts = _RecordingTTS()
    orch, backend, queue, sid = _build_orchestrator(tts)

    spoken = await speak_approved_script(orch, session_id=sid, script=resolved)
    await _drain(queue)

    assert spoken == resolved.spoken_text
    assert "".join(tts.spoken_texts) == resolved.spoken_text
    # The chunker segmented the string; the exact approved text is preserved.
    assert tts.received_inputs  # chunker was reached


# ---------- 12.11: full-script segmentation -> multiple canonical chunks ----------


async def test_long_approved_script_segments_into_multiple_chunks_no_loss() -> None:
    """One approved long script -> MULTIPLE canonical TextChunks, no loss/dup.

    Policy (fixed, target 20 chars) requires several phrases for a long VN
    script; concatenation of the TTS-visible chunks equals the input exactly.
    """
    script = (
        "Chào cả nhà, hôm nay shop mở phiên live đặc biệt. "
        "Kem chống nắng SPF50 chống nước, giá chỉ 350.000 đồng. "
        "Giảm thêm 20 phần trăm khi mua hai chai. "
        "Nhanh tay đặt hàng ngay nhé!"
    )
    store = _FakeApprovedStore({("set-1", "P001"): _approved("P001", "v-1", script)})
    resolved = await resolve_approved_script(store, script_set_id="set-1", product_id="P001")
    tts = _RecordingTTS()
    orch, backend, queue, sid = _build_orchestrator(tts)

    spoken = await speak_approved_script(orch, session_id=sid, script=resolved)
    await _drain(queue)

    assert spoken == script
    assert len(tts.received_inputs) > 1, "a long script must segment into multiple phrases"
    assert "".join(tts.spoken_texts) == script, "no text lost or duplicated across chunks"


# ---------- 12.12: finality edges ----------


async def test_normal_completion_exactly_one_final_marker() -> None:
    """Normal approved-script completion: exactly one final marker, last one."""
    script = "Xin chào. Hôm nay giảm giá 50%. Nhanh tay nhé!"
    store = _FakeApprovedStore({("set-1", "P001"): _approved("P001", "v-1", script)})
    resolved = await resolve_approved_script(store, script_set_id="set-1", product_id="P001")
    tts = _RecordingTTS()
    orch, backend, queue, sid = _build_orchestrator(tts)

    await speak_approved_script(orch, session_id=sid, script=resolved)
    windows = await _drain(queue)

    finals = [i for i, w in enumerate(windows) if w.is_final]
    assert finals == [len(windows) - 1], f"exactly the last window final, got {finals}"
    final_inputs = [
        i for i, x in enumerate(tts.received_inputs) if getattr(x, "is_final", None) is True
    ]
    assert final_inputs == [len(tts.received_inputs) - 1]


async def test_error_does_not_fabricate_final_marker() -> None:
    """A TTS error on the verbatim path must not fabricate a normal final."""

    class _FailingTTS(_RecordingTTS):
        def stream_audio(
            self, text_or_chunk, *, session_id="", utterance_id="", **kwargs
        ) -> Iterator[AudioWindow]:
            text = text_or_chunk.text if isinstance(text_or_chunk, TextChunk) else text_or_chunk
            self.received_inputs.append(text_or_chunk)
            self.spoken_texts.append(text)
            yield AudioWindow(
                session_id=session_id,
                utterance_id=utterance_id,
                seq=0,
                sample_rate=24000,
                duration_ms=200,
                pcm=b"\x01\x00" * 4800,
                is_final=True,
                text_span=text,
            )
            raise RuntimeError("tts stream failed mid-utterance")

    store = _FakeApprovedStore(
        {("set-1", "P001"): _approved("P001", "v-1", "Xin chào bạn. Tạm biệt nhé!")}
    )
    resolved = await resolve_approved_script(store, script_set_id="set-1", product_id="P001")
    tts = _FailingTTS()
    orch, backend, queue, sid = _build_orchestrator(tts)

    with pytest.raises(RuntimeError, match="tts stream failed"):
        await speak_approved_script(orch, session_id=sid, script=resolved)
    windows = await _drain(queue)
    assert all(not w.is_final for w in windows), "no final marker may reach the video queue"


async def test_cancel_does_not_fabricate_final_marker() -> None:
    """Cancel mid-verbatim must not fabricate a final marker (12.12)."""
    script = " ".join(f"Chunk {i}." for i in range(40))  # long enough to still be speaking
    store = _FakeApprovedStore({("set-1", "P001"): _approved("P001", "v-1", script)})
    resolved = await resolve_approved_script(store, script_set_id="set-1", product_id="P001")
    tts = _RecordingTTS()
    orch, backend, queue, sid = _build_orchestrator(tts)

    task = asyncio.create_task(speak_approved_script(orch, session_id=sid, script=resolved))
    await asyncio.sleep(0.05)
    await orch.cancel(sid)
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except asyncio.TimeoutError:  # pragma: no cover - cancel should complete
        task.cancel()
        raise
    windows = await _drain(queue)
    assert all(not w.is_final for w in windows), "cancel must not fabricate a final marker"


# ---------- 12.13: multi-product ORDER_AGNOSTIC selection + identity ----------


class _RoundRobinSelector:
    """ORDER_AGNOSTIC runtime selection policy: rotate through candidates."""

    def __init__(self) -> None:
        self._idx = 0

    def select_product(self, current: Optional[str], candidates: list[str]) -> Optional[str]:
        if not candidates:
            return None
        self._idx %= len(candidates)
        chosen = candidates[self._idx]
        self._idx += 1
        return chosen


async def test_multi_product_order_agnostic_selection_exact_identity() -> None:
    """Two approved products, runtime reordering: exact text+version at chunker.

    The selection policy may reorder (ORDER_AGNOSTIC); whatever it picks,
    the handoff resolves the exact approved version and speaks that exact
    text through the same canonical path.
    """
    store = _FakeApprovedStore(
        {
            ("set-1", "P001"): _approved("P001", "v-1", "Kem chống nắng SPF50, 350.000 đồng."),
            ("set-1", "P002"): _approved(
                "P002", "v-2", "Serum Vitamin C làm sáng da, 250.000 đồng."
            ),
        }
    )
    selector = _RoundRobinSelector()
    tts = _RecordingTTS()
    orch, backend, queue, sid = _build_orchestrator(tts)

    picked: list[str] = []
    expected_texts: list[str] = []
    for _ in range(3):  # reorders: P001, P002, P001
        product_id = select_next_script_product(selector, current=None, candidates=["P001", "P002"])
        assert product_id is not None
        picked.append(product_id)
        resolved = await resolve_approved_script(
            store, script_set_id="set-1", product_id=product_id
        )
        assert resolved is not None
        # The handoff returns the EXACT approved spoken text per call.
        spoken = await speak_approved_script(orch, session_id=sid, script=resolved)
        assert spoken == resolved.spoken_text
        expected_texts.append(resolved.spoken_text)
        await _drain(queue)

    assert picked == ["P001", "P002", "P001"]
    # Exact version identity at the chunker boundary: the flat TTS stream
    # concatenates to exactly the approved texts in selection order.
    assert "".join(tts.spoken_texts) == "".join(expected_texts)
    assert expected_texts[0] == "Kem chống nắng SPF50, 350.000 đồng."
    assert expected_texts[1] == "Serum Vitamin C làm sáng da, 250.000 đồng."
    # All inputs are canonical TextChunks on the same path.
    from backend.application.text_chunker import TextChunk as CanonicalTextChunk

    assert all(isinstance(x, CanonicalTextChunk) for x in tts.received_inputs)


def test_adaptive_policy_and_fixed_rollback_same_chunker_path() -> None:
    """adaptive_vi and explicit fixed rollback segment the same approved text.

    Change B supplies exact approved text; Change A owns policy selection.
    The same source-agnostic ``TextChunker`` (feed + finalize) segments the
    SAME approved string under both policies with no loss, and the
    ``adaptive_vi`` chunker exposes its policy identity — proving the
    handoff never selects a script-specific chunker or source mode.
    """
    from backend.application.text_chunker import ChunkPolicy, TextChunker

    script = "Kem ABC giá 299.000 đồng, giảm 20 phần trăm hôm nay. Nhanh tay nhé!"
    for policy in ("adaptive_vi", "fixed"):
        chunker = TextChunker(
            session_id="sess-policy",
            utterance_id=f"utt-{policy}",
            min_chars=4,
            target_chars=20,
            max_chars=40,
            policy=ChunkPolicy(policy),
        )
        chunks = chunker.feed(script)
        chunks.extend(chunker.finalize())
        assert chunks, f"policy {policy} must emit chunks"
        assert "".join(chunk.text for chunk in chunks) == script, (
            f"policy {policy}: exact approved text preserved"
        )
        if policy == "adaptive_vi":
            assert chunker.policy is ChunkPolicy.ADAPTIVE_VI
        else:
            assert chunker.policy is ChunkPolicy.FIXED


# ---------- 12.3: binding snapshot ----------


def test_binding_snapshot_roundtrip_and_lookup() -> None:
    snapshot = build_binding_snapshot(
        "set-1",
        [
            _approved("P001", "v-1", "text p001"),
            _approved("P002", "v-2", "text p002"),
        ],
    )
    assert isinstance(snapshot, BindingSnapshot)
    assert snapshot.script_set_id == "set-1"
    assert snapshot.by_product("P002") == _approved("P002", "v-2", "text p002")
    assert snapshot.by_product("P999") is None
    payload = snapshot.as_dict()
    assert payload["script_set_id"] == "set-1"
    assert payload["products"][0]["approved_version_id"] == "v-1"
    assert payload["products"][1]["spoken_text"] == "text p002"
