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
    planned = 30 + a.audio_seconds * 2 + 10 + a.idle_seconds + a.soak_seconds + 15
    if planned > a.max_total_s:
        raise SystemExit(f"planned ~{planned:.0f}s exceeds --max-total-s {a.max_total_s:.0f}")


# ---- real run (network) -----------------------------------------------------------------------


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
        if obs.first_video_at is None:
            obs.first_video_at = now
            obs.first_frame_t = now
        obs.last_frame_t, obs.frames = now, obs.frames + 1
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

    def on_track(track: Any, _pub: Any, p: Any) -> None:
        if p.identity != avatar_id:
            return
        if track.kind == rtc.TrackKind.KIND_VIDEO:
            asyncio.ensure_future(_consume_video(rtc, obs, track))
        elif track.kind == rtc.TrackKind.KIND_AUDIO:
            obs.audio_track = True

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
                )
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
    try:
        await _speak(channel, pcm, "spike-1")
        finished = await channel.wait_finished("spike-1", secs + 8)
    except Exception as exc:
        results.append(Result("audio_playback", "FAIL", {"error_type": type(exc).__name__}))
        return mono()
    obs.saving = False
    t_end = mono()
    started_at = channel.playback_started_at.get("spike-1")
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
    # (c) clear_buffer
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
    last_audio = mono()
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
