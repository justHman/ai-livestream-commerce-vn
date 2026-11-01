"""BYOK avatar credentials at the HTTP edge.

* ``open_tenant_credential``: opens the transit envelope of a session start into memory.
* ``POST /internal/v1/avatar/credential-check``: key/avatar verification without a billable session.

Every rejection is HTTP 400, code ``AVATAR_ENVELOPE_INVALID`` (the app-wide error envelope puts it
at ``error.code`` and repeats it as ``error.message``); the precise reason
goes to the log as a stable word (never the key or the ciphertext). Contract:
``023-prep/BYOK_AVATAR_CONTRACT_2026-11-01.md`` (API addendum).
"""

from __future__ import annotations

import logging
from typing import Any, Literal, NoReturn, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.api.dependencies import container_from_request
from backend.application.rate_limit import quota_identity_key

from . import router

logger = logging.getLogger(__name__)

internal_router = APIRouter(prefix="/internal/v1")
ENVELOPE_INVALID = "AVATAR_ENVELOPE_INVALID"


def reject_envelope(reason: str) -> NoReturn:
    """Log a stable reason word and raise the one public error."""
    logger.warning("avatar credential envelope rejected reason=%s", reason)
    raise HTTPException(
        status_code=400, detail={"code": ENVELOPE_INVALID, "message": ENVELOPE_INVALID}
    )


def open_tenant_credential(
    config: Any,
    *,
    session_ref: Optional[str],
    tenant_id: Optional[str],
    provider: Optional[str],
    envelope: Any,
):
    from backend.application.clients.avatar.byok import load_transit_key, open_envelope

    if not (getattr(config, "avatar_byok_enabled", False) and config.avatar_cred_transit_key):
        reject_envelope("byok_disabled")
    if not (envelope and session_ref and tenant_id and provider):
        reject_envelope("tenant_fields_missing")
    if "|" in session_ref or "|" in tenant_id:  # the AAD is "|"-separated: no ambiguity
        reject_envelope("aad_separator")
    try:
        return open_envelope(
            envelope,
            session_id=session_ref,
            tenant_id=tenant_id,
            provider=provider,
            key=load_transit_key(config.avatar_cred_transit_key),
            kid=config.avatar_cred_transit_key_id,
        )
    except ValueError as exc:  # EnvelopeError is a ValueError; so is a malformed transit key
        reject_envelope(getattr(exc, "code", "transit_key_invalid"))


class CredentialCheckReq(BaseModel):
    provider: Literal[
        "lemonslice", "simli", "bithuman", "beyondpresence", "tavus", "anam", "runway"
    ]
    tenant_id: str = Field(min_length=1, max_length=128)
    check_id: str = Field(min_length=1, max_length=128)
    avatar_id: Optional[str] = Field(default=None, max_length=64)
    avatar_credential_envelope: Union[str, dict[str, Any]] = Field(repr=False)


async def _check_guard(request: Request) -> None:
    """Token first (constant time), THEN the per-caller bucket; a wrong token only drains its own
    small bucket and never touches the legitimate caller's."""
    from backend.api.security.authentication import tokens_match

    config = container_from_request(request).config
    expected = (getattr(config, "avatar_internal_token", "") or "").strip()
    given = (request.headers.get("x-livento-internal-token") or "").strip()
    identity = quota_identity_key(request, router._rate_limit_config(request))
    if not expected or not given or not tokens_match(given, expected):
        if not await request.app.state.api_limiter.allow(f"avatar-check-bad:{identity}"):
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        raise HTTPException(status_code=403, detail={"code": "avatar_override_not_authorized"})
    if not await request.app.state.api_limiter.allow(f"avatar-check:{identity}"):
        raise HTTPException(status_code=429, detail="rate limit exceeded")


@internal_router.post("/avatar/credential-check")
async def credential_check(
    req: CredentialCheckReq,
    request: Request,
    _guard: None = Depends(_check_guard),
) -> dict[str, str]:
    """Returns {code}: ok | invalid_key | not_found | unknown | provider_error."""
    from backend.application.clients.avatar.byok import check_credential

    d = container_from_request(request)
    credential = open_tenant_credential(
        d.config,
        session_ref=f"check:{req.check_id}",  # a start envelope can never be replayed as a check
        tenant_id=req.tenant_id,
        provider=req.provider,
        envelope=req.avatar_credential_envelope,
    )
    code = await check_credential(
        req.provider,
        credential.api_key,
        req.avatar_id,
        simli_base=d.config.simli_api_base,
        bithuman_base=d.config.bithuman_api_base,
        wave2_bases=d.config.wave2_api_bases(),
    )
    return {"code": code}
