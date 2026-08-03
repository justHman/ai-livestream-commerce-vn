"""Task 1.8-1.10 tests: daily UTC rotation, retention, logfmt alignment, TTY/file color split."""

from __future__ import annotations

import io
import logging
import shutil
import tempfile
import re
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest

from tts.observability import scoped
from tts.observability.logging.config import validate_config
from tts.observability.logging.daily_handler import DailyHandler
from tts.observability.logging.formatter import ContextFormatter
from tts.observability.logging.platform_collector import PlatformCollector
from tts.observability.logging.setup import reset_logging, setup_logging

SERVICE = "tts"
GROUP = "product"
FIXED_DAY = date(2026, 8, 3)

DEFAULT_LEVELS = (
    (logging.DEBUG, "DEBUG"),
    (logging.INFO, "INFO"),
    (logging.WARNING, "WARNING"),
    (logging.ERROR, "ERROR"),
)

TS_PATTERN = re.compile(r"^\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


@contextmanager
def removable_temp_dir() -> Generator[Path, None, None]:
    path = Path(tempfile.mkdtemp())
    try:
        yield path
    finally:
        shutil.rmtree(path)


def fixed_clock(day: date = FIXED_DAY) -> Callable[[], datetime]:
    return lambda: datetime.combine(day, time(12, 0), tzinfo=timezone.utc)


def make_clock(days: list[date]) -> Callable[[], datetime]:
    state = {"index": 0}

    def _clock() -> datetime:
        day = days[min(state["index"], len(days) - 1)]
        state["index"] += 1
        return datetime.combine(day, time(12, 0), tzinfo=timezone.utc)

    return _clock


def make_record(message: str = "event") -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, message, (), None)


def make_daily_handler(
    root: Path,
    *,
    service: str = SERVICE,
    group: str = GROUP,
    days: int = 30,
    day: date | None = None,
) -> DailyHandler:
    return DailyHandler(
        service=service,
        group=group,
        daily_root=root,
        retention_days=days,
        clock=fixed_clock(day or FIXED_DAY),
    )


def write_daily_file(root: Path, day: date, content: str = "kept\n") -> Path:
    path = root / GROUP / SERVICE / f"{day.isoformat()}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def render(**extra: object) -> str:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ContextFormatter(service=SERVICE))
    logger = logging.getLogger("tts.observability.test.daily_render")
    old_handlers, old_propagate = logger.handlers[:], logger.propagate
    logger.handlers, logger.propagate = [handler], False
    logger.setLevel(logging.INFO)
    try:
        logger.info("event", extra=extra)
        return stream.getvalue()
    finally:
        logger.handlers, logger.propagate = old_handlers, old_propagate
        handler.close()
        stream.close()


# --- DailyHandler pathing and rotation ------------------------------------


def test_daily_handler_writes_grouped_utc_file() -> None:
    with removable_temp_dir() as root:
        handler = make_daily_handler(root)
        try:
            handler.handle(make_record("one"))
            handler.handle(make_record("two"))
            path = root / GROUP / SERVICE / f"{FIXED_DAY.isoformat()}.log"
            assert path.read_text(encoding="utf-8").splitlines() == ["one", "two"]
        finally:
            handler.close()


def test_daily_handler_rotates_on_utc_date_change() -> None:
    with removable_temp_dir() as root:
        handler = DailyHandler(
            service=SERVICE,
            daily_root=root,
            retention_days=30,
            clock=make_clock([FIXED_DAY, date(2026, 8, 4), FIXED_DAY]),
        )
        try:
            handler.handle(make_record("first-day"))
            handler.handle(make_record("second-day"))
            assert (root / GROUP / SERVICE / "2026-08-03.log").read_text(
                encoding="utf-8"
            ).splitlines() == ["first-day"]
            assert (root / GROUP / SERVICE / "2026-08-04.log").read_text(
                encoding="utf-8"
            ).splitlines() == ["second-day"]
        finally:
            handler.close()


def test_daily_handler_uses_utc_date_even_for_midnight_instant() -> None:
    with removable_temp_dir() as root:
        handler = DailyHandler(
            service=SERVICE,
            daily_root=root,
            retention_days=30,
            clock=lambda: datetime.combine(FIXED_DAY, time(0, 0), tzinfo=timezone.utc),
        )
        try:
            handler.handle(make_record("midnight"))
        finally:
            handler.close()
        assert (root / GROUP / SERVICE / "2026-08-03.log").read_text(
            encoding="utf-8"
        ) == "midnight\n"


