"""Pure logic of scripts/simli_spike.py (no network, no LiveKit)."""

from __future__ import annotations

import importlib.util
import shutil
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


def test_camera_validation_accepts_docs_ranges():
    cam = {"zoom": 0.8, "pos_z_off": 1.0, "pos_y_off": 0.3, "orbit_x_off": -80, "rot_z_off": 180}
    assert spike.validate_camera(cam) == cam


@pytest.mark.parametrize(
    "cam",
    [
        {"zoom": 0},
        {"zoom": 4.1},
        {"pos_z_off": 5.5},
        {"pos_x_off": -10.1},
        {"orbit_x_off": 81},
        {"rot_x_off": 181},
        {"bogus": 1},
        {"zoom": "1"},
        {"zoom": True},
        {},
        [1],
    ],
)
def test_camera_validation_refuses_out_of_range_or_unknown(cam):
    with pytest.raises(ValueError):
        spike.validate_camera(cam)


def test_cli_camera_and_position_flags_are_validated():
    base = ["--confirm-real-api", "--audio", "x.wav"]
    ok = spike.parse_args(base + ["--camera-json", '{"zoom":0.8}', "--avatar-position-x", "0.5"])
    assert ok.avatar_position_x == 0.5
    for bad in (["--camera-json", '{"zoom":9}'], ["--avatar-position-y", "1.5"]):
        with pytest.raises(SystemExit):
            spike.parse_args(base + bad)


def test_no_clear_shortens_the_plan():
    base = ["--confirm-real-api", "--audio", "x.wav", "--max-total-s", "70"]
    assert spike.parse_args(base + ["--no-clear"]).no_clear is True
    with pytest.raises(SystemExit):
        spike.parse_args(base)  # clear step does not fit in 70 s


def test_record_flag_needs_ffmpeg(tmp_path):
    base = ["--confirm-real-api", "--audio", "x.wav", "--record-mp4", str(tmp_path / "a.mp4")]
    with pytest.raises(SystemExit):
        spike.parse_args(base + ["--ffmpeg", str(tmp_path / "missing-ffmpeg")])
    assert spike.parse_args(base + ["--ffmpeg", sys.executable]).record_mp4


def test_repeat_count_places_frames_on_the_timeline():
    assert spike.repeat_count(0, 0.0, 25) == 1  # first frame
    assert spike.repeat_count(1, 0.04, 25) == 1  # on time
    assert spike.repeat_count(2, 0.04, 25) == 0  # early frame is skipped
    assert spike.repeat_count(2, 0.24, 25) == 5  # 200 ms gap is filled by repeats
    assert spike.repeat_count(0, 100.0, 25) == 50  # bounded


def test_silence_gap_samples_ignores_jitter():
    assert spike.silence_gap_samples(16000, 1.02, 16000) == 0
    assert spike.silence_gap_samples(16000, 1.5, 16000) == 8000


def test_ffmpeg_commands():
    v = spike.ffmpeg_video_cmd("ffmpeg", 1366, 768, 25, "v.mp4")
    assert v[v.index("-video_size") + 1] == "1366x768" and v[-1] == "v.mp4" and "pipe:0" in v
    m = spike.ffmpeg_mux_cmd("ffmpeg", "v.mp4", "a.pcm", 48000, 1, -0.12, "o.mp4")
    assert m[m.index("-itsoffset") + 1] == "-0.120" and m.index("-itsoffset") < m.index("a.pcm")
    assert m[m.index("-t") + 1] == "60"


def test_recorder_fps_is_measured_and_clamped():
    rec = spike.Recorder("ffmpeg", "x.mp4")
    rec.arm(24.6)
    assert rec.fps == 25
    rec.arm(200)
    assert rec.fps == 60
    rec.arm(0)
    assert rec.fps == 25
    rec.finish()


def test_recorder_without_frames_reports_and_cleans_up():
    rec = spike.Recorder("ffmpeg", "x.mp4")
    tmp = Path(rec.tmp)
    assert rec.finish()["error"] == "no_video_frames" and not tmp.exists()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_recorder_makes_an_mp4_with_sent_pcm_audio(tmp_path):
    out = tmp_path / "clip.mp4"
    rec = spike.Recorder("ffmpeg", str(out))
    rec.arm(10)
    rec.sent_pcm, rec.sent_wall = b"\x01\x00" * 16000, 0.0
    for i in range(10):
        rec.write_video(100.0 + i * 0.1, 904, 64, bytes([i * 20, 0, 0, 255]) * 904 * 64)
    info = rec.finish()
    assert info.get("error") is None and info["audio_source"] == "sent_pcm"
    assert out.stat().st_size > 0 and not Path(rec.tmp).exists()


def _fake_ffmpeg(monkeypatch):
    sink = [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"]
    monkeypatch.setattr(spike, "ffmpeg_video_cmd", lambda *a, **k: sink)


def test_recorder_waits_for_the_high_layer_and_counts_dropped_sizes(monkeypatch):
    _fake_ffmpeg(monkeypatch)
    rec = spike.Recorder("ffmpeg", "x.mp4")
    rec.arm(10)
    rec.write_video(10.0, 340, 192, b"\x00" * 340 * 192 * 4)  # low layer first: not started
    assert rec.proc is None
    rec.write_video(11.0, 1020, 576, b"\x00" * 1020 * 576 * 4)  # high layer starts the clip
    assert rec.size == (1020, 576)
    rec.write_video(11.1, 340, 192, b"\x00" * 340 * 192 * 4)  # later low frame: counted, not silent
    info = rec.finish()
    assert info["started_on_low_layer"] is False
    assert info["dropped_other_size"] == {"340x192": 1}


def test_recorder_falls_back_to_the_low_layer_after_the_grace_and_says_so(monkeypatch):
    _fake_ffmpeg(monkeypatch)
    rec = spike.Recorder("ffmpeg", "x.mp4")
    rec.arm(10)
    rec.write_video(10.0, 340, 192, b"\x00" * 340 * 192 * 4)
    rec.write_video(10.0 + spike.LOW_LAYER_GRACE_S + 0.1, 340, 192, b"\x00" * 340 * 192 * 4)
    assert rec.size == (340, 192)
    assert rec.finish()["started_on_low_layer"] is True


def test_observer_layer_summary_is_relative_to_the_first_video():
    obs = spike.Observer("a", 0, None)
    obs.first_video_at = 100.0
    obs.layers = {
        "340x192": {"first": 100.0, "frames": 12},
        "1020x576": {"first": 103.5, "frames": 90},
    }
    assert obs.layer_summary() == {
        "340x192": {"frames": 12, "first_seen_s": 0.0},
        "1020x576": {"frames": 90, "first_seen_s": 3.5},
    }
    assert spike.layer_key(1020, 576) == "1020x576"
