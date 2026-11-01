"""BYOK avatar credentials: transit envelope, stable error codes, key/avatar check.

A shop may bring its own avatar-provider account. The API (S1) seals the shop's key for ONE
Runtime start request (AES-256-GCM); S1 -> S2 is plain HTTP, so the key never crosses it in
clear. This module opens the envelope into memory. Nothing here persists or logs a key.

Envelope (JSON object or string)::

    {"v":1,"alg":"A256GCM","kid":"<id>","nonce":"<b64 12B>","ct":"<b64 ct+tag>","exp":<unix s>}

Plaintext ``{"api_key": "...", "provider": "simli"}``; AAD ``session_id|tenant_id|provider|exp``
(ASCII). ``session_id`` is the id the caller knows at start: the business session id (the Runtime
session id does not exist yet); the check endpoint uses its own ``check_id``.

Contract: ``D:/LiventoWorkspace/023-prep/BYOK_AVATAR_CONTRACT_2026-11-01.md``.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_INVALID = "AVATAR_KEY_INVALID"
OUT_OF_CREDIT = "AVATAR_OUT_OF_CREDIT"
NOT_FOUND = "AVATAR_NOT_FOUND"
PROVIDER_UNAVAILABLE = "AVATAR_PROVIDER_UNAVAILABLE"
CONCURRENCY_LIMIT = "AVATAR_CONCURRENCY_LIMIT"
AVATAR_CODES = frozenset(
    {KEY_INVALID, OUT_OF_CREDIT, NOT_FOUND, PROVIDER_UNAVAILABLE, CONCURRENCY_LIMIT}
)
# a live that hits one of these cannot continue on this account: ends like platform credit loss
AVATAR_FATAL_CODES = frozenset({KEY_INVALID, OUT_OF_CREDIT})

PROVIDERS = ("lemonslice", "simli", "bithuman")
MAX_ENVELOPE_WINDOW_S = 120  # exp may be at most this far ahead of now
CLOCK_SKEW_S = 30  # S1/S2 clocks are not synchronized: exp may be at most now + 150 s

# per-provider avatar id validation (contract table; bitHuman pattern is UNVERIFIED)
_AVATAR_ID = {
    "lemonslice": re.compile(r"agent_[0-9a-f]{8,32}"),
    "simli": re.compile(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    ),
    "bithuman": re.compile(r"[A-Za-z0-9_-]{3,64}"),
}


def valid_avatar_id(provider: str, avatar_id: str) -> bool:
    pattern = _AVATAR_ID.get(provider)
    return bool(pattern and pattern.fullmatch(avatar_id))


def status_to_avatar_code(status: int | None) -> str | None:
    """Provider HTTP status -> stable code (None = not a BYOK-specific failure)."""
    if status in (401, 403):
        return KEY_INVALID
    if status == 402:
        return OUT_OF_CREDIT
    if status == 404:
        return NOT_FOUND
    if status == 429:
        return CONCURRENCY_LIMIT
    if isinstance(status, int) and status >= 500:
        return PROVIDER_UNAVAILABLE
    return None


@dataclass(frozen=True)
class AvatarCredential:
    """A tenant's provider key, held in memory for one session. Never printed."""

    api_key: str = field(repr=False)
    provider: str

    def __repr__(self) -> str:
        return f"AvatarCredential(provider={self.provider!r}, <redacted>)"