@pytest.mark.parametrize(
    ("service", "group", "match"),
    [
        ("bogus", "product", "Unknown product service"),
        ("livekit", "product", "Unknown product service"),
        (SERVICE, "admin", "Unknown log group"),
    ],
)
def test_daily_handler_validates_service_and_group(service: str, group: str, match: str) -> None:
    with removable_temp_dir() as root:
        with pytest.raises(ValueError, match=match):
            make_daily_handler(root, service=service, group=group)


def test_daily_handler_accepts_platform_group() -> None:
    with removable_temp_dir() as root:
        handler = DailyHandler(service="livekit", group="platform", daily_root=root)
        try:
            handler.handle(make_record("ready"))
            assert (root / "platform" / "livekit" / "2026-08-03.log").read_text(
                encoding="utf-8"
            ) == "ready\n"
        finally:
            handler.close()


def test_daily_handler_close_is_terminal_and_idempotent() -> None:
    with removable_temp_dir() as root:
        handler = make_daily_handler(root)
        handler.handle(make_record("kept"))
        handler.close()
        handler.close()
        handler.handle(make_record("after-close"))
        path = root / GROUP / SERVICE / "2026-08-03.log"
        assert path.read_text(encoding="utf-8") == "kept\n"


def test_daily_file_never_contains_ansi_escapes() -> None:
    with removable_temp_dir() as root:
        handler = make_daily_handler(root)
        # The default formatter never colorizes; a handler configured with a
        # colorize formatter still must not be reachable from file output, so
        # the file view uses the setup-owned non-colorizing formatter.
        handler.setFormatter(ContextFormatter(service=SERVICE, colorize=False))
        try:
            handler.handle(make_record("plain intent"))
            content = (root / GROUP / SERVICE / "2026-08-03.log").read_text(encoding="utf-8")
        finally:
            handler.close()
        assert "\x1b[" not in content
        assert "INFO" in content


# --- Retention ------------------------------------------------------------


def test_daily_retention_deletes_only_expired_files() -> None:
    with removable_temp_dir() as root:
        # today is 2026-08-10, retention 7 -> cutoff 2026-08-03; strictly-older
        # files (before the 3rd) expire, the 3rd and newer survive.
        old_days = [date(2026, 8, 1), date(2026, 8, 2)]
        kept_days = [date(2026, 8, 3), date(2026, 8, 9), date(2026, 8, 10)]
        old_paths = [write_daily_file(root, day, "old\n") for day in old_days]
        kept_paths = [write_daily_file(root, day, "kept\n") for day in kept_days]
        handler = DailyHandler(
            service=SERVICE,
            daily_root=root,
            retention_days=7,
            clock=lambda: datetime.combine(date(2026, 8, 10), time(12, 0), tzinfo=timezone.utc),
        )
        try:
            deleted = handler.retain()
        finally:
            handler.close()
        assert sorted(deleted) == sorted(old_paths)
        assert all(path.exists() for path in kept_paths)
        assert all(not path.exists() for path in old_paths)


def test_daily_retention_honors_integer_window() -> None:
    with removable_temp_dir() as root:
        for day in (FIXED_DAY, date(2026, 8, 2), date(2026, 8, 1)):
            write_daily_file(root, day)
        handler = make_daily_handler(root, days=1)
        try:
            deleted = handler.retain()
        finally:
            handler.close()
        # days=1 -> cutoff 2026-08-02; only the 1st expires.
        assert (root / GROUP / SERVICE / "2026-08-03.log").exists()
        assert (root / GROUP / SERVICE / "2026-08-02.log").exists()
        assert sorted(path.name for path in deleted) == ["2026-08-01.log"]


@pytest.mark.parametrize("value", [0, -1, True, "7", 7.0])
def test_daily_retention_rejects_non_positive_integer(value: object) -> None:
    with removable_temp_dir() as root:
        with pytest.raises(ValueError, match="LOG_RETENTION_DAYS"):
            DailyHandler(service=SERVICE, daily_root=root, retention_days=value)  # type: ignore[arg-type]


