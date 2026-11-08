"""Offline prompt measurement for the product-intake extractor and the ordered script writer.

Not a test (no test_ prefix) and never touches production. The model is injected:

    EVAL_LLM_URL    OpenAI-compatible base URL, e.g. http://localhost:8000/v1   (required unless --fake)
    EVAL_LLM_MODEL  model name sent in the request
    EVAL_LLM_KEY    optional bearer token, read from the environment only and never printed

    uv run python tests/prompt_eval/run_eval.py --runs 5            # real model
    uv run python tests/prompt_eval/run_eval.py --fake --runs 1     # self-check, no model

Per case it prints intake counters (kept, dropped by reason, longest fact in words, facts opening with the
product name) and writer counters (first-attempt guard rejections by reason, banned filler, digits left in
the text, longest unit in sentences). The writer is asked ONCE per unit with no retry, so the rejection rate
is the first-attempt rate; the production retry loop would lower it. PASS/FAIL lines apply the case's
`expect` block to what was produced.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from backend.application.product_intake import IntakeError, extract_facts
from backend.application.script_authoring.generation.ordered_units import (
    ProductBrief,
    SessionBrief,
    build_unit_prompt,
    check_unit_text,
    plan_units,
    repeats_source,
    revisit_source_index,
)

CORPUS = Path(__file__).with_name("corpus.json")
FILLER = re.compile(
    r"yên tâm|thật lòng|cực kỳ|vô cùng|có tò mò|thấy không|đúng không nào", re.IGNORECASE
)


def http_llm(temperature: float) -> Callable[[str], str]:
    base, model = os.environ["EVAL_LLM_URL"].rstrip("/"), os.environ["EVAL_LLM_MODEL"]
    headers = {"Content-Type": "application/json"}
    if os.environ.get("EVAL_LLM_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["EVAL_LLM_KEY"]

    def call(prompt: str) -> str:
        body = json.dumps(
            {
                "model": model,
                "temperature": temperature,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode()
        request = urllib.request.Request(base + "/chat/completions", body, headers)
        with urllib.request.urlopen(request, timeout=90) as response:  # noqa: S310 - operator-chosen URL
            return json.load(response)["choices"][0]["message"]["content"]

    return call


def fake_llm(prompt: str) -> str:
    """Deterministic stand-in so the harness itself can be checked without a model."""
    if "DATA-" in prompt:
        lines = [
            x.strip()
            for x in prompt.split("-BEGIN\n", 1)[1].rsplit("\nDATA-", 1)[0].splitlines()
            if x.strip()
        ]
        facts = [
            {"type": "feature", "text": x.lstrip("- ").strip(), "evidence": x.lstrip("- ").strip()}
            for x in lines
        ]
        return json.dumps({"facts": facts}, ensure_ascii=False)
    return "Mình nói lại các ý bên dưới thật ngắn gọn cho mọi người nghe nhé."


def run_intake(case: dict, llm: Callable[[str], str]) -> dict:
    try:
        result = extract_facts(case["text"], llm)
    except IntakeError as exc:
        return {"error": exc.code, "facts": [], "dropped": Counter()}
    return {"facts": list(result.facts), "dropped": Counter(result.dropped_by_reason())}


def run_writer(case: dict, claims: tuple[str, ...], llm: Callable[[str], str]) -> dict:
    product = ProductBrief(
        "eval",
        case["name"],
        prices=tuple(case.get("prices", ())),
        discounts=tuple(case.get("discounts", ())),
        claims=claims,
    )
    session = SessionBrief(notes=case.get("notes", ""))
    specs = plan_units(product, first=True, last=True, target_duration_s=360)
    approved = (*product.claims, *product.discounts)
    texts: dict[int, str] = {}
    rejected: Counter[str] = Counter()
    prompts: list[str] = []
    for i, spec in enumerate(specs):
        prompt = build_unit_prompt(session, product, specs, i)
        prompts.append(prompt)
        named = sum(case["name"].lower() in t.lower() for t in texts.values())
        text, why = check_unit_text(
            llm(prompt),
            spec=spec,
            prices=product.prices,
            allow_bridge=spec.bridge,
            approved=approved,
            product_name=case["name"],
            name_used_elsewhere=named,
            must=spec.must,
        )
        source = revisit_source_index(specs, i) if spec.revisit else None
        if text is not None and source in texts and repeats_source(text, texts[source]):
            text, why = None, "repeat"
        if text is None:
            rejected[why or "empty"] += 1
        else:
            texts[i] = text
    return {
        "texts": list(texts.values()),
        "rejected": rejected,
        "units": len(specs),
        "prompts": prompts,
    }


def expect_lines(case: dict, intake: dict | None, writer: dict | None) -> list[str]:
    expect, out = case.get("expect", {}), []
    facts = intake["facts"] if intake else []
    spoken = " ".join(writer["texts"]).lower() if writer else ""
    prompts = " ".join(writer["prompts"]).lower() if writer else ""
    if intake and "min_facts" in expect:
        ok = len(facts) >= expect["min_facts"]
        out.append(f"{'PASS' if ok else 'FAIL'} min_facts {len(facts)}/{expect['min_facts']}")
    for needle in expect.get("evidence_contains", []) if intake else []:
        ok = any(needle.lower() in (f.evidence + " " + f.text).lower() for f in facts)
        out.append(f"{'PASS' if ok else 'FAIL'} keeps {needle!r}")
    for flag in expect.get("flags", []) if intake else []:
        ok = any(flag in f.flags for f in facts)
        out.append(f"{'PASS' if ok else 'FAIL'} flag {flag}")
    haystack = (
        " ".join(f.text for f in facts).lower() + " " + spoken + (prompts if not intake else "")
    )
    for needle in expect.get("forbid", []):
        out.append(f"{'FAIL' if needle.lower() in haystack else 'PASS'} absent {needle!r}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--case", action="append", help="case id prefix, repeatable")
    args = parser.parse_args(argv)
    if not args.fake and not (os.environ.get("EVAL_LLM_URL") and os.environ.get("EVAL_LLM_MODEL")):
        sys.exit(
            "Set EVAL_LLM_URL and EVAL_LLM_MODEL (or use --fake). This tool never calls production."
        )
    intake_llm = fake_llm if args.fake else http_llm(0.1)
    writer_llm = fake_llm if args.fake else http_llm(0.7)
    failures = 0
    for case in json.loads(CORPUS.read_text(encoding="utf8")):
        if args.case and not any(case["id"].startswith(c) for c in args.case):
            continue
        print(f"\n== {case['id']}")
        for run in range(1, args.runs + 1):
            intake = run_intake(case, intake_llm) if "text" in case else None
            claims = (
                tuple(f.text for f in intake["facts"]) if intake else tuple(case.get("claims", ()))
            )
            writer = run_writer(case, claims, writer_llm) if claims else None
            parts = [f"run {run}"]
            if intake:
                words = max((len(f.text.split()) for f in intake["facts"]), default=0)
                named = sum(
                    f.text.lower().startswith(case["name"].lower()) for f in intake["facts"]
                )
                parts.append(
                    f"intake kept={len(intake['facts'])} dropped={dict(intake['dropped'])} "
                    f"longest={words}w name_first={named}"
                    + (f" ERROR={intake['error']}" if "error" in intake else "")
                )
            if writer:
                joined = " ".join(writer["texts"])
                digits = len(re.findall(r"\d", joined))
                longest = max((len(re.findall(r"[.!?]", t)) for t in writer["texts"]), default=0)
                parts.append(
                    f"writer units={writer['units']} rejected={dict(writer['rejected'])} "
                    f"filler={len(FILLER.findall(joined))} digits={digits}"
                    f"longest={longest}s"
                )
            print(" | ".join(parts))
            lines = expect_lines(case, intake, writer)
            failures += sum(line.startswith("FAIL") for line in lines)
            for line in lines:
                if line.startswith("FAIL"):
                    print("   ", line)
    print(f"\nexpectation failures: {failures}")
    return (
        1 if failures and not args.fake else 0
    )  # fake output is canned; only a real model is judged


if __name__ == "__main__":
    sys.exit(main())
