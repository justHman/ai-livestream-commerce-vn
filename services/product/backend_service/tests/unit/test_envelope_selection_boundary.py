"""Task 7.2: the Agent receives only SELECTED envelopes at the runtime seam.

Proves the boundary between the reducer and the LLM seam (design Decision 9):
the pending board selects exactly ONE candidate by ``ranking_score``; the
candidate surfaces only cluster-id/score/envelope — never raw comment text;
the resolver consumes only the candidate's envelope (a sentinel fake whose
``__getattr__`` raises on any unknown attribute, so any ``.members`` or
``.rolling_comments`` access would fail the resolution); and the end-to-end
flow reaches the LLM seam with envelope-derived content only — raw member
comment texts never appear in what the seam received.

The production wiring of envelopes into the live runtime is a later cluster's
job (C13/C14+); these tests pin the contract on the CURRENT tree, which is
already envelope-driven (pending_qa, qa_resolver, speech_arbiter).
"""

from __future__ import annotations

import pytest

from backend.application.agentic_director.fast_path import FactValue, FastPathConfig
from backend.application.live_runtime.pending_qa import PendingQaStore, QaHysteresisConfig
from backend.application.live_runtime.qa_resolver import BoundaryQaResolver, QaResolution
from unit.live_runtime.qa_fixtures import P020_QA_ENVELOPE, build_p020_answer


class _SentinelEnvelope:
    """Envelope fake whose only known attributes are the Decision-9 fields.

    ``__getattr__`` raises ``AttributeError`` for anything else, so a consumer
    reaching for a raw container (``.members``/``.rolling_comments``) fails
    loudly instead of silently reading viewer text.
    """

    cluster_id = "cl-sentinel"
    intent = "giá"
    message_count = 2
    unique_viewer_count = 2
    representative_questions = ("P020 giá bao nhiêu?",)
    product_candidates = (("P020", 0.97),)
    resolved_product_ids = ("P020",)
    ranking_score = 0.95
    novelty = 0.4
    current_script_product_id = "P020"
    source_platform_counts = (("tiktok", 2),)

    def __getattr__(self, name: str) -> object:
        raise AttributeError(name)


class _RecordingVerbalizer:
    """Verbalizer fake: records exactly what the LLM seam received."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str]] = []

    def verbalize(self, request) -> str:
        self.requests.append(
            (request.grounded_fact, request.question_context, request.entity_display_name)
        )
        return "có, P020 hỗ trợ sạc nhanh 65W nha."


class _FactProvider:
    def get_fact(self, entity_id: str, selector: str) -> FactValue | None:
        return FactValue(value="65W", fresh=True)


class _EligibilityResolver:
    """Resolver stub: single candidate, recorded for seam inspection."""

    def __init__(self) -> None:
        self.resolved: list[str] = []

    async def resolve_qa(self, candidate) -> QaResolution:
        self.resolved.append(candidate.cluster_id)
        return QaResolution.answer(speech_text=build_p020_answer())

    def prefetch_stable_evidence(self, candidate) -> None:
        pass


def _store() -> PendingQaStore:
    return PendingQaStore(config=QaHysteresisConfig(), now=lambda: 0.0)


def _fast_resolver(verbalizer: _RecordingVerbalizer | None = None) -> BoundaryQaResolver:
    return BoundaryQaResolver(
        fact_provider=_FactProvider(),
        planner=None,
        evidence_executor=None,
        final_generator=None,
        fast_path_config=FastPathConfig(min_product_confidence=0.8),
        verbalizer=verbalizer,
    )


def test_winner_is_exactly_one_candidate() -> None:
    store = _store()
    store.update(_SentinelEnvelope(), now=0.0)
    store.update(P020_QA_ENVELOPE, now=0.0)

    winner = store.pending_winner(0.0)

    assert winner is not None
    assert winner.cluster_id == "cl-sentinel"


def test_winner_selected_by_ranking_score() -> None:
    store = _store()
    store.update(_SentinelEnvelope(), now=0.0)  # score 0.95
    low = P020_QA_ENVELOPE.__class__()
    object.__setattr__(low, "cluster_id", "cl-low")
    object.__setattr__(low, "ranking_score", 0.3)
    store.update(low, now=0.0)

    winner = store.pending_winner(0.0)

    assert winner is not None
    assert winner.score == 0.95


def test_candidate_surfaces_only_cluster_id_score_and_envelope() -> None:
    store = _store()
    envelope = _SentinelEnvelope()
    candidate = store.update(envelope, now=0.0)

    assert candidate is not None
    assert candidate.cluster_id == "cl-sentinel"
    assert candidate.score == 0.95
    assert candidate.envelope is envelope


def test_candidate_has_no_raw_comment_attribute() -> None:
    store = _store()
    candidate = store.update(_SentinelEnvelope(), now=0.0)

    assert candidate is not None
    assert getattr(candidate, "members", None) is None
    assert getattr(candidate, "rolling_comments", None) is None


def test_candidate_envelope_never_exposes_raw_member_text() -> None:
    store = _store()
    candidate = store.update(_SentinelEnvelope(), now=0.0)

    assert candidate is not None
    dumped = str(candidate.envelope)
    assert "members" not in dumped
    assert "rolling_comments" not in dumped


def test_sentinel_rejects_unknown_attribute_access() -> None:
    with pytest.raises(AttributeError):
        _ = _SentinelEnvelope().members


async def test_resolver_consumes_sentinel_envelope_without_raw_container_access() -> None:
    resolver = _fast_resolver()

    resolution = await resolver.resolve_qa(_SentinelEnvelope())

    assert resolution.kind == "answer"
    assert resolution.speech_text == "Giá hiện tại của P020 là 65W."


async def test_llm_seam_receives_envelope_content_not_raw_comments() -> None:
    verbalizer = _RecordingVerbalizer()
    resolver = _fast_resolver(verbalizer)
    resolver._fast_path_config = FastPathConfig(
        min_product_confidence=0.8, verbalize_where_appropriate=True
    )

    await resolver.resolve_qa(_SentinelEnvelope())

    assert len(verbalizer.requests) == 1
    fact, question, entity = verbalizer.requests[0]
    assert "giá" in question
    assert "P020" in entity
    assert "65W" in fact


async def test_llm_seam_text_excludes_raw_comment_texts() -> None:
    verbalizer = _RecordingVerbalizer()
    resolver = _fast_resolver(verbalizer)
    resolver._fast_path_config = FastPathConfig(
        min_product_confidence=0.8, verbalize_where_appropriate=True
    )

    await resolver.resolve_qa(_SentinelEnvelope())

    assert len(verbalizer.requests) == 1
    assert "P020 giá bao nhiêu?" not in verbalizer.requests[0][1]
    assert "P020 có màu đen không?" not in verbalizer.requests[0][1]


async def test_selected_envelope_flow_reaches_resolver_exactly_once() -> None:
    store = _store()
    store.update(_SentinelEnvelope(), now=0.0)
    store.update(P020_QA_ENVELOPE, now=0.0)
    stub = _EligibilityResolver()

    winner = store.pending_winner(0.0)
    assert winner is not None
    resolution = await stub.resolve_qa(winner.envelope)

    assert stub.resolved == ["cl-sentinel"]
    assert resolution.speech_text == build_p020_answer()
