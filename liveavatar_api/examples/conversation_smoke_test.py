"""Live conversation smoke test — full LITE turn cycle in sandbox.

Uses the stub LLM + tone TTS (no model downloads) to prove the
viewer-text -> LLM -> TTS(PCM) -> avatar loop works end-to-end against
the real sandbox. Swap llm=/tts= for real models later.

Usage:
    cd implementations
    python -m liveavatar_api.examples.conversation_smoke_test
"""

from __future__ import annotations

from liveavatar_api.backend.client import LiveAvatarClient
from liveavatar_api.backend.conversation import LiteConversation


def main() -> None:
    c = LiveAvatarClient()
    print("== LiveAvatar LITE conversation smoke test (sandbox) ==\n")
    print(f"[credits]   {c.get_credits()} left")

    convo = LiteConversation(client=c, is_sandbox=True)
    front = convo.start()
    print(f"[session]   id={front['session_id']}")
    print(f"[frontend]  livekit_url present={bool(front['livekit_url'])}, "
          f"client_token present={bool(front['livekit_client_token'])}")

    try:
        for msg in ["Xin chào shop!", "Giá kem chống nắng bao nhiêu?"]:
            reply = convo.turn(msg)
            print(f"[turn]      viewer={msg!r}")
            print(f"            avatar={reply!r}")
    finally:
        convo.stop()
        print("[session]   stopped [OK]")

    print("\n== Conversation loop OK ==")


if __name__ == "__main__":
    main()
