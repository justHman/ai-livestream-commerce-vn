"""Self-host avatar service client — thin HTTP proxy to the avatar service.

Canonical outbound transport (Task 1.22/1.32): calls the self-host
avatar_service session endpoints and returns typed results. Only the
browser-safe LiveKit URL and client token cross this boundary.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

import httpx


class AvatarClientError(RuntimeError):
    """Typed transport failure for an avatar client."""


@dataclass
class AvatarStartResult:
    """Browser-safe start data returned to the backend (no provider secrets)."""

    session_id: str
    livekit_url: str
    livekit_client_token: str
    mode: str = "self-host"


class SelfHostedAvatarClient:
    """HTTP client for the self-host avatar service."""

    def __init__(
        self,
        base_url: str = "",
        *,
        api_key: str = "",
        timeout: float = 30.0,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        base = (base_url or os.environ.get("AVATAR_BASE_URL", "") or "").strip()
        if not base:
            raise AvatarClientError("SelfHostedAvatarClient needs base_url or env AVATAR_BASE_URL")
        self._base_url = base.rstrip("/")
        self._api_key = api_key or os.environ.get("AVATAR_AUTH_TOKEN", "") or ""
        self._timeout = float(timeout)
        self._client = http_client

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def start(self, *, avatar_id: str = "default", is_sandbox: bool = True) -> AvatarStartResult:
        client = self._get_client()
        url = urljoin(self._base_url + "/", "v1/sessions")
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = {"avatar_id": avatar_id, "is_sandbox": is_sandbox}
        try:
            resp = client.post(url, json=body, headers=headers)
        except httpx.RequestError as exc:
            raise AvatarClientError(f"self-host avatar start failed: {exc}") from exc
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = (resp.text or "")[:300]
            raise AvatarClientError(
                f"self-host avatar start: HTTP {resp.status_code} {detail}"
            ) from exc
        data = resp.json() if resp.content else {}
        return AvatarStartResult(
            session_id=str(data.get("session_id", "")),
            livekit_url=str(data.get("livekit_url", "")),
            livekit_client_token=str(data.get("livekit_client_token", "")),
            mode=str(data.get("mode", "self-host")),
        )

    def stop(self, session_id: str) -> None:
        client = self._get_client()
        url = urljoin(self._base_url + "/", f"v1/sessions/{session_id}/stop")
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            resp = client.post(url, headers=headers)
        except httpx.RequestError as exc:
            raise AvatarClientError(f"self-host avatar stop failed: {exc}") from exc
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AvatarClientError(f"self-host avatar stop: HTTP {resp.status_code}") from exc

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
