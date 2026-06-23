"""TTS adapters — each wraps ONE model's official code behind TTSEngine.

Importing this package registers every adapter whose deps import cleanly.
Adapters DEFER heavy imports to from_config(), so this module is safe to import
even when a model's package isn't installed (e.g. on a CPU dev box).

Add a model = add a `<name>.py` with a @register_engine("<name>") class +
import it here. The say-loop / Director never change.

Engines (commercial-OK unless noted):
  vieneu    pnnbao-ump/VieNeu-TTS-v2|v3-Turbo   Apache-2.0  VN-native  [DEFAULT]
  kokoro    hexgrad/Kokoro-82M                  Apache-2.0  (no native VN voice)
  cosyvoice FunAudioLLM/CosyVoice2-0.5B         Apache-2.0  streaming
  xtts      coqui/XTTS-v2                        CPML (NON-commercial; research)
"""

from __future__ import annotations

# Each import is guarded: a missing optional dep must not break the registry.
for _mod in ("vieneu", "kokoro", "cosyvoice", "xtts"):
    try:
        __import__(f"{__name__}.{_mod}", fromlist=["*"])
    except Exception:
        # adapter file present but its heavy deps absent -> skip registration;
        # from_config() will raise a clear error if actually selected.
        pass