def test_daily_retention_accepts_explicit_days_override() -> None:
    with removable_temp_dir() as root:
        future = date(2026, 8, 20)
        write_daily_file(root, future)
        write_daily_file(root, FIXED_DAY)
        handler = make_daily_handler(root, days=7)
        try:
            deleted = handler.retain(days=30)
        finally:
            handler.close()
        # cutoff 2026-08-03 - 30 = 2026-07-04; nothing is strictly older.
        assert deleted == []


def test_daily_retention_explicit_override_removes_expired() -> None:
    with removable_temp_dir() as root:
        write_daily_file(root, date(2026, 6, 1))
        write_daily_file(root, FIXED_DAY)
        handler = make_daily_handler(root, days=7)
        try:
            deleted = handler.retain(days=30)
        finally:
            handler.close()
        assert sorted(path.name for path in deleted) == ["2026-06-01.log"]
        assert (root / GROUP / SERVICE / "2026-08-03.log").exists()


def test_daily_retention_ignores_non_date_files() -> None:
    with removable_temp_dir() as root:
        write_daily_file(root, date(2026, 8, 1))
        directory = root / GROUP / SERVICE
        directory.mkdir(parents=True, exist_ok=True)
        stray = directory / "README.log"
        stray.write_text("notes\n", encoding="utf-8")
        dotted = directory / "2026-08-03.log.backup"
        dotted.write_text("x\n", encoding="utf-8")
        handler = make_daily_handler(root, days=1)
        try:
            deleted = handler.retain()
        finally:
            handler.close()
        assert [path.name for path in deleted] == ["2026-08-01.log"]
        assert stray.exists() and dotted.exists()
        # No dated .log file was written for the third; only dummies exist,
        # and neither an unrelated .log nor a non-.log sibling is removed.
        assert (root / GROUP / SERVICE / "2026-08-03.log").parent.is_dir()


def test_daily_retention_without_directory_returns_empty() -> None:
    with removable_temp_dir() as root:
        handler = make_daily_handler(root)
        try:
            assert handler.retain() == []
        finally:
            handler.close()


# --- Logfmt output --------------------------------------------------------


def test_formatter_aligns_level_and_service_columns() -> None:
    formatter = ContextFormatter(service=SERVICE)
    for levelno, levelname in DEFAULT_LEVELS:
        record = make_record()
        record.levelno = levelno
        record.levelname = levelname
        line = formatter.format(record)
        matches = re.match(r"^.*? \| (.{7}) \| (.{8}): ", line)
        assert matches is not None
        assert matches.group(1) == levelname.ljust(7)
        assert matches.group(2) == SERVICE.ljust(8)


def test_formatter_renders_utc_timestamp_and_heading() -> None:
    formatter = ContextFormatter(service=SERVICE)
    line = formatter.format(make_record("hello"))
    assert TS_PATTERN.match(line)
    assert f"INFO    | {SERVICE.ljust(8)}: hello" in line


def test_formatter_renders_short_approved_field_names() -> None:
    output = render(
        session_id="s1",
        request_id="r1",
        component="be",
        event="started",
        trace_id="t1",
        provider="openai",
        latency_ms="42",
        method="GET",
        path="/v1/sessions",
        status_code="200",
        error="boom",
    )
    assert "sid=s1" in output
    assert "rid=r1" in output
    assert "cmp=be" in output
    assert "evt=started" in output
    assert "trace_id=t1" in output
    assert "provider=openai" in output
    assert "latency_ms=42" in output
    assert "method=GET" in output
    assert "path=/v1/sessions" in output
    assert "status_code=200" in output
    assert "error=boom" in output
    assert "session_id=" not in output
    assert "request_id=" not in output


def test_formatter_quotes_values_with_whitespace_only() -> None:
    output = render(error="connection refused", provider="self", latency_ms="3")
    assert 'error="connection refused"' in output
    assert "provider=self" in output
    assert "latency_ms=3" in output


def test_formatter_quotes_empty_string_value() -> None:
    output = render(provider="")
    assert 'provider=""' in output


def test_formatter_tty_only_color() -> None:
    record = make_record()
    plain = ContextFormatter(service=SERVICE).format(record)
    colored = ContextFormatter(service=SERVICE, colorize=True).format(record)
    assert "\x1b[" not in plain
    assert colored.startswith("\x1b[32m") and colored.endswith("\x1b[0m")


