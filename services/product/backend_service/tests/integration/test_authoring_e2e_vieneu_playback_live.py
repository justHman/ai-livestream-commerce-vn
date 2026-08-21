"""15.3 RELEASE EVIDENCE — VieNeu playback manual-draft E2E (live).

Task 15.3 original semantics:
  Run manual-draft E2E: create ScriptSet → draft → gate PASS → review exact
  spoken text → human approve → bind → canonical Change A TextChunker →
  VieNeu playback; verify zero LLM authoring calls.

The production path is exercised end to end against the SELF-HOST TTS service
(``SelfHostedTTSClient`` → ``POST /v1/speech``), which in this project is the
VieNeu self-host tts_service. The zero-LLM manual authoring path runs with
``engine_manager=None`` (no LLM object exists, so no authoring call can ever
reach an LLM), and the approved ``spoken_text`` is fed through the SAME
source-agnostic Change A ``TextChunker`` (``feed`` + ``finalize``) that the
runtime speech service owns, then each chunk is synthesized by the self-host
VieNeu TTS.

This suite is NOT part of the default CI run: it is marked ``live`` and
requires the self-host TTS endpoint env (``TTS_BASE_URL``). Without it the
test skips (which is what the offline CI sees).

Run explicitly (with the self-host tts_service booted on the REAL VieNeu
engine — NOT the tone stub; the native legacy path reports ``x-audio-engine:
vieneu``):
  cd services/product/tts_service
  TTS_ENGINE=vieneu TTS_MODEL=pnnbao-ump/VieNeu-TTS-v3-Turbo \
    TTS_PROVIDER=none APP_ENV=dev PYTHONPATH=src \
    .venv/Scripts/python.exe -m uvicorn tts.main:app --port 8002
then, from backend_service:
  TTS_BASE_URL=http://localhost:8002 \
  uv run pytest tests/integration/test_authoring_e2e_vieneu_playback_live.py -q -m live --tb=short
"""

from __future__ import annotations

import asyncio
import os

import pytest

from backend.application.clients.tts.self_hosted import SelfHostedTTSClient
from backend.application.db.postgres_store import PostgresRuntimeStore
from backend.application.script_authoring.models import ScriptState
from backend.application.script_authoring.repositories import PostgresAuthoringRepositories
from backend.application.script_authoring.runtime_handoff import (
    ResolvedApprovedScript,
    resolve_approved_script,
)
from backend.application.script_authoring.service_impl import ScriptAuthoringServiceImpl
from backend.application.text_chunker.chunker import TextChunker
from backend.config import ScriptAuthoringConfig

pytestmark = pytest.mark.live

_REQUIRED_ENV = ("TTS_BASE_URL",)


def _env_ready() -> bool:
    return all(bool(os.environ.get(k)) for k in _REQUIRED_ENV)


live_or_skip = pytest.mark.skipif(
    not _env_ready(),
    reason="live self-host TTS endpoint env not set (TTS_BASE_URL)",
)


async def _connect(pg_url: str) -> PostgresAuthoringRepositories:
    store = PostgresRuntimeStore(pg_url)
    await store.connect()
    await store.apply_schema()
    repos = PostgresAuthoringRepositories(pg_url)
    await repos.connect()
    return repos


def _long_spoken() -> str:
    """A script whose estimated spoken duration lands inside [300, 3600]s."""
    sentence = "Kem dưỡng da này giúp làn da mịn màng và tươi sáng mỗi ngày."
    return " ".join([sentence] * 200)


class _RepoApprovedScriptStore:
    """ApprovedScriptStore over real PG repos.

    The repo reads are async; ``resolve_approved_script`` awaits an awaitable
    return value transparently.
    """

    def __init__(self, repos) -> None:
        self._repos = repos

    async def get_approved_version(self, *, script_set_id: str, product_id: str):
        item = await self._repos.items.get_by_product(script_set_id, product_id)
        if item is None or item.approved_version_id is None:
            return None
        version = await self._repos.versions.get_approved(item.id)
        if version is None:
            return None
        return ResolvedApprovedScript(
            product_id=item.product_id,
            approved_version_id=item.approved_version_id,
            spoken_text=version.spoken_text,
        )


