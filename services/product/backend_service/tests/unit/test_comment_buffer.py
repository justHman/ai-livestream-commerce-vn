"""Unit tests for ChatQueue (Phase B).

Covers:
  - put N comments, drain_window returns all within fresh window
  - timestamps spread over 200s, drain_window(75) filters correctly
  - max_size eviction (put 600 with max_size=500 -> len==500)
  - stats() returns pending count and oldest_ms_ago
  - clear() empties the queue
"""

from __future__ import annotations

import time

from backend.application.director.comment_buffer import ChatQueue, IncomingComment


def test_put_and_drain_all_fresh():
    """Put 5 comments with current timestamps; drain_window(75) returns all 5."""
    q = ChatQueue("sess-1", max_size=500)
    now = time.time()
    for i in range(5):
        q.put(f"comment {i}", f"user{i}", ts=now + i * 0.1)
    result = q.drain_window(75.0)
    assert len(result) == 5
    assert all(isinstance(c, IncomingComment) for c in result)
    # Verify order preserved.
    assert [c.text for c in result] == [f"comment {i}" for i in range(5)]


def test_drain_window_filters_old_comments():
    """Put 10 comments spread over 200s; drain_window(75) returns only recent ones."""
    q = ChatQueue("sess-2", max_size=500)
    now = time.time()
    for i in range(10):
        # Spread: 0s, 22s, 44s, ..., 198s ago
        ts = now - (9 - i) * 22.0
        q.put(f"msg {i}", "viewer", ts=ts)

    result = q.drain_window(75.0)
    # Comments within last 75s: those with ts >= now - 75.
    # The comment at index i has age (9 - i) * 22 seconds.
    # age <= 75 => (9 - i) * 22 <= 75 => 9 - i <= 3.4 => i >= 5.6 => i in {6, 7, 8, 9}
    expected_count = 4  # indices 6, 7, 8, 9
    assert len(result) == expected_count
    assert result[0].text == "msg 6"
    assert result[-1].text == "msg 9"


def test_max_size_eviction():
    """Put 600 comments with max_size=500 -> oldest 100 are evicted."""
    q = ChatQueue("sess-3", max_size=500)
    now = time.time()
    for i in range(600):
        q.put(f"c{i}", "author", ts=now + i * 0.001)
    assert len(q) == 500
    # The first comment should be c100 (oldest 100 evicted).
    window = q.drain_window(9999.0)
    assert len(window) == 500
    assert window[0].text == "c100"
    assert window[-1].text == "c599"


def test_stats_pending_and_oldest():
    """stats() returns pending count and oldest_ms_ago."""
    q = ChatQueue("sess-4", max_size=500)
    s0 = q.stats()
    assert s0["pending"] == 0
    assert s0["oldest_ms_ago"] is None

    now = time.time()
    q.put("first", "a", ts=now - 2.0)  # 2 seconds ago
    q.put("second", "b", ts=now - 0.5)  # 0.5 seconds ago
    q.put("third", "c", ts=now)

    s1 = q.stats()
    assert s1["pending"] == 3
    # oldest_ms_ago should be approximately 2000ms (2 seconds ago).
    assert s1["oldest_ms_ago"] is not None
    assert s1["oldest_ms_ago"] > 1500  # at least 1.5s
    assert s1["total_put"] == 3


def test_put_assigns_unique_ids():
    """Each put() call generates a unique comment id."""
    q = ChatQueue("sess-5")
    c1 = q.put("hello", "a")
    c2 = q.put("hello", "a")
    assert c1.id != c2.id
    assert len(c1.id) == 32  # uuid4 hex


def test_clear_empties_queue():
    """clear() drops all comments."""
    q = ChatQueue("sess-6")
    for i in range(10):
        q.put(f"msg {i}", "author")
    assert len(q) == 10
    q.clear()
    assert len(q) == 0
    assert q.stats()["pending"] == 0


def test_put_default_timestamp():
    """put() without explicit ts uses time.time()."""
    q = ChatQueue("sess-7")
    before = time.time()
    c = q.put("auto-ts", "user")
    after = time.time()
    assert before <= c.ts <= after
