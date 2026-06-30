"""MockRenderBackend — streaming mock avatar renderer (Task 5).

Consumes ``AudioWindow`` objects (the TTS stage output) and synthesizes
``VideoWindow`` objects whose ``frames`` are real JPEG bytes drawn with
PIL. This is a MOCK renderer: only the avatar rendering is mocked — the
LLM and TTS stages upstream remain real.

Animation is driven by the audio payload when available (mouth openness
from PCM RMS) with deterministic fallbacks (sinusoid from frame index)
otherwise. No network, no LiveKit, no model downloads. Useful for local
development, CI, and offline streaming-pipeline tests.

Public surface:
  MockRenderBackend(RenderBackend)
    start(opts) -> StartResult
    stream_audio(session_id, audio_window) -> Iterator[VideoWindow]
    interrupt(session_id) -> None
    stop(session_id) -> None
    session_status(session_id) -> str
    get_last_frame_png(session_id) -> bytes
    iter_mjpeg_frames(session_id) -> Iterator[bytes]
    iter_idle_frames(session_id) -> Iterator[bytes]
    get_idle_frame_jpeg(session_id, t_ms=None) -> bytes
"""

from __future__ import annotations

import io
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Iterator, Optional

from .base import RenderBackend, StartOptions, StartResult
from .windows import AudioWindow, VideoWindow, num_frames_for


# ---------------------------------------------------------------------------
# Internal session state
# ---------------------------------------------------------------------------


@dataclass
class _MockSession:
    """Per-session state for the mock renderer."""

    session_id: str
    status: str = "active"
    current_utterance: Optional[str] = None
    last_frame_png: bytes = b""
    frames: list[bytes] = field(default_factory=list)  # last utterance's JPEGs
    frame_counter: int = 0
    created_at: float = field(default_factory=time.monotonic)
    last_audio_window: Optional[AudioWindow] = None
    # Per-session VideoWindow sequence counter.
    video_seq: int = 0
    # Pre-rendered idle loop frames (JPEG bytes). Populated on start().
    # Length is `MockRenderBackend._idle_loop_len` (default 75 = 3s @ 25fps).
    idle_loop_frames: list[bytes] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Frame synthesis (pure, no session state)
# ---------------------------------------------------------------------------


def _rms(int16_samples) -> float:
    """Return normalized RMS in [0, 1] from int16 samples (any numpy array)."""
    # numpy is imported lazily so importing this module never requires numpy
    # for callers that only use start/stop/status.
    import numpy as np

    if int16_samples.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(int16_samples.astype(np.float32) ** 2)))
    # int16 range is [-32768, 32767]; normalize to ~[0, 1].
    return min(1.0, rms / 32768.0)


def _sin01(phase: float) -> float:
    """Sinusoid in [0, 1]."""
    return 0.5 + 0.5 * math.sin(phase)


