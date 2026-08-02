"""TTS adapters — unified inference behind the TTSEngine seam.

Importing this package registers every adapter whose deps import cleanly.
Adapters DEFER heavy imports to from_config(), so this module is safe to import
even when a model's package isn't installed (e.g. on a CPU dev box).

Engine selection (Option A: transformers-primary + 1-2 native fallback):
  transformers  PRIMARY — HF AutoModelForTextToWaveform (MMS-VN, Bark, SpeechT5...)
                        swap model = change id string, same API
  vieneu        NATIVE FALLBACK — VieNeu NeuTTS (VN-native, RQ3 prosody)
  cosyvoice     NATIVE FALLBACK — CosyVoice2 (true streaming, zero-shot clone)
  tone          BUILT-IN — offline sine-tone (tests / CI, no deps)

Removed (covered by transformers primary now):
  kokoro  → use transformers with a Kokoro HF checkpoint instead
  xtts    → CPML non-commercial; use transformers SpeechT5/MMS for voice clone
"""

from __future__ import annotations

# Each import is guarded: a missing optional dep must not break the registry.
# remote_http + elevenlabs only need httpx (already a core dep) — still guarded.
for _mod in ("transformers", "vieneu", "cosyvoice", "remote_http", "elevenlabs", "openai_speech"):
    try:
        __import__(f"{__name__}.{_mod}", fromlist=["*"])
    except Exception:
        # adapter file present but its heavy deps absent -> skip registration;
        # from_config() will raise a clear error if actually selected.
        pass
