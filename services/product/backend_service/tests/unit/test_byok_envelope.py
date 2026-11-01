"""BYOK transit envelope: frozen Go/Python interop vector, rejection rules, code mapping, check."""

from __future__ import annotations

import asyncio
import base64
import json
import logging

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.application.clients.avatar.byok import (
    AvatarCredential,
    EnvelopeError,
    check_credential,
    load_transit_key,
    open_envelope,
    status_to_avatar_code,
    valid_avatar_id,
)
from backend.observability.logging.filters import StructuredFieldsFilter

# FROZEN: identical to cmd/ai-connector-service/internal/avatarcred/testdata/transit_vector.json
# in the API repo (contract addendum 2026-11-01). Never regenerate it.
VECTOR = {
    "key_b64": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
    "kid": "t1",
    "nonce_b64": "oKGio6Slpqeoqaqr",
    "session_id": "33333333-3333-4333-8333-333333333333",
    "tenant_id": "22222222-2222-4222-8222-222222222222",
    "provider": "simli",
    "exp": 1893456000,
    "aad": "33333333-3333-4333-8333-333333333333|22222222-2222-4222-8222-222222222222|simli|1893456000",
    "plaintext": '{"api_key":"sk_test_0123456789","provider":"simli"}',
    "ct_b64": "nTodXSyUadobR73xdBGfqhXfLU+ihnBfqDsQsUeSVy3wBjWQ2Us3WC2+Pup6E+6VLjk7C/BXZa2u20omtqqir+RoeQ==",
}
KEY = base64.b64decode(VECTOR["key_b64"])
NOW = VECTOR["exp"] - 60


def envelope(**over) -> dict:
    env = {
        "v": 1,
        "alg": "A256GCM",
        "kid": VECTOR["kid"],
        "nonce": VECTOR["nonce_b64"],
        "ct": VECTOR["ct_b64"],
        "exp": VECTOR["exp"],
    }
    return env | over


def opened(env=None, **over):
    args = dict(
        session_id=VECTOR["session_id"],
        tenant_id=VECTOR["tenant_id"],
        provider="simli",
        key=KEY,
        kid="t1",
        now=NOW,
    )
    return open_envelope(envelope() if env is None else env, **(args | over))


def test_vector_decrypts_to_the_exact_plaintext():
    plain = AESGCM(KEY).decrypt(
        base64.b64decode(VECTOR["nonce_b64"]),
        base64.b64decode(VECTOR["ct_b64"]),
        VECTOR["aad"].encode(),
    )
    assert plain == VECTOR["plaintext"].encode()


def test_vector_encrypts_to_the_exact_ciphertext():
    ct = AESGCM(KEY).encrypt(
        base64.b64decode(VECTOR["nonce_b64"]),
        VECTOR["plaintext"].encode(),
        VECTOR["aad"].encode(),
    )
    assert base64.b64encode(ct).decode() == VECTOR["ct_b64"]


def test_open_envelope_returns_the_key_in_a_redacted_credential():
    cred = opened()
    assert cred == AvatarCredential(api_key="sk_test_0123456789", provider="simli")


def test_credential_repr_never_shows_the_key():
    assert "sk_test_0123456789" not in repr(opened())


def test_envelope_may_arrive_as_a_json_string():
    assert opened(json.dumps(envelope())).api_key == "sk_test_0123456789"


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({}, "envelope_expired"),  # exp in the past (now moved forward below)
        ({"kid": "other"}, "envelope_kid"),
        ({"nonce": base64.b64encode(b"short").decode()}, "envelope_malformed"),
        ({"ct": "***"}, "envelope_malformed"),
        ({"alg": "A128GCM"}, "envelope_malformed"),
        ({"v": 2}, "envelope_malformed"),
        ({"exp": True}, "envelope_malformed"),
    ],
)
def test_bad_envelopes_are_rejected_with_a_stable_code(over, code):
    now = VECTOR["exp"] + 1 if code == "envelope_expired" else NOW
    with pytest.raises(EnvelopeError) as err:
        opened(envelope(**over), now=now)
    assert err.value.code == code


def test_exp_beyond_the_window_is_rejected():
    with pytest.raises(EnvelopeError) as err:
        opened(now=VECTOR["exp"] - 151)
    assert err.value.code == "envelope_window"


def test_exp_inside_the_window_with_clock_skew_is_accepted():
    assert opened(now=VECTOR["exp"] - 150).provider == "simli"


@pytest.mark.parametrize("part", ["session_id", "tenant_id", "provider"])
def test_a_changed_aad_part_fails_the_tag(part):
    with pytest.raises(EnvelopeError) as err:
        opened(**{part: "bithuman" if part == "provider" else "x"})
    assert err.value.code == "envelope_auth"


def test_a_changed_exp_fails_the_tag():
    with pytest.raises(EnvelopeError) as err:
        opened(envelope(exp=VECTOR["exp"] + 1))
    assert err.value.code == "envelope_auth"


def test_a_flipped_ciphertext_bit_fails_the_tag():
    raw = bytearray(base64.b64decode(VECTOR["ct_b64"]))
    raw[0] ^= 1
    with pytest.raises(EnvelopeError) as err:
        opened(envelope(ct=base64.b64encode(bytes(raw)).decode()))
    assert err.value.code == "envelope_auth"


