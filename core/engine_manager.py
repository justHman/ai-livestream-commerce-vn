"""EngineManager — runtime LLM/TTS engine registry with swap + unload.

Holds the currently-loaded LLMEngine and TTSEngine singletons. When the user
swaps a model from the UI, this manager:
  1. Loads the NEW engine (from a cfg dict).
  2. Unloads the OLD engine (frees VRAM via engine.unload()).
  3. Re-configures the cloud RenderBackend with the new engine.

This is what makes "swap model = change dropdown, not restart the server" work.
Without it, swapping requires a full process restart (unacceptable for a live
stream — you'd lose the session + the LiveKit connection).

Thread-safety: swap operations are serialized via a lock (loading + unloading
concurrently would corrupt VRAM). Generation calls during a swap block briefly
on the lock — the swap takes ~10-30s (model load), so a viewer message arriving
mid-swap waits, then gets the new model. Acceptable for a demo; production uses
a warm-standby pool (load new BEFORE unloading old) — future work.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from .llm import LLMEngine, load_engine as load_llm_engine, to_llm_fn
from .tts import TTSEngine, load_engine as load_tts_engine, to_tts_fn


@dataclass
class EngineInfo:
    """Serializable info about a loaded (or available) engine — for the UI."""

    engine: str            # "vllm" | "llamacpp" | "transformers" | "vieneu" | ...
    model: str             # model id or path
    name: str              # engine.name (runtime)
    loaded: bool           # is it currently in VRAM?
    sample_rate: Optional[int] = None  # TTS only


AVAILABLE_LLM_PRESETS = [
    {"engine": "llamacpp", "label": "Gemma 3 4B (GGUF Q4_K_M)", "model": "google/gemma-3-4b-it-GGUF",
     "gguf_file": "gemma-3-4b-it-Q4_K_M.gguf", "device": "cuda", "n_gpu_layers": -1},
    {"engine": "llamacpp", "label": "Qwen3 4B (GGUF Q4_K_M)", "model": "Qwen/Qwen3-4B-GGUF",
     "gguf_file": "Qwen3-4B-Q4_K_M.gguf", "device": "cuda", "n_gpu_layers": -1},
    {"engine": "llamacpp", "label": "Qwen3.5 4B (GGUF Q4_K_M)",
     "model": "unsloth/Qwen3.5-4B-GGUF",
     "gguf_file": "Qwen3.5-4B-Q4_K_M.gguf", "device": "cuda", "n_gpu_layers": -1},
    {"engine": "vllm", "label": "Qwen3 4B Instruct (vLLM)", "model": "Qwen/Qwen3-4B-Instruct",
     "device": "cuda", "enable_prefix_caching": True},
    {"engine": "vllm", "label": "SeaLLMs v3 7B (vLLM)", "model": "SeaLLMs/SeaLLMs-v3-7B-Chat",
     "device": "cuda", "enable_prefix_caching": True},
    {"engine": "hf", "label": "Qwen3 4B (transformers fallback)", "model": "Qwen/Qwen3-4B-Instruct",
     "device": "auto"},
    {"engine": "none", "label": "Echo stub (no model, offline)", "model": ""},
]

AVAILABLE_TTS_PRESETS = [
    {"engine": "vieneu", "label": "VieNeu-TTS v2 (VN-native, Apache)", "model": "pnnbao-ump/VieNeu-TTS-v2",
     "device": "cuda", "sample_rate": 24000},
    {"engine": "vieneu", "label": "VieNeu-TTS v3 Turbo (VN-native, Apache)", "model": "pnnbao-ump/VieNeu-TTS-v3-Turbo",
     "device": "cuda", "sample_rate": 24000},
    {"engine": "transformers", "label": "MMS-TTS Vietnamese (HF, Apache)", "model": "facebook/mms-tts-vie",
     "device": "cuda", "sample_rate": 24000},
    {"engine": "cosyvoice", "label": "CosyVoice2 0.5B (streaming, Apache)", "model": "FunAudioLLM/CosyVoice2-0.5B",
     "device": "cuda", "sample_rate": 24000},
    {"engine": "tone", "label": "Tone stub (offline, no model)", "model": "", "sample_rate": 24000},
]


class EngineManager:
    """Singleton managing loaded LLM + TTS engines with runtime swap.

    The server creates ONE instance and injects it into the API layer.
    The API layer calls swap_llm(cfg) / swap_tts(cfg) when the UI requests a change.
    """

    def __init__(self) -> None:
        self._llm: Optional[LLMEngine] = None
        self._tts: Optional[TTSEngine] = None
        self._llm_cfg: dict = {}
        self._tts_cfg: dict = {}
        self._system_prompt: str = ""
        self._lock = threading.Lock()

    # ── LLM ─────────────────────────────────────────────────────────────

    @property
    def llm(self) -> Optional[LLMEngine]:
        return self._llm

    @property
    def llm_cfg(self) -> dict:
        return self._llm_cfg

    def set_system_prompt(self, prompt: str) -> None:
        self._system_prompt = prompt

    def load_llm(self, cfg: dict) -> EngineInfo:
        """Load an LLM engine from cfg. If one is already loaded, unload it first.
        After loading, runs warmup() to JIT CUDA kernels + populate prefix cache."""
        with self._lock:
            # Unload old engine (free VRAM) before loading the new one.
            if self._llm is not None:
                self._llm.unload()
                self._llm = None
            # Load new engine (this is the slow part: 10-30s for a 4B model).
            self._llm = load_llm_engine(cfg)
            self._llm_cfg = cfg
            # Warmup: JIT CUDA kernels + populate prefix cache with persona.
            self._llm.warmup(system_prompt=self._system_prompt or None)
            return EngineInfo(
                engine=cfg.get("engine", "?"),
                model=cfg.get("model", cfg.get("model_path", cfg.get("weights_path", ""))),
                name=self._llm.name,
                loaded=True,
            )

    def get_llm_fn(self):
        """Return the (text)->str callable for the cloud RenderBackend."""
        if self._llm is None:
            return None
        return to_llm_fn(self._llm, system_prompt=self._system_prompt)

    # ── TTS ─────────────────────────────────────────────────────────────

    @property
    def tts(self) -> Optional[TTSEngine]:
        return self._tts

    @property
    def tts_cfg(self) -> dict:
        return self._tts_cfg

    def load_tts(self, cfg: dict) -> EngineInfo:
        """Load a TTS engine from cfg. If one is already loaded, unload it first.
        After loading, runs warmup() to JIT CUDA kernels."""
        with self._lock:
            if self._tts is not None:
                self._tts.unload()
                self._tts = None
            self._tts = load_tts_engine(cfg)
            self._tts_cfg = cfg
            # Warmup: JIT CUDA kernels + allocate GPU buffers.
            self._tts.warmup()
            return EngineInfo(
                engine=cfg.get("engine", "?"),
                model=cfg.get("model", cfg.get("weights_path", "")),
                name=self._tts.name,
                loaded=True,
                sample_rate=self._tts.sample_rate,
            )

    def get_tts_fn(self):
        """Return the (text)->(bytes,rate) callable for the cloud RenderBackend."""
        if self._tts is None:
            return None
        voice = self._tts_cfg.get("ref_audio")
        return to_tts_fn(self._tts, voice=voice)

    # ── Cloud re-configure ──────────────────────────────────────────────

    def reconfigure_cloud(self) -> None:
        """Push the current LLM + TTS engines into the cloud RenderBackend.
        Call this after a swap so the say-loop uses the new engines."""
        from .render import cloud

        cloud.configure(llm=self.get_llm_fn(), tts=self.get_tts_fn())

    # ── Status (for the UI) ─────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "llm": {
                "engine": self._llm_cfg.get("engine", "none"),
                "model": self._llm_cfg.get("model", self._llm_cfg.get("model_path", "")),
                "name": self._llm.name if self._llm else "none(stub)",
                "loaded": self._llm is not None,
            },
            "tts": {
                "engine": self._tts_cfg.get("engine", "tone"),
                "model": self._tts_cfg.get("model", self._tts_cfg.get("weights_path", "")),
                "name": self._tts.name if self._tts else "tone(stub)",
                "loaded": self._tts is not None,
                "sample_rate": self._tts.sample_rate if self._tts else 24000,
            },
            "available_llm_presets": AVAILABLE_LLM_PRESETS,
            "available_tts_presets": AVAILABLE_TTS_PRESETS,
        }
