"""VieNeu v3 Turbo provider — single and mixed-voice batched synthesis.

Provider-owned lifecycle: the provider builds the SDK model in ``__init__``.
The ``vieneu`` import is deferred so the module (and anything importing it)
loads safely on installs without the SDK; construction failure raises
``ProviderUnavailableError`` instead of crashing the app.

All SDK access lives in this module (task 7.1 isolation); the scheduler and
API layers only ever see the provider-neutral ``TTSProvider`` surface. The
low-level ``V3TurboBatchEngine`` is reached through ``tts._get_batch_engine()``
on the PyTorch backend; the ONNX/CPU backend has no batch engine and falls
back to sequential single-path synthesis.
"""

from __future__ import annotations

import inspect
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

# Baseline generation params passed through to the SDK single-inference call
# and the batch engine's batch-wide scalar knobs (task 7.3 batch_key).
_TEMPERATURE = 0.8
_TOP_K = 25
_TOP_P = 0.95
_MAX_NEW_FRAMES = 300
_REPETITION_PENALTY = 1.2
_BATCH_SCALAR_PARAMS = (
    "temperature",
    "top_k",
    "top_p",
    "repetition_penalty",
    "max_new_frames",
)

ProfileLoader = Callable[[str, str], tuple[object, dict]]


def _accelerator_device(accelerator: str) -> str:
    return {"gpu": "cuda", "cpu": "cpu", "auto": "auto"}[accelerator]


