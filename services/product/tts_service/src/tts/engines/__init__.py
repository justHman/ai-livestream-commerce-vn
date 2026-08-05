"""Self-host TTS engines (vieneu | cosyvoice | transformers).

transformers remains for the legacy offline fallback path; the
``ENGINES`` registry and the parity contract require it.
"""

from . import cosyvoice, transformers, vieneu

__all__ = ["cosyvoice", "transformers", "vieneu"]