def _draw_avatar(
    img,
    draw,
    *,
    width: int,
    height: int,
    frame_idx: int,
    fps: int,
    mouth_open: float,
    blink: bool,
    text_lines: list[str],
) -> None:
    """Draw a simple half-body avatar silhouette + text overlay onto img/draw.

    ``img`` is a PIL RGB image; ``draw`` is its ImageDraw. Mutates in place.
    """
    # Background: light cream.
    draw.rectangle([0, 0, width, height], fill=(245, 240, 230))

    # Geometry (centered half-body).
    cx = width // 2
    head_r = min(width, height) // 8
    head_cy = int(height * 0.32)

    # Head bob: small vertical sinusoid offset (amplitude ~ 4px).
    bob = int(4 * math.sin(2 * math.pi * frame_idx / max(1, fps)))
    head_cy += bob

    # Head.
    draw.ellipse(
        [cx - head_r, head_cy - head_r, cx + head_r, head_cy + head_r],
        fill=(210, 190, 170),
        outline=(60, 50, 40),
        width=2,
    )

    # Eyes.
    eye_dx = head_r // 2
    eye_dy = head_r // 4
    eye_r = max(2, head_r // 8)
    if blink:
        # Closed eyes: short horizontal lines.
        draw.line(
            [cx - eye_dx - eye_r, head_cy - eye_dy, cx - eye_dx + eye_r, head_cy - eye_dy],
            fill=(40, 30, 20),
            width=2,
        )
        draw.line(
            [cx + eye_dx - eye_r, head_cy - eye_dy, cx + eye_dx + eye_r, head_cy - eye_dy],
            fill=(40, 30, 20),
            width=2,
        )
    else:
        draw.ellipse(
            [cx - eye_dx - eye_r, head_cy - eye_dy - eye_r,
             cx - eye_dx + eye_r, head_cy - eye_dy + eye_r],
            fill=(40, 30, 20),
        )
        draw.ellipse(
            [cx + eye_dx - eye_r, head_cy - eye_dy - eye_r,
             cx + eye_dx + eye_r, head_cy - eye_dy + eye_r],
            fill=(40, 30, 20),
        )

    # Mouth: openness in [0, 1] -> ellipse height in [2, head_r//2].
    mouth_w = max(4, head_r // 2)
    mouth_h = max(2, int(2 + mouth_open * (head_r // 2)))
    mouth_cy = head_cy + head_r // 2 + 4
    draw.ellipse(
        [cx - mouth_w // 2, mouth_cy - mouth_h // 2,
         cx + mouth_w // 2, mouth_cy + mouth_h // 2],
        fill=(120, 40, 40),
    )

    # Torso (rectangle below head).
    torso_top = head_cy + head_r + 4
    torso_w = int(head_r * 2.4)
    torso_h = int(height * 0.32)
    draw.rounded_rectangle(
        [cx - torso_w // 2, torso_top, cx + torso_w // 2, torso_top + torso_h],
        radius=12,
        fill=(70, 110, 160),
        outline=(40, 60, 90),
        width=2,
    )

    # Arms: two rectangles. One arm waves (angle sinusoid), one static.
    arm_w = max(6, head_r // 3)
    arm_len = int(torso_h * 0.7)
    arm_top = torso_top + 6

    # Static left arm.
    draw.rectangle(
        [cx - torso_w // 2 - arm_w, arm_top,
         cx - torso_w // 2, arm_top + arm_len],
        fill=(70, 110, 160),
        outline=(40, 60, 90),
        width=2,
    )

    # Waving right arm: angle oscillates +/- ~25deg around horizontal.
    angle = math.sin(2 * math.pi * frame_idx / max(1, fps)) * (math.pi / 7)
    ax_start = (cx + torso_w // 2, arm_top + 4)
    ax_end = (
        ax_start[0] + int(arm_len * math.cos(angle)),
        ax_start[1] - int(arm_len * math.sin(angle)),
    )
    draw.line([ax_start, ax_end], fill=(70, 110, 160), width=arm_w)
    # Hand (small ellipse at the end).
    draw.ellipse(
        [ax_end[0] - arm_w, ax_end[1] - arm_w, ax_end[0] + arm_w, ax_end[1] + arm_w],
        fill=(210, 190, 170),
        outline=(60, 50, 40),
        width=2,
    )

    # Text overlay (top-left).
    try:
        from PIL import ImageFont

        font = ImageFont.load_default()
    except Exception:
        font = None

    ty = 6
    for line in text_lines:
        draw.text((8, ty), line, fill=(20, 20, 20), font=font)
        ty += 14


def _synthesize_frame(
    *,
    width: int,
    height: int,
    frame_idx: int,
    fps: int,
    audio_samples,
    text_lines: list[str],
) -> bytes:
    """Render a single frame and return JPEG bytes."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (245, 240, 230))
    draw = ImageDraw.Draw(img)

    # Mouth openness from audio RMS if available, else deterministic sinusoid.
    if audio_samples is not None and getattr(audio_samples, "size", 0) > 0:
        # Subsample: estimate RMS from a slice around this frame's time slot.
        n = audio_samples.size
        # Map frame index to a slice of samples.
        frame_samples = max(1, n // max(1, fps))
        start = min(n, frame_idx * frame_samples)
        end = min(n, start + frame_samples)
        mouth_open = _rms(audio_samples[start:end])
    else:
        mouth_open = _sin01(2 * math.pi * frame_idx / max(1, fps))

    # Blink every ~15 frames, closed for 2 frames.
    blink = (frame_idx % 15) in (0, 1)

    _draw_avatar(
        img, draw,
        width=width, height=height,
        frame_idx=frame_idx, fps=fps,
        mouth_open=mouth_open, blink=blink,
        text_lines=text_lines,
    )

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _synthesize_idle_frame(
    *,
    width: int,
    height: int,
    frame_idx: int,
    loop_len: int,
    fps: int,
    text_lines: list[str],
) -> bytes:
    """Render a single idle-loop frame and return JPEG bytes.

    Differs from ``_synthesize_frame`` in three ways:
      - Mouth fully closed (audio_rms=0).
      - Head bob is a small sinusoid whose period is exactly ``loop_len``
        frames, so frame ``loop_len`` would be identical to frame 0 (seamless
        loop). Amplitude ~ 0.3px (clamped to integer 0 by PIL but kept via
        floor so the bob is visually subtle while still mathematically
        seamless across the wrap boundary).
      - Blink only once per ~50 idle frames (one closed-eye frame at idx % 50 == 0).
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (245, 240, 230))
    draw = ImageDraw.Draw(img)

    mouth_open = 0.0
    blink = (frame_idx % 50) == 0

    # We want a seamless loop, so we re-implement the body-draw with bob that
    # uses loop_len as the period (not fps). _draw_avatar's internal bob and
    # waving-arm formulas use fps for periodicity, which would NOT wrap
    # seamlessly at frame=loop_len-1 -> frame=0. We therefore replicate the
    # drawing here but parameterize the periodic terms on loop_len.

    # Background.
    draw.rectangle([0, 0, width, height], fill=(245, 240, 230))

    cx = width // 2
    head_r = min(width, height) // 8
    head_cy = int(height * 0.32)

    # Seamless head bob: amplitude 0.3 px nominally; rounded down by int(),
    # giving a 0..1 px jitter that wraps perfectly across the loop boundary.
    bob = int(0.3 * math.sin(2 * math.pi * frame_idx / max(1, loop_len)))
    head_cy += bob

    # Head.
    draw.ellipse(
        [cx - head_r, head_cy - head_r, cx + head_r, head_cy + head_r],
        fill=(210, 190, 170),
        outline=(60, 50, 40),
        width=2,
    )

    # Eyes.
    eye_dx = head_r // 2
    eye_dy = head_r // 4
    eye_r = max(2, head_r // 8)
    if blink:
        draw.line(
            [cx - eye_dx - eye_r, head_cy - eye_dy, cx - eye_dx + eye_r, head_cy - eye_dy],
            fill=(40, 30, 20),
            width=2,
        )
        draw.line(
            [cx + eye_dx - eye_r, head_cy - eye_dy, cx + eye_dx + eye_r, head_cy - eye_dy],
            fill=(40, 30, 20),
            width=2,
        )
    else:
        draw.ellipse(
            [cx - eye_dx - eye_r, head_cy - eye_dy - eye_r,
             cx - eye_dx + eye_r, head_cy - eye_dy + eye_r],
            fill=(40, 30, 20),
        )
        draw.ellipse(
            [cx + eye_dx - eye_r, head_cy - eye_dy - eye_r,
             cx + eye_dx + eye_r, head_cy - eye_dy + eye_r],
            fill=(40, 30, 20),
        )

    # Mouth fully closed (mouth_open=0 -> minimal mouth_h).
    mouth_w = max(4, head_r // 2)
    mouth_h = 2
    mouth_cy = head_cy + head_r // 2 + 4
    draw.ellipse(
        [cx - mouth_w // 2, mouth_cy - mouth_h // 2,
         cx + mouth_w // 2, mouth_cy + mouth_h // 2],
        fill=(120, 40, 40),
    )

    # Torso.
    torso_top = head_cy + head_r + 4
    torso_w = int(head_r * 2.4)
    torso_h = int(height * 0.32)
    draw.rounded_rectangle(
        [cx - torso_w // 2, torso_top, cx + torso_w // 2, torso_top + torso_h],
        radius=12,
        fill=(70, 110, 160),
        outline=(40, 60, 90),
        width=2,
    )

    # Arms: idle pose. Static left arm; "right" arm sways gently on the loop
    # period (also seamless) instead of the fps period used by utterance frames.
    arm_w = max(6, head_r // 3)
    arm_len = int(torso_h * 0.7)
    arm_top = torso_top + 6

    draw.rectangle(
        [cx - torso_w // 2 - arm_w, arm_top,
         cx - torso_w // 2, arm_top + arm_len],
        fill=(70, 110, 160),
        outline=(40, 60, 90),
        width=2,
    )

    # Idle sway: smaller amplitude than utterance wave, period = loop_len.
    angle = math.sin(2 * math.pi * frame_idx / max(1, loop_len)) * (math.pi / 20)
    ax_start = (cx + torso_w // 2, arm_top + 4)
    ax_end = (
        ax_start[0] + int(arm_len * math.cos(angle)),
        ax_start[1] - int(arm_len * math.sin(angle)),
    )
    draw.line([ax_start, ax_end], fill=(70, 110, 160), width=arm_w)
    draw.ellipse(
        [ax_end[0] - arm_w, ax_end[1] - arm_w, ax_end[0] + arm_w, ax_end[1] + arm_w],
        fill=(210, 190, 170),
        outline=(60, 50, 40),
        width=2,
    )

    # Text overlay.
    try:
        from PIL import ImageFont

        font = ImageFont.load_default()
    except Exception:
        font = None

    ty = 6
    for line in text_lines:
        draw.text((8, ty), line, fill=(20, 20, 20), font=font)
        ty += 14

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _placeholder_png(width: int, height: int) -> bytes:
    """A simple placeholder PNG (no session-specific content)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (245, 240, 230))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width, height], fill=(245, 240, 230))
    try:
        from PIL import ImageFont

        font = ImageFont.load_default()
    except Exception:
        font = None
    draw.text((8, 8), "mock avatar — waiting for audio", fill=(80, 80, 80), font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _placeholder_jpeg(width: int, height: int) -> bytes:
    """A simple placeholder JPEG for MJPEG streams without a prior utterance."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (245, 240, 230))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width, height], fill=(245, 240, 230))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


_MJPEG_BOUNDARY = "mockmjpegboundary"


class MockRenderBackend(RenderBackend):
    """Streaming mock avatar renderer.

    Renders ``AudioWindow`` -> ``VideoWindow`` with PIL-synthesized JPEG
    frames. Fully offline; no LiveKit, no network.
    """

    name = "mock"

    def __init__(self, fps: int = 25, width: int = 640, height: int = 360) -> None:
        if fps <= 0:
            raise ValueError(f"fps must be > 0, got {fps}")
        if width <= 0 or height <= 0:
            raise ValueError(f"width/height must be > 0, got {width}x{height}")
        self.fps = fps
        self.width = width
        self.height = height
        self._sessions: dict[str, _MockSession] = {}
        # Idle loop length: 3 seconds at the configured fps (75 frames @ 25fps).
        # Kept on self so tests / callers can introspect or override.
        self._idle_loop_len: int = max(1, fps * 3)

    # -- RenderBackend ABC --------------------------------------------------

    def start(self, opts: StartOptions) -> StartResult:
        session_id = f"mock-{uuid.uuid4().hex[:12]}"
        sess = _MockSession(session_id=session_id)
        sess.idle_loop_frames = self._build_idle_loop(session_id)
        self._sessions[session_id] = sess
        return StartResult(
            session_id=session_id,
            livekit_url=f"mock://livekit/{session_id}",
            livekit_client_token=f"mock-token-{uuid.uuid4().hex[:12]}",
            mode="MOCK",
        )

    def say(self, session_id: str, text: str, generate: bool = True) -> str:
        raise NotImplementedError(
            "MockRenderBackend is streaming-only; use stream_audio()"
        )

    def interrupt(self, session_id: str) -> None:
        sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError(session_id)
        sess.status = "interrupting"
        # No frame-gen work to cancel for the mock; return to active.
        sess.status = "active"

    def stop(self, session_id: str) -> None:
        sess = self._sessions.pop(session_id, None)
        if sess is None:
            raise KeyError(session_id)

    def session_status(self, session_id: str) -> str:
        """Return the current status string for a session.

        Concrete default on the base class returns "unknown"; this mock
        override returns the tracked per-session status, raising KeyError
        for unknown sessions so callers can distinguish "no such session"
        from "stopped".
        """
        sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError(session_id)
        return sess.status

    # -- Streaming avatar renderer -----------------------------------------

    def stream_audio(
        self,
        session_id: str,
        audio_window: AudioWindow,
    ) -> Iterator[VideoWindow]:
        """Consume one ``AudioWindow`` and yield one ``VideoWindow``.

        Raises KeyError if ``session_id`` was not returned by ``start()``.
        """
        sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError(session_id)

        # Resolve duration_ms: trust the window; derive from PCM if zero.
        duration_ms = audio_window.duration_ms
        if duration_ms <= 0 and audio_window.pcm:
            # int16 mono: 2 bytes/sample.
            duration_ms = int(
                len(audio_window.pcm) // 2 * 1000 / max(1, audio_window.sample_rate)
            )

        # Build a derived AudioWindow with the resolved duration for frame
        # counting (don't mutate the caller's frozen dataclass).
        if duration_ms != audio_window.duration_ms:
            counted = AudioWindow(
                session_id=audio_window.session_id,
                utterance_id=audio_window.utterance_id,
                seq=audio_window.seq,
                sample_rate=audio_window.sample_rate,
                duration_ms=duration_ms,
                is_final=audio_window.is_final,
                pcm=audio_window.pcm,
                audio_path=audio_window.audio_path,
                text_span=audio_window.text_span,
                id=audio_window.id,
            )
        else:
            counted = audio_window

        num_frames = num_frames_for(counted, self.fps)

        # Parse PCM into int16 numpy array for RMS-driven mouth.
        audio_samples = None
        if audio_window.pcm:
            try:
                import numpy as np

                audio_samples = np.frombuffer(audio_window.pcm, dtype="<i2")
            except Exception:
                audio_samples = None

        text_span = (audio_window.text_span or "")[:40]
        timestamp_ms = 0

        frames: list[bytes] = []
        last_jpeg = b""
        for i in range(num_frames):
            timestamp_ms = int(i * 1000 / max(1, self.fps))
            text_lines = [
                f"session: {session_id}",
                f"utt: {audio_window.utterance_id}",
                f"frame: {i}/{num_frames}",
                f"t: {timestamp_ms}ms",
                f"text: {text_span}",
            ]
            jpeg = _synthesize_frame(
                width=self.width,
                height=self.height,
                frame_idx=i,
                fps=self.fps,
                audio_samples=audio_samples,
                text_lines=text_lines,
            )
            frames.append(jpeg)
            last_jpeg = jpeg
            sess.frame_counter += 1

        # Update session state: store last frame as PNG + the JPEG list.
        if last_jpeg:
            try:
                from PIL import Image

                with io.BytesIO(last_jpeg) as jbuf:
                    img = Image.open(jbuf)
                    img.load()
                    pbuf = io.BytesIO()
                    img.save(pbuf, format="PNG")
                    sess.last_frame_png = pbuf.getvalue()
            except Exception:
                # Fall back to a fresh placeholder PNG if JPEG->PNG fails.
                sess.last_frame_png = _placeholder_png(self.width, self.height)
        else:
            sess.last_frame_png = _placeholder_png(self.width, self.height)

        sess.frames = frames
        sess.current_utterance = audio_window.utterance_id
        sess.last_audio_window = counted
        if sess.status == "active":
            sess.status = "speaking"
        # After yielding, return to active (no continuous stream for mock).
        seq = sess.video_seq
        sess.video_seq += 1

        vw = VideoWindow(
            session_id=session_id,
            utterance_id=audio_window.utterance_id,
            seq=seq,
            frames=frames,
            fps=self.fps,
            duration_ms=duration_ms,
            audio_window_id=audio_window.id,
            is_final=audio_window.is_final,
        )
        if audio_window.is_final:
            sess.status = "active"
        yield vw

    # -- Media endpoint helpers --------------------------------------------

    def get_last_frame_png(self, session_id: str) -> bytes:
        """Return the last rendered frame as PNG bytes.

        Raises KeyError if the session is unknown. If no frame has been
        rendered yet, returns a placeholder PNG at the configured dims.
        """
        sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError(session_id)
        if not sess.last_frame_png:
            sess.last_frame_png = _placeholder_png(self.width, self.height)
        return sess.last_frame_png

    def iter_mjpeg_frames(self, session_id: str) -> Iterator[bytes]:
        """Yield MJPEG multipart frames for the ``/mock/video.mjpeg`` endpoint.

        Bounded: yields the stored frames from the last ``stream_audio``
        call once, then stops. If no frames were stored, yields a single
        placeholder frame and stops.
        """
        sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError(session_id)
        frames = list(sess.frames) if sess.frames else []
        if not frames:
            frames = [_placeholder_jpeg(self.width, self.height)]

        boundary = _MJPEG_BOUNDARY
        for jpeg in frames:
            header = (
                f"--{boundary}\r\n"
                f"Content-Type: image/jpeg\r\n"
                f"Content-Length: {len(jpeg)}\r\n\r\n"
            ).encode("ascii")
            yield header + jpeg + b"\r\n"
        # Closing boundary.
        yield f"--{boundary}--\r\n".encode("ascii")

    @staticmethod
    def mjpeg_boundary() -> str:
        """Return the MJPEG multipart boundary used by ``iter_mjpeg_frames``."""
        return _MJPEG_BOUNDARY

    # -- Idle loop ---------------------------------------------------------

    def _build_idle_loop(self, session_id: str) -> list[bytes]:
        """Pre-render the seamless idle loop frames for one session.

        Returns ``self._idle_loop_len`` JPEG frames (default 75 = 3 s @ 25 fps).
        Each frame is rendered with mouth fully closed and a head bob whose
        sinusoidal period equals ``_idle_loop_len`` so frame N wraps cleanly
        to frame 0. Blinks once per ~50 idle frames.
        """
        frames: list[bytes] = []
        n = self._idle_loop_len
        for i in range(n):
            text_lines = [
                f"session: {session_id}",
                "idle",
            ]
            jpeg = _synthesize_idle_frame(
                width=self.width,
                height=self.height,
                frame_idx=i,
                loop_len=n,
                fps=self.fps,
                text_lines=text_lines,
            )
            frames.append(jpeg)
        return frames

    def iter_idle_frames(self, session_id: str) -> Iterator[bytes]:
        """Yield idle-loop JPEG frames indefinitely (no pacing, FIFO by frame_idx).

        The caller controls the rate (e.g. one frame per 40 ms for 25 fps).
        Raises KeyError if ``session_id`` is unknown. If the session was
        started but the cache is somehow empty, it is rebuilt lazily.
        """
        sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError(session_id)
        if not sess.idle_loop_frames:
            sess.idle_loop_frames = self._build_idle_loop(session_id)
        frames = sess.idle_loop_frames
        i = 0
        n = len(frames)
        while True:
            yield frames[i % n]
            i += 1

    def get_idle_frame_jpeg(
        self,
        session_id: str,
        t_ms: Optional[int] = None,
    ) -> bytes:
        """Return one idle-loop JPEG frame deterministically by logical time.

        ``t_ms`` is in milliseconds. The returned frame is
        ``idle_loop[(t_ms // 40) % loop_len]`` (40 ms = one 25-fps frame).
        If ``t_ms`` is None, uses ``time.monotonic_ns() // 1_000_000``.

        Raises KeyError if ``session_id`` is unknown.
        """
        sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError(session_id)
        if not sess.idle_loop_frames:
            sess.idle_loop_frames = self._build_idle_loop(session_id)
        if t_ms is None:
            t_ms = time.monotonic_ns() // 1_000_000
        # 40 ms per frame matches 25 fps idle pacing; loops via modulo.
        n = len(sess.idle_loop_frames)
        idx = (int(t_ms) // 40) % n
        return sess.idle_loop_frames[idx]
