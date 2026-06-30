"""Smoke test — exercise the LiveAvatar API end-to-end in sandbox mode.

Runs the full backend lifecycle against the real API using the sandbox
avatar (free, ~1-min sessions, no credits):

  1. health: confirm API key loaded
  2. discovery: list voices (and avatars, likely empty on a fresh account)
  3. context: create a Vietnamese live-commerce host context
  4. FULL: create token -> start -> (frontend would join LiveKit) -> stop
  5. LITE: create token -> start (note ws_url) -> stop

Usage:
    cd implementations
    python -m providers.liveavatar_cloud.examples.smoke_test
"""

from __future__ import annotations

from providers.liveavatar_cloud.sdk.client import LiveAvatarClient, SANDBOX_AVATAR_ID


def _short(token: str, keep: int = 6) -> str:
    """Redact a secret for safe printing."""
    if not token:
        return "(empty)"
    return f"{token[:keep]}...<redacted>...({len(token)} chars)"


def main() -> None:
    c = LiveAvatarClient()
    print("== LiveAvatar API smoke test (sandbox) ==\n")

    # 1. Discovery
    voices = c.list_voices()
    avatars = c.list_avatars()
    print(f"[discovery] voices={len(voices)}  avatars={len(avatars)}")
    if voices:
        v = voices[0]
        print(f"            sample voice: {v['name']} ({v['language']}, {v['gender']})")

    # 2. Context (the avatar's brain in FULL mode)
    ctx_id = c.create_context(
        name="VN Live Commerce Host (smoke)",
        prompt=(
            "Ban la MC ban hang livestream tieng Viet cho my pham. "
            "Tra loi ngan gon, nhiet tinh, tap trung vao san pham va khuyen mai."
        ),
        opening_text="Xin chao ca nha! Hom nay shop co deal cuc hot nha!",
    )
    print(f"[context]   created context_id={ctx_id}")

    # 3. FULL-mode lifecycle
    print("\n-- FULL mode --")
    full_body = c.build_full_token(
        avatar_id=SANDBOX_AVATAR_ID,
        context_id=ctx_id,
        language="en",  # sandbox avatar/voice are EN; see README note on VN
        is_sandbox=True,
    )
    full_tok = c.create_session_token(full_body)
    print(f"[full]      session_id={full_tok.session_id}")
    print(f"[full]      session_token={_short(full_tok.session_token)}")
    full_started = c.start_session(full_tok.session_token)
    print(f"[full]      livekit_url={full_started.livekit_url}")
    print(f"[full]      client_token={_short(full_started.livekit_client_token)}")
    print(f"[full]      ws_url={full_started.ws_url}  (None expected for FULL)")
    c.stop_session(full_tok.session_token)
    print("[full]      stopped OK")

    # 4. LITE-mode lifecycle
    print("\n-- LITE mode --")
    lite_body = c.build_lite_token(avatar_id=SANDBOX_AVATAR_ID, is_sandbox=True)
    lite_tok = c.create_session_token(lite_body)
    print(f"[lite]      session_id={lite_tok.session_id}")
    lite_started = c.start_session(lite_tok.session_token)
    print(f"[lite]      livekit_url={lite_started.livekit_url}")
    print(f"[lite]      ws_url={lite_started.ws_url}  (audio channel for your TTS)")
    c.stop_session(lite_tok.session_token)
    print("[lite]      stopped OK")

    print("\n== All lifecycle calls succeeded ==")


if __name__ == "__main__":
    main()