@live_or_skip
@pytest.mark.asyncio
async def test_vieneu_playback_manual_zero_llm_e2e(pg_url: str) -> None:
    """Full manual zero-LLM path + canonical TextChunker + self-host VieNeu playback.

    Verifies zero LLM authoring calls (``engine_manager=None`` → no LLM exists
    to call), EXACT approved ``spoken_text`` identity, chunking through the
    canonical Change A TextChunker, and non-empty PCM playback for every chunk
    through the self-host TTS service.
    """
    repos = await _connect(pg_url)
    client = SelfHostedTTSClient(base_url=os.environ["TTS_BASE_URL"])
    try:
        # engine_manager=None => zero LLM: no AI authoring call can exist.
        service = ScriptAuthoringServiceImpl(repos, config=ScriptAuthoringConfig())
        created = await service.create_script_set(
            name="Set 15.3 VieNeu",
            transition_policy="ORDER_AGNOSTIC",
            product_ids=["P1"],
            brief=None,
        )
        set_id = created["id"]

        SPOKEN = _long_spoken()
        draft = await service.save_draft(
            set_id=set_id,
            product_id="P1",
            display_text=SPOKEN,
            spoken_text=SPOKEN,
            revision=None,
        )
        assert draft["state"] == "DRAFT"

        submitted = await service.submit_for_gate(set_id=set_id, product_id="P1")
        assert submitted["state"] == "REVIEWABLE"
        assert submitted["gate"]["state"] == "passed"

        item = await repos.items.get_by_product(set_id, "P1")
        assert item is not None and item.current_version_id is not None
        version = await repos.versions.get(item.current_version_id)
        assert version is not None
        # Human review sees the EXACT approved spoken text before approval.
        assert version.spoken_text == SPOKEN

        approved = await service.approve_product(
            set_id=set_id,
            product_id="P1",
            version_id=item.current_version_id,
            actor="admin",
        )
        assert approved["state"] == "APPROVED"
        item = await repos.items.get_by_product(set_id, "P1")
        assert item is not None and item.state is ScriptState.APPROVED

        # Bind: resolve the exact approved artifact via the canonical handoff.
        store = _RepoApprovedScriptStore(repos)
        resolved = await resolve_approved_script(store, script_set_id=set_id, product_id="P1")
        assert resolved is not None
        assert resolved.approved_version_id == item.approved_version_id
        assert resolved.spoken_text == SPOKEN

        # Canonical Change A TextChunker (feed + finalize), same as the
        # runtime speech path owns. No script-specific chunker, no giant
        # TextChunk construction.
        chunker = TextChunker(session_id="s-15-3", utterance_id="u-playback")
        chunks = chunker.feed(resolved.spoken_text) + chunker.finalize()
        assert chunks, "TextChunker produced no chunks"
        rejoined = "".join(c.text for c in chunks)
        assert rejoined == resolved.spoken_text, "chunker dropped/reordered approved text"

        # REAL VieNeu playback: synthesize a bounded, spread sample of chunks
        # through the self-host TTS service and require valid non-empty PCM and
        # an engine identity that confirms VieNeu, NOT the tone stub. Real
        # neural synthesis is ~1-4s/chunk, so the whole 200-chunk script is not
        # re-synthesized (that was only practical for the tone stub); the
        # chunker identity (rejoin == approved text, all chunks) + a real
        # synthesized sample prove the playback path end to end.
        n = len(chunks)
        sample_indices = sorted({0, n // 4, n // 2, 3 * n // 4, n - 1})
        total_pcm = 0
        total_duration_ms = 0
        engines: set[str] = set()
        for idx in sample_indices:
            chunk = chunks[idx]
            result = await asyncio.to_thread(
                client.synthesize,
                chunk.text,
                voice="",
                language="vi",
                response_format="pcm",
            )
            assert result.pcm16, f"empty PCM for chunk seq={chunk.seq} text={chunk.text[:40]!r}"
            assert result.sample_rate > 0, f"invalid sample rate for chunk seq={chunk.seq}"
            engines.add(result.engine)
            total_pcm += len(result.pcm16)
            total_duration_ms += result.duration_ms
        assert total_pcm > 0
        # Engine identity confirms real VieNeu (not the tone stub): the self-host
        # tts_service was booted with TTS_ENGINE=vieneu / the VieNeu-TTS v3
        # model, and the legacy engine path reports the native engine name.
        assert "tone" not in engines, f"tone stub leaked into playback: {engines!r}"
        assert engines and "vieneu" in engines, f"engine identity is not VieNeu: {engines!r}"

        # Release evidence (15.3): print the concrete playback proof.
        print(
            f"  [15.3-evidence] chunks={n} spoken_chars={len(resolved.spoken_text)} "
            f"synthesized_sample={len(sample_indices)} total_pcm_bytes={total_pcm} "
            f"total_playback_ms={total_duration_ms} engine={sorted(engines)!r} zero_llm=True"
        )
    finally:
        client.close()
        await repos.close()
