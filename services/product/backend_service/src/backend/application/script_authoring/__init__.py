"""Pre-live script authoring pipeline (OpenSpec approved-script-authoring-pipeline).

Gate-first, AI-optional authoring: deterministic ScriptGate validation,
optional bounded AI generation/repair, immutable versioning, human-only
approval, and runtime binding to the canonical Change A
``backend.application.text_chunker`` speech path.

This package MUST NOT import ``backend.application.speech_chunking`` or
``render.windows.TextChunk`` (see ``change_a_contract``).
"""
