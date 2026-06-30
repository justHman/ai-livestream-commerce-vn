"""Audio Encoder — Wav2Vec2 audio embedding extraction.

Loads a Wav2Vec2 model (real weights, lightweight ~350MB) and extracts
frame-aligned audio embeddings for conditioning the avatar generator.

For the mock demo, any Wav2Vec2 model works — the embedding dimension
is what matters for the pipeline shape compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import torch


@dataclass
class AudioEmbedding:
    """Audio embedding result."""

    embedding: torch.Tensor   # (num_frames, embed_dim)
    sample_rate: int
    duration_ms: float


class Wav2Vec2AudioEncoder:
    """Wav2Vec2-based audio encoder for avatar conditioning.

    Parameters
    ----------
    model_name : str
        HuggingFace model name (default: facebook/wav2vec2-base).
    device : str
        Torch device.
    sample_rate : int
        Target sample rate for audio input.
    """

    # Embedding dimension for common Wav2Vec2 models
    EMBED_DIMS = {
        "facebook/wav2vec2-base": 768,
        "facebook/wav2vec2-large": 1024,
        "facebook/wav2vec2-base-960h": 768,
    }

    def __init__(
        self,
        model_name: str = "facebook/wav2vec2-base",
        device: str = "cpu",
        sample_rate: int = 16000,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.sample_rate = sample_rate
        self._model = None
        self._processor = None
        self._embed_dim = self.EMBED_DIMS.get(model_name, 768)

    def _load_model(self) -> None:
        """Lazy-load the Wav2Vec2 model and processor."""
        if self._model is not None:
            return

        try:
            from transformers import Wav2Vec2Model, Wav2Vec2Processor

            self._processor = Wav2Vec2Processor.from_pretrained(self.model_name)
            self._model = Wav2Vec2Model.from_pretrained(self.model_name)
            self._model.to(self.device)
            self._model.eval()
        except Exception as e:
            print(f"[audio_encoder] Could not load Wav2Vec2 ({e}). "
                  "Using random embeddings for mock mode.")
            self._model = None
            self._processor = None

    @property
    def embed_dim(self) -> int:
        return self._embed_dim

    def encode(self, audio_path: Path) -> AudioEmbedding:
        """Extract audio embeddings from a WAV file.

        Parameters
        ----------
        audio_path : Path
            Path to the audio file (WAV preferred, 16kHz mono).

        Returns
        -------
        AudioEmbedding
            Frame-aligned embedding tensor.
        """
        self._load_model()

        if self._model is None:
            return self._mock_encode(audio_path)

        # Load audio
        import scipy.io.wavfile as wavfile

        sr, waveform = wavfile.read(str(audio_path))

        # Resample if needed
        if sr != self.sample_rate:
            waveform = self._resample(waveform, sr, self.sample_rate)
            sr = self.sample_rate

        # Normalize
        if waveform.dtype == np.int16:
            waveform = waveform.astype(np.float32) / 32768.0
        elif waveform.dtype == np.int32:
            waveform = waveform.astype(np.float32) / 2147483648.0

        # Mono
        if waveform.ndim > 1:
            waveform = waveform.mean(axis=1)

        # Extract features
        inputs = self._processor(
            waveform, sampling_rate=self.sample_rate, return_tensors="pt",
            padding=True,
        )
        input_values = inputs.input_values.to(self.device)

        with torch.no_grad():
            outputs = self._model(input_values)
            # Use last hidden state: (1, num_frames, embed_dim)
            hidden = outputs.last_hidden_state.squeeze(0)

        duration_ms = len(waveform) / self.sample_rate * 1000

        return AudioEmbedding(
            embedding=hidden,
            sample_rate=self.sample_rate,
            duration_ms=duration_ms,
        )

    def encode_chunk(self, audio_array: np.ndarray, sr: int = 16000) -> AudioEmbedding:
        """Encode a raw audio chunk (for streaming).

        Parameters
        ----------
        audio_array : np.ndarray
            Raw audio waveform (float32, mono).
        sr : int
            Sample rate.

        Returns
        -------
        AudioEmbedding
        """
        self._load_model()

        if self._model is None:
            num_frames = max(1, len(audio_array) // 320)  # ~20ms per frame
            embed = torch.randn(num_frames, self._embed_dim) * 0.1
            duration_ms = len(audio_array) / sr * 1000
            return AudioEmbedding(
                embedding=embed, sample_rate=sr, duration_ms=duration_ms
            )

        inputs = self._processor(
            audio_array, sampling_rate=sr, return_tensors="pt", padding=True,
        )
        input_values = inputs.input_values.to(self.device)

        with torch.no_grad():
            hidden = self._model(input_values).last_hidden_state.squeeze(0)

        duration_ms = len(audio_array) / sr * 1000
        return AudioEmbedding(
            embedding=hidden, sample_rate=sr, duration_ms=duration_ms
        )

    def _mock_encode(self, audio_path: Path) -> AudioEmbedding:
        """Generate random embeddings when Wav2Vec2 is unavailable."""
        # Estimate duration from file size
        file_size = audio_path.stat().st_size
        duration_ms = (file_size - 44) / (self.sample_rate * 2) * 1000
        num_frames = max(1, int(duration_ms / 20))  # ~20ms per frame

        embed = torch.randn(num_frames, self._embed_dim) * 0.1

        return AudioEmbedding(
            embedding=embed,
            sample_rate=self.sample_rate,
            duration_ms=duration_ms,
        )

    @staticmethod
    def _resample(waveform: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
        """Simple resampling via scipy."""
        try:
            from scipy.signal import resample

            num_samples = int(len(waveform) * target_sr / orig_sr)
            return resample(waveform, num_samples).astype(np.float32)
        except ImportError:
            # Fallback: linear interpolation
            indices = np.linspace(0, len(waveform) - 1, int(len(waveform) * target_sr / orig_sr))
            return np.interp(indices, np.arange(len(waveform)), waveform).astype(np.float32)
