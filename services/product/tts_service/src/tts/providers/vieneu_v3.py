"""VieNeu v3 Turbo provider — single synthesis path (Change T tasks 6.1-6.8).

Provider-owned lifecycle: the provider builds the SDK model in ``__init__``.
The ``vieneu`` import is deferred so the module (and anything importing it)
loads safely on installs without the SDK; construction failure raises
``ProviderUnavailableError`` instead of crashing the app.

All SDK access lives in this module (task 7.1 isolation); the scheduler and
API layers only ever see the provider-neutral ``TTSProvider`` surface.
The low-level ``V3TurboBatchEngine`` stays unused here — cluster 5 wires
``synthesize_batch`` around ``_get_batch_engine()``.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

import numpy as np

from tts.config import RuntimeConfig
from tts.providers.capabilities import ProviderCapabilities
from tts.providers.errors import (
    CapabilityError,
    ProfileNotFoundError,
    ProviderInferenceError,
    ProviderUnavailableError,
)
from tts.providers.models import AudioResult, ProviderRequest, ProviderResult

logger = logging.getLogger("tts.providers.vieneu_v3")

SAMPLE_RATE_HZ = 48_000

# Provider-neutral style ids (canonical). VieNeu style ids are exposed
# aliased: English and Vietnamese both resolve to the same SDK style.
CANONICAL_STYLES: tuple[str, ...] = ("natural", "news", "storytelling")
_STYLE_ALIASES: dict[str, str] = {
    "natural": "natural",
    "news": "news",
    "tin_tuc": "news",
    "storytelling": "storytelling",
    "doc_truyen": "storytelling",
}
_VIENEU_STYLES: dict[str, str] = {
    "natural": "tu_nhien",
    "news": "tin_tuc",
    "tin_tuc": "tin_tuc",
    "storytelling": "doc_truyen",
    "doc_truyen": "doc_truyen",
}

SUPPORTED_CUES: tuple[str, ...] = ("[cười]", "[thở dài]", "[hắng giọng]")

# Baseline generation params passed through to the SDK single-inference call.
_TEMPERATURE = 0.8
_TOP_K = 25
_TOP_P = 0.95
_MAX_NEW_FRAMES = 300
_REPETITION_PENALTY = 1.2

ProfileLoader = Callable[[str, str], tuple[object, dict]]


def _accelerator_device(accelerator: str) -> str:
    return {"gpu": "cuda", "cpu": "cpu", "auto": "auto"}[accelerator]


class VieNeuV3TurboProvider:
    """TTSProvider over the pinned VieNeu v3 Turbo SDK."""

    provider_name = "vieneu_v3"

    def __init__(
        self,
        config: RuntimeConfig,
        profile_loader: Optional[ProfileLoader] = None,
    ) -> None:
        if config.provider != self.provider_name:
            raise ProviderUnavailableError(
                f"config provider {config.provider!r} does not match {self.provider_name!r}"
            )
        self._config = config
        self._profile_loader = profile_loader
        self._tts = self._build_tts(config)
        self._backend = str(self._tts.backend)
        if self._backend not in ("pytorch", "onnx"):
            raise ProviderUnavailableError(
                f"unexpected VieNeu backend {self._backend!r}; expected 'pytorch' or 'onnx'"
            )
        self._capabilities = self._build_capabilities(config, self._backend)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def _build_tts(self, config: RuntimeConfig):
        device = _accelerator_device(config.accelerator)
        try:
            from vieneu import Vieneu

            return Vieneu(mode="v3turbo", backbone_repo=config.model_revision, device=device)
        except ProviderUnavailableError:
            raise
        except Exception as exc:
            if config.accelerator == "gpu":
                raise ProviderUnavailableError(
                    f"forced GPU path failed: VieNeu could not load on CUDA "
                    f"(model {config.model_revision!r}); check the CUDA runtime and "
                    f"PyTorch build, or set TTS_ACCELERATOR=cpu/auto"
                ) from exc
            raise ProviderUnavailableError(
                f"VieNeu v3 Turbo initialization failed (model {config.model_revision!r}): {exc}"
            ) from exc

    @staticmethod
    def _build_capabilities(config: RuntimeConfig, backend: str) -> ProviderCapabilities:
        is_pytorch = backend == "pytorch"
        return ProviderCapabilities(
            provider_name="vieneu_v3",
            model_revision=config.model_revision,
            sample_rate_hz=SAMPLE_RATE_HZ,
            supports_native_batch=is_pytorch,
            max_batch_size=32 if is_pytorch else 1,
            supports_voice_cloning=True,
            supports_mixed_voice_batch=is_pytorch,
            supported_styles=CANONICAL_STYLES,
            supported_expressive_cues=SUPPORTED_CUES,
            supported_response_formats=("pcm", "wav"),
        )

    def close(self) -> None:
        self._tts = None

    # ── TTSProvider surface ──────────────────────────────────────────────────
    @property
    def backend(self) -> str:
        """Actual SDK backend selected at init ("pytorch" or "onnx")."""
        return self._backend

    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def batch_key(self, request: ProviderRequest) -> object:
        # Batched generation params (cluster 5); single path always computes
        # one compatible key so the scheduler may coalesce.
        cfg = request.generation_config
        return (self.provider_name, cfg.temperature, cfg.seed)

    def synthesize(self, request: ProviderRequest) -> AudioResult:
        """Synthesize one request; cluster 5 adds the batched path."""
        self._validate_style(request.style)
        self._validate_cues(request.input_text)
        payload = self._resolve_profile(request)
        try:
            wav = self._tts.infer(
                request.input_text,
                voice=payload,
                style=_VIENEU_STYLES[request.style],
                temperature=_TEMPERATURE,
                top_k=_TOP_K,
                top_p=_TOP_P,
                max_new_frames=_MAX_NEW_FRAMES,
                repetition_penalty=_REPETITION_PENALTY,
            )
        except Exception as exc:
            raise ProviderInferenceError(
                f"VieNeu inference failed for request {request.request_id!r}"
            ) from exc
        return self._to_result(request, np.asarray(wav, dtype=np.float32).reshape(-1))

    def synthesize_batch(self, requests: list[ProviderRequest]) -> list[ProviderResult]:
        # Cluster 5 replaces this with the V3TurboBatchEngine path.
        return [self.synthesize(r) for r in requests]

    def enroll_voice(self, reference_audio: bytes, options: dict) -> object:
        raise ProviderUnavailableError("VieNeu provider enrollment is not wired yet (cluster 5)")

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _validate_style(style: str) -> None:
        if style not in _STYLE_ALIASES:
            raise CapabilityError(
                f"unsupported style {style!r}; supported: {', '.join(CANONICAL_STYLES)}"
            )

    @staticmethod
    def _validate_cues(text: str) -> None:
        for cue in SUPPORTED_CUES:
            if cue in text:
                return
        if any(marker in text for marker in ("[", "]")):
            raise CapabilityError(
                f"unsupported expressive cue in text; supported: {', '.join(SUPPORTED_CUES)}"
            )

    def _resolve_profile(self, request: ProviderRequest) -> dict:
        voice_profile_id = request.voice_profile_id
        if voice_profile_id == "default" or not voice_profile_id:
            preset = self._tts.get_preset_voice()
            return preset
        if self._profile_loader is None:
            raise ProfileNotFoundError(
                f"voice profile {voice_profile_id!r} not found (profile service not wired)"
            )
        profile, payload = self._profile_loader(voice_profile_id, request.session_id)
        if profile.provider_payload_location.startswith("preset://"):
            name = profile.provider_payload_location[len("preset://") :]
            try:
                return self._tts.get_preset_voice(name)
            except Exception as exc:
                raise ProfileNotFoundError(
                    f"preset voice {name!r} unavailable in the pinned SDK"
                ) from exc
        if profile.profile_kind != "cloned":
            raise ProfileNotFoundError(
                f"voice profile {voice_profile_id!r} has no resolvable payload"
            )
        from tts.voices.payloads import decode_vieneu_payload

        try:
            decoded = decode_vieneu_payload(payload)
        except Exception as exc:
            raise ProviderInferenceError(
                f"voice profile {voice_profile_id!r} payload failed validation"
            ) from exc
        speaker_emb = np.asarray(decoded["speaker_emb"], dtype=np.float32)
        ref_codes = decoded.get("ref_codes")
        voice_payload = {"speaker_emb": speaker_emb}
        if ref_codes is not None:
            voice_payload["codes"] = np.asarray(ref_codes, dtype=np.int64)
        return voice_payload

    def _to_result(self, request: ProviderRequest, wav: np.ndarray) -> AudioResult:
        duration_ms = int(len(wav) / SAMPLE_RATE_HZ * 1000) if len(wav) else 0
        return AudioResult(
            request_id=request.request_id,
            sample_rate=SAMPLE_RATE_HZ,
            waveform=wav,
            response_format=request.response_format,
            duration_ms=duration_ms,
        )
