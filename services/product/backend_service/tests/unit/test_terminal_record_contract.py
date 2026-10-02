"""C-TERMINAL-001 pure contract: shared fixture parity with Go (byte-identical copy)."""

import hashlib
import json
from pathlib import Path

import pytest

from backend.application.execution_contract import (
    TERMINAL_CAPABILITY,
    Capabilities,
    Cleanup,
    ExecutionIdentity,
    TerminalRecord,
    TerminalRecordInvalid,
    reduce_cleanup,
    reduce_terminal,
    set_terminal_advertised,
    terminal_canonical_input,
    terminal_record_hash,
    terminal_record_id,
    validate_terminal_record,
)

# The Go twin pins the same digest: editing one copy without the other fails both suites.
FIXTURE_SHA256 = "773140a62b075458fe4e840c4c67166e200a65c25bd14a4e5a0fbaa0670247cb"
FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "terminal_record_v1.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
RECORDS = {name: TerminalRecord.model_validate(raw) for name, raw in FIXTURE["records"].items()}


def test_fixture_is_lf_only_byte_identical_to_the_go_copy_and_schema_matches():
    assert b"\r" not in FIXTURE_PATH.read_bytes()
    assert hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest() == FIXTURE_SHA256
    assert FIXTURE["schema"] == "p0.terminal.v1"


@pytest.mark.parametrize("case", FIXTURE["ids"], ids=lambda c: c["terminal_record_id"][:16])
def test_terminal_record_id_matches_go(case):
    assert terminal_record_id(ExecutionIdentity(**case["identity"])) == case["terminal_record_id"]


@pytest.mark.parametrize("case", FIXTURE["canonical"], ids=lambda c: c["record"])
def test_canonical_input_matches_go(case):
    assert terminal_canonical_input(RECORDS[case["record"]]) == case["input"]


@pytest.mark.parametrize("name", sorted(RECORDS))
def test_record_hash_matches_fixture(name):
    assert terminal_record_hash(RECORDS[name]) == RECORDS[name].record_hash


@pytest.mark.parametrize("case", FIXTURE["validation"], ids=lambda c: c["name"])
def test_validation_matches_go(case):
    try:
        record = TerminalRecord.model_validate(case["record"])
        validate_terminal_record(record)
    except (TerminalRecordInvalid, ValueError):
        assert not case["valid"]
    else:
        assert case["valid"]


@pytest.mark.parametrize("case", FIXTURE["precedence"], ids=lambda c: c["name"])
def test_precedence_matches_go(case):
    existing = RECORDS[case["existing"]] if case["existing"] else None
    decision = reduce_terminal(existing, RECORDS[case["incoming"]])
    assert decision == (case["action"], case["reason"], case["audit"], case["cleanup_failure"])


@pytest.mark.parametrize("case", FIXTURE["cleanup"], ids=lambda c: c["name"])
def test_cleanup_is_monotonic_like_go(case):
    merged, changed = reduce_cleanup(Cleanup(**case["existing"]), Cleanup(**case["incoming"]))
    assert (merged.model_dump(), changed) == (
        Cleanup(**case["merged"]).model_dump(),
        case["changed"],
    )


def test_a_naive_timestamp_is_rejected_not_guessed():
    raw = dict(FIXTURE["records"]["ended_normal"], terminal_at="2026-09-30T00:00:06")
    with pytest.raises(TerminalRecordInvalid):
        validate_terminal_record(TerminalRecord.model_validate(raw))


def test_terminal_capability_is_absent_by_default_and_only_advertised_when_set():
    assert TERMINAL_CAPABILITY not in Capabilities().available
    assert not Capabilities().supports(TERMINAL_CAPABILITY)
    set_terminal_advertised(True)
    try:
        assert Capabilities().supports(TERMINAL_CAPABILITY)
    finally:
        set_terminal_advertised(False)
    assert TERMINAL_CAPABILITY not in Capabilities().available
