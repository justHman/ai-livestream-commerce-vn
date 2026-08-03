"""CloudRenderBackend — LiveAvatar cloud, behind the RenderBackend seam.

Reuses the sandbox-verified `providers.liveavatar_cloud` code (LiveAvatarClient,
LiteConversation, LiteAudioAgent) without rewriting it. This adapter is the ONLY
place core/ depends on providers.liveavatar_cloud; swapping to self-host changes nothing above.

LLM/TTS injection (production):
  configure(llm=..., tts=...) accepts EITHER:
    - an LLMEngine / TTSEngine instance (preferred — full seam, swap by config)
    - a raw callable (legacy compat: fn(text)->str / fn(text)->(bytes,rate))
  The server auto-builds engines from env (LLM_ENGINE, TTS_ENGINE) at startup;
  colab_deploy can also call configure() manually with custom cfg.
"""

from __future__ import annotations

from typing import Optional, Union

from providers.liveavatar_cloud.sdk.client import LiveAvatarClient, SANDBOX_AVATAR_ID
from providers.liveavatar_cloud.service.conversation import (
    LiteConversation,
    echo_llm,
    tone_tts,
)

from .base import FullPipelineBackend, StartOptions, StartResult

# Module-level injected backends. Defaults are the lightweight stubs so the
# backend runs anywhere out of the box (offline / CI / sandbox).
_llm_fn = echo_llm
_tts_fn = tone_tts
# Raw LLMEngine + default system_prompt kept so CloudRenderBackend can build a
# per-session llm_fn with a session-specific shop profile (2-layer persona).
# Set by configure() when it receives an LLMEngine (not a pre-wrapped callable).
_llm_engine = None
_default_system_prompt: Optional[str] = None
_llm_defaults: dict = {}


def configure(
    llm: Optional[Union[object, callable]] = None,
    tts: Optional[Union[object, callable]] = None,
    system_prompt: Optional[str] = None,
    llm_defaults: Optional[dict] = None,
) -> None:
    """Inject LLM/TTS before serving.

    Accepts either an engine instance (LLMEngine / TTSEngine) or a raw callable:
      - LLMEngine  → wrapped via core.llm.to_llm_fn (applies chat template + persona)
      - TTSEngine  → wrapped via core.tts.to_tts_fn (normalizes output to pcm16)
      - callable   → used as-is (legacy compat for fn(text)->str / fn(text)->(bytes,rate))

    `system_prompt` is the default (env SHOP_PROFILE) persona; per-session shop
    profiles override it via CloudRenderBackend.set_persona().

    This duck-typing keeps the seam open: production passes engines, quick demos
    pass lambdas, and the server's env-driven path passes engines it built.
    """
    global _llm_fn, _tts_fn, _llm_engine, _default_system_prompt, _llm_defaults
    if llm is not None:
        if _is_llm_engine(llm):
            from ..llm import to_llm_fn

            _llm_engine = llm
            _default_system_prompt = system_prompt
            _llm_defaults = dict(llm_defaults or {})
            _llm_fn = to_llm_fn(llm, system_prompt=system_prompt, **_llm_defaults)
        elif callable(llm):
            _llm_engine = None
            _llm_fn = llm
        else:
            raise TypeError(f"configure(llm=...): expected LLMEngine or callable, got {type(llm)}")
    if tts is not None:
        if _is_tts_engine(tts):
            from ..tts import to_tts_fn

            _tts_fn = to_tts_fn(tts)
        elif callable(tts):
            _tts_fn = tts
        else:
            raise TypeError(f"configure(tts=...): expected TTSEngine or callable, got {type(tts)}")


def _is_llm_engine(obj) -> bool:
    """Duck-type check (avoid hard import to keep the seam decoupled)."""
    return hasattr(obj, "generate") and hasattr(obj, "name") and hasattr(obj, "from_config")


def _is_tts_engine(obj) -> bool:
    return hasattr(obj, "synthesize") and hasattr(obj, "name") and hasattr(obj, "from_config")


class CloudRenderBackend(FullPipelineBackend):
    """Renders via LiveAvatar cloud (LITE mode)."""

    name = "cloud"

    def __init__(self, client: Optional[LiveAvatarClient] = None) -> None:
        self._client = client or LiveAvatarClient()
        self._convos: dict[str, LiteConversation] = {}
        # Per-session shop-profile persona override (2-layer persona).
        # session_id -> composed persona (BASE_SALE_PERSONA + shop_profile).
        self._personas: dict[str, str] = {}

    def verify_credentials(self) -> dict:
        """Probe the cheapest authenticated LiveAvatar endpoint."""
        credits = self._client.get_credits()
        return {"credits_available": credits > 0}

    def set_persona(self, session_id: str, shop_profile: str) -> None:
        """Set a per-session shop profile persona.

        `shop_profile` is the shop info text (name, address, phone, ...) sent
        by the FE/team-SE via /lite/attach. Composed with BASE_SALE_PERSONA
        from config into the full system prompt used for this session's LLM
        calls. Overrides the env SHOP_PROFILE default.
        """
        from ..config import _build_persona

        self._personas[session_id] = _build_persona(shop_profile)

    def start(self, opts: StartOptions) -> StartResult:
        convo = LiteConversation(
            client=self._client,
            llm=_llm_fn,
            tts=_tts_fn,
            avatar_id=opts.avatar_id or SANDBOX_AVATAR_ID,
            is_sandbox=opts.is_sandbox,
        )
        front = convo.start()  # blocking; API runs this off the event loop
        self._convos[front["session_id"]] = convo
        return StartResult(
            session_id=front["session_id"],
            livekit_url=front["livekit_url"],
            livekit_client_token=front["livekit_client_token"],
            mode="LITE",
        )

    def say(self, session_id: str, text: str, generate: bool = True) -> str:
        convo = self._convos.get(session_id)
        if convo is None:
            raise KeyError(session_id)
        # Per-session persona override: rebuild llm_fn with this session's
        # composed persona if one was set via set_persona(). Falls back to the
        # default _llm_fn (env SHOP_PROFILE) when no override exists.
        persona = self._personas.get(session_id)
        if generate and persona is not None and _llm_engine is not None:
            from ..llm import to_llm_fn

            saved_llm = convo.llm
            convo.llm = to_llm_fn(_llm_engine, system_prompt=persona, **_llm_defaults)
            try:
                return convo.turn(text)
            finally:
                convo.llm = saved_llm
        if generate:
            return convo.turn(text)
        return convo.speak_verbatim(text)

    def interrupt(self, session_id: str) -> None:
        convo = self._convos.get(session_id)
        if convo is not None and convo.agent is not None:
            convo.agent.interrupt()

    def stop(self, session_id: str) -> None:
        convo = self._convos.pop(session_id, None)
        self._personas.pop(session_id, None)
        if convo is None:
            raise KeyError(session_id)
        convo.stop()

    def stop_all(self) -> None:
        for session_id in list(self._convos):
            self.stop(session_id)

    def has(self, session_id: str) -> bool:
        return session_id in self._convos
