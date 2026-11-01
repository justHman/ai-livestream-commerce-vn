#!/usr/bin/env python
"""Manual REAL-API spike for the ``cloud_simli`` backend. It spends Simli minutes: run it by hand.

    uv run --project services/product/backend_service python scripts/simli_spike.py \
        --confirm-real-api --env-file .env.simli --audio sample_vi.wav \
        --report out/simli-report.json --save-frames 6 --frames-dir out/frames

Refuses to run without ``--confirm-real-api``. Config comes from the environment, or from the file
named by ``--env-file`` (no file is read by default): SIMLI_API_KEY, LIVEKIT_URL (or LIVEKIT_HOST),
LIVEKIT_API_KEY, LIVEKIT_API_SECRET, optional SIMLI_FACE_ID. Secrets are never printed or written.

Face choice: ``--face-id`` > SIMLI_FACE_ID > the first preset face of ``GET {api_base}/{faces-path}``
(default path ``faces``, UNVERIFIED: see https://docs.simli.com/api-reference/preset-faces and
.../get-all-faces) preferring Trinity-2 half-body entries (``pick_face``). If listing fails the run
stops before anything is created: pass --face-id.

Two LiveKit connections are used: the *runtime* (sends audio, receives lk.playback_* RPCs, is the
agent the avatar publishes on behalf of) and an *observer* (subscribe-only: counts video frames,
measures motion, saves PNG frames, sees the avatar enter/leave). Scenarios, each one line
PASS | FAIL | INCONCLUSIVE:
  create_session, audio_playback, clear_buffer, idle_timeout + idle_silence_resets
  (--idle-seconds), soak (--soak-seconds), close_session. Frames: --save-frames N.
Safety: total run <= --max-total-s (default 120 s, also sent as maxSessionLength); the room is
always left in ``finally`` (that ends the avatar: no terminate endpoint is documented). The room
name is ``simli-spike-<timestamp>``; livekit-api is not installed, so the room is not deleted and
closes on LiveKit's empty timeout. Nothing in this file runs at import time.

Short run (~25-30 s) to look at lip-sync and framing:
    ... --audio vi.wav --audio-seconds 12 --no-clear --record-mp4 out/clip.mp4 \
        --camera-json '{"zoom":0.8,"pos_z_off":1.0}' --avatar-position-y 0.3
``--record-mp4`` (needs ffmpeg on PATH, or --ffmpeg) records the observer view from the start of the
audio scenario at the measured fps, <= 60 s, with sound: the avatar audio track when there is one,
else the PCM the runtime sent (report ``record.audio_source`` = avatar_track | sent_pcm).
Expected A/V offset error ~100-300 ms (see ``Recorder``). ``--camera-json`` (keys/ranges checked, Simli
does not clamp: pos_*_off -10..10, pos_z_off -5..5, zoom >0..4, rot_* -180..180, orbit_x_off -80..80,
orbit_y_off -180..180), ``--avatar-position-x/y`` (0..1) go straight into the compose/token body and
are echoed in the report. ``--no-clear`` skips the clear_buffer scenario. Idle/soak stay off by default.
"""

from __future__ import annotations

import argparse
import array
import asyncio
import json
import os
import re
import struct
import sys
import time
import wave
import zlib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SAMPLE_RATE = 16000
FREE_MINUTES = 50.0
_JWT = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]*")


# ---- pure helpers (unit-tested, no network) ---------------------------------------------------


def parse_env_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        out[key] = value.strip().strip("\"'")
    return out


def resolve_config(env: dict[str, str]) -> dict[str, str]:
    """Names the missing variables, never echoes a value."""
    url = env.get("LIVEKIT_URL") or env.get("LIVEKIT_HOST") or ""
    if url and "://" not in url:
        url = "wss://" + url
    url = re.sub(r"^http(s?)://", lambda m: "ws" + m.group(1) + "://", url)
    cfg = {
        "simli_api_key": env.get("SIMLI_API_KEY", ""),
        "livekit_url": url,
        "livekit_api_key": env.get("LIVEKIT_API_KEY", ""),
        "livekit_api_secret": env.get("LIVEKIT_API_SECRET", ""),
        "face_id": env.get("SIMLI_FACE_ID", "").strip(),
        "api_base": env.get("SIMLI_API_BASE", "") or "https://api.simli.ai",
    }
    missing = [
        name
        for name, key in (
            ("SIMLI_API_KEY", "simli_api_key"),
            ("LIVEKIT_URL/LIVEKIT_HOST", "livekit_url"),
            ("LIVEKIT_API_KEY", "livekit_api_key"),
            ("LIVEKIT_API_SECRET", "livekit_api_secret"),
        )
        if not cfg[key]
    ]
    if missing:
        raise SystemExit("missing configuration: " + ", ".join(missing))
    return cfg


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if len(secret) >= 4:
            text = text.replace(secret, "<redacted>")
    return _JWT.sub("<jwt-redacted>", text)


