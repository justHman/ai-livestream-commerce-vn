"""Per-session avatar provider router (BYOK).

``AvatarProviderRouter`` wraps the GLOBAL backend (``RENDER_BACKEND``) and, for sessions that
bring a tenant credential, the backend of that session's ``avatar_provider``. Sessions without a
tenant credential always go to the global backend, exactly as before. It is only built when
``AVATAR_BYOK_ENABLED`` is true; otherwise the container holds the plain backend.

Every per-session call after ``start`` (stream, interrupt, stop, status, lifecycle) is routed by
the session id to the backend that started it. Everything else (``avatar_override_mode``,
``speak_ready_gate_enabled``, ...) comes from the global backend, so ``getattr``/``hasattr``
probes by the control plane behave as they did.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any

from backend.application.clients.avatar.byok import PROVIDER_UNAVAILABLE, AvatarCredential
from backend.application.clients.avatar.lemonslice import LemonSliceError
from backend.application.render.engines_base import (
    StartOptions,
    StartResult,
    StreamingAvatarBackend,
)
from backend.application.render.windows import AudioWindow, VideoWindow
from backend.application.usage_evidence.cost import cost_meter

log = logging.getLogger(__name__)

_NAME_TO_PROVIDER = {
    "cloud_lemonslice": "lemonslice",
    "cloud_simli": "simli",
    "cloud_bithuman": "bithuman",
}


# per-session probes the control plane duck-types, with the neutral answer for a backend without
_SESSION_PROBES: dict[str, Any] = {
    "lifecycle_status": "",
    "set_session_phase": None,
    "remember_ready_expiry": None,
    "avatar_ready": True,
    "wait_speak_ready": True,
    "require_avatar_recovery": None,
    "stop_reason": "",
    "playback_info": None,
}


class AvatarProviderRouter(StreamingAvatarBackend):
    def __init__(self, default: Any, by_provider: dict[str, Any]) -> None:
        self._default = default
        self._by_provider = by_provider  # providers a TENANT session may use
        self._owner: dict[str, Any] = {}  # session id -> backend that started it

    @classmethod
    def build(cls, default: Any, builders: dict[str, Callable[[], Any]]) -> AvatarProviderRouter:
        """Build the secondary backends eagerly (the LemonSlice plugin must be imported on the
        main thread). A provider whose backend cannot be built is unavailable to tenants."""
        saved = (cost_meter.avatar_provider, cost_meter.avatar_idle_bound_s)
        own = _NAME_TO_PROVIDER.get(getattr(default, "name", ""))
        by_provider: dict[str, Any] = {own: default} if own else {}
        for provider, builder in builders.items():
            if provider in by_provider:
                continue
            try:
                by_provider[provider] = builder()
            except Exception as exc:  # noqa: BLE001 - never echo the message (may name config)
                log.warning(
                    "avatar provider unavailable for BYOK provider=%s error_type=%s",
                    provider,
                    type(exc).__name__,
                )
        cost_meter.avatar_provider, cost_meter.avatar_idle_bound_s = saved
        return cls(default, by_provider)

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._default.name

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("_"):
            raise AttributeError(attr)
        if attr in _SESSION_PROBES:
            # present only when some backend has it, so the control plane's getattr/hasattr
            # probes see the same capability as without the router
            if not any(hasattr(b, attr) for b in self._backends()):
                raise AttributeError(attr)
            return lambda session_id, *a, **kw: self._probe(attr, session_id, *a, **kw)
        return getattr(self._default, attr)

    def _probe(self, attr: str, session_id: str, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self._for(session_id), attr, None)
        if fn is None:  # this session's backend has no such probe: the neutral answer
            return _SESSION_PROBES[attr]
        return fn(session_id, *args, **kwargs)

    def _for(self, session_id: str) -> Any:
        return self._owner.get(session_id, self._default)

    def _backends(self) -> list[Any]:
        seen: list[Any] = []
        for b in [self._default, *self._by_provider.values()]:
            if not any(b is s for s in seen):
                seen.append(b)
        return seen

    # ---- RenderBackend ------------------------------------------------------------------
    def start(self, opts: StartOptions) -> StartResult:
        backend = self._default
        if isinstance(opts.extra.get("avatar_credential"), AvatarCredential):
            backend = self._by_provider.get(opts.avatar_provider or "")
            if backend is None:
                raise LemonSliceError(PROVIDER_UNAVAILABLE, f"{PROVIDER_UNAVAILABLE}: no backend")
        result = backend.start(opts)
        self._owner[result.session_id] = backend
        return result

    def stop(self, session_id: str) -> None:
        self._for(session_id).stop(session_id)  # a failed stop keeps the owner for the retry
        self._owner.pop(session_id, None)

    def interrupt(self, session_id: str) -> None:
        self._for(session_id).interrupt(session_id)

    def stop_all(self) -> None:
        for b in self._backends():
            try:
                b.stop_all()
            except Exception as exc:  # noqa: BLE001 - one provider must not block the others
                log.warning("avatar router stop_all error_type=%s", type(exc).__name__)
        self._owner.clear()

    def session_status(self, session_id: str) -> str:
        return self._for(session_id).session_status(session_id)

    def stream_audio(self, session_id: str, audio_window: AudioWindow) -> Iterator[VideoWindow]:
        return self._for(session_id).stream_audio(session_id, audio_window)

    def session_ids(self) -> tuple[str, ...]:
        ids: list[str] = []
        for b in self._backends():
            probe = getattr(b, "session_ids", None)
            if probe is not None:
                ids.extend(probe())
        return tuple(dict.fromkeys(ids))
