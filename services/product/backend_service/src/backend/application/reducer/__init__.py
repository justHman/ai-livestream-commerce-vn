"""Event-driven fast reducer for the persistent live-demand pipeline (OpenSpec 4).

Public surface of the reducer package: the ``FastReducer`` (one instance per
app, session-scoped state), its typed ``FastReducerConfig`` knobs, and the
``AcceptedComment`` item the ingestion pipeline hands it.
"""

from .fast_reducer import AcceptedComment, FastReducer, FastReducerConfig

__all__ = ["AcceptedComment", "FastReducer", "FastReducerConfig"]
