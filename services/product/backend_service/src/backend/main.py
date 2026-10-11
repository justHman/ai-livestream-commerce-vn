"""``backend.main`` — production entrypoint for the backend service.

Usage:
    uvicorn backend.main:app --port 8800
    # or: python -m backend.main
"""

import logging
import os

from .bootstrap import create_app

_level = os.getenv("LOG_LEVEL", "INFO").upper()
# Without this the root logger only shows WARNING+: decisions, speech timing and rollovers were
# invisible in production. Chatty client libraries stay at WARNING.
logging.basicConfig(
    level=_level if _level in logging.getLevelNamesMapping() else "INFO",
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
for _noisy in ("httpx", "httpcore", "livekit", "uvicorn.access", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

app = create_app()


def main() -> None:
    import uvicorn

    from backend.config import AppConfig

    cfg = AppConfig.from_env()
    uvicorn.run(app, host="0.0.0.0", port=cfg.port)


if __name__ == "__main__":
    main()

__all__ = ["app", "create_app"]
