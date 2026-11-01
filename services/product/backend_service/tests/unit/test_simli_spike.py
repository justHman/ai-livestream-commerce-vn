"""Pure logic of scripts/simli_spike.py (no network, no LiveKit)."""

from __future__ import annotations

import importlib.util
import struct
import sys
import wave
import zlib
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[5] / "scripts" / "simli_spike.py"
spec = importlib.util.spec_from_file_location("simli_spike", SCRIPT)
assert spec and spec.loader
spike = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = spike  # @dataclass resolves its module by name
spec.loader.exec_module(spike)


def write_wav(path, rate, channels, samples, width=2):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(samples)


def test_wav_48k_stereo_becomes_16k_mono_pcm16(tmp_path):
    frames = struct.pack("<hh", 1000, 3000) * 48000  # 1 s stereo
    p = tmp_path / "a.wav"
    write_wav(p, 48000, 2, frames)
    pcm = spike.audio_to_pcm16k(str(p))
    assert len(pcm) == 16000 * 2
    assert struct.unpack("<h", pcm[:2])[0] == 2000  # channel average


def test_wav_already_16k_mono_is_unchanged(tmp_path):
    data = struct.pack("<h", 7) * 1600
    p = tmp_path / "a.wav"
    write_wav(p, 16000, 1, data)
    assert spike.audio_to_pcm16k(str(p)) == data


def test_max_seconds_trims(tmp_path):
    p = tmp_path / "a.wav"
    write_wav(p, 16000, 1, b"\x01\x00" * 32000)
    assert len(spike.audio_to_pcm16k(str(p), 0.5)) == 8000 * 2


def test_raw_pcm_is_taken_as_16k_mono(tmp_path):
    p = tmp_path / "a.pcm"
    p.write_bytes(b"\x01\x00" * 100 + b"\x09")  # odd trailing byte dropped
    assert len(spike.audio_to_pcm16k(str(p))) == 200


def test_unsupported_wav_width_is_refused(tmp_path):
    p = tmp_path / "a.wav"
    write_wav(p, 16000, 1, b"\x00\x00\x00" * 10, width=3)
    with pytest.raises(ValueError):
        spike.audio_to_pcm16k(str(p))


def test_silence_and_chunks():
    assert spike.silence_pcm(100) == b"\x00" * 3200
    parts = spike.chunk_pcm(b"\x01\x00" * 4000, 100)
    assert [len(c) for c in parts] == [3200, 3200, 1600]


def test_config_requires_names_only_and_accepts_livekit_host():
    with pytest.raises(SystemExit) as err:
        spike.resolve_config({"SIMLI_API_KEY": "secret-key-value"})
    assert "LIVEKIT_API_SECRET" in str(err.value) and "secret-key-value" not in str(err.value)
    cfg = spike.resolve_config(
        {
            "SIMLI_API_KEY": "k",
            "LIVEKIT_HOST": "lk.example.test",
            "LIVEKIT_API_KEY": "a",
            "LIVEKIT_API_SECRET": "s",
        }
    )
    assert cfg["livekit_url"] == "wss://lk.example.test" and cfg["face_id"] == ""


def test_env_file_parsing(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# c\nexport A=1\nB='two'\n\nbad line\nC=x=y\n", encoding="utf-8")
    assert spike.parse_env_file(str(p)) == {"A": "1", "B": "two", "C": "x=y"}


def test_redact_removes_secrets_and_jwts():
    text = "key=abcd1234 tok=eyJhbGciOi.eyJzdWIiOi.sig end"
    out = spike.redact(text, ["abcd1234"])
    assert "abcd1234" not in out and "eyJ" not in out


def test_token_body_uses_the_backend_builder():
    from backend.application.clients.avatar.simli import SimliSettings, build_token_body

    s = SimliSettings("u", "k", "s", "key", max_session_s=120, max_idle_s=20)
    assert build_token_body(s, "face-12345") == {
        "faceId": "face-12345",
        "handleSilence": True,
        "maxSessionLength": 180,
        "maxIdleTime": 20,
    }


def test_pick_face_prefers_trinity2_half_body():
    faces = [
        {"id": "f-1", "name": "Old head"},
        {"id": "f-2", "name": "Trinity-1 half body"},
        {"faceId": "f-3", "name": "Anna", "model": "Trinity-2", "framing": "half-body"},
    ]
    assert spike.pick_face(faces) == "f-3"
    assert spike.pick_face({"faces": faces[:2]}) == "f-2"
    assert spike.pick_face(faces[:1]) == "f-1"
    with pytest.raises(ValueError):
        spike.pick_face([])


def test_png_is_valid_and_motion_energy():
    rgba = bytes([10, 20, 30, 255]) * 4
    png = spike.png_bytes(2, 2, rgba)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    idat = png[png.index(b"IDAT") + 4 : png.index(b"IEND") - 8]
    assert zlib.decompress(idat) == (b"\x00" + rgba[:8]) * 2
    assert spike.motion_energy(b"\x00" * 1000, b"\x00" * 1000) == 0.0
    assert spike.motion_energy(b"\x00" * 1000, b"\x10" * 1000) == 16.0


def test_result_line_has_status_metrics_and_evidence():
    r = spike.Result("clear_buffer", "PASS", {"ack_s": 0.2}, "ok")
    assert r.line() == "[PASS] clear_buffer | ack_s=0.2 | ok"


def test_refuses_without_confirm_flag(capsys):
    with pytest.raises(SystemExit):
        spike.parse_args(["--audio", "x.wav"])
    assert "--confirm-real-api" in capsys.readouterr().err


def test_plan_limits():
    base = ["--confirm-real-api", "--audio", "x.wav"]
    assert spike.parse_args(base).max_total_s == 120
    with pytest.raises(SystemExit):
        spike.parse_args(base + ["--soak-seconds", "300"])  # needs a raised --max-total-s
    ok = spike.parse_args(base + ["--soak-seconds", "300", "--max-total-s", "400"])
    assert ok.soak_seconds == 300
    with pytest.raises(SystemExit):
        spike.parse_args(base + ["--idle-seconds", "30"])  # < 2.2 x max-idle
