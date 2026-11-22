"""Call-time opt-in for relaxed script gate rules."""

import os


def relaxed_gate_enabled() -> bool:
    return os.environ.get("SCRIPT_GATE_RELAXED_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
