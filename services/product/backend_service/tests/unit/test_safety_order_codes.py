"""Order-by-comment codes ("Chốt D30 1") must not be read as obfuscated abuse.

Regression: leetspeak folding turned the shop code D30 into "deo" (a curated insult), so the
safety gate rejected the viewer's order with reason toxicity and the host never saw it.
"""

import pytest

from backend.application.safety_gate.resources import load_all_curated_patterns, match_curated


@pytest.fixture(scope="module")
def sets():
    return load_all_curated_patterns()


@pytest.mark.parametrize(
    "comment",
    [
        "Chốt D30 1",
        "Chốt D30 1 nha",
        "Chốt đơn D30",
        "Cho mình đặt D30 giúp",
        "Chốt D60 1",
        "Chốt M1 2",
        "Chốt W1 1",
        "Chốt T420 1",
        "Chốt G30 1",
    ],
)
def test_order_comment_with_a_short_code_is_not_flagged(sets, comment):
    assert match_curated(comment, sets) == []


@pytest.mark.parametrize("comment", ["Mày d3o hả", "đ3o mày", "l0n", "d1t me"])
def test_leetspeak_abuse_is_still_flagged(sets, comment):
    assert "toxicity" in match_curated(comment, sets)
