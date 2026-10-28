"""Operator-set address of the self-host TTS server.

The address of a rented GPU changes every time the box is replaced. The operator sets the new one through the admin
API instead of editing the env file and restarting; it lives in Redis (AOF on, named volume) so it survives restarts.
Off unless TTS_ADMIN_TOKEN (>= 32 chars) and REDIS_URL are both set. Any Redis trouble falls back to the env address:
a broken override store must never take speech down.
"""

from __future__ import annotations

import ipaddress
import json
import os
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

KEY = "livento:tts:endpoint_override"
CACHE_TTL_S = 5.0
MAX_URL_LEN = 200

_lock = threading.Lock()
# (expires_at monotonic, override record or None)
_cache: tuple[float, dict | None] | None = None


def admin_token() -> str:
    return (os.environ.get("TTS_ADMIN_TOKEN", "") or "").strip()


def enabled() -> bool:
    return len(admin_token()) >= 32 and bool((os.environ.get("REDIS_URL", "") or "").strip())


def validate_base_url(raw: str) -> str:
    """Return the normalized ``scheme://host[:port]`` or raise ValueError. Only the address shape is checked: it is
    never resolved here, and loopback / link-local (cloud metadata) / unspecified / multicast hosts are refused."""
    value = (raw or "").strip()
    if not value or len(value) > MAX_URL_LEN:
        raise ValueError("address is empty or too long")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ValueError("address is not a valid URL") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("address must be http(s)://host[:port]")
    if (
        parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
    ):
        raise ValueError("address must not carry credentials, path, query or fragment")
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("loopback addresses are not allowed")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved
    ):
        raise ValueError("loopback, link-local and reserved addresses are not allowed")
    shown = f"[{host}]" if ip is not None and ip.version == 6 else host
    return f"{parts.scheme}://{shown}" + (f":{port}" if port else "")


def _redis():
    import redis

    return redis.Redis.from_url(
        os.environ["REDIS_URL"],
        socket_timeout=1.0,
        socket_connect_timeout=1.0,
        decode_responses=True,
    )


def read_override() -> dict | None:
    """The stored record {base_url, updated_by, updated_at}, or None. Raises on Redis errors (callers decide)."""
    raw = _redis().get(KEY)
    if not raw:
        return None
    record = json.loads(raw)
    record["base_url"] = validate_base_url(record["base_url"])
    return record


def write_override(base_url: str, actor: str) -> dict:
    record = {
        "base_url": validate_base_url(base_url),
        "updated_by": actor,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _redis().set(KEY, json.dumps(record))
    _forget()
    return record


def clear_override() -> None:
    _redis().delete(KEY)
    _forget()


def _forget() -> None:
    global _cache
    with _lock:
        _cache = None


def current_override() -> dict | None:
    """Cached for CACHE_TTL_S; None when disabled, unset, unreadable or invalid."""
    global _cache
    if not enabled():
        return None
    now = time.monotonic()
    with _lock:
        if _cache is not None and _cache[0] > now:
            return _cache[1]
    try:
        record = read_override()
    except Exception:  # noqa: BLE001 - fall back to the env address
        record = None
    with _lock:
        _cache = (now + CACHE_TTL_S, record)
    return record


def effective_base_url(static_base_url: str) -> str:
    record = current_override()
    return record["base_url"] if record else static_base_url