def _phonemize(text: str) -> str:
    """Phonemize with the SDK's emotion-preserving phonemizer.

    The import is lazy so modules importing this one load on installs without
    ``vieneu_utils`` (it ships with the pinned vieneu wheel).
    """
    from vieneu_utils.phonemize_text import phonemize_text_with_emotions

    return phonemize_text_with_emotions(text)


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
        self._batch_engine = None
        if self._backend == "pytorch":
            self._verify_batch_contract()
            self._batch_engine = self._tts._get_batch_engine()
            if self._batch_engine is None:
                raise ProviderUnavailableError(
                    "VieNeu pytorch backend reported no batch engine; batched "
                    "synthesis is unavailable"
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
        self._batch_engine = None
        self._tts = None

    # ── TTSProvider surface ──────────────────────────────────────────────────
    @property
    def backend(self) -> str:
        """Actual SDK backend selected at init ("pytorch" or "onnx")."""
        return self._backend

    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def batch_key(self, request: ProviderRequest) -> object:
        """Compatibility key: batch-wide engine scalars + model revision.

        The engine only consumes the batch-wide sampling params as scalars
        (task 7.3); ``voice_profile_id``/``style`` are per-row and intentionally
        absent, and ``speed``/``seed``/``response_format`` never reach
        ``generate_batch`` so they stay out of the key.
        """
        cfg = request.generation_config
        return (
            self.provider_name,
            self._config.model_revision,
            cfg.temperature,
            _TOP_K,
            _TOP_P,
            _REPETITION_PENALTY,
            _MAX_NEW_FRAMES,
        )

    def synthesize(self, request: ProviderRequest) -> AudioResult:
        """Synthesize one request via the SDK single-inference path."""
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
        """Mixed-voice static batch via the SDK batch engine; order preserved.

        The scheduler coalesces on ``batch_key``, but the provider re-checks so
        a misrouted request can never ride the wrong batch. The ONNX/CPU
        backend has no batch engine: it falls back to sequential single-path
        synthesis (each request resolves its own profile/style).
        """
        if not requests:
            return []
        if self._backend != "pytorch":
            return [self.synthesize(request) for request in requests]
        first_key = self.batch_key(requests[0])
        for request in requests[1:]:
            if self.batch_key(request) != first_key:
                raise ProviderInferenceError(
                    "mixed batch_key requests in one batch; scheduler must "
                    "coalesce on batch_key before dispatch"
                )
        engine_dicts = [self._build_engine_request(request) for request in requests]
        params = self._batch_scalar_params(requests[0])
        try:
            waveforms = self._batch_engine.generate_batch(engine_dicts, **params)
        except Exception as exc:
            raise ProviderInferenceError(
                f"VieNeu batch inference failed for {len(requests)} requests "
                f"(first {requests[0].request_id!r})"
            ) from exc
        if len(waveforms) != len(requests):
            raise ProviderInferenceError(
                f"VieNeu batch engine returned {len(waveforms)} waveforms for "
                f"{len(requests)} requests; refusing to misalign results"
            )
        results = []
        for request, waveform in zip(requests, waveforms):
            wav = np.asarray(waveform, dtype=np.float32).reshape(-1)
            if wav.ndim != 1:
                raise ProviderInferenceError(
                    f"VieNeu batch engine returned non-1D waveform for "
                    f"request {request.request_id!r}"
                )
            results.append(self._to_result(request, wav))
        return results

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

    def _verify_batch_contract(self) -> None:
        """Startup check (task 7.2): pinned engine must satisfy the batch surface.

        Failures raise ``ProviderUnavailableError`` — readiness fails loud
        instead of degrading to a silently single-path runtime on a PyTorch
        box. Accessing the internal engine through ``tts._get_batch_engine()``
        keeps ``vieneu.v3_turbo_serve`` imports inside this module (task 7.1).
        """
        try:
            engine = self._tts._get_batch_engine()
        except Exception as exc:
            raise ProviderUnavailableError(
                f"VieNeu batch engine failed to initialize: {exc}"
            ) from exc
        if engine is None:
            raise ProviderUnavailableError(
                "VieNeu pytorch backend has no batch engine; expected "
                "V3TurboBatchEngine for batched synthesis"
            )
        signature = inspect.signature(engine.generate_batch)
        parameters = signature.parameters
        if "requests" not in parameters:
            raise ProviderUnavailableError(
                "VieNeu batch engine generate_batch lacks a 'requests' parameter"
            )
        missing = [name for name in _BATCH_SCALAR_PARAMS if name not in parameters]
        if missing:
            raise ProviderUnavailableError(
                "VieNeu batch engine generate_batch lacks batch-wide scalar "
                f"params: {', '.join(missing)}"
            )
        if not hasattr(self._tts.engine, "_resolve_style_id"):
            raise ProviderUnavailableError(
                "VieNeu engine lacks _resolve_style_id; per-row style resolution is unavailable"
            )

    @staticmethod
    def _batch_scalar_params(request: ProviderRequest) -> dict:
        """Batch-wide engine scalars, derived from the request generation config."""
        return {
            "temperature": request.generation_config.temperature,
            "top_k": _TOP_K,
            "top_p": _TOP_P,
            "repetition_penalty": _REPETITION_PENALTY,
            "max_new_frames": _MAX_NEW_FRAMES,
        }

    def _build_engine_request(self, request: ProviderRequest) -> dict:
        """One engine batch row: phonemes, voice anchor, style, ref-code flag.

        Phonemes are pre-computed with the SDK's emotion-preserving phonemizer
        so cue handling stays consistent with the single path; each row keeps
        its own style/profile — nothing can leak across rows (tasks 7.4/7.10).
        """
        payload = self._resolve_profile(request)
        speaker_emb = payload["speaker_emb"]
        ref_codes = payload.get("codes")
        return {
            "phonemes": _phonemize(request.input_text),
            "speaker_emb": speaker_emb,
            "ref_codes": ref_codes,
            "style": _VIENEU_STYLES[request.style],
            "use_ref_codes": True,
        }

    def _to_result(self, request: ProviderRequest, wav: np.ndarray) -> AudioResult:
        duration_ms = int(len(wav) / SAMPLE_RATE_HZ * 1000) if len(wav) else 0
        return AudioResult(
            request_id=request.request_id,
            sample_rate=SAMPLE_RATE_HZ,
            waveform=wav,
            response_format=request.response_format,
            duration_ms=duration_ms,
        )
