"""``cloud_bithuman`` render backend (bitHuman cloud avatar), the third BYOK provider.

Same architecture as ``cloud_simli`` / ``cloud_lemonslice``: the bitHuman avatar joins a
Livento-owned LiveKit room (room name = runtime session id) and the Runtime stays the single
speaker, sending guarded TTS PCM over the avatar data-stream protocol (``AvatarAudioChannel``,
topic ``lk.audio_stream``, 16 kHz PCM16 mono). This class SUBCLASSES ``LemonSliceRenderBackend``;
room/loop thread, JWT minting (identity ``bithuman-avatar-agent`` + ``lk.publish_on_behalf``),
ready gate, audio channel, interrupt (``lk.clear_buffer``), stop/stop_all, near-cap guard, the
terminate retry loop and cost evidence are the LemonSlice code. Only what differs is overridden:

* start = ONE plain HTTPS POST ``{base}/v1/runtime-tokens/request`` (header ``api-secret``);
* end = ``POST {base}/v1/runtime-sessions/{session_id}/end`` (stops billing at once) through the
  inherited terminate loop (``_control_header`` / ``_control_body`` / ``terminate_path``), then the
  room is left; no keep-alive and no rollover (as Simli).

Default OFF: ``BITHUMAN_ENABLED`` must be true for the router to build it. The POST is never
retried (a retry could start a second billable session); no key, token or URL is logged.

UNVERIFIED ASSUMPTIONS (no paid bitHuman account was available; nothing ran against the service):
  B1  ``POST {BITHUMAN_API_BASE}/v1/runtime-tokens/request``, header ``api-secret``, JSON
      ``{mode:"gpu", model, agent_id, livekit_url, livekit_token, room_name}``; response has
      ``session_id`` (top level or under ``data``). A missing/odd id fails the start.
  B2  End: ``POST /v1/runtime-sessions/{session_id}/end`` with header ``api-secret`` and an empty
      JSON body; 2xx or 404 = ended.
  B3  Statuses: 401 invalid key; 403 body ``PLAN_REQUIRED`` -> out of credit,
      ``CONCURRENCY_LIMIT_REACHED`` -> concurrency limit, other 403 -> invalid key;
      404 / 409 (``MODEL_NOT_GENERATED``, ``AGENT_NOT_READY``) -> avatar not found/ready;
      503 (``Retry-After``) -> provider unavailable. The code field name inside the 403 body is
      searched as text, not parsed by key.
  B4  Audio: ``lk.audio_stream`` takes 16 kHz mono PCM16; the worker honours ``lk.clear_buffer``
      and emits playback RPCs like the other providers (else ``playback_unconfirmed`` recovery).
  B5  Idle and maximum session duration are UNKNOWN: ``BITHUMAN_MAX_SESSION_S`` is the Runtime
      near-cap guard only, ``BITHUMAN_IDLE_BOUND_S`` is the end bound of a derived cost row.
  B6  Agent code format ``[A-Za-z0-9_-]{3,64}``; model default ``essence-2`` (``BITHUMAN_MODEL``).

Runbook: ``docs/runbooks/byok-avatar-runtime.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.application.clients.avatar.byok import (
    CONCURRENCY_LIMIT,
    NOT_FOUND,
    OUT_OF_CREDIT,
    PROVIDER_UNAVAILABLE,
    status_to_avatar_code,
)
from backend.application.clients.avatar.lemonslice import (
    _SAFE_SESSION_ID,
    LemonSliceError,
    LemonSliceRenderBackend,
    LemonSliceSettings,
)
from backend.application.clients.avatar.simli import AsyncHttpPost, SimliRenderBackend
from backend.application.render.engines_base import StartOptions

log = logging.getLogger(__name__)

_AGENT_CODE = re.compile(r"[A-Za-z0-9_-]{3,64}")
_END_PATH = "v1/runtime-sessions/{session_id}/end"


@dataclass(frozen=True)
class BitHumanSettings:
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str
    bithuman_api_key: str = field(default="", repr=False)
    api_base: str = "https://api.bithuman.ai"
    agent_id: str = ""
    model: str = "essence-2"
    avatar_identity: str = "bithuman-avatar-agent"
    runtime_identity: str = "livento-runtime"
    max_session_s: float = 3600.0
    idle_bound_s: int = 120
    ready_timeout_s: float = 30.0
    request_timeout_s: float = 15.0
    speak_ready_gate_enabled: bool = True
    speak_ready_wait_s: float = 45.0
    fallback_publish: bool = False
    render_offset_ms: int = 0
    ready_ttl_s: float = 1200.0
    payment_backoff_s: float = 3600.0

    def __post_init__(self) -> None:
        for name in ("max_session_s", "request_timeout_s"):
            v = getattr(self, name)
            if (
                isinstance(v, bool)
                or not isinstance(v, int | float)
                or not math.isfinite(v)
                or v <= 0
            ):
                raise ValueError(f"{name} must be a finite, positive number")
        if not isinstance(self.idle_bound_s, int) or isinstance(self.idle_bound_s, bool):
            raise ValueError("idle_bound_s must be an integer >= 1")
        if self.idle_bound_s < 1:
            raise ValueError("idle_bound_s must be an integer >= 1")
        if self.agent_id and not _AGENT_CODE.fullmatch(self.agent_id):
            raise ValueError("agent_id has an invalid format")

    def __repr__(self) -> str:  # never print secrets
        return "BitHumanSettings(<redacted>)"

    def as_lemonslice(self) -> LemonSliceSettings:
        """Shared-machinery view: keep-alive, rollover and budget stay off; terminate = end."""
        return LemonSliceSettings(
            livekit_url=self.livekit_url,
            livekit_api_key=self.livekit_api_key,
            livekit_api_secret=self.livekit_api_secret,
            lemonslice_api_key=self.bithuman_api_key,
            api_base=self.api_base,
            agent_id=self.agent_id,
            audio_sample_rate=16000,
            idle_timeout_s=self.idle_bound_s,
            ready_timeout_s=self.ready_timeout_s,
            speak_ready_gate_enabled=self.speak_ready_gate_enabled,
            speak_ready_wait_s=self.speak_ready_wait_s,
            request_timeout_s=self.request_timeout_s,
            avatar_identity=self.avatar_identity,
            runtime_identity=self.runtime_identity,
            fallback_publish=self.fallback_publish,
            render_offset_ms=self.render_offset_ms,
            terminate_path=_END_PATH,
            keepalive_s=0.0,
            max_session_s=self.max_session_s,
            rollover_after_s=0.0,
            ready_ttl_s=self.ready_ttl_s,
            payment_backoff_s=self.payment_backoff_s,
        )


def status_to_code(status: int, body: Any) -> str | None:
    """bitHuman status (+ 403 body text) -> stable avatar code (B3)."""
    if status == 403:
        text = json.dumps(body, default=str) if body is not None else ""
        if "CONCURRENCY_LIMIT_REACHED" in text:
            return CONCURRENCY_LIMIT
        if "PLAN_REQUIRED" in text:
            return OUT_OF_CREDIT
    if status == 409:
        return NOT_FOUND
    return status_to_avatar_code(status)


class BitHumanRenderBackend(LemonSliceRenderBackend):
    name = "cloud_bithuman"
    start_mode = "BITHUMAN"
    cost_provider = "bithuman"
    _control_header = "api-secret"
    # no replacement session (as Simli): recovery only flushes the avatar's buffer
    _maybe_rollover = SimliRenderBackend._maybe_rollover

    @staticmethod
    def _control_body(event: str) -> dict[str, Any]:
        return {}

    def __init__(
        self,
        settings: BitHumanSettings,
        *,
        room_factory: Callable[[], Any] | None = None,
        async_http_post: AsyncHttpPost | None = None,
        audio_track_factory: Callable[[Any, int], Any] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bh = settings
        super().__init__(
            settings.as_lemonslice(),
            room_factory=room_factory,
            session_client_factory=lambda: None,  # LemonSlice plugin client: never used here
            async_http_post=async_http_post,
            audio_track_factory=audio_track_factory,
            clock=clock,
            monotonic=monotonic,
        )

    def _credit_error(self) -> LemonSliceError:
        return LemonSliceError(OUT_OF_CREDIT, f"{OUT_OF_CREDIT}: bitHuman out of credit")

    def _resolve_avatar(self, opts: StartOptions) -> tuple[str | None, str | None]:
        bh = self._bh
        if opts.avatar_provider not in (None, "bithuman"):
            raise LemonSliceError("avatar_provider_invalid", "avatar_provider is not supported")
        if opts.avatar_image_url:
            raise LemonSliceError(
                "avatar_source_invalid", "bitHuman needs an agent code, not an image url"
            )
        if opts.avatar_agent_id and opts.avatar_provider != "bithuman":
            raise LemonSliceError(
                "avatar_provider_invalid", "a per-session avatar needs avatar_provider=bithuman"
            )
        if opts.avatar_id and opts.avatar_id != bh.agent_id:
            raise LemonSliceError("avatar_not_allowed", "avatar_id is not the configured agent")
        agent = opts.avatar_agent_id or bh.agent_id
        if not agent:
            raise LemonSliceError("agent_not_configured", "BITHUMAN_AGENT_ID is not configured")
        if not _AGENT_CODE.fullmatch(agent):
            raise LemonSliceError("agent_id_invalid", "avatar_agent_id has an invalid format")
        return agent, None

    async def _request_session(
        self,
        agent_id: str | None,
        image_url: str | None,
        avatar_token: str,
        room_sid: str,
        api_key: str | None = None,
        room_name: str = "",
    ) -> Any:
        """Start the provider session: one POST (B1). Returns the provider session id."""
        bh = self._bh
        self._check_creation(api_key)
        assert agent_id and self._async_post is not None
        key = bh.bithuman_api_key if api_key is None else api_key
        url = f"{bh.api_base.rstrip('/')}/v1/runtime-tokens/request"
        body = {
            "mode": "gpu",
            "model": bh.model,
            "agent_id": agent_id,
            "livekit_url": bh.livekit_url,
            "livekit_token": avatar_token,
            "room_name": room_name or room_sid,
        }
        failure: Exception | None = None
        status, data = 0, {}
        try:
            status, data = await asyncio.wait_for(
                self._async_post(url, {"api-secret": key}, body, bh.request_timeout_s),
                bh.request_timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = exc
        # raised outside the except block: no __context__, never the original message (URL)
        if failure is not None:
            code = PROVIDER_UNAVAILABLE if api_key is not None else "session_request_failed"
            raise LemonSliceError(
                code, f"bitHuman request failed error_type={type(failure).__name__}"
            )
        if not 200 <= status < 300:
            code = status_to_code(status, data)
            if code == OUT_OF_CREDIT and api_key is None:
                self._trip_credit()
            if code is not None and (api_key is not None or code == OUT_OF_CREDIT):
                raise LemonSliceError(code, f"{code}: bitHuman status={status}")
            raise LemonSliceError(
                "session_request_failed", f"bitHuman request failed status={status}"
            )
        data = data if isinstance(data, dict) else {}
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        sid = payload.get("session_id") if isinstance(payload, dict) else None
        if not isinstance(sid, str) or not _SAFE_SESSION_ID.fullmatch(sid):
            raise LemonSliceError("session_id_invalid", "bitHuman returned an unusable session id")
        self._register_tenant_key(sid, api_key)
        return sid
