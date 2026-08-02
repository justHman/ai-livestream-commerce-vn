"""Self-host TTS engines."""

for _module in ("transformers", "vieneu", "cosyvoice"):
    try:
        __import__(f"{__name__}.{_module}", fromlist=["*"])
    except Exception:
        pass

__all__ = ["transformers", "vieneu", "cosyvoice"]
