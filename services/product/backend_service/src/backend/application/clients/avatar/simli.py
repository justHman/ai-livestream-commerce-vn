"""``cloud_simli`` render backend (Simli Trinity-2), the second provider next to LemonSlice.

Same architecture as ``cloud_lemonslice``: the Simli avatar joins a Livento-owned LiveKit room
(room name = runtime session id), the Runtime stays the single speaker and sends guarded TTS PCM
over the LiveKit avatar data-stream protocol (``AvatarAudioChannel``, topic ``lk.audio_stream``,
16 kHz PCM16 mono). This class SUBCLASSES ``LemonSliceRenderBackend``: the room/loop thread,
JWT minting, ready gate, audio channel, interrupt, stop/stop_all, near-cap guard and cost
evidence are the LemonSlice code, unchanged. Only what differs is overridden:

* session start = two plain HTTPS POSTs (no AgentSession, no livekit-agents, no plugin):
  ``POST {base}/compose/token`` then ``POST {base}/integrations/livekit/agents``;
* no terminate endpoint: ``stop`` leaves the room, which ends the avatar;
* no replacement session (rollover) and no keep-alive.

Errors reuse ``LemonSliceError`` (the coordinator catches that type). Neither POST is ever
retried (a retried POST could start a second billable session); no key, token or URL is logged.

UNVERIFIED ASSUMPTIONS (gate external proof, not local implementation; behaviour is configurable
and fails safe). Nothing here was run against the real Simli service.
  S1  ``POST {SIMLI_API_BASE}/compose/token``, header ``x-simli-api-key``, JSON ``faceId``,
      ``handleSilence``, ``maxSessionLength``, ``maxIdleTime`` plus optional Trinity-2 fields
      (``streaming_width/height``, ``avatar_position_x/y``, ``background_id``, ``foreground_id``,
      ``camera``) sent only when configured; response ``session_token`` (+ optional ``warnings``).
  S2  ``POST {base}/integrations/livekit/agents`` JSON ``session_token``, ``livekit_token``,
      ``livekit_url``; response ``session_id`` (a missing/odd id fails the start).
  S3  No terminate endpoint. The avatar leaves when our participant leaves (``stop`` disconnects
      the Runtime participant; the room is NOT deleted: no LiveKit room-service helper exists).
      That the avatar really leaves is not verified from our side, so cost rows are settled as
      ``derived`` with the idle bound as the latest end; ``maxIdleTime`` is the server backstop.
  S4  The Simli worker honours ``lk.clear_buffer`` and emits ``lk.playback_started`` /
      ``lk.playback_finished`` (the official plugin relies on the same protocol; the docs do not
      say so). If the RPCs do not arrive, the existing LemonSlice behaviour applies
      (``playback_unconfirmed``, then recovery).
  S5  ``lk.audio_stream`` takes 16 kHz PCM16 mono; ``AvatarAudioChannel`` resamples to it.
  S6  ``maxSessionLength``: the true cap for >= 60 min is unknown. We send
      ``SIMLI_MAX_SESSION_S`` + 60 s so the Runtime near-cap guard (``SIMLI_MAX_SESSION_S``)
      fires first and the live ends cleanly instead of the provider cutting it.
  S7  Idle: ``maxIdleTime`` = seconds without speech before the avatar leaves; ``handleSilence``
      renders idle video. Whether silence PCM resets the timer is unknown, so no keep-alive
      exists; keep ``SIMLI_MAX_IDLE_S`` above the longest pause between utterances.
  S8  JWT claim names for ``kind=agent`` / ``lk.publish_on_behalf`` (same as LemonSlice, LK).
  S9  Face id format (``[A-Za-z0-9_-]{8,64}``) and the HTTP status for exhausted credit (402).
  S10 No rollover: a vanished avatar cannot be replaced (no terminate to make a second one
      safe). ``near_cap`` / ``avatar_absent`` end the live through the existing guards.

Runbook: ``docs/runbooks/simli-backend.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from backend.application.clients.avatar.byok import PROVIDER_UNAVAILABLE
from backend.application.clients.avatar.lemonslice import (
    _CREDIT_CODE,
    _SAFE_SESSION_ID,
    LemonSliceError,
    LemonSliceRenderBackend,
    LemonSliceSettings,
    ProviderCleanupIncomplete,
    _Sess,
)
from backend.application.render.engines_base import StartOptions
from backend.application.usage_evidence.cost import cost_meter

log = logging.getLogger(__name__)

SESSION_LENGTH_MARGIN_S = 60  # provider cap sits above the Runtime near-cap guard (S6)
_FACE_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
_ASPECTS = (1.0, 16 / 9, 9 / 16)

AsyncHttpPost = Callable[
    [str, dict[str, str], dict[str, Any], float], Awaitable[tuple[int, dict[str, Any]]]
]


@dataclass(frozen=True)
class SimliSettings:
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str
    simli_api_key: str = field(repr=False)
    api_base: str = "https://api.simli.ai"
    face_id: str = ""
    avatar_identity: str = "simli-avatar-agent"
    runtime_identity: str = "livento-runtime"
    max_session_s: float = 3600.0
    max_idle_s: int = 120
    streaming_width: int | None = None
    streaming_height: int | None = None
    avatar_position_x: float | None = None
    avatar_position_y: float | None = None
    background_id: str = ""
    foreground_id: str = ""
    camera: dict[str, Any] | None = None
    ready_timeout_s: float = 30.0  # also bounds the start (room join + avatar video)
    request_timeout_s: float = 15.0  # ONE POST; the start allows two
    speak_ready_gate_enabled: bool = True
    speak_ready_wait_s: float = 45.0
    fallback_publish: bool = False
    render_offset_ms: int = 0
    ready_ttl_s: float = 600.0
    payment_backoff_s: float = 3600.0

    def __post_init__(self) -> None:
        if not isinstance(self.max_idle_s, int) or isinstance(self.max_idle_s, bool):
            raise ValueError("max_idle_s must be an integer >= 1")
        if self.max_idle_s < 1:
            raise ValueError("max_idle_s must be an integer >= 1")
        for name in ("max_session_s", "request_timeout_s"):
            v = getattr(self, name)
            if (
                isinstance(v, bool)
                or not isinstance(v, int | float)
                or not math.isfinite(v)
                or v <= 0
            ):
                raise ValueError(f"{name} must be a finite, positive number")
        if (self.streaming_width is None) != (self.streaming_height is None):
            raise ValueError("streaming_width and streaming_height must be set together")
        if self.streaming_width is not None and self.streaming_height is not None:
            w, h = self.streaming_width, self.streaming_height
            if min(w, h) < 512:
                raise ValueError("streaming_width/height must be >= 512")
            if not any(abs(w / h - a) / a < 0.01 for a in _ASPECTS):
                raise ValueError("streaming_width/height ratio must be 1:1, 16:9 or 9:16")
        for name in ("avatar_position_x", "avatar_position_y"):
            v = getattr(self, name)
            if v is not None and not (isinstance(v, int | float) and 0 <= v <= 1):
                raise ValueError(f"{name} must be between 0 and 1")
        if self.face_id and not _FACE_ID.fullmatch(self.face_id):
            raise ValueError("face_id has an invalid format")

    def __repr__(self) -> str:  # never print secrets
        return "SimliSettings(<redacted>)"

    def as_lemonslice(self) -> LemonSliceSettings:
        """Shared-machinery view: keep-alive, rollover, terminate and budget stay off."""
        return LemonSliceSettings(
            livekit_url=self.livekit_url,
            livekit_api_key=self.livekit_api_key,
            livekit_api_secret=self.livekit_api_secret,
            lemonslice_api_key=self.simli_api_key,
            api_base=self.api_base,
            agent_id=self.face_id,
            audio_sample_rate=16000,
            idle_timeout_s=self.max_idle_s,
            ready_timeout_s=self.ready_timeout_s,
            speak_ready_gate_enabled=self.speak_ready_gate_enabled,
            speak_ready_wait_s=self.speak_ready_wait_s,
            request_timeout_s=self.request_timeout_s * 2 + 1,  # two sequential POSTs
            avatar_identity=self.avatar_identity,
            runtime_identity=self.runtime_identity,
            fallback_publish=self.fallback_publish,
            render_offset_ms=self.render_offset_ms,
            terminate_path="",
            keepalive_s=0.0,
            max_session_s=self.max_session_s,
            rollover_after_s=0.0,
            ready_ttl_s=self.ready_ttl_s,
            payment_backoff_s=self.payment_backoff_s,
        )


def build_token_body(sm: SimliSettings, face_id: str) -> dict[str, Any]:
    """``compose/token`` JSON body; optional Trinity-2 fields only when configured (S1, S6)."""
    body: dict[str, Any] = {
        "faceId": face_id,
        "handleSilence": True,
        "maxSessionLength": int(math.ceil(sm.max_session_s)) + SESSION_LENGTH_MARGIN_S,
        "maxIdleTime": sm.max_idle_s,
    }
    optional = {
        "streaming_width": sm.streaming_width,
        "streaming_height": sm.streaming_height,
        "avatar_position_x": sm.avatar_position_x,
        "avatar_position_y": sm.avatar_position_y,
        "background_id": sm.background_id or None,
        "foreground_id": sm.foreground_id or None,
        "camera": sm.camera,
    }
    body.update({k: v for k, v in optional.items() if v is not None})
    return body


class SimliRenderBackend(LemonSliceRenderBackend):
    name = "cloud_simli"
    start_mode = "SIMLI"
    cost_provider = "simli"

    def __init__(
        self,
        settings: SimliSettings,
        *,
        room_factory: Callable[[], Any] | None = None,
        async_http_post: AsyncHttpPost | None = None,
        audio_track_factory: Callable[[Any, int], Any] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sm = settings
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
        return LemonSliceError(_CREDIT_CODE, f"{_CREDIT_CODE}: Simli hết credit")

    def _resolve_avatar(self, opts: StartOptions) -> tuple[str | None, str | None]:
        sm = self._sm
        if opts.avatar_provider not in (None, "simli"):
            raise LemonSliceError("avatar_provider_invalid", "avatar_provider is not supported")
        if opts.avatar_image_url:
            raise LemonSliceError(
                "avatar_source_invalid", "Simli needs a face id, not an image url"
            )
        if opts.avatar_agent_id and opts.avatar_provider != "simli":
            raise LemonSliceError(
                "avatar_provider_invalid", "a per-session avatar needs avatar_provider=simli"
            )
        if opts.avatar_id and opts.avatar_id != sm.face_id:
            raise LemonSliceError("avatar_not_allowed", "avatar_id is not the configured face")
        face = opts.avatar_agent_id or sm.face_id
        if not face:
            raise LemonSliceError("agent_not_configured", "SIMLI_FACE_ID is not configured")
        if not _FACE_ID.fullmatch(face):
            raise LemonSliceError("agent_id_invalid", "avatar_agent_id has an invalid format")
        return face, None

    def _token_body(self, face_id: str) -> dict[str, Any]:
        return build_token_body(self._sm, face_id)

    async def _simli_post(
        self,
        path: str,
        headers: dict[str, str],
        body: dict[str, Any],
        stage: str,
        tenant: bool = False,
    ):
        sm = self._sm
        url = f"{sm.api_base.rstrip('/')}/{path}"
        assert self._async_post is not None
        failure: Exception | None = None
        status, data = 0, {}
        try:
            status, data = await asyncio.wait_for(
                self._async_post(url, headers, body, sm.request_timeout_s), sm.request_timeout_s
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = exc
        # raised outside the except block: no __context__, never the original message (it may
        # carry the URL); the provider body is never echoed
        if failure is not None:
            raise LemonSliceError(
                PROVIDER_UNAVAILABLE if tenant else "session_request_failed",
                f"Simli {stage} request failed error_type={type(failure).__name__}",
            )
        if tenant and (mapped := self._provider_status_error(status)):
            raise mapped  # the tenant's own account: stable code, no platform credit state
        if status == 402:
            self._trip_credit()
            raise self._credit_error()
        if not 200 <= status < 300:
            raise LemonSliceError(
                "session_request_failed", f"Simli {stage} request failed status={status}"
            )
        return data

    async def _request_session(
        self,
        agent_id: str | None,
        image_url: str | None,
        avatar_token: str,
        room_sid: str,
        api_key: str | None = None,
        room_name: str = "",
    ) -> Any:
        """Start the provider session: compose token, then join our room. Returns session_id."""
        sm = self._sm
        self._check_creation(api_key)
        assert agent_id
        key = sm.simli_api_key if api_key is None else api_key
        token_data = await self._simli_post(
            "compose/token",
            {"x-simli-api-key": key},
            self._token_body(agent_id),
            "token",
            tenant=api_key is not None,
        )
        session_token = token_data.get("session_token")
        if not isinstance(session_token, str) or not session_token:
            raise LemonSliceError("session_request_failed", "Simli token response has no token")
        self._log_warnings(token_data.get("warnings"), (key, session_token))
        join_data = await self._simli_post(
            "integrations/livekit/agents",
            {},
            {
                "session_token": session_token,
                "livekit_token": avatar_token,
                "livekit_url": sm.livekit_url,
            },
            "join",
            tenant=api_key is not None,
        )
        sid = join_data.get("session_id")
        if not isinstance(sid, str) or not _SAFE_SESSION_ID.fullmatch(sid):
            raise LemonSliceError("session_id_invalid", "Simli returned an unusable session id")
        self._register_tenant_key(sid, api_key)  # label only: Simli has no control calls
        return sid

    @staticmethod
    def _log_warnings(warnings: Any, secrets: tuple[str, ...]) -> None:
        if not isinstance(warnings, list):
            return
        for w in warnings[:5]:
            text = json.dumps(w, ensure_ascii=False)[:200] if not isinstance(w, str) else w[:200]
            for secret in secrets:
                text = text.replace(secret, "<redacted>")
            log.warning("simli token warning: %s", text)

    async def _maybe_rollover(self, sess: _Sess) -> None:
        """No replacement session (S10). Recovery = flush the avatar's buffer while it is present."""
        if (
            sess.closing
            or not sess.recovery_required
            or self._s.avatar_identity not in sess.room.remote_participants
        ):
            return
        try:
            await sess.channel.clear_buffer()
        except Exception as exc:
            log.info("simli recovery flush failed error_type=%s", type(exc).__name__)
            return
        sess.recovery_required = False

    async def _terminate_provider(self, sess: Any) -> None:
        """No terminate endpoint (S3): leave the room (ends the avatar) and settle cost as derived."""
        sid = sess.provider_session_id
        if not sid:
            return
        room = getattr(sess, "room", None)
        failure: Exception | None = None
        if room is not None:
            try:
                await asyncio.wait_for(room.disconnect(), self._s.io_timeout_s)
            except Exception as exc:
                failure = exc
        cost_meter.avatar_close(sid, confirmed=False, extra_s=float(self._s.idle_timeout_s))
        self._tenant_keys.pop(sid, None)
        if failure is not None:
            log.error("simli room leave failed error_type=%s", type(failure).__name__)
            raise ProviderCleanupIncomplete()