class EnvelopeError(ValueError):
    """Opening failed. ``code`` is stable and safe to return; the message holds no secret."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def load_transit_key(key_b64: str) -> bytes:
    try:
        key = base64.b64decode(key_b64, validate=True)
    except (binascii.Error, ValueError):
        key = b""
    if len(key) != 32:
        raise ValueError("AVATAR_CRED_TRANSIT_KEY must be base64 of exactly 32 bytes")
    return key


def _b64d(value: Any) -> bytes:
    if not isinstance(value, str):
        raise EnvelopeError("envelope_malformed")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise EnvelopeError("envelope_malformed") from None


class NonceCache:
    """Nonces already opened, kept until their envelope can no longer be valid (replay guard).

    In memory and per process: a Runtime restart forgets them, but an envelope lives at most
    `MAX_ENVELOPE_WINDOW_S + CLOCK_SKEW_S` seconds, so the exposure is that window only.
    Full cache = refuse (fail closed): it takes the transit key to fill it.
    """

    def __init__(self, capacity: int = 10_000) -> None:
        self._capacity = capacity
        self._seen: dict[bytes, float] = {}  # nonce -> unix time after which it is forgotten
        self._lock = threading.Lock()

    def first_use(self, nonce: bytes, forget_at: float, now: float) -> bool:
        with self._lock:
            if len(self._seen) >= self._capacity:
                self._seen = {n: t for n, t in self._seen.items() if t > now}
            if nonce in self._seen or len(self._seen) >= self._capacity:
                return False
            self._seen[nonce] = forget_at
            return True


_SEEN = NonceCache()


def open_envelope(
    envelope: Any,
    *,
    session_id: str,
    tenant_id: str,
    provider: str,
    key: bytes,
    kid: str,
    now: float | None = None,
    seen: NonceCache | None = None,
) -> AvatarCredential:
    """Decrypt into memory or raise ``EnvelopeError``. Expired, wrong kid / AAD / tag all fail."""
    if isinstance(envelope, str):
        try:
            envelope = json.loads(envelope)
        except ValueError:
            raise EnvelopeError("envelope_malformed") from None
    if not isinstance(envelope, dict) or envelope.get("v") != 1 or envelope.get("alg") != "A256GCM":
        raise EnvelopeError("envelope_malformed")
    exp = envelope.get("exp")
    if not isinstance(exp, int) or isinstance(exp, bool):
        raise EnvelopeError("envelope_malformed")
    current = time.time() if now is None else now
    if exp < current:
        raise EnvelopeError("envelope_expired")
    if exp > current + MAX_ENVELOPE_WINDOW_S + CLOCK_SKEW_S:
        raise EnvelopeError("envelope_window")
    if envelope.get("kid") != kid:
        raise EnvelopeError("envelope_kid")
    nonce, ct = _b64d(envelope.get("nonce")), _b64d(envelope.get("ct"))
    if len(nonce) != 12:
        raise EnvelopeError("envelope_malformed")
    try:
        aad = f"{session_id}|{tenant_id}|{provider}|{exp}".encode("ascii")
        plain = AESGCM(key).decrypt(nonce, ct, aad)
    except (InvalidTag, UnicodeEncodeError, ValueError):
        raise EnvelopeError("envelope_auth") from None
    try:
        body = json.loads(plain)
    except ValueError:
        raise EnvelopeError("envelope_malformed") from None
    api_key = body.get("api_key") if isinstance(body, dict) else None
    if (
        not isinstance(api_key, str)
        or not 1 <= len(api_key) <= 512
        or body.get("provider") != provider
    ):
        raise EnvelopeError("envelope_malformed")
    cache = _SEEN if seen is None else seen
    if not cache.first_use(nonce, exp + CLOCK_SKEW_S, current):
        raise EnvelopeError("envelope_replay")
    return AvatarCredential(api_key=api_key, provider=provider)


# ---- check (no billable session) -------------------------------------------------------

AsyncHttpGet = Callable[[str, dict[str, str], float], Awaitable[tuple[int, Any]]]


async def httpx_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, headers=headers)
    try:
        data = r.json()
    except ValueError:
        data = None
    return r.status_code, data


async def check_credential(
    provider: str,
    api_key: str,
    avatar_id: str | None,
    *,
    simli_base: str,
    bithuman_base: str,
    http_get: AsyncHttpGet = httpx_get,
    timeout_s: float = 10.0,
) -> str:
    """Best-effort key/avatar verification. Returns ok | invalid_key | not_found | unknown |
    provider_error. Never starts a billable session; the key is only sent to its provider.

    UNVERIFIED: the Simli ``/faces`` list shape and the bitHuman ``/v1/me`` response. LemonSlice
    has no cheap authenticated call in the Runtime client: format-only, so ``unknown``.
    """
    if avatar_id is not None and not valid_avatar_id(provider, avatar_id):
        return "not_found"
    if provider == "lemonslice":
        return "unknown"
    if provider == "simli":
        url, headers = f"{simli_base.rstrip('/')}/faces", {"x-simli-api-key": api_key}
    elif provider == "bithuman":
        url, headers = f"{bithuman_base.rstrip('/')}/v1/me", {"api-secret": api_key}
    else:
        return "unknown"
    try:
        status, data = await http_get(url, headers, timeout_s)
    except Exception:  # noqa: BLE001 - the message may carry the URL; only the verdict leaves
        return "provider_error"
    if status in (401, 403):
        return "invalid_key"
    if not 200 <= status < 300:
        return "provider_error"
    if avatar_id is None:
        return "ok"
    if provider == "simli" and avatar_id.lower() in json.dumps(data, default=str).lower():
        return "ok"
    return "unknown"  # key is valid, the avatar cannot be confirmed without a billable session