def test_setup_never_writes_ansi_to_daily_file() -> None:
    with removable_temp_dir() as runtime_root:
        logger = setup_logging(validate_config(runtime_root=runtime_root))
        try:
            with scoped(session_id="s1"):
                logger.info("started", extra={"event": "started"})
            files = list((runtime_root / "daily").rglob("*.log"))
            assert len(files) == 1
            assert "\x1b[" not in files[0].read_text(encoding="utf-8")
        finally:
            reset_logging()


def test_setup_attaches_console_and_daily_handlers() -> None:
    with removable_temp_dir() as runtime_root:
        logger = setup_logging(validate_config(runtime_root=runtime_root))
        try:
            owned = [h for h in logger.handlers if getattr(h, "_tts_observability_handler", False)]
            assert len(owned) == 2
            assert sum(isinstance(h, DailyHandler) for h in owned) == 1
            assert sum(isinstance(h, logging.StreamHandler) for h in owned) == 1
        finally:
            reset_logging()


def test_setup_logging_writes_daily_file_with_aligned_logfmt() -> None:
    with removable_temp_dir() as runtime_root:
        logger = setup_logging(validate_config(runtime_root=runtime_root))
        try:
            with scoped(session_id="s1"):
                logger.info("started", extra={"event": "started"})
            files = list((runtime_root / "daily").rglob("*.log"))
            assert len(files) == 1
            content = files[0].read_text(encoding="utf-8")
            assert "INFO" in content
            assert "evt=started" in content
            assert "sid=s1" in content
            assert "\x1b[" not in content
        finally:
            reset_logging()


def test_setup_is_idempotent_and_closes_owned_handlers() -> None:
    with removable_temp_dir() as runtime_root:
        logger = setup_logging(validate_config(runtime_root=runtime_root))
        owned = [h for h in logger.handlers if getattr(h, "_tts_observability_handler", False)]
        assert len(owned) == 2
        setup_logging(validate_config(runtime_root=runtime_root))
        assert (
            len([h for h in logger.handlers if getattr(h, "_tts_observability_handler", False)])
            == 2
        )
        reset_logging()
        assert all(item._closed for item in owned)
    assert not runtime_root.exists()


# --- Platform daily view -----------------------------------------------------


def test_platform_collector_writes_daily_platform_log() -> None:
    with removable_temp_dir() as root:
        collector = PlatformCollector(active_root=root / "active", daily_root=root / "daily")
        try:
            collector.run_stream(
                ["evt=room_created provider=livekit latency_ms=12.5", "token=sk-secret"],
                service="livekit",
            )
        finally:
            collector.close()
        daily_files = list((root / "daily" / "platform" / "livekit").glob("*.log"))
        assert len(daily_files) == 1
        content = daily_files[0].read_text(encoding="utf-8")
        assert "evt=platform_event" in content
        assert "evt=sensitive_field_dropped" in content
        assert "sk-secret" not in content
        assert (root / "active" / "platform" / "livekit.log").exists()


def test_platform_collector_emit_event_also_writes_daily() -> None:
    with removable_temp_dir() as root:
        collector = PlatformCollector(active_root=root / "active", daily_root=root / "daily")
        try:
            collector.emit_event("postgres", "evt=connection_accepted")
        finally:
            collector.close()
        files = list((root / "daily" / "platform" / "postgres").glob("*.log"))
        assert len(files) == 1
        assert "evt=platform_event" in files[0].read_text(encoding="utf-8")


def test_platform_collector_retain_daily_honors_window() -> None:
    with removable_temp_dir() as root:
        collector = PlatformCollector(active_root=root / "active", daily_root=root / "daily")
        daily_dir = root / "daily" / "platform" / "livekit"
        daily_dir.mkdir(parents=True, exist_ok=True)
        old_path = daily_dir / "2026-07-01.log"
        new_path = daily_dir / "2026-08-03.log"
        old_path.write_text("x\n", encoding="utf-8")
        new_path.write_text("x\n", encoding="utf-8")
        try:
            deleted = collector.retain_daily("livekit", days=7)
        finally:
            collector.close()
        assert deleted == [old_path]
        assert new_path.exists() and not old_path.exists()


def test_daily_root_derived_from_active_root_when_absent() -> None:
    collector = PlatformCollector(active_root=Path(".runtime/logs/active-sessions"))
    assert collector.daily_root == Path(".runtime/logs/daily")
