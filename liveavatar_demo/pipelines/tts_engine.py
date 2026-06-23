"""TTS Engine — EdgeTTS for Vietnamese text-to-speech.

Uses Microsoft Edge TTS (free, zero-setup, native Vietnamese voices).
Online-only (requires internet connection).

Vietnamese voices:
- vi-VN-HoaiMyNeural (female)
- vi-VN-NamMinhNeural (male)
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import edge_tts
except ImportError:
    edge_tts = None  # type: ignore[assignment]


# Default Vietnamese voices
VI_FEMALE = "vi-VN-HoaiMyNeural"
VI_MALE = "vi-VN-NamMinhNeural"


@dataclass
class TTSResult:
    """Result from TTS generation."""

    wav_path: Path
    text: str
    voice: str
    duration_ms: float


class EdgeTTSEngine:
    """EdgeTTS-based Vietnamese text-to-speech engine.

    Thread-safe: manages its own event loop for async EdgeTTS calls.
    """

    def __init__(
        self,
        voice: str = VI_FEMALE,
        rate: str = "+0%",
        volume: str = "+0%",
        output_dir: Optional[Path] = None,
    ) -> None:
        if edge_tts is None:
            raise ImportError("edge-tts is required. Install with: uv add edge-tts")
        self.voice = voice
        self.rate = rate
        self.volume = volume
        self.output_dir = output_dir or Path(tempfile.mkdtemp(prefix="tts_"))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._counter = 0
        # Dedicated event loop for this thread (avoids conflict with Gradio's loop)
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Get or create an event loop for the current thread."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        self._loop = loop
        return loop

    async def synthesize(self, text: str) -> TTSResult:
        """Generate speech from Vietnamese text."""
        self._counter += 1

        communicate = edge_tts.Communicate(
            text=text,
            voice=self.voice,
            rate=self.rate,
            volume=self.volume,
        )

        mp3_path = self.output_dir / f"tts_{self._counter:04d}.mp3"
        await communicate.save(str(mp3_path))

        wav_path = await self._convert_to_wav(mp3_path)
        duration_ms = self._estimate_duration(wav_path)

        return TTSResult(
            wav_path=wav_path,
            text=text,
            voice=self.voice,
            duration_ms=duration_ms,
        )

    def synthesize_sync(self, text: str) -> TTSResult:
        """Thread-safe synchronous wrapper.

        Creates a new event loop if needed (safe to call from any thread,
        including threads that already have an asyncio loop like Gradio's).
        """
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self._run_in_new_loop, text)
            return future.result(timeout=60)

    def _run_in_new_loop(self, text: str) -> TTSResult:
        """Run synthesize in a brand new event loop (for thread safety)."""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self.synthesize(text))
        finally:
            loop.close()

    async def _convert_to_wav(self, mp3_path: Path) -> Path:
        """Convert MP3 to WAV using available tools."""
        wav_path = mp3_path.with_suffix(".wav")

        # Try ffmpeg first (most reliable)
        try:
            import subprocess

            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(mp3_path),
                    "-acodec", "pcm_s16le", "-ar", "16000",
                    "-ac", "1", str(wav_path),
                ],
                capture_output=True,
                check=True,
            )
            mp3_path.unlink(missing_ok=True)
            return wav_path
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass

        # Fallback: try pydub
        try:
            from pydub import AudioSegment

            audio = AudioSegment.from_mp3(str(mp3_path))
            audio = audio.set_frame_rate(16000).set_channels(1)
            audio.export(str(wav_path), format="wav")
            mp3_path.unlink(missing_ok=True)
            return wav_path
        except ImportError:
            pass

        # Last resort: return mp3 path and let audio_encoder handle it
        return mp3_path

    @staticmethod
    def _estimate_duration(wav_path: Path) -> float:
        """Rough duration estimate from WAV file size."""
        if not wav_path.exists():
            return 0.0
        file_size = wav_path.stat().st_size
        if wav_path.suffix == ".wav":
            duration_ms = (file_size - 44) / 32000 * 1000
        else:
            duration_ms = file_size / 16000 * 1000
        return duration_ms

    @property
    def available_voices(self) -> list[str]:
        """Return Vietnamese voice options."""
        return [VI_FEMALE, VI_MALE]
