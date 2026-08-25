"""Aggregate governed CI gate results (audit R8.5).

The final `CI / gate` job MUST fail if ANY governed validation job fails,
including repo-tools. This script is the single aggregation point so the
logic is unit-testable with failure injection (no shell-only loop that can
silently omit a result).

Usage:
    python scripts/ci/gate_aggregate.py --result scan=success --result repo=failure ...
Exit code 0 = every governed result is `success` or `skipped`.
"""

import argparse
import sys
from typing import Dict, List

ACCEPTED = frozenset({"success", "skipped"})


def aggregate(results: Dict[str, str]) -> List[str]:
    """Return failure lines; empty means the gate passes.

    A missing/empty value or any result outside {success, skipped} is a
    governed failure (fail closed).
    """
    failures: List[str] = []
    for name, value in results.items():
        if value not in ACCEPTED:
            failures.append(
                f"CI / gate failed: job '{name}' result '{value}' is not success or skipped."
            )
    return failures


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--result",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="governed job result; repeat per job",
    )
    args = parser.parse_args(argv)

    results: Dict[str, str] = {}
    for kv in args.result:
        if "=" not in kv:
            print(f"invalid --result {kv!r} (expected NAME=VALUE)", file=sys.stderr)
            return 2
        name, value = kv.split("=", 1)
        results[name] = value

    failures = aggregate(results)
    if failures:
        for line in failures:
            print(line, file=sys.stderr)
        print(
            f"CI / gate failed: {len(failures)} governed result(s) not success/skipped.",
            file=sys.stderr,
        )
        return 1
    print("CI / gate passed: all governed jobs succeeded or skipped neutrally.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
