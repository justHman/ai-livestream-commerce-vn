"""SelfHostedTTSClient.list_voices: tolerant parsing of the not-yet-confirmed /v1/voices shape."""

from __future__ import annotations

import httpx
import pytest

from backend.application.clients.tts.self_hosted import (
    VOICES_MAX,
    SelfHostedTTSClient,
    TTSClientError,
    parse_voices,
)


def _client(handler, api_key: str = "tok") -> SelfHostedTTSClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return SelfHostedTTSClient("http://tts.test", api_key=api_key, http_client=http)


def _voices(body, status: int = 200) -> list[dict]:
    return _client(lambda request: httpx.Response(status, json=body)).list_voices()


def _plain(voice_id: str) -> dict:
    return {"id": voice_id, "name": voice_id, "language": None, "gender": None}


def test_list_shape_returns_all_fields() -> None:
    body = [{"id": "a", "name": "Anh", "language": "vi", "gender": "male"}]
    assert _voices(body) == [{"id": "a", "name": "Anh", "language": "vi", "gender": "male"}]


def test_data_wrapper() -> None:
    assert [v["id"] for v in _voices({"data": [{"id": "a"}, {"id": "b"}]})] == ["a", "b"]


def test_voices_wrapper() -> None:
    assert [v["id"] for v in _voices({"voices": [{"id": "a"}]})] == ["a"]


def test_strings_only_use_id_as_name() -> None:
    assert _voices(["Hai Dang"]) == [_plain("Hai Dang")]


def test_odd_keys_are_mapped() -> None:
    body = [{"voice_id": "v1", "display_name": "Voice One", "lang": "vi-VN", "sex": "f"}]
    assert _voices(body) == [{"id": "v1", "name": "Voice One", "language": "vi-VN", "gender": "f"}]


def test_id_falls_back_through_voice_then_name() -> None:
    assert [v["id"] for v in _voices([{"voice": "x"}, {"name": "y"}])] == ["x", "y"]


def test_locale_key_is_read_and_unrelated_fields_are_ignored() -> None:
    voice = _voices([{"id": "a", "locale": "vi", "secret": "leak", "url": "http://x"}])[0]
    assert voice == {"id": "a", "name": "a", "language": "vi", "gender": None}


def test_entries_without_id_are_dropped() -> None:
    assert _voices([{}, {"id": ""}, {"id": "  "}, 5, None, {"id": "ok"}]) == [_plain("ok")]


def test_duplicates_are_dropped() -> None:
    body = [{"id": "a", "name": "one"}, {"id": "a", "name": "two"}]
    assert [v["name"] for v in _voices(body)] == ["one"]


def test_capped_at_200() -> None:
    assert len(_voices([f"v{i}" for i in range(500)])) == VOICES_MAX


def test_strings_are_trimmed_and_name_is_bounded() -> None:
    voice = _voices([{"id": "  a  ", "name": "n" * 500}])[0]
    assert (voice["id"], len(voice["name"])) == ("a", 128)


def test_overlong_id_is_dropped() -> None:
    assert [v["id"] for v in _voices([{"id": "i" * 129}, {"id": "i" * 128}])] == ["i" * 128]


def test_control_characters_reject_the_entry() -> None:
    assert _voices([{"id": "bad\u0000id"}, {"id": "bad\nid"}, {"id": "ok"}]) == [_plain("ok")]


def test_control_characters_in_language_become_none() -> None:
    assert _voices([{"id": "a", "language": "vi\u001b[0m"}])[0]["language"] is None


def test_sends_bearer_to_v1_voices_with_5s_timeout() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["timeout"] = request.extensions["timeout"]["read"]
        return httpx.Response(200, json=[])

    _client(handler).list_voices()
    assert seen == {"url": "http://tts.test/v1/voices", "auth": "Bearer tok", "timeout": 5.0}


@pytest.mark.parametrize("body", [{"nope": 1}, "text", 5, None, {"data": "x"}])
def test_unexpected_shape_raises(body) -> None:
    with pytest.raises(TTSClientError):
        _voices(body)


def test_non_2xx_raises() -> None:
    with pytest.raises(TTSClientError):
        _voices([], status=503)


def test_non_json_raises() -> None:
    client = _client(lambda request: httpx.Response(200, text="<html>"))
    with pytest.raises(TTSClientError):
        client.list_voices()


def test_connection_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(TTSClientError):
        _client(handler).list_voices()


def test_c1_controls_and_bidi_marks_are_dropped() -> None:
    voices = parse_voices(
        [{"id": "ok"}, {"id": "bad\u0085id"}, {"id": "bidi\u202eid"}, {"id": "zw\u200bid"}]
    )
    assert [v["id"] for v in voices] == ["ok"]


def test_language_and_gender_are_bounded_and_label_is_accepted() -> None:
    voices = parse_voices([{"id": "a", "label": "Anh", "language": "v" * 500, "gender": "g" * 500}])
    assert voices[0]["name"] == "Anh"
    assert len(voices[0]["language"]) == 128 and len(voices[0]["gender"]) == 128


def test_only_the_first_thousand_entries_are_inspected() -> None:
    junk = [{"nothing": 1}] * 1_000
    assert parse_voices(junk + [{"id": "late"}]) == []


def test_an_oversized_response_is_refused() -> None:
    big = b"[" + b'"x",' * 400_000 + b'"y"]'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big)

    with pytest.raises(TTSClientError):
        _client(handler).list_voices()
