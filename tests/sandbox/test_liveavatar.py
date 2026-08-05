"""Sandbox-only LiveAvatar playback contracts (OpenSpec 1.50).

These tests exercise the provider-layer LiteConversation/LiteAudioAgent
seam, which is NOT selected by ordinary CI. Missing credentials must fail
loudly (the module-level guard below), never silently skip.

Migrated from ``core/tests/test_stage2_auto_demo_sequence.py``
(``test_liveavatar_turn_waits_until_avatar_finishes``).
"""

from __future__ import annotations

import os

if not os.environ.get("LIVEAVATAR_API_KEY"):
    raise RuntimeError(
        "sandbox tests require LIVEAVATAR_API_KEY; they are not part of "
        "ordinary CI and must fail loudly when credentials are missing"
    )

from backend.application.clients.avatar.liveavatar_sdk import LiteConversation


class _Agent:
    def __init__(self) -> None:
        self.wait_values: list[bool] = []

    def start_listening(self) -> None:
        pass

    def stop_listening(self) -> None:
        pass

    def stream_pcm(self, chunks, *, source_rate: int, wait: bool) -> None:
        list(chunks)
        self.wait_values.append(wait)


def test_liveavatar_turn_waits_until_avatar_finishes() -> None:
    conversation = LiteConversation(
        client=object(),
        llm=lambda _: "Xin chào",
        tts=lambda _: (b"\x00\x00" * 100, 24_000),
    )
    agent = _Agent()
    conversation.agent = agent

    conversation.turn("Giới thiệu sản phẩm")

    assert agent.wait_values == [True]