def test_a_wrong_transit_key_fails_the_tag():
    with pytest.raises(EnvelopeError) as err:
        opened(key=bytes(32))
    assert err.value.code == "envelope_auth"


def _sealed(plain: dict, provider="simli") -> dict:
    nonce = bytes(range(12))
    aad = f"s|t|{provider}|{VECTOR['exp']}".encode()
    ct = AESGCM(KEY).encrypt(nonce, json.dumps(plain).encode(), aad)
    return envelope(nonce=base64.b64encode(nonce).decode(), ct=base64.b64encode(ct).decode())


def test_plaintext_provider_must_match_the_request_provider():
    env = _sealed({"api_key": "k", "provider": "lemonslice"})
    with pytest.raises(EnvelopeError) as err:
        open_envelope(
            env, session_id="s", tenant_id="t", provider="simli", key=KEY, kid="t1", now=NOW
        )
    assert err.value.code == "envelope_malformed"


def test_a_sealed_envelope_round_trips():
    env = _sealed({"api_key": "k", "provider": "simli"})
    got = open_envelope(
        env, session_id="s", tenant_id="t", provider="simli", key=KEY, kid="t1", now=NOW
    )
    assert got.api_key == "k"


def test_transit_key_must_be_32_bytes():
    with pytest.raises(ValueError):
        load_transit_key(base64.b64encode(b"short").decode())


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "AVATAR_KEY_INVALID"),
        (403, "AVATAR_KEY_INVALID"),
        (402, "AVATAR_OUT_OF_CREDIT"),
        (404, "AVATAR_NOT_FOUND"),
        (429, "AVATAR_CONCURRENCY_LIMIT"),
        (500, "AVATAR_PROVIDER_UNAVAILABLE"),
        (503, "AVATAR_PROVIDER_UNAVAILABLE"),
        (400, None),
        (200, None),
    ],
)
def test_provider_status_maps_to_a_stable_code(status, code):
    assert status_to_avatar_code(status) == code


@pytest.mark.parametrize(
    ("provider", "avatar_id", "ok"),
    [
        ("lemonslice", "agent_093d2f9535db94ef", True),
        ("lemonslice", "agent_UPPER", False),
        ("simli", "11111111-2222-4333-8444-555555555555", True),
        ("simli", "not-a-uuid", False),
        ("bithuman", "A_1234", True),
        ("bithuman", "x", False),
    ],
)
def test_avatar_id_is_validated_per_provider(provider, avatar_id, ok):
    assert valid_avatar_id(provider, avatar_id) is ok


def _check(provider, status, data=None, avatar_id=None, raises=None):
    seen = {}

    async def http_get(url, headers, timeout):
        seen.update(url=url, headers=headers)
        if raises:
            raise raises
        return status, data

    code = asyncio.run(
        check_credential(
            provider,
            "the-key",
            avatar_id,
            simli_base="https://simli.test",
            bithuman_base="https://bh.test",
            http_get=http_get,
        )
    )
    return code, seen


def test_simli_check_sends_the_key_header_to_faces():
    code, seen = _check("simli", 200)
    assert (code, seen["url"], seen["headers"]) == (
        "ok",
        "https://simli.test/faces",
        {"x-simli-api-key": "the-key"},
    )


def test_simli_check_finds_a_listed_face():
    face = "11111111-2222-4333-8444-555555555555"
    assert _check("simli", 200, [{"id": face}], face)[0] == "ok"


def test_simli_check_cannot_confirm_an_unlisted_face():
    assert _check("simli", 200, [], "11111111-2222-4333-8444-555555555555")[0] == "unknown"


def test_bithuman_check_uses_the_account_endpoint_and_api_secret_header():
    code, seen = _check("bithuman", 200)
    assert (code, seen["url"], seen["headers"]) == (
        "ok",
        "https://bh.test/v1/me",
        {"api-secret": "the-key"},
    )


@pytest.mark.parametrize(
    ("status", "code"), [(401, "invalid_key"), (403, "invalid_key"), (500, "provider_error")]
)
def test_check_maps_the_provider_answer(status, code):
    assert _check("simli", status)[0] == code


def test_check_maps_a_network_failure_without_its_message():
    assert _check("simli", 0, raises=OSError("https://x?key=the-key"))[0] == "provider_error"


def test_lemonslice_check_is_format_only():
    assert _check("lemonslice", 200)[0] == "unknown"


def test_a_malformed_avatar_id_is_not_found_without_a_call():
    code, seen = _check("simli", 200, avatar_id="nope")
    assert (code, seen) == ("not_found", {})


def _log_text(message: str, *args) -> str:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, message, args, None)
    StructuredFieldsFilter().filter(record)
    return record.getMessage()


def test_log_filter_scrubs_an_api_key_in_free_text():
    assert "sk_live_abc" not in _log_text("call failed api_key=sk_live_abc status=401")


def test_log_filter_scrubs_a_json_envelope_in_free_text():
    text = _log_text('body avatar_credential_envelope={"ct":"AAAA","nonce":"BBBB"} end')
    assert "AAAA" not in text and "end" in text


def test_log_filter_scrubs_a_quoted_key_with_lazy_args():
    assert "sk_x" not in _log_text("sent %s", '"api-secret": "sk_x"')


def test_log_filter_leaves_ordinary_messages_alone():
    assert _log_text("session %s started", "abc") == "session abc started"
