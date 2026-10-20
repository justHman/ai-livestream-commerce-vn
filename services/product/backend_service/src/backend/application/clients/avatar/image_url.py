"""Strict validation of a per-session avatar image URL.

The provider fetches this URL, and we cannot control whether it follows redirects. So the URL must be https on an exact
allow-listed host, with no port tricks, credentials, IP literals, encoded path separators or dot segments. Every host in
AVATAR_IMAGE_HOSTS must serve static files and must never host an open redirect.
"""

from __future__ import annotations

import ipaddress
from typing import Iterable
from urllib.parse import urlsplit

MAX_IMAGE_URL_LENGTH = 2_048
_ENCODED_PATH_TRICKS = ("%2e", "%2f", "%5c")


def normalize_host(host: str) -> str:
    """Lower-case IDNA (punycode) form of a host name; raises ValueError for anything that is not a plain name."""
    if not host or host.endswith(".") or "%" in host:
        raise ValueError("invalid host")
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("invalid host") from exc
    try:
        ipaddress.ip_address(ascii_host.strip("[]"))
    except ValueError:
        return ascii_host
    raise ValueError("IP literals are not allowed")


def check_image_url(value: str, hosts: Iterable[str] | None = None) -> None:
    """Raise ValueError unless ``value`` is an acceptable https image URL (and on ``hosts`` when they are given)."""
    if len(value) > MAX_IMAGE_URL_LENGTH or any(ord(ch) <= 32 or ord(ch) == 127 or ch == "\\" for ch in value):
        raise ValueError("avatar_image_url is malformed")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.fragment or parsed.username is not None or parsed.password is not None:
        raise ValueError("avatar_image_url must be https without credentials or a fragment")
    host = normalize_host(parsed.hostname or "")
    if parsed.netloc.lower().rsplit("@", 1)[-1] not in (parsed.hostname, f"{parsed.hostname}:443"):
        raise ValueError("avatar_image_url must not set a port")
    path = parsed.path.lower()
    if any(trick in path for trick in _ENCODED_PATH_TRICKS) or any(
        segment in (".", "..") for segment in path.split("/")
    ):
        raise ValueError("avatar_image_url path is not allowed")
    if hosts is not None and host not in {normalize_host(h) for h in hosts if h.strip()}:
        raise ValueError("avatar_image_url is not on an allowed host")
