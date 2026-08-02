from __future__ import annotations

import logging
import shutil
import tempfile
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from avatar.observability.logging.active_session_handler import ActiveSessionHandler


@contextmanager
def removable_temp_dir() -> Generator[Path, None, None]:
    path = Path(tempfile.mkdtemp())
    try:
        yield path
    finally:
        shutil.rmtree(path)


def make_logger() -> tuple[logging.Logger, ActiveSessionHandler, Path]:
    active_root = Path(tempfile.mkdtemp())
    handler = ActiveSessionHandler(service="backend", active_root=active_root)
    logger = logging.getLogger("test.active_session.avatar")
    logger.handlers, logger.propagate = [handler], False
    logger.setLevel(logging.INFO)
    return logger, handler, active_root


def test_first_start_truncates_empty_path() -> None:
    logger, handler, active_root = make_logger()
    try:
        handler.start_session()
        logger.info("first")
        assert (
            active_root.joinpath("product", "backend.log").read_text(encoding="utf-8") == "first\n"
        )
    finally:
        logger.handlers.clear()
        handler.close()
        shutil.rmtree(active_root)


def test_new_session_overwrites_previous() -> None:
    logger, handler, active_root = make_logger()
    try:
        handler.start_session()
        logger.info("session-a")
        handler.end_session()
        handler.start_session()
        logger.info("session-b")
        assert (
            active_root.joinpath("product", "backend.log").read_text(encoding="utf-8")
            == "session-b\n"
        )
    finally:
        logger.handlers.clear()
        handler.close()
        shutil.rmtree(active_root)


def test_emits_append_within_session() -> None:
    logger, handler, active_root = make_logger()
    try:
        handler.start_session()
        logger.info("one")
        logger.info("two")
        logger.info("three")
        assert (
            active_root.joinpath("product", "backend.log").read_text(encoding="utf-8")
            == "one\ntwo\nthree\n"
        )
    finally:
        logger.handlers.clear()
        handler.close()
        shutil.rmtree(active_root)


def test_file_retained_after_end_session() -> None:
    logger, handler, active_root = make_logger()
    path = active_root.joinpath("product", "backend.log")
    try:
        handler.start_session()
        logger.info("kept")
        handler.end_session()
        assert path.exists()
        assert path.read_text(encoding="utf-8") == "kept\n"
    finally:
        logger.handlers.clear()
        handler.close()
        shutil.rmtree(active_root)


def test_no_session_id_in_filename_or_content() -> None:
    logger, handler, active_root = make_logger()
    session_id = "hostile/../../evil"
    try:
        handler.start_session()
        logger.info("session=%s", session_id)
        for path in active_root.rglob("*"):
            assert session_id not in str(path)
        assert active_root.joinpath("product", "backend.log").exists()
    finally:
        logger.handlers.clear()
        handler.close()
        shutil.rmtree(active_root)


def test_platform_group_and_platform_services() -> None:
    with removable_temp_dir() as active_root:
        for service in ("livekit", "lmcache", "postgres", "redis"):
            handler = ActiveSessionHandler(
                service=service, group="platform", active_root=active_root
            )
            handler.start_session()
            handler.emit(make_record("ready"))
            handler.end_session()
            handler.close()
            assert (
                active_root.joinpath("platform", f"{service}.log").read_text(encoding="utf-8")
                == "ready\n"
            )


def test_product_services_are_rejected_for_platform_group() -> None:
    with removable_temp_dir() as active_root:
        with pytest.raises(ValueError, match="Unknown platform service"):
            ActiveSessionHandler(service="backend", group="platform", active_root=active_root)


def test_unknown_service_is_rejected() -> None:
    with removable_temp_dir() as active_root:
        with pytest.raises(ValueError, match="Unknown product service"):
            ActiveSessionHandler(service="../../etc/passwd", active_root=active_root)


def test_unknown_group_is_rejected() -> None:
    with removable_temp_dir() as active_root:
        with pytest.raises(ValueError, match="Unknown active-session group"):
            ActiveSessionHandler(service="backend", group="admin", active_root=active_root)


def test_close_and_setup_are_idempotent() -> None:
    logger, handler, active_root = make_logger()
    path = active_root.joinpath("product", "backend.log")
    try:
        handler.start_session()
        logger.info("before-close")
        handler.close()
        handler.close()
        handler.start_session()
        handler.start_session()
        logger.info("after-close")
        handler.end_session()
        handler.end_session()
        assert path.read_text(encoding="utf-8") == "after-close\n"
    finally:
        logger.handlers.clear()
        handler.close()
        shutil.rmtree(active_root)


def test_concurrent_writes_do_not_interleave_lines() -> None:
    logger, handler, active_root = make_logger()
    line = "x" * 100
    try:
        handler.start_session()
        threads = [
            threading.Thread(target=lambda: [logger.info(line) for _ in range(20)])
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        content = active_root.joinpath("product", "backend.log").read_text(encoding="utf-8")
        assert len(content.splitlines()) == 160
        assert all(entry == line for entry in content.splitlines())
    finally:
        logger.handlers.clear()
        handler.close()
        shutil.rmtree(active_root)


def make_record(message: str) -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, message, (), None)
