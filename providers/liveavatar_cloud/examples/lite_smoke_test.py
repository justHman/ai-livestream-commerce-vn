"""LITE-mode live smoke test — exercises the full audio WebSocket path.

Runs against the REAL LiveAvatar sandbox (free, ~1-min, no credits):

  1. create LITE session token -> start -> get ws_url
  2. connect the LiteAudioAgent, wait for `connected`
  3. start_listening -> stop_listening
  4. send a 24 kHz test tone via agent.speak / agent.speak_end
  5. wait for agent.speak_ended
  6. stop session

If the test tone path works end-to-end, the audio wiring + format are
correct and any real TTS that outputs 24 kHz PCM will work.

Usage:
    cd implementations
    python -m providers.liveavatar_cloud.examples.lite_smoke_test
"""

from __future__ import annotations

import time

from providers.liveavatar_cloud.sdk import audio
from providers.liveavatar_cloud.sdk.client import LiveAvatarClient, SANDBOX_AVATAR_ID
from providers.liveavatar_cloud.service.lite_agent import LiteAudioAgent


def main() -> None:
    c = LiveAvatarClient()
    print("== LiveAvatar LITE live smoke test (sandbox) ==\n")
    print(f"[credits]   {c.get_credits()} left (LITE = 1 credit/min)")

    body = c.build_lite_token(avatar_id=SANDBOX_AVATAR_ID, is_sandbox=True)
    tok = c.create_session_token(body)
    started = c.start_session(tok.session_token)
    print(f"[session]   id={tok.session_id}")
    print(f"[session]   ws_url present={bool(started.ws_url)}")
    assert started.ws_url, "LITE start must return ws_url"

    events: list[str] = []
    agent = LiteAudioAgent(started.ws_url, on_event=lambda e: events.append(e.get("type", "?")))

    try:
        agent.connect(timeout=15.0)
        print("[ws]        connected [OK]")

        agent.start_listening()
        time.sleep(0.5)
        agent.stop_listening()
        print("[ws]        listening toggled [OK]")

        tone = audio.test_tone(seconds=1.0, freq=440)
        print(f"[audio]     sending {len(tone)} bytes PCM 24kHz ({len(tone)//audio.BYTES_PER_SEC*1000 or 1000}ms tone)")
        agent.speak_pcm(tone, wait=True)
        print("[ws]        speak_ended received [OK]" if agent._speak_ended.is_set()
              else "[ws]        speak_end sent (no speak_ended within timeout)")

    finally:
        agent.close()
        c.stop_session(tok.session_token)
        print("[session]   stopped [OK]")

    print(f"\n[events]    server events seen: {sorted(set(events))}")
    print("== LITE audio path OK ==")


if __name__ == "__main__":
    main()
