import logging

import pytest

from backend.application.clients import composition
from backend.application.contracts.tts_engines import TTSRequest


class _Client:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def synthesize(self, text: str):
        self.texts.append(text)
        return type("R", (), {"pcm": b"\x00\x00", "sample_rate": 24_000})()

    def close(self) -> None:
        return None


@pytest.mark.parametrize("engine_cls", [composition.ElevenLabsTTSEngine, composition.OpenAISpeechTTSEngine])
def test_hosted_adapters_say_once_that_a_session_voice_is_ignored(engine_cls, caplog) -> None:
    engine = engine_cls(_Client())
    with caplog.at_level(logging.WARNING):
        engine.synthesize(TTSRequest(text="a", voice="Hai Dang"))
        engine.synthesize(TTSRequest(text="b", voice="Hai Dang"))
    warnings = [r for r in caplog.records if "tts_voice_override_ignored" in r.getMessage()]
    assert len(warnings) == 1
    assert "Hai Dang" not in warnings[0].getMessage()


@pytest.mark.parametrize("engine_cls", [composition.ElevenLabsTTSEngine, composition.OpenAISpeechTTSEngine])
def test_hosted_adapters_stay_quiet_without_a_session_voice(engine_cls, caplog) -> None:
    engine = engine_cls(_Client())
    with caplog.at_level(logging.WARNING):
        engine.synthesize(TTSRequest(text="a"))
    assert not [r for r in caplog.records if "tts_voice_override_ignored" in r.getMessage()]
