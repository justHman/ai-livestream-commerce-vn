"""BYOK wave 2 render backends: ``cloud_beyondpresence``, ``cloud_tavus``, ``cloud_anam``,
``cloud_runway``.

Same architecture as ``cloud_simli``: the provider's avatar worker joins a
Livento-owned LiveKit room (room name = runtime session id) as participant kind ``agent`` with
attribute ``lk.publish_on_behalf``; the Runtime stays the single speaker and sends guarded TTS PCM
over ``AvatarAudioChannel`` (topic ``lk.audio_stream``) at the sample rate the provider expects
(``AvatarAudioChannel`` resamples our PCM). ``_ByokBackend`` SUBCLASSES ``LemonSliceRenderBackend``;
room/loop thread, JWT minting, ready gate, interrupt (``lk.clear_buffer``), stop/stop_all, the
terminate retry loop, per-session tenant keys (``_tenant_keys``) and cost evidence are inherited.
Each provider only declares its start request, its session-id field, its status mapping and how a
session ends. They are TENANT-ONLY: a start without a tenant credential is refused, and the
platform budget/credit guards never see them. Default OFF per provider flag
(``BEYOND_PRESENCE_ENABLED``, ``TAVUS_ENABLED``, ``ANAM_ENABLED``, ``RUNWAY_ENABLED``).

Start requests were checked against the LiveKit plugins (github.com/livekit/agents, main,
2026-11-02). Differences from the research file are marked [DIFF].

UNVERIFIED ASSUMPTIONS (no provider account exists; nothing ran against any service).
Common:
  W1  The worker honours ``lk.clear_buffer`` and emits ``lk.playback_started/finished`` like the
      other providers (the plugins rely on that protocol; no vendor doc says so). Otherwise the
      LemonSlice ``playback_unconfirmed`` recovery applies (flush only: no replacement session).
  W2  The declared byte-stream sample rate is honoured (Tavus/Anam 24 kHz, bey/Runway 16 kHz); the
      rate is ``<PROVIDER>_AUDIO_SAMPLE_RATE``.
  W3  Maximum session length is only known from plan pages; ``<PROVIDER>_MAX_SESSION_S`` is the
      Runtime near-cap guard, ``<PROVIDER>_IDLE_BOUND_S`` bounds a derived cost row.
  W4  The avatar identity in the room is OURS (the JWT we mint). The plugins default to
      ``bey-avatar-agent``, ``tavus-avatar-agent``, ``anam-avatar-agent``, ``runway-avatar-agent``;
      we use the same names (env ``<PROVIDER>_AVATAR_IDENTITY``). Whether a worker accepts another
      identity is unknown.
  W5  The id formats in ``byok.py`` and the whole ``avatar_params`` vocabulary.
Beyond Presence:
  BEY1 ``POST {base}/v1/sessions`` header ``x-api-key`` JSON ``{transport:"livekit", avatar_id,
       url, token}`` (identical to the plugin); 201 body ``id``.
  BEY2 No end API is documented: ``stop`` leaves the room (the worker is expected to end) and the
       cost row is settled as derived (idle bound). Billing may continue until the provider's own
       idle end (docs: 1 h idle, one concurrency slot held meanwhile).
  BEY3 Statuses: 401 key, 402 out of minutes, 403 key/foreign resource, 404, 422 (bad body ->
       not found), 429 concurrency (docs table).
Tavus:
  TAV1 ``POST {base}/v2/conversations`` header ``x-api-key``; response ``conversation_id``.
       [DIFF] the plugin ALWAYS sends ``pal_id`` (default stock echo PAL ``pb87e71797da``,
       ``TAVUS_DEFAULT_PAL_ID``) and adds ``conversation_name``; the research file listed pal_id
       as optional. We send the default when ``avatar_params.pal_id`` is absent.
       [DIFF] the plugin sends no ``max_call_duration``; we send ``max_call_duration`` =
       max_session_s + 60 (research file). A plan below that may answer 400.
  TAV2 End: ``POST {base}/v2/conversations/{id}/end`` header ``x-api-key``, empty JSON body; 2xx
       or 404/410 = ended.
  TAV3 Limits answer 400 with a message: text containing ``concurren`` -> concurrency, ``minute``
       / ``credit`` / ``quota`` -> out of credit, any other 400 -> not found (bad face/pal id).
Anam:
  ANA1 ``POST {base}/v1/engine/session`` header ``Authorization: Bearer`` JSON ``{personaConfig:
       {type:"ephemeral", name, avatarId, llmId:"CUSTOMER_CLIENT_V1", avatarModel?},
       environment:{livekitUrl, livekitToken}}`` (identical to the plugin; no ``sessionOptions``
       so the default frame size and the AI disclosure default apply); response ``sessionId``.
  ANA2 End: ``POST {base}/v1/sessions/{id}/stop`` (Anam API reference "Stop session"; Bearer).
       The docs say it answers 404 for LiveKit sessions (no engine to stop): 404 counts as ended,
       so the room leave is what really ends the session (cost row may be optimistic).
  ANA3 The plugin waits for the first playback event (``wait_playback_start``) before audio; we
       rely on the avatar-video ready gate only. Anam docs recommend 16 kHz; the plugin uses
       24 kHz (default here).
  ANA4 Statuses beyond 401/403/404/503 (402/429 meaning) are unknown; 400/422 -> not found.
Runway:
  RUN1 ``POST {base}/v1/realtime_sessions`` headers ``Authorization: Bearer`` and
       ``X-Runway-Version: 2024-11-06``, JSON ``{model:"gwm1_avatars", avatar:{type:"custom",
       avatarId}|{type:"runway-preset", presetId}, livekit:{url, token, roomName, agentIdentity},
       maxDuration}`` (identical to the plugin; ``agentIdentity`` = our runtime identity);
       response ``id``. ``maxDuration`` = max_session_s + 60.
  RUN2 End: data message ``{"type":"END_CALL"}`` to the avatar (best effort), then
       ``DELETE {base}/v1/realtime_sessions/{id}``; 2xx or 404/410 = ended (the plugin prefers
       END_CALL and uses DELETE as the fallback).
  RUN3 Error statuses are undocumented: 401/403 key, 429 concurrency, 400/422 -> out of credit if
       the body text mentions ``credit`` else not found.

Runbook: ``docs/runbooks/byok-avatar-runtime.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode

from backend.application.clients.avatar.byok import (
    CONCURRENCY_LIMIT,
    NOT_FOUND,
    OUT_OF_CREDIT,
    PROVIDER_UNAVAILABLE,
    AvatarCredential,
    status_to_avatar_code,
    valid_avatar_id,
    valid_avatar_params,
)
from backend.application.clients.avatar.lemonslice import (
    _SAFE_SESSION_ID,
    _TERMINAL_STATUS,
    LemonSliceError,
    LemonSliceRenderBackend,
    LemonSliceSettings,
    ProviderCleanupIncomplete,
)
from backend.application.clients.avatar.simli import AsyncHttpPost, SimliRenderBackend
from backend.application.render.engines_base import StartOptions
from backend.application.usage_evidence.cost import cost_meter

log = logging.getLogger(__name__)

SESSION_LENGTH_MARGIN_S = 60
RUNWAY_VERSION = "2024-11-06"
TAVUS_DEFAULT_PAL_ID = "pb87e71797da"
AsyncHttpDelete = Callable[[str, dict[str, str], float], Awaitable[int]]


async def httpx_delete(url: str, headers: dict[str, str], timeout: float) -> int:
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        return (await client.delete(url, headers=headers)).status_code


@dataclass(frozen=True)
class ByokProviderSettings:
    """Shared by the four backends; ``""`` / ``0`` mean "the backend's own default"."""

    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str
    api_base: str = ""
    avatar_identity: str = ""
    audio_sample_rate: int = 0
    runtime_identity: str = "livento-runtime"
    max_session_s: float = 3600.0
    idle_bound_s: int = 120
    ready_timeout_s: float = 30.0
    request_timeout_s: float = 15.0
    default_pal_id: str = TAVUS_DEFAULT_PAL_ID  # Tavus only
    speak_ready_gate_enabled: bool = True
    speak_ready_wait_s: float = 45.0
    fallback_publish: bool = False
    render_offset_ms: int = 0
    ready_ttl_s: float = 600.0
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
        for name in ("idle_bound_s", "audio_sample_rate"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ValueError(f"{name} must be an integer >= 0")
        if self.idle_bound_s < 1:
            raise ValueError("idle_bound_s must be an integer >= 1")
        if not valid_avatar_params("tavus", {"pal_id": self.default_pal_id}):
            raise ValueError("default_pal_id has an invalid format")

    def __repr__(self) -> str:  # holds no key, but stay uniform
        return "ByokProviderSettings(<redacted>)"


def _pack(agent_id: str, params: dict[str, str]) -> str:
    """Carry avatar_params through the inherited ``_start(agent_id, ...)`` as one safe string.
    Ids and values match ``[A-Za-z0-9_.-]``, so ``?``/``&``/``=`` never occur inside them."""
    return f"{agent_id}?{urlencode(sorted(params.items()))}" if params else agent_id


def _unpack(target: str) -> tuple[str, dict[str, str]]:
    agent, _, query = target.partition("?")
    return agent, dict(parse_qsl(query))


def _text(body: Any) -> str:
    return json.dumps(body, default=str).lower() if body is not None else ""


class _ByokBackend(LemonSliceRenderBackend):
    """Tenant-only provider backend. Subclasses fill the class attributes and the hooks."""

    provider = ""  # the avatar_provider value
    default_base = ""
    default_identity = ""
    default_rate = 16000
    session_id_field = "id"
    end_path = ""  # relative to the base, ``{session_id}``; "" = leave the room
    # no replacement session: recovery only flushes the avatar's buffer
    _maybe_rollover = SimliRenderBackend._maybe_rollover

    def __init__(
        self,
        settings: ByokProviderSettings,
        *,
        room_factory: Callable[[], Any] | None = None,
        async_http_post: AsyncHttpPost | None = None,
        audio_track_factory: Callable[[Any, int], Any] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bp = settings
        self._base = (settings.api_base or self.default_base).rstrip("/")
        ls = LemonSliceSettings(
            livekit_url=settings.livekit_url,
            livekit_api_key=settings.livekit_api_key,
            livekit_api_secret=settings.livekit_api_secret,
            lemonslice_api_key="",  # tenant-only: never a platform key
            api_base=self._base,
            audio_sample_rate=settings.audio_sample_rate or self.default_rate,
            idle_timeout_s=settings.idle_bound_s,
            ready_timeout_s=settings.ready_timeout_s,
            speak_ready_gate_enabled=settings.speak_ready_gate_enabled,
            speak_ready_wait_s=settings.speak_ready_wait_s,
            request_timeout_s=settings.request_timeout_s,
            avatar_identity=settings.avatar_identity or self.default_identity,
            runtime_identity=settings.runtime_identity,
            fallback_publish=settings.fallback_publish,
            render_offset_ms=settings.render_offset_ms,
            terminate_path=self.end_path,
            keepalive_s=0.0,
            max_session_s=settings.max_session_s,
            rollover_after_s=0.0,
            ready_ttl_s=settings.ready_ttl_s,
            payment_backoff_s=settings.payment_backoff_s,
        )
        super().__init__(
            ls,
            room_factory=room_factory,
            session_client_factory=lambda: None,  # LemonSlice plugin client: never used here
            async_http_post=async_http_post,
            audio_track_factory=audio_track_factory,
            clock=clock,
            monotonic=monotonic,
        )

    # ---- provider hooks -------------------------------------------------------------------
    def _headers(self, key: str) -> dict[str, str]:
        raise NotImplementedError

    def _start_body(
        self, avatar: str, params: dict[str, str], token: str, room_name: str
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _start_url(self) -> str:
        raise NotImplementedError

    def _status_code(self, status: int, body: Any) -> str | None:
        return status_to_avatar_code(status)

    # ---- start ----------------------------------------------------------------------------
    def _credit_error(self) -> LemonSliceError:
        return LemonSliceError(OUT_OF_CREDIT, f"{OUT_OF_CREDIT}: {self.provider} out of credit")

    def _resolve_avatar(self, opts: StartOptions) -> tuple[str | None, str | None]:
        if not isinstance(opts.extra.get("avatar_credential"), AvatarCredential):
            raise LemonSliceError(
                "avatar_credential_required", f"{self.provider} is only available with own key"
            )
        if opts.avatar_provider != self.provider:
            raise LemonSliceError("avatar_provider_invalid", "avatar_provider is not supported")
        if opts.avatar_image_url or opts.avatar_id:
            raise LemonSliceError(
                "avatar_source_invalid", f"{self.provider} needs avatar_agent_id only"
            )
        agent = opts.avatar_agent_id or ""
        if not valid_avatar_id(self.provider, agent):
            raise LemonSliceError("agent_id_invalid", "avatar_agent_id has an invalid format")
        params = opts.extra.get("avatar_params") or {}
        if not valid_avatar_params(self.provider, params):
            raise LemonSliceError("avatar_params_invalid", "avatar_params is not accepted")
        return _pack(agent, params), None

    async def _request_session(
        self,
        agent_id: str | None,
        image_url: str | None,
        avatar_token: str,
        room_sid: str,
        api_key: str | None = None,
        room_name: str = "",
    ) -> Any:
        """Start the provider session: ONE POST, never retried. Returns the provider session id."""
        if not api_key:
            raise LemonSliceError("avatar_credential_required", "tenant key missing")
        assert agent_id and self._async_post is not None
        avatar, params = _unpack(agent_id)
        body = self._start_body(avatar, params, avatar_token, room_name or room_sid)
        timeout = self._bp.request_timeout_s
        failure: Exception | None = None
        status, data = 0, {}
        try:
            status, data = await asyncio.wait_for(
                self._async_post(self._start_url(), self._headers(api_key), body, timeout),
                timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = exc
        # raised outside the except block: no __context__, never the original message (URL)
        if failure is not None:
            raise LemonSliceError(
                PROVIDER_UNAVAILABLE,
                f"{self.provider} request failed error_type={type(failure).__name__}",
            )
        if not 200 <= status < 300:
            code = self._status_code(status, data)
            if code is not None:
                raise LemonSliceError(code, f"{code}: {self.provider} status={status}")
            raise LemonSliceError(
                "session_request_failed", f"{self.provider} request failed status={status}"
            )
        sid = data.get(self.session_id_field) if isinstance(data, dict) else None
        if not isinstance(sid, str) or not _SAFE_SESSION_ID.fullmatch(sid):
            raise LemonSliceError(
                "session_id_invalid", f"{self.provider} returned an unusable session id"
            )
        self._register_tenant_key(sid, api_key)
        return sid

    # ---- end: POST via the inherited terminate loop (``end_path``) ---------------------------
    @staticmethod
    def _control_body(event: str) -> dict[str, Any]:
        return {}


class _LeaveRoomBackend(_ByokBackend):
    """No end API (bey): ``stop`` leaves the room; cost is settled as derived (idle bound)."""

    _terminate_provider = SimliRenderBackend._terminate_provider


# ---- Beyond Presence --------------------------------------------------------------------------


class BeyondPresenceRenderBackend(_LeaveRoomBackend):
    name = "cloud_beyondpresence"
    start_mode = "BEYONDPRESENCE"
    cost_provider = "beyondpresence"
    provider = "beyondpresence"
    default_base = "https://api.bey.dev"
    default_identity = "bey-avatar-agent"
    default_rate = 16000

    def _headers(self, key: str) -> dict[str, str]:
        return {"x-api-key": key}

    def _start_url(self) -> str:
        return f"{self._base}/v1/sessions"

    def _start_body(self, avatar, params, token, room_name):
        return {
            "transport": "livekit",
            "avatar_id": avatar,
            "url": self._bp.livekit_url,
            "token": token,
        }

    def _status_code(self, status: int, body: Any) -> str | None:
        if status == 422:  # body/avatar id rejected
            return NOT_FOUND
        return status_to_avatar_code(status)


# ---- Tavus ------------------------------------------------------------------------------------


class TavusRenderBackend(_ByokBackend):
    name = "cloud_tavus"
    start_mode = "TAVUS"
    cost_provider = "tavus"
    provider = "tavus"
    default_base = "https://tavusapi.com"
    default_identity = "tavus-avatar-agent"
    default_rate = 24000
    session_id_field = "conversation_id"
    end_path = "v2/conversations/{session_id}/end"
    _control_header = "x-api-key"

    def _headers(self, key: str) -> dict[str, str]:
        return {"x-api-key": key, "Content-Type": "application/json"}

    def _start_url(self) -> str:
        return f"{self._base}/v2/conversations"

    def _start_body(self, avatar, params, token, room_name):
        return {
            "pal_id": params.get("pal_id") or self._bp.default_pal_id,
            "face_id": avatar,
            "properties": {
                "livekit_ws_url": self._bp.livekit_url,
                "livekit_room_token": token,
                "max_call_duration": int(math.ceil(self._bp.max_session_s))
                + SESSION_LENGTH_MARGIN_S,
            },
        }

    def _status_code(self, status: int, body: Any) -> str | None:
        if status == 400:  # limits and bad ids both answer 400 (message text decides)
            text = _text(body)
            if "concurren" in text:
                return CONCURRENCY_LIMIT
            if any(w in text for w in ("minute", "credit", "quota")):
                return OUT_OF_CREDIT
            return NOT_FOUND
        return status_to_avatar_code(status)


# ---- Anam -------------------------------------------------------------------------------------


class AnamRenderBackend(_ByokBackend):
    name = "cloud_anam"
    start_mode = "ANAM"
    cost_provider = "anam"
    provider = "anam"
    default_base = "https://api.anam.ai"
    default_identity = "anam-avatar-agent"
    default_rate = 24000
    session_id_field = "sessionId"
    end_path = "v1/sessions/{session_id}/stop"
    _control_header = "Authorization"
    _control_prefix = "Bearer "

    def _headers(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _start_url(self) -> str:
        return f"{self._base}/v1/engine/session"

    def _start_body(self, avatar, params, token, room_name):
        persona: dict[str, Any] = {
            "type": "ephemeral",
            "name": "livento",
            "avatarId": avatar,
            "llmId": "CUSTOMER_CLIENT_V1",
        }
        if params.get("avatar_model"):
            persona["avatarModel"] = params["avatar_model"]
        return {
            "personaConfig": persona,
            "environment": {"livekitUrl": self._bp.livekit_url, "livekitToken": token},
        }

    def _status_code(self, status: int, body: Any) -> str | None:
        if status in (400, 422):
            return NOT_FOUND
        return status_to_avatar_code(status)


# ---- Runway -----------------------------------------------------------------------------------


class RunwayRenderBackend(_ByokBackend):
    name = "cloud_runway"
    start_mode = "RUNWAY"
    cost_provider = "runway"
    provider = "runway"
    default_base = "https://api.dev.runwayml.com"
    default_identity = "runway-avatar-agent"
    default_rate = 16000

    def __init__(
        self,
        settings: ByokProviderSettings,
        *,
        async_http_delete: AsyncHttpDelete | None = None,
        **kw,
    ) -> None:
        self._delete = async_http_delete or httpx_delete
        super().__init__(settings, **kw)

    def _headers(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}", "X-Runway-Version": RUNWAY_VERSION}

    def _start_url(self) -> str:
        return f"{self._base}/v1/realtime_sessions"

    def _start_body(self, avatar, params, token, room_name):
        if params.get("kind", "avatar") == "preset":
            target: dict[str, Any] = {"type": "runway-preset", "presetId": avatar}
        else:
            target = {"type": "custom", "avatarId": avatar}
        return {
            "model": "gwm1_avatars",
            "avatar": target,
            "livekit": {
                "url": self._bp.livekit_url,
                "token": token,
                "roomName": room_name,
                "agentIdentity": self._s.runtime_identity,
            },
            "maxDuration": int(math.ceil(self._bp.max_session_s)) + SESSION_LENGTH_MARGIN_S,
        }

    def _status_code(self, status: int, body: Any) -> str | None:
        if status in (400, 422):
            return OUT_OF_CREDIT if "credit" in _text(body) else NOT_FOUND
        return status_to_avatar_code(status)

    async def _terminate_provider(self, sess: Any) -> None:
        """END_CALL to the avatar (best effort), then DELETE the session (RUN2)."""
        sid = sess.provider_session_id
        if not sid:
            return
        s = self._s
        room = getattr(sess, "room", None)
        publish = getattr(getattr(room, "local_participant", None), "publish_data", None)
        if publish is not None:
            try:
                await asyncio.wait_for(
                    publish(
                        json.dumps({"type": "END_CALL"}).encode(),
                        reliable=True,
                        destination_identities=[s.avatar_identity],
                    ),
                    s.io_timeout_s,
                )
            except Exception as exc:  # the DELETE below is the reliable end
                log.info("runway END_CALL not sent error_type=%s", type(exc).__name__)
        url = f"{self._base}/v1/realtime_sessions/{quote(sid, safe='')}"
        for attempt in range(max(1, s.terminate_attempts)):
            key = self._tenant_keys.get(sid, "")
            try:
                if not key:  # fail closed: never fall back to another account's key
                    raise LemonSliceError("avatar_credential_required", "tenant key missing")
                status = await asyncio.wait_for(
                    self._delete(url, self._headers(key), s.request_timeout_s), self._deadline()
                )
                if 200 <= status < 300 or status in _TERMINAL_STATUS:
                    cost_meter.avatar_close(sid)  # provider-confirmed end
                    self._tenant_keys.pop(sid, None)
                    return
                err = f"status={status}"
            except Exception as exc:
                err = f"error_type={type(exc).__name__}"
            log.warning("runway end failed attempt=%d %s", attempt + 1, err)
            await asyncio.sleep(0.2)
        log.error("runway session NOT ended after retries; max duration applies")
        raise ProviderCleanupIncomplete()


# ---- flags / registry -------------------------------------------------------------------------

BACKENDS: dict[str, type[_ByokBackend]] = {
    "beyondpresence": BeyondPresenceRenderBackend,
    "tavus": TavusRenderBackend,
    "anam": AnamRenderBackend,
    "runway": RunwayRenderBackend,
}
NAME_TO_PROVIDER = {cls.name: provider for provider, cls in BACKENDS.items()}