def audio_to_pcm16k(path: str, max_seconds: float | None = None) -> bytes:
    """WAV (8/16-bit, any rate/channels) or raw .pcm/.raw (assumed 16 kHz mono PCM16) -> PCM16 16k."""
    if Path(path).suffix.lower() in {".pcm", ".raw"}:
        pcm = Path(path).read_bytes()
        pcm = pcm[: len(pcm) // 2 * 2]
        return pcm[: int(max_seconds * SAMPLE_RATE) * 2] if max_seconds else pcm
    with wave.open(path, "rb") as w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        frames = w.readframes(w.getnframes())
    if width == 2:
        samples = array.array("h")
        samples.frombytes(frames[: len(frames) // 2 * 2])
        if sys.byteorder == "big":
            samples.byteswap()
    elif width == 1:
        samples = array.array("h", [(b - 128) << 8 for b in frames])
    else:
        raise ValueError(f"unsupported WAV sample width {width * 8} bit (use 16-bit)")
    if channels > 1:
        usable = len(samples) // channels * channels
        samples = array.array(
            "h",
            [sum(samples[i : i + channels]) // channels for i in range(0, usable, channels)],
        )
    if rate != SAMPLE_RATE and len(samples):
        n_out = int(len(samples) * SAMPLE_RATE / rate)
        out = array.array("h")
        last = len(samples) - 1
        for i in range(n_out):
            pos = i * rate / SAMPLE_RATE
            idx = int(pos)
            nxt = min(idx + 1, last)
            frac = pos - idx
            out.append(int(samples[min(idx, last)] * (1 - frac) + samples[nxt] * frac))
        samples = out
    if max_seconds:
        samples = samples[: int(max_seconds * SAMPLE_RATE)]
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


def silence_pcm(ms: int) -> bytes:
    return b"\x00\x00" * (SAMPLE_RATE * ms // 1000)


def chunk_pcm(pcm: bytes, ms: int = 100) -> list[bytes]:
    size = SAMPLE_RATE * ms // 1000 * 2
    return [pcm[i : i + size] for i in range(0, len(pcm), size)] or [b""]


def pick_face(faces: Any) -> str:
    """Prefer a Trinity-2 half-body preset, then any Trinity, then the first face."""
    if isinstance(faces, dict):
        faces = faces.get("faces") or faces.get("data") or faces.get("items") or []
    entries = []
    for f in faces if isinstance(faces, list) else []:
        if isinstance(f, dict):
            fid = f.get("id") or f.get("faceId") or f.get("face_id")
            if isinstance(fid, str) and fid:
                entries.append((fid, json.dumps(f).lower()))
    if not entries:
        raise ValueError("no face found in the listing")
    half = ("half", "upper", "torso", "bust", "waist")
    for rule in (
        lambda t: "trinity-2" in t and any(h in t for h in half),
        lambda t: "trinity" in t and any(h in t for h in half),
        lambda t: "trinity" in t,
        lambda t: True,
    ):
        for fid, text in entries:
            if rule(text):
                return fid
    raise AssertionError("unreachable")


def png_bytes(width: int, height: int, rgba: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    stride = width * 4
    raw = b"".join(b"\x00" + rgba[y * stride : (y + 1) * stride] for y in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def motion_energy(prev: bytes, cur: bytes, step: int = 251) -> float:
    """Mean absolute difference of sub-sampled bytes: a cheap 'is the mouth moving' proxy."""
    a, b = prev[::step], cur[::step]
    n = min(len(a), len(b))
    return sum(abs(x - y) for x, y in zip(a, b)) / n if n else 0.0


@dataclass
class Result:
    name: str
    status: str  # PASS | FAIL | INCONCLUSIVE
    metrics: dict[str, Any] = field(default_factory=dict)
    evidence: str = ""

    def line(self) -> str:
        m = " ".join(f"{k}={v}" for k, v in self.metrics.items())
        return f"[{self.status}] {self.name} | {m} | {self.evidence}"


def validate_plan(a: argparse.Namespace) -> None:
    if a.soak_seconds > 0 and a.max_total_s < a.soak_seconds + 60:
        raise SystemExit("--soak-seconds needs --max-total-s >= soak + 60 (raised on purpose)")
    if a.idle_seconds and a.idle_seconds < a.max_idle_s * 2.2:
        raise SystemExit("--idle-seconds must be >= 2.2 x --max-idle-s")
    clear = 0 if a.no_clear else 22  # the clear scenario sends up to 12 s of audio, then waits
    planned = 30 + a.audio_seconds + clear + a.idle_seconds + a.soak_seconds + 15
    if planned > a.max_total_s:
        raise SystemExit(f"planned ~{planned:.0f}s exceeds --max-total-s {a.max_total_s:.0f}")


# Camera keys and ranges from the Simli docs; Simli does not clamp, so out-of-range is refused.
CAMERA_RANGES: dict[str, tuple[float, float]] = {
    "pos_x_off": (-10, 10),
    "pos_y_off": (-10, 10),
    "pos_z_off": (-5, 5),
    "zoom": (0, 4),  # exclusive lower bound, see validate_camera
    "rot_x_off": (-180, 180),
    "rot_y_off": (-180, 180),
    "rot_z_off": (-180, 180),
    "orbit_x_off": (-80, 80),
    "orbit_y_off": (-180, 180),
}


def validate_camera(cam: Any) -> dict[str, float]:
    if not isinstance(cam, dict) or not cam:
        raise ValueError("camera must be a non-empty JSON object")
    out: dict[str, float] = {}
    for key, value in cam.items():
        if key not in CAMERA_RANGES:
            raise ValueError(f"unknown camera key {key!r}")
        if isinstance(value, bool) or not isinstance(value, int | float) or value != value:
            raise ValueError(f"camera {key} must be a number")
        low, high = CAMERA_RANGES[key]
        if value > high or value < low or (key == "zoom" and value <= 0):
            raise ValueError(f"camera {key} is out of range ({low}..{high})")
        out[key] = value
    return out


def repeat_count(written: int, elapsed_s: float, fps: int) -> int:
    """Frames to write now so frame N sits at N/fps: repeats fill gaps, 0 skips an early frame."""
    return max(0, min(50, int(round(elapsed_s * fps)) + 1 - written))


def silence_gap_samples(received: int, elapsed_s: float, rate: int, tol_s: float = 0.05) -> int:
    """Samples of silence to insert when audio arrived later than its place on the timeline."""
    gap = int(elapsed_s * rate) - received
    return gap if gap > tol_s * rate else 0


def ffmpeg_video_cmd(ffmpeg: str, width: int, height: int, fps: int, out: str) -> list[str]:
    return [
        ffmpeg, "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-video_size", f"{width}x{height}",
        "-framerate", str(fps), "-i", "pipe:0",
        "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p", out,
    ]  # fmt: skip


def ffmpeg_mux_cmd(
    ffmpeg: str, video: str, audio: str, rate: int, channels: int, offset_s: float, out: str
) -> list[str]:
    """Video copied, PCM audio shifted by ``offset_s`` (audio start minus video start)."""
    return [
        ffmpeg, "-y", "-loglevel", "error", "-i", video,
        "-itsoffset", f"{offset_s:.3f}", "-f", "s16le", "-ar", str(rate), "-ac", str(channels),
        "-i", audio, "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac",
        "-shortest", "-t", str(MAX_RECORD_S), out,
    ]  # fmt: skip


MAX_RECORD_S = 60
HIGH_WIDTH = 900  # the full simulcast layer is ~1020x576; the low one 340x192
LOW_LAYER_GRACE_S = 3.0  # recording may start on a low frame only if no high one came this soon


def layer_key(width: int, height: int) -> str:
    return f"{width}x{height}"


# ---- real run (network) -----------------------------------------------------------------------


class Recorder:
    """Observer-side mp4 recording through ffmpeg (subprocess, no Python dependency).

    Video frames are piped live to a temp H.264 file at the MEASURED fps (``repeat_count`` repeats
    or skips frames so each sits at its arrival time). Audio is raw PCM in the temp dir: the avatar
    audio track if one exists (gaps filled with silence), else the PCM the runtime sent, placed at
    ``lk.playback_started`` (or the first write). Both are muxed with an ``-itsoffset`` of
    (audio start - video start). Expected A/V offset error: ~100-300 ms for the avatar track (frame
    delivery jitter); the sent-PCM fallback is only approximate (avatar latency is not modelled), so
    judge lip-sync by the ``avatar_track`` case. Recording stops after MAX_RECORD_S seconds.
    """

    def __init__(self, ffmpeg: str, path: str, max_s: float = MAX_RECORD_S) -> None:
        import tempfile

        self.ffmpeg, self.path, self.max_s = ffmpeg, path, min(max_s, MAX_RECORD_S)
        self.tmp = tempfile.mkdtemp(prefix="simli-rec-")
        self.fps = 25
        self.armed = False
        self.proc: Any = None
        self.size: tuple[int, int] | None = None
        self.video_t0: float | None = None
        self.video_wall0 = 0.0
        self.written = 0
        self.audio_file: Any = None
        self.audio_t0: float | None = None
        self.audio_samples = 0
        self.audio_rate, self.audio_channels = 48000, 1
        self.sent_pcm = b""
        self.sent_wall: float | None = None
        self.error = ""
        self.low_seen_at: float | None = None
        self.started_on_low = False
        self.dropped: dict[str, int] = {}  # frames of another size than the clip, by size

    def arm(self, fps: float) -> None:
        self.fps = int(min(60, max(5, round(fps)))) if fps else 25
        self.armed = True

    def live(self, now: float) -> bool:
        return self.armed and (self.video_t0 is None or now - self.video_t0 < self.max_s)

    def write_video(self, now: float, width: int, height: int, rgba: bytes) -> None:
        import subprocess

        if not self.live(now):
            return
        if self.proc is None and width < HIGH_WIDTH:
            # The clip size is fixed by its first frame: prefer the full simulcast layer.
            if self.low_seen_at is None:
                self.low_seen_at = now
            if now - self.low_seen_at < LOW_LAYER_GRACE_S:
                return
            self.started_on_low = True
        if self.proc is None:
            self.size, self.video_t0, self.video_wall0 = (width, height), now, time.time()
            self.proc = subprocess.Popen(
                ffmpeg_video_cmd(
                    self.ffmpeg, width, height, self.fps, str(Path(self.tmp) / "video.mp4")
                ),
                stdin=subprocess.PIPE,
            )
        if (width, height) != self.size or self.video_t0 is None:
            key = layer_key(width, height)
            self.dropped[key] = self.dropped.get(key, 0) + 1
            return
        n = repeat_count(self.written, now - self.video_t0, self.fps)
        try:
            for _ in range(n):
                self.proc.stdin.write(rgba)
            self.proc.stdin.flush()
        except OSError:
            self.error = "ffmpeg_pipe_closed"
            self.armed = False
            return
        self.written += n

    def write_audio(self, now: float, data: bytes, samples: int, rate: int, channels: int) -> None:
        if not self.live(now) or self.video_t0 is None:
            return
        if self.audio_file is None:
            self.audio_file = open(Path(self.tmp) / "audio.pcm", "wb")
            self.audio_t0, self.audio_rate, self.audio_channels = now, rate, channels
        assert self.audio_t0 is not None
        gap = silence_gap_samples(self.audio_samples, now - self.audio_t0, rate)
        if gap:
            self.audio_file.write(b"\x00\x00" * channels * gap)
            self.audio_samples += gap
        self.audio_file.write(data)
        self.audio_samples += samples

    def finish(self) -> dict[str, Any]:
        """Close ffmpeg, mux, delete the temp dir. Blocking: call it in a thread."""
        import shutil
        import subprocess

        info: dict[str, Any] = {
            "path": self.path,
            "frames_written": self.written,
            "fps": self.fps,
            "started_on_low_layer": self.started_on_low,
            "dropped_other_size": self.dropped,
        }
        try:
            if self.audio_file is not None:
                self.audio_file.close()
            if self.proc is None:
                info["error"] = "no_video_frames"
                return info
            self.proc.stdin.close()
            self.proc.wait(timeout=30)
            video = str(Path(self.tmp) / "video.mp4")
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            audio, rate, ch, source = str(Path(self.tmp) / "audio.pcm"), self.audio_rate, 1, ""
            offset = 0.0
            if self.audio_samples:
                ch, source = self.audio_channels, "avatar_track"
                offset = (self.audio_t0 or 0.0) - (self.video_t0 or 0.0)
            elif self.sent_pcm:
                Path(audio).write_bytes(self.sent_pcm)
                rate, source = SAMPLE_RATE, "sent_pcm"
                offset = max(0.0, (self.sent_wall or self.video_wall0) - self.video_wall0)
            if source:
                cmd = ffmpeg_mux_cmd(self.ffmpeg, video, audio, rate, ch, offset, self.path)
                subprocess.run(cmd, check=True, timeout=90)
            else:
                shutil.copyfile(video, self.path)
            info |= {
                "audio_source": source or "none",
                "audio_offset_s": round(offset, 3),
                "size": f"{self.size[0]}x{self.size[1]}" if self.size else None,
            }
        except Exception as exc:  # fixed text only
            info["error"] = type(exc).__name__
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)
        if self.error:
            info["warning"] = self.error
        return info


class Observer:
    """State seen by the subscribe-only room."""

    def __init__(self, identity: str, save_frames: int, frames_dir: str | None) -> None:
        self.identity = identity
        self.save_frames, self.frames_dir = save_frames, frames_dir
        self.joined_at: float | None = None
        self.first_video_at: float | None = None
        self.left_at: float | None = None
        self.audio_track = False
        self.size: tuple[int, int] | None = None
        self.frames = 0
        self.first_frame_t = self.last_frame_t = 0.0
        self.motion: deque[tuple[float, float]] = deque(maxlen=30000)
        self.saved = 0
        self.saving = False
        self.save_every = 5
        self.disconnect_reason = ""
        self.rec: Recorder | None = None
        self.layers: dict[str, dict[str, float]] = {}  # "WxH" -> first_seen (mono), frames
        self.high_at: float | None = None
        self.quality: dict[str, Any] = {"requested": False}

    def layer_summary(self) -> dict[str, Any]:
        t0 = self.first_video_at or 0.0
        return {
            k: {"frames": int(v["frames"]), "first_seen_s": round(v["first"] - t0, 2)}
            for k, v in self.layers.items()
        }

    def fps(self) -> float:
        span = self.last_frame_t - self.first_frame_t
        return round(self.frames / span, 1) if span > 0 else 0.0

    def mean_motion(self, t0: float, t1: float) -> float:
        vals = [e for t, e in self.motion if t0 <= t <= t1]
        return sum(vals) / len(vals) if vals else 0.0


async def _consume_video(rtc: Any, obs: Observer, track: Any) -> None:
    prev: bytes | None = None
    async for event in rtc.VideoStream(track):
        now = time.monotonic()
        frame = event.frame.convert(rtc.VideoBufferType.RGBA)
        data = bytes(frame.data)
        obs.size = (frame.width, frame.height)
        layer = obs.layers.setdefault(
            layer_key(frame.width, frame.height), {"first": now, "frames": 0}
        )
        layer["frames"] += 1
        if frame.width >= HIGH_WIDTH and obs.high_at is None:
            obs.high_at = now
        if obs.first_video_at is None:
            obs.first_video_at = now
            obs.first_frame_t = now
        obs.last_frame_t, obs.frames = now, obs.frames + 1
        if obs.rec is not None and obs.rec.live(now):
            await asyncio.to_thread(obs.rec.write_video, now, frame.width, frame.height, data)
        if prev is not None:
            obs.motion.append((now, motion_energy(prev, data)))
        prev = data
        if (
            obs.saving
            and obs.frames_dir
            and obs.saved < obs.save_frames
            and obs.frames % obs.save_every == 0
        ):
            obs.saved += 1
            out = Path(obs.frames_dir) / f"frame_{obs.saved:03d}.png"
            out.write_bytes(png_bytes(frame.width, frame.height, data))


async def _consume_audio(rtc: Any, obs: Observer, track: Any) -> None:
    async for event in rtc.AudioStream(track):
        f = event.frame
        obs.audio_track = True
        if obs.rec is not None:
            obs.rec.write_audio(
                time.monotonic(),
                bytes(f.data),
                f.samples_per_channel,
                f.sample_rate,
                f.num_channels,
            )


async def _post_json(client: Any, url: str, headers: dict, body: dict, timeout: float):
    r = await client.post(url, headers=headers, json=body, timeout=timeout)
    try:
        data = r.json()
    except ValueError:
        data = {}
    return r.status_code, data if isinstance(data, dict) else {}


async def run(cfg: dict[str, str], args: argparse.Namespace, results: list[Result]) -> dict:
    import httpx
    from livekit import rtc

    from backend.application.clients.avatar.simli import SimliSettings, build_token_body
    from backend.application.publishing.datastream import AvatarAudioChannel
    from backend.application.publishing.livekit import mint_room_token

    room_name = f"simli-spike-{int(time.time())}"
    avatar_id, runtime_id = args.avatar_identity, "livento-runtime"
    info: dict[str, Any] = {"room": room_name}
    pcm = audio_to_pcm16k(args.audio, args.audio_seconds)
    if not pcm:
        raise SystemExit("audio is empty after conversion")
    obs = Observer(avatar_id, args.save_frames, args.frames_dir)
    camera = validate_camera(json.loads(args.camera_json)) if args.camera_json else None
    if args.record_mp4:
        obs.rec = Recorder(args.ffmpeg, args.record_mp4)
    runtime, observer = rtc.Room(), rtc.Room()
    channel: AvatarAudioChannel | None = None
    t_join_done: float | None = None
    started = time.monotonic()

    def mint(identity: str, **kw: Any) -> str:
        return mint_room_token(
            api_key=cfg["livekit_api_key"],
            api_secret=cfg["livekit_api_secret"],
            room=room_name,
            identity=identity,
            ttl_sec=600,
            **kw,
        )

    def on_joined(p: Any) -> None:
        if p.identity == avatar_id and obs.joined_at is None:
            obs.joined_at = time.monotonic()

    def on_left(p: Any, *_: Any) -> None:
        if p.identity == avatar_id:
            obs.left_at = time.monotonic()

    def on_track(track: Any, pub: Any, p: Any) -> None:
        if p.identity != avatar_id:
            return
        if track.kind == rtc.TrackKind.KIND_VIDEO:
            # The server may start on the lowest simulcast layer (340x192): ask for the full one
            # so the clip and PNGs show what a viewer would get. Outcome goes to the report.
            obs.quality = {"requested": True, "simulcasted": None, "result": ""}
            try:
                obs.quality["simulcasted"] = bool(pub.simulcasted)
                if pub.simulcasted:
                    pub.set_video_quality(rtc.VideoQuality.VIDEO_QUALITY_HIGH)
                    obs.quality["result"] = "set_high"
                else:
                    obs.quality["result"] = "not_simulcast: only one layer is published"
            except Exception as exc:
                obs.quality["result"] = f"error:{type(exc).__name__}"
            asyncio.ensure_future(_consume_video(rtc, obs, track))
        elif track.kind == rtc.TrackKind.KIND_AUDIO:
            obs.audio_track = True
            asyncio.ensure_future(_consume_audio(rtc, obs, track))

    observer.on("participant_connected", on_joined)
    observer.on("participant_disconnected", on_left)
    observer.on("track_subscribed", on_track)
    observer.on("disconnected", lambda reason=None: setattr(obs, "disconnect_reason", str(reason)))
    runtime_reason: list[str] = []
    runtime.on("disconnected", lambda reason=None: runtime_reason.append(str(reason)))

    try:
        async with asyncio.timeout(args.max_total_s):
            await runtime.connect(
                cfg["livekit_url"], mint(runtime_id, can_publish=False, can_publish_data=True)
            )
            await observer.connect(
                cfg["livekit_url"],
                mint(
                    "spike-observer", can_publish=False, can_subscribe=True, can_publish_data=False
                ),
            )
            async with httpx.AsyncClient() as client:
                # (a) create_session
                s = SimliSettings(
                    livekit_url=cfg["livekit_url"],
                    livekit_api_key=cfg["livekit_api_key"],
                    livekit_api_secret=cfg["livekit_api_secret"],
                    simli_api_key=cfg["simli_api_key"],
                    api_base=cfg["api_base"],
                    face_id=cfg["face_id"],
                    avatar_identity=avatar_id,
                    runtime_identity=runtime_id,
                    max_session_s=args.max_total_s,
                    max_idle_s=args.max_idle_s,
                    streaming_width=args.streaming_width,
                    streaming_height=args.streaming_height,
                    avatar_position_x=args.avatar_position_x,
                    avatar_position_y=args.avatar_position_y,
                    camera=camera,
                )
                info["camera"] = camera
                info["avatar_position"] = [args.avatar_position_x, args.avatar_position_y]
                base = cfg["api_base"].rstrip("/")
                t0 = time.monotonic()
                st, data = await _post_json(
                    client,
                    f"{base}/compose/token",
                    {"x-simli-api-key": cfg["simli_api_key"]},
                    build_token_body(s, cfg["face_id"]),
                    20,
                )
                t_token = time.monotonic()
                token = data.get("session_token")
                if st >= 300 or not isinstance(token, str) or not token:
                    results.append(Result("create_session", "FAIL", {"stage": "token", "http": st}))
                    return info
                info["token_warnings"] = data.get("warnings")
                avatar_jwt = mint(
                    avatar_id,
                    name=avatar_id,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=True,
                    kind="agent",
                    attributes={"lk.publish_on_behalf": runtime_id},
                )
                st, data = await _post_json(
                    client,
                    f"{base}/integrations/livekit/agents",
                    {},
                    {
                        "session_token": token,
                        "livekit_token": avatar_jwt,
                        "livekit_url": cfg["livekit_url"],
                    },
                    20,
                )
                t_join_done = time.monotonic()
                if st >= 300:
                    results.append(Result("create_session", "FAIL", {"stage": "join", "http": st}))
                    return info
                info["simli_session_id"] = data.get("session_id")
                deadline = time.monotonic() + 45
                while obs.first_video_at is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
                ok = obs.first_video_at is not None
                results.append(
                    Result(
                        "create_session",
                        "PASS" if ok else "FAIL",
                        {
                            "token_s": round(t_token - t0, 2),
                            "join_post_s": round(t_join_done - t_token, 2),
                            "avatar_in_room_s": None
                            if obs.joined_at is None
                            else round(obs.joined_at - t0, 2),
                            "first_video_s": None if not ok else round(obs.first_video_at - t0, 2),
                        },
                        "avatar joined and published video" if ok else "no video within 45 s",
                    )
                )
                if not ok:
                    return info
            await asyncio.sleep(2.0)  # idle baseline
            deadline = time.monotonic() + 15
            while obs.high_at is None and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            high = obs.high_at is not None
            results.append(
                Result(
                    "video_layer",
                    "PASS" if high else "INCONCLUSIVE",
                    {
                        "high_first_seen_s": None
                        if not high
                        else round(obs.high_at - (obs.first_video_at or 0.0), 2),
                        "layers": obs.layer_summary(),
                        "quality_request": obs.quality,
                    },
                    "full layer received before audio"
                    if high
                    else f"no frame >= {HIGH_WIDTH}px wide within 15 s (low layer only)",
                )
            )
            channel = AvatarAudioChannel(runtime, avatar_id, sample_rate=SAMPLE_RATE)
            last_audio_at = await _scenarios(rtc, args, results, obs, channel, pcm, info)
            info["last_audio_at"] = last_audio_at
    except TimeoutError:
        results.append(Result("total_deadline", "FAIL", {"max_total_s": args.max_total_s}))
    except Exception as exc:  # fixed text: provider/SDK messages may carry URLs
        results.append(Result("run_error", "FAIL", {"error_type": type(exc).__name__}))
    finally:
        info["runtime_disconnect_reason"] = runtime_reason[-1] if runtime_reason else ""
        await _close(runtime, observer, obs, results, info)
        info["minutes_used_estimate"] = (
            0.0 if t_join_done is None else round((time.monotonic() - t_join_done) / 60, 2)
        )
        info["total_s"] = round(time.monotonic() - started, 1)
        if obs.rec is not None:
            info["record"] = await asyncio.to_thread(obs.rec.finish)
            info["record"]["layers_seen"] = obs.layer_summary()
            info["record"]["high_layer_first_seen_s"] = (
                None if obs.high_at is None else round(obs.high_at - (obs.first_video_at or 0.0), 2)
            )
            info["record"]["video_quality_request"] = obs.quality
            print("recording:", info["record"].get("error") or "saved " + obs.rec.path)
        if channel is not None:
            try:
                await channel.shutdown()
            except Exception:
                pass
    return info


async def _speak(channel: Any, pcm: bytes, uid: str, final: bool = True) -> float:
    t = time.time()
    for i, part in enumerate(chunk_pcm(pcm)):
        await channel.send(
            part,
            src_rate=SAMPLE_RATE,
            utterance_id=uid,
            epoch=channel.epoch,
            final=final and i == len(chunk_pcm(pcm)) - 1,
        )
    return t


async def _scenarios(rtc, args, results, obs, channel, pcm, info) -> float:
    secs = len(pcm) / 2 / SAMPLE_RATE
    mono = time.monotonic
    # (b) audio_playback
    base = obs.mean_motion(mono() - 2.0, mono())
    obs.saving = args.save_frames > 0
    obs.save_every = max(1, int(max(obs.fps(), 10) * secs / max(args.save_frames, 1)))
    t_send_wall, t_send = time.time(), mono()
    if obs.rec is not None:
        obs.rec.arm(obs.fps())
        obs.rec.sent_pcm, obs.rec.sent_wall = pcm, t_send_wall
    try:
        await _speak(channel, pcm, "spike-1")
        finished = await channel.wait_finished("spike-1", secs + 8)
    except Exception as exc:
        results.append(Result("audio_playback", "FAIL", {"error_type": type(exc).__name__}))
        return mono()
    obs.saving = False
    t_end = mono()
    started_at = channel.playback_started_at.get("spike-1")
    if obs.rec is not None and started_at:
        obs.rec.sent_wall = started_at
    first_motion = next((t for t, e in obs.motion if t >= t_send and e > base * 3 + 1.0), None)
    speech = obs.mean_motion(t_send, t_end)
    moved = first_motion is not None
    results.append(
        Result(
            "audio_playback",
            "PASS" if (moved or started_at) and obs.frames else "INCONCLUSIVE",
            {
                "audio_s": round(secs, 1),
                "resolution": f"{obs.size[0]}x{obs.size[1]}" if obs.size else None,
                "fps": obs.fps(),
                "avatar_audio_track": obs.audio_track,
                "playback_started_rpc": started_at is not None,
                "playback_finished_rpc": finished,
                "start_to_playback_started_s": None
                if started_at is None
                else round(started_at - t_send_wall, 2),
                "first_chunk_to_first_motion_s": None
                if not moved
                else round(first_motion - t_send, 2),
                "motion_ratio_vs_idle": round(speech / base, 1) if base else None,
                "frames_saved": obs.saved,
            },
            "motion rose while audio played" if moved else "no motion detected (mouth not proven)",
        )
    )
    last_audio = t_end
    if not args.no_clear:
        await _clear_scenario(args, results, obs, channel, pcm, info, base)
        last_audio = mono()
    return await _tail_scenarios(args, results, obs, channel, pcm, info, last_audio)


async def _clear_scenario(args, results, obs, channel, pcm, info, base) -> None:
    mono = time.monotonic
    long_pcm = (pcm * (1 + int(8 * SAMPLE_RATE * 2 // max(len(pcm), 1))))[: 12 * SAMPLE_RATE * 2]
    t_wall = time.time()
    await _speak(channel, long_pcm, "spike-2", final=False)
    await asyncio.sleep(args.clear_after_s)
    t_clear = mono()
    pre = obs.mean_motion(t_clear - 1.0, t_clear)
    try:
        await channel.clear_buffer()
        ack_s: float | None = round(mono() - t_clear, 2)
    except Exception as exc:
        ack_s = None
        info["clear_error"] = type(exc).__name__
    quiet = None
    deadline = mono() + 8
    while mono() < deadline and quiet is None:
        await asyncio.sleep(0.25)
        if obs.mean_motion(mono() - 0.75, mono()) <= base * 1.5 + 1.0:
            quiet = round(mono() - t_clear, 2)
    finished2 = await channel.wait_finished("spike-2", 3)
    results.append(
        Result(
            "clear_buffer",
            "FAIL"
            if ack_s is None
            else ("PASS" if quiet is not None and pre > base * 1.5 else "INCONCLUSIVE"),
            {
                "ack_s": ack_s,
                "video_quiet_after_s": quiet,
                "motion_before_clear_vs_idle": round(pre / base, 1) if base else None,
                "playback_started_rpc": channel.playback_started_at.get("spike-2") is not None,
                "playback_finished_rpc_after_clear": finished2,
                "since_send_s": round(time.time() - t_wall, 1),
            },
            "clear acknowledged" if ack_s is not None else "clear_buffer raised or timed out",
        )
    )


async def _tail_scenarios(args, results, obs, channel, pcm, info, last_audio) -> float:
    mono = time.monotonic
    # (d) idle_timeout / idle_silence_resets
    if args.idle_seconds and obs.left_at is None:
        idle = args.max_idle_s
        await asyncio.sleep(max(0.0, last_audio + 0.6 * idle - mono()))
        t_sil = mono()
        try:
            await _speak(channel, silence_pcm(300), "spike-sil")
        except Exception as exc:
            info["silence_error"] = type(exc).__name__
        end = last_audio + min(args.idle_seconds, idle * 2.2)
        while mono() < end and obs.left_at is None:
            await asyncio.sleep(0.25)
        if obs.left_at is None:
            results.append(
                Result("idle_timeout", "INCONCLUSIVE", {"max_idle_s": idle}, "avatar stayed")
            )
            results.append(Result("idle_silence_resets", "INCONCLUSIVE", {}, "avatar never left"))
        else:
            dt = obs.left_at - last_audio
            results.append(
                Result(
                    "idle_timeout",
                    "PASS",
                    {"max_idle_s": idle, "left_after_last_audio_s": round(dt, 1)},
                    "avatar left on its own",
                )
            )
            resets = dt >= (t_sil - last_audio) + idle * 0.85
            results.append(
                Result(
                    "idle_silence_resets",
                    "PASS" if (resets or dt <= idle * 1.15) else "INCONCLUSIVE",
                    {
                        "silence_reset_idle": resets,
                        "silence_sent_at_s": round(t_sil - last_audio, 1),
                    },
                    "silence PCM reset the idle timer" if resets else "left at ~maxIdleTime",
                )
            )
        last_audio = mono()
    # (e) soak
    if args.soak_seconds and obs.left_at is None:
        t0, end = mono(), mono() + args.soak_seconds
        n = 0
        while mono() < end and obs.left_at is None and channel.broken is None:
            try:
                await _speak(channel, pcm[: 2 * SAMPLE_RATE * 2], f"soak-{n}")
            except Exception:
                break
            n += 1
            await asyncio.sleep(8)
        ended = round(mono() - t0, 1)
        full = mono() >= end
        results.append(
            Result(
                "soak",
                "PASS" if full and obs.left_at is None else "FAIL",
                {"soak_s": args.soak_seconds, "ended_after_s": ended, "utterances": n},
                "completed"
                if full
                else f"ended early: avatar_left={obs.left_at is not None} channel={channel.broken}",
            )
        )
        last_audio = mono()
    return last_audio


async def _close(runtime, observer, obs, results, info) -> None:
    t = time.monotonic()
    try:
        await asyncio.wait_for(runtime.disconnect(), 10)
    except Exception as exc:
        info["runtime_leave_error"] = type(exc).__name__
    if obs.left_at is not None and obs.left_at < t:
        results.append(Result("close_session", "INCONCLUSIVE", {}, "avatar had already left"))
    else:
        deadline = time.monotonic() + 30
        while obs.left_at is None and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
        left = obs.left_at is not None
        results.append(
            Result(
                "close_session",
                "PASS" if left else "FAIL",
                {"avatar_left_after_s": round(obs.left_at - t, 2) if left else None},
                "avatar left after the runtime left" if left else "avatar still in room after 30 s",
            )
        )
    try:
        await asyncio.wait_for(observer.disconnect(), 10)
    except Exception:
        pass
    info["room_deleted"] = False  # livekit-api is not installed; empty room closes by itself


async def fetch_face(cfg: dict[str, str], faces_path: str) -> str:
    import httpx

    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{cfg['api_base'].rstrip('/')}/{faces_path.lstrip('/')}",
            headers={"x-simli-api-key": cfg["simli_api_key"]},
            timeout=20,
        )
    if r.status_code >= 300:
        raise SystemExit(f"face listing failed status={r.status_code}; pass --face-id")
    return pick_face(r.json())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--confirm-real-api", action="store_true", help="REQUIRED: spends Simli minutes")
    p.add_argument("--env-file", help="read config from this file (default: environment only)")
    p.add_argument("--audio", required=True, help="WAV, or raw 16 kHz mono PCM16 (.pcm/.raw)")
    p.add_argument("--audio-seconds", type=float, default=20.0, help="max audio sent (default 20)")
    p.add_argument("--report", default="simli-spike-report.json", help="JSON report path")
    p.add_argument("--face-id")
    p.add_argument("--faces-path", default="faces")
    p.add_argument("--avatar-identity", default="simli-avatar-agent")
    p.add_argument("--streaming-width", type=int)
    p.add_argument("--streaming-height", type=int)
    p.add_argument("--save-frames", type=int, default=0)
    p.add_argument("--frames-dir")
    p.add_argument("--clear-after-s", type=float, default=1.5)
    p.add_argument("--no-clear", action="store_true", help="skip the clear_buffer scenario")
    p.add_argument("--record-mp4", help="save a short mp4 (video + sound) via ffmpeg, <= 60 s")
    p.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable (default: on PATH)")
    p.add_argument("--camera-json", help='JSON object, e.g. {"zoom":0.8,"pos_z_off":1.0}')
    p.add_argument("--avatar-position-x", type=float, help="0..1, sent to compose/token")
    p.add_argument("--avatar-position-y", type=float, help="0..1, sent to compose/token")
    p.add_argument("--max-idle-s", type=int, default=20, help="maxIdleTime sent (default 20)")
    p.add_argument("--idle-seconds", type=float, default=0, help="idle test budget; 0 = skip")
    p.add_argument("--soak-seconds", type=float, default=0, help="long session; 0 = skip (default)")
    p.add_argument("--max-total-s", type=float, default=120.0)
    p.add_argument("--free-minutes", type=float, default=FREE_MINUTES)
    a = p.parse_args(argv)
    if not a.confirm_real_api:
        p.error("refusing to run: pass --confirm-real-api (this calls the real Simli API)")
    if a.save_frames and not a.frames_dir:
        p.error("--save-frames needs --frames-dir")
    for name in ("avatar_position_x", "avatar_position_y"):
        v = getattr(a, name)
        if v is not None and not 0 <= v <= 1:
            p.error(f"--{name.replace('_', '-')} must be between 0 and 1")
    if a.camera_json:
        try:
            validate_camera(json.loads(a.camera_json))
        except ValueError as exc:  # message names the key, never a secret
            p.error(f"--camera-json: {exc}")
    if a.record_mp4:
        import shutil

        if shutil.which(a.ffmpeg) is None:
            p.error("--record-mp4 needs ffmpeg on PATH (or --ffmpeg PATH)")
    validate_plan(a)
    return a


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env = dict(os.environ)
    if args.env_file:
        env.update(parse_env_file(args.env_file))
    cfg = resolve_config(env)
    if args.face_id:
        cfg["face_id"] = args.face_id
    secrets = [cfg["simli_api_key"], cfg["livekit_api_secret"]]
    if not cfg["face_id"]:
        cfg["face_id"] = asyncio.run(fetch_face(cfg, args.faces_path))
        print("face chosen from the preset listing (Trinity-2 half-body preferred)")
    if args.frames_dir:
        Path(args.frames_dir).mkdir(parents=True, exist_ok=True)
    print(
        f"WARNING: this run may use up to {args.max_total_s / 60:.1f} min of the "
        f"{args.free_minutes:.0f} free minutes. Ctrl+C is safe: the room is left in finally."
    )
    results: list[Result] = []
    info = asyncio.run(run(cfg, args, results))
    for r in results:
        print(redact(r.line(), secrets))
    used = info.get("minutes_used_estimate", 0.0)
    print(
        f"minutes used (estimate): {used}; free minutes left (estimate): {args.free_minutes - used:.1f}"
    )
    report = {
        "api_base": cfg["api_base"],
        "face_id": cfg["face_id"],
        "args": {k: v for k, v in vars(args).items() if k != "env_file"},
        "info": info,
        "results": [r.__dict__ for r in results],
    }
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(redact(json.dumps(report, indent=2, default=str), secrets), encoding="utf-8")
    print(f"report written: {out}")
    return 0 if all(r.status != "FAIL" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
