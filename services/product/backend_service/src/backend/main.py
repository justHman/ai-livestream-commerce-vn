"""``backend.main`` — production entrypoint for the backend service.

Usage:
    uvicorn backend.main:app --port 8800
    # or: python -m backend.main
"""

from .bootstrap import create_app

app = create_app()


def main() -> None:
    import uvicorn

    from backend.config import AppConfig

    cfg = AppConfig.from_env()
    uvicorn.run(app, host="0.0.0.0", port=cfg.port)


if __name__ == "__main__":
    main()

__all__ = ["app", "create_app"]
