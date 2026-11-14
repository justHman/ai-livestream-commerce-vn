"""Task B0 tests: offline script-quality metrics + the QA harness CLI.

Covers (per the task sheet):
- every metric has at least one behavioral test;
- number fraud catches "tám trăm bốn mươi kg" against claims that only have
  "0,84 kg", and does NOT false-alarm "0,38 kg" when 0,38 kg IS a claim;
- real-baseline number false alarms: "Bây giờ" is not read as "7 giờ"
  (diacritic-exact digit words), digit-by-digit size readings ("một sáu tám
  năm hai đến năm tám kg") match claim ranges (1m60–1m68, 52–58kg) and
  unsegmentable runs are "uncertain", not "error"; written price fraud
  ("hai triệu năm trăm nghìn đồng") is still caught;
- written-number normalization, repetition, flatness;
- regeneration measurement: each attempt records workflow status, error
  fields and the re-read gate; ``--regen-trials`` calls the regenerate
  endpoint exactly N times round-robin; both success rates are reported;
- ``run --dry-run`` makes no network calls (urllib is patched to explode);
- ``run`` refuses non-ZZ-TEST workspaces;
- the LIVENTO_QA_TOKEN never appears in any output.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.qa import script_quality_metrics as m
from scripts.qa import script_quality_qa as qa

FIXTURES = Path(__file__).parent / "fixtures" / "script_quality"
SECRET_TOKEN = "SK-SECRET-B0-TOKEN-VALUE"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _analyze(doc: dict, **kwargs) -> dict:
    return m.analyze_script(doc["sections"], doc["claims"], doc.get("product"), **kwargs)


# ── Duration / totals ────────────────────────────────────────────────────────


def test_duration_estimate_uses_runtime_16_chars_per_s() -> None:
    sections = [{"text": "a" * 320}]  # 320 chars / 16 = 20 s
    analysis = m.analyze_script(sections, [], {}, floor_s=60, ceiling_s=240)
    totals = analysis["totals"]
    assert totals["chars"] == 320
    assert totals["estimated_duration_s"] == pytest.approx(20.0)
    assert totals["duration_pct_of_floor"] == pytest.approx(20.0 / 60.0 * 100, rel=0.01)
    assert m.VOICE_CHARS_PER_S == 16  # mirrored Runtime constant (ordered_units.py)


def test_duration_below_70pct_floor_is_an_issue() -> None:
    analysis = m.analyze_script([{"text": "g" * 160}], [], {}, floor_s=180)
    assert "duration_below_70pct_floor" in analysis["issues"]  # 10 s of 180 s floor
    # 70% of 180 s = 126 s = 2016 chars: exactly at the line passes.
    ok = m.analyze_script([{"text": "g" * 2100}], [], {}, floor_s=180)
    assert "duration_below_70pct_floor" not in ok["issues"]


# ── Written-number normalization ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ("tám trăm bốn mươi", "840"),
        ("năm ba", "53"),
        ("một trăm linh một", "101"),
        ("hai nghìn không trăm lẻ năm", "2005"),
        ("một triệu ba trăm chín mươi nghìn", "1390000"),
        ("hai mươi lăm", "25"),
        ("mười lăm", "15"),
        ("bốn trăm nghìn", "400000"),
        ("không phẩy tám tư", "0,84"),
        ("hai mươi ba phần trăm", "23"),
        ("một năm", "1"),
    ],
)
def test_written_number_normalization(words: str, expected: str) -> None:
    found = m.written_numbers(words)
    assert found, f"no written number detected in {words!r}"
    assert found[0]["value"] == expected


def test_written_number_with_unit() -> None:
    found = m.written_numbers("tám trăm bốn mươi kg")
    assert found[0]["value"] == "840"
    assert found[0]["unit"] == "kg"


# ── Number fraud ─────────────────────────────────────────────────────────────


def test_number_fraud_catches_written_words_for_0_84_kg() -> None:
    # The TASK_B §1 case: 0,84 kg spoken as "tám trăm bốn mươi kg" must be caught.
    claims = ["Mỗi chai được làm từ 0,84 kg nguyên liệu tươi."]
    sections = [{"text": "Mỗi chai được làm từ tám trăm bốn mươi kg nguyên liệu tươi."}]
    analysis = m.analyze_script(sections, claims, {}, floor_s=30)
    assert analysis["numbers"]["written_violations"], analysis
    violation = analysis["numbers"]["written_violations"][0]
    assert violation["value"] == "840"
    assert violation["unit"] == "kg"
    assert "written_number_not_in_claims" in analysis["issues"]


def test_number_fraud_does_not_flag_0_38_kg_when_claimed() -> None:
    # The TASK_B §1 false-alarm case: 0,38 kg IS in the claims, digits in the
    # text must pass (the CLAIM_FACTUAL gate used to warn anyway).
    claims = ["Hộp serum đóng gói nặng 0,38 kg.", "Mỗi chai được làm từ 0,84 kg nguyên liệu tươi."]
    sections = [{"text": "Hộp serum này nặng 0,38 kg cầm chắc tay, làm từ 0,84 kg nguyên liệu."}]
    analysis = m.analyze_script(sections, claims, {}, floor_s=30)
    assert analysis["numbers"]["violation_count"] == 0, analysis["numbers"]


def test_decimal_comma_and_thousand_price_match_claims() -> None:
    claims = ["Giá niêm yết 1.790.000đ, giá live 1.390.000đ.", "Nặng 0,84 kg."]
    sections = [{"text": "Giá live 1.390.000đ thôi, cân 0,84 kg nhé."}]
    analysis = m.analyze_script(sections, claims, {}, floor_s=30)
    assert analysis["numbers"]["violation_count"] == 0, analysis["numbers"]


def test_bluetooth_written_pair_not_flagged_when_claim_has_version() -> None:
    # "Bluetooth năm ba" matches the claim "Bluetooth 5.3" (TASK_B §4 example).
    claims = ["Máy dùng Bluetooth 5.3."]
    sections = [{"text": "Máy dùng Bluetooth năm ba, kết nối ổn."}]
    analysis = m.analyze_script(sections, claims, {}, floor_s=30)
    assert analysis["numbers"]["violation_count"] == 0, analysis["numbers"]


def test_digit_number_missing_from_claims_is_flagged() -> None:
    claims = ["Giá live 299.000đ."]
    sections = [{"text": "Giá chỉ còn 350.000đ thôi các bạn ạ."}]
    analysis = m.analyze_script(sections, claims, {}, floor_s=30)
    assert analysis["numbers"]["digit_violations"], analysis["numbers"]
    assert analysis["numbers"]["digit_violations"][0]["value"] == "350000"


# ── Number fraud: real-baseline false alarms (B0-fix) ────────────────────────


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ("một sáu tám", "168"),  # digit-by-digit height 1m68
        ("năm hai", "52"),
        ("một sáu", "16"),  # 1m60
        ("tám lăm", "85"),
        ("một sáu tám năm hai", "16852"),
    ],
)
def test_digit_by_digit_words_concatenate(words: str, expected: str) -> None:
    found = m.written_numbers(words)
    assert found, f"no written number detected in {words!r}"
    assert found[0]["value"] == expected


def test_bay_gio_is_not_read_as_7_gio() -> None:
    # Real baseline: "Bây giờ" matched the accentless "bay" (7) + unit "giờ".
    # Digit words must match WITH their diacritics: "bảy" yes, "bây"/"bay" no.
    assert m.written_numbers("Bây giờ") == []
    assert m.written_numbers("bây giờ") == []
    assert [o["value"] for o in m.written_numbers("bảy giờ")] == ["7"]
    claims = ["Giá niêm yết 1.790.000đ, giá live 1.390.000đ."]
    analysis = m.analyze_script(
        [{"text": "Bây giờ chúng ta cùng điểm qua sản phẩm nồi chiên nhé."}],
        claims,
        {},
        floor_s=30,
    )
    assert analysis["numbers"]["violation_count"] == 0
    assert analysis["numbers"]["uncertain_count"] == 0


def test_digit_by_digit_size_readings_match_claim_ranges() -> None:
    # Real baseline: "một sáu tám năm hai" was mis-merged into "31", "một bảy
    # tư năm tám" into "34". Digit-by-digit readings must resolve against the
    # claim ranges (1m60–1m68, 52–58kg, 1m65–1m74, 58–66kg, …) and pass.
    claims = [
        "Size M phù hợp 1m60–1m68 và 52–58kg.",
        "Size L phù hợp 1m65–1m74 và 58–66kg.",
        "Size XXL phù hợp đến 85kg.",
    ]
    sentences = [
        "M phù hợp từ một sáu đến một sáu tám năm hai đến năm tám kg.",
        "L cho từ một sáu lăm đến một bảy tư năm tám đến sáu sáu kg.",
        "XXL phù hợp đến tám lăm kg.",
    ]
    for sentence in sentences:
        numbers = m.analyze_script([{"text": sentence}], claims, {}, floor_s=30)["numbers"]
        assert numbers["violation_count"] == 0, (sentence, numbers)
        assert numbers["uncertain_count"] == 0, (sentence, numbers)


def test_spoken_range_and_interior_value_covered_by_claim_range() -> None:
    claims = ["Size L phù hợp 1m65–1m74 và 58–66kg."]
    occurrences = m.written_numbers("một sáu lăm đến một bảy tư")
    assert [o["value"] for o in occurrences] == ["165", "174"]
    assert occurrences[1]["range_low"] == "165"  # spoken low–high via "đến"
    # Both endpoints of the claim range, and a value inside it, are covered.
    for sentence in (
        "Size L cho người cao một sáu lăm đến một bảy tư.",
        "Size L vừa người cao một sáu tám.",  # 168 inside 1m65–1m74
    ):
        numbers = m.analyze_script([{"text": sentence}], claims, {}, floor_s=30)["numbers"]
        assert numbers["violation_count"] == 0, (sentence, numbers)
        assert numbers["uncertain_count"] == 0, (sentence, numbers)


def test_ambiguous_digit_run_downgraded_to_uncertain_not_error() -> None:
    # "một bảy tám tám sáu" (1m78 … 86?) has no segmentation fully covered by
    # the claims (66–75kg has no 86): reported as uncertain, NOT a hard error.
    claims = ["Size XL phù hợp 1m70–1m78 và 66–75kg."]
    analysis = m.analyze_script(
        [{"text": "XL lên đến một bảy tám tám sáu đến bảy lăm kg."}], claims, {}, floor_s=30
    )
    numbers = analysis["numbers"]
    assert numbers["violation_count"] == 0
    assert numbers["uncertain_count"] == 1
    uncertain = numbers["written_uncertain"][0]
    assert uncertain["as_written"] == "một bảy tám tám sáu"
    assert uncertain["value"] == "17886"
    assert uncertain["severity"] == "uncertain"
    assert "written_number_not_in_claims" not in analysis["issues"]
    assert "digit_number_not_in_claims" not in analysis["issues"]


def test_written_price_fraud_still_caught() -> None:
    # Do-not-weaken guard: a written price absent from the claims stays a hard
    # error (magnitude words make the reading unambiguous).
    claims = ["Giá niêm yết 1.790.000đ, giá live 1.390.000đ."]
    analysis = m.analyze_script(
        [{"text": "Giá bán cho sản phẩm tuyệt vời này là hai triệu năm trăm nghìn đồng."}],
        claims,
        {},
        floor_s=30,
    )
    numbers = analysis["numbers"]
    assert len(numbers["written_violations"]) == 1
    violation = numbers["written_violations"][0]
    assert violation["value"] == "2500000"
    assert violation["unit"] == "dong"
    assert violation["severity"] == "error"
    assert "written_number_not_in_claims" in analysis["issues"]
    assert analysis["numbers"]["uncertain_count"] == 0


def test_fixture_baseline_size_readings_no_false_alarms() -> None:
    # Sentences extracted verbatim from the real baseline run: zero hard
    # number errors; the only uncertain runs are the unsegmentable ones.
    doc = _load("size_readings_baseline.json")
    analysis = _analyze(doc)
    numbers = analysis["numbers"]
    assert numbers["violation_count"] == 0, numbers
    assert numbers["uncertain_count"] == 2, numbers
    assert {u["as_written"] for u in numbers["written_uncertain"]} == {
        "một năm",  # "một năm rưỡi lăm" — "rưỡi" is an unsupported reading
        "một bảy tám tám sáu",
    }


# ── Buzzwords ────────────────────────────────────────────────────────────────


def test_buzzword_flagged_when_not_in_claims() -> None:
    sections = [{"text": "Đây là hot item đẳng cấp của shop mình đó."}]
    analysis = m.analyze_script(sections, ["Giá live 299.000đ."], {}, floor_s=30)
    phrases = {hit["phrase"] for hit in analysis["buzzwords"]["hits"]}
    assert {"hot item", "đẳng cấp"} <= phrases
    assert "buzzword_not_in_claims" in analysis["issues"]


def test_buzzword_not_flagged_when_claim_covers_it() -> None:
    # A praising phrase that the owner approved as a claim is legitimate.
    claims = ["Sản phẩm là lựa chọn lý tưởng cho da khô."]
    sections = [{"text": "Đây chính là lựa chọn lý tưởng cho da khô của bạn."}]
    analysis = m.analyze_script(sections, claims, {}, floor_s=30)
    assert analysis["buzzwords"]["hit_count"] == 0, analysis["buzzwords"]


def test_buzzword_list_is_configurable() -> None:
    sections = [{"text": "Cái này hợp lắm."}]
    analysis = m.analyze_script(sections, [], {}, floor_s=30, buzzwords=("hợp lý", "hot item"))
    assert analysis["buzzwords"]["hit_count"] == 0  # "hợp lắm" is not "hợp lý"
    assert analysis["buzzwords"]["phrases_checked"] == 2


# ── Repetition ───────────────────────────────────────────────────────────────


def test_verbatim_sentence_between_main_and_recap_is_flagged() -> None:
    sections = [
        {
            "role": "highlights",
            "text": "Giá live hôm nay là 299.000đ, giảm 23% so với giá niêm yết.",
        },
        {"role": "recap", "text": "Giá live hôm nay là 299.000đ, giảm 23% so với giá niêm yết."},
    ]
    analysis = m.analyze_script(sections, ["Giá live 299.000đ, giảm 23%."], {}, floor_s=30)
    assert analysis["repetition"]["verbatim_main_recap"], analysis["repetition"]
    assert "verbatim_repeat_main_recap" in analysis["issues"]


def test_recap_sections_are_detected_from_role_or_kind() -> None:
    for field in ("role", "kind"):
        sections = [{"field_value": "", "text": "một hai ba"}]
        sections[0] = {field: "Revisit", "text": "một hai ba"}
        analysis = m.analyze_script(sections, [], {}, floor_s=30)
        assert analysis["repetition"]["recap_sections"] == [0], field


def test_repetition_without_roles_falls_back_to_cross_section_overlap() -> None:
    sections = [
        {"text": "Chai này nặng 0,38 kg nhé."},
        {"text": "Chai này nặng 0,38 kg nhé."},
    ]
    analysis = m.analyze_script(sections, ["Hộp serum nặng 0,38 kg."], {}, floor_s=30)
    assert analysis["repetition"]["repeated_across_sections"], analysis["repetition"]
    assert "sentence_repeated_across_sections" in analysis["issues"]


def test_similar_openings_and_quay_lai_are_detected() -> None:
    sections = [
        {"role": "intro", "text": "Mình quay lại với phần giới thiệu giá nhé."},
        {"role": "recap", "text": "Mình quay lại với phần giá lần cuối."},
    ]
    analysis = m.analyze_script(sections, ["Giá live 299.000đ."], {}, floor_s=30)
    assert analysis["repetition"]["similar_openings"], analysis["repetition"]
    assert analysis["repetition"]["quay_lai_openings"], analysis["repetition"]
    assert "similar_openings" in analysis["issues"]


# ── Flatness ─────────────────────────────────────────────────────────────────


def test_flatness_sentence_length_stats() -> None:
    sections = [{"text": "aaaa bbbb cccc. dd. ee. " + "x" * 100 + "."}]
    analysis = m.analyze_script(sections, [], {}, floor_s=30)
    flatness = analysis["flatness"]
    lengths = [15, 3, 3, 101]  # chars per sentence as split
    assert flatness["sentence_count"] == 4
    assert flatness["avg_sentence_chars"] == pytest.approx(sum(lengths) / 4, rel=0.01)
    assert flatness["stdev_sentence_chars"] > 0


def test_flatness_interaction_ratio_and_max_gap() -> None:
    # Interactions at char 0 and far past the 90 s (1440 chars) target.
    filler = "y" * 1500
    text = "Bạn nào xem live nè. " + filler + " câu giữa. " + filler + " câu cuối bạn nào ơi."
    sections = [{"text": text}]
    analysis = m.analyze_script(sections, [], {}, floor_s=30, interaction_target_s=90)
    flatness = analysis["flatness"]
    assert flatness["interaction_count"] == 2
    assert flatness["interaction_ratio"] == pytest.approx(2 / 3, rel=0.01)
    assert flatness["max_interaction_gap_s"] > 90  # gap between the two interactions
    assert "interaction_gap_over_target" in analysis["issues"]


def test_no_interaction_at_all_is_an_issue() -> None:
    analysis = m.analyze_script([{"text": "chỉ nói suông thôi." * 20}], [], {}, floor_s=30)
    assert analysis["flatness"]["interaction_count"] == 0
    assert "no_interaction" in analysis["issues"]


def test_structure_type_count() -> None:
    sections = [
        {
            "text": (
                "Bạn nào da khô nè? "  # question
                "Hãy thử lấy 2–3 giọt mỗi tối nhé. "  # invitation
                "Giá live 299.000đ. "  # short affirmative (<60 chars)
                "Công thức có hyaluronic acid hai kích cỡ phân tử kết hợp chiết xuất lô hội, "
                "rau má và vitamin B5 giúp da mềm mượt. "  # long
            )
        }
    ]
    analysis = m.analyze_script(sections, ["Giá live 299.000đ.", "Lấy 2–3 giọt."], {}, floor_s=30)
    types = analysis["flatness"]["structure_types"]
    assert types.get("question") == 1
    assert types.get("invitation") == 1
    assert types.get("short_affirmative") == 1
    assert types.get("long") == 1
    assert analysis["flatness"]["structure_type_count"] == 4


# ── Fixtures (good / number fraud / repetition) ──────────────────────────────


def test_fixture_good_script_passes_all_metrics() -> None:
    analysis = _analyze(_load("good_script.json"))
    assert analysis["passed"], analysis["issues"]
    assert analysis["numbers"]["violation_count"] == 0
    assert analysis["buzzwords"]["hit_count"] == 0
    assert analysis["repetition"]["verbatim_main_recap"] == []
    assert analysis["repetition"]["similar_openings"] == []
    assert analysis["flatness"]["interaction_count"] >= 3
    assert (
        analysis["flatness"]["max_interaction_gap_s"]
        <= analysis["flatness"]["interaction_target_s"]
    )


def test_fixture_number_fraud_script_catches_written_weight_and_not_claimed_digits() -> None:
    doc = _load("number_fraud_script.json")
    analysis = _analyze(doc)
    numbers = analysis["numbers"]
    assert not numbers["digit_violations"]  # 0,38 kg and 0,84 kg digits are claimed
    assert len(numbers["written_violations"]) == 1
    violation = numbers["written_violations"][0]
    assert violation["value"] == "840" and violation["unit"] == "kg"
    assert violation["as_written"] == "tám trăm bốn mươi kg"
    assert not analysis["passed"]


def test_fixture_repetition_script_flags_verbatim_and_openings() -> None:
    analysis = _analyze(_load("repetition_script.json"))
    repetition = analysis["repetition"]
    assert len(repetition["verbatim_main_recap"]) == 1
    assert repetition["recap_sections"] == [2]
    assert repetition["similar_openings"]
    assert "verbatim_repeat_main_recap" in analysis["issues"]
    assert "similar_openings" in analysis["issues"]
    assert analysis["numbers"]["violation_count"] == 0  # only repetition is wrong here


# ── analyze CLI ──────────────────────────────────────────────────────────────


def test_analyze_cli_writes_report_and_prints_table(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    source = tmp_path / "doc.json"
    source.write_text(
        json.dumps(_load("number_fraud_script.json"), ensure_ascii=False), encoding="utf-8"
    )
    report = tmp_path / "report.json"
    exit_code = qa.main(["analyze", "--input", str(source), "--report", str(report)])
    out = capsys.readouterr().out
    assert "FAIL" in out and "PASS" not in out  # the single row fails
    assert "tám trăm bốn mươi" in out  # issue details name the written fraud
    assert exit_code == 1  # the fraud fixture must fail
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["schema"] == qa.REPORT_SCHEMA
    assert payload["summary"] == {
        "documents": 1,
        "passed": 0,
        "failed": 1,
        "regen_attempt_success_rate": None,  # plain script doc: no regen data
        "regen_clean_success_rate": None,
    }
    assert payload["documents"][0]["analysis"]["numbers"]["written_violations"]


def test_analyze_cli_handles_run_output_and_good_fixture(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    results = {
        "schema": qa.RESULTS_SCHEMA,
        "products": [
            {
                "product_id": "zz-serum-moc-lac-dewmist",
                "rounds": [
                    {"round": 1, "script": _load("good_script.json"), "gate": {"state": "passed"}},
                    {"round": 2, "script": _load("repetition_script.json")},
                ],
            }
        ],
    }
    source = tmp_path / "run.json"
    source.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    exit_code = qa.main(["analyze", "--input", str(source)])
    out = capsys.readouterr().out
    assert "zz-serum-moc-lac-dewmist r1" in out
    assert "zz-serum-moc-lac-dewmist r2" in out
    assert exit_code == 1  # r2 (repetition fixture) fails, r1 passes
    report = source.with_name(source.stem + "_report.json")
    assert json.loads(report.read_text(encoding="utf-8"))["summary"]["passed"] == 1


# ── products fixture ─────────────────────────────────────────────────────────


def test_products_fixture_has_five_products_with_expected_claim_counts() -> None:
    products = qa.load_products(
        Path(__file__).parents[2] / "scripts" / "qa" / "fixtures" / "products.json"
    )
    by_id = {p["id"]: p for p in products}
    assert len(products) == 5
    assert len(by_id) == 5
    assert len(by_id["zz-serum-moc-lac-dewmist"]["allowed_claims"]) >= 10
    assert len(by_id["zz-ao-khoac-hanoe-breeze"]["allowed_claims"]) >= 10
    assert len(by_id["zz-noi-chien-airchef"]["allowed_claims"]) >= 10
    assert len(by_id["zz-tra-gao-lung-rung-xanh"]["allowed_claims"]) == 6
    assert len(by_id["zz-may-xay-casa-blend"]["allowed_claims"]) == 20


def test_product_facts_payload_shape_matches_scripts_api() -> None:
    products = qa.load_products(
        Path(__file__).parents[2] / "scripts" / "qa" / "fixtures" / "products.json"
    )
    facts = qa.product_facts_payload(products[0])
    assert set(facts) == {"product_name", "prices", "discounts", "skus", "allowed_claims"}
    assert isinstance(facts["product_name"], str)
    assert all(
        isinstance(facts[key], list) for key in ("prices", "discounts", "skus", "allowed_claims")
    )


# ── run CLI guards ───────────────────────────────────────────────────────────


def test_dry_run_makes_no_network_call_and_redacts_token(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _forbid(*args, **kwargs):  # noqa: ANN002, ANN003 — any urllib use fails the test
        raise AssertionError("urllib.request.urlopen was called during --dry-run")

    monkeypatch.setattr(qa.urllib.request, "urlopen", _forbid)
    monkeypatch.setenv(qa._TOKEN_ENV, SECRET_TOKEN)
    exit_code = qa.main(
        [
            "run",
            "--base-url",
            "http://127.0.0.1:9",
            "--workspace",
            "ZZ-TEST-demo",
            "--rounds",
            "1",
            "--only",
            "zz-serum",
            "--dry-run",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert qa._REDACTED_TOKEN in captured.out
    assert "POST" in captured.out and "/api/v1/script-sets" in captured.out
    assert SECRET_TOKEN not in captured.out + captured.err


def test_run_refuses_workspace_not_starting_with_zz_test(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbid(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("network must not be touched when the workspace guard refuses")

    monkeypatch.setattr(qa.urllib.request, "urlopen", _forbid)
    monkeypatch.setenv(qa._TOKEN_ENV, SECRET_TOKEN)
    with pytest.raises(SystemExit) as excinfo:
        qa.main(["run", "--base-url", "http://127.0.0.1:9", "--workspace", "LIVE-CUSTOMER-01"])
    assert "ZZ-TEST" in str(excinfo.value)


def test_run_requires_token_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbid(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("network must not be touched without a token")

    monkeypatch.setattr(qa.urllib.request, "urlopen", _forbid)
    monkeypatch.delenv(qa._TOKEN_ENV, raising=False)
    with pytest.raises(SystemExit) as excinfo:
        qa.main(["run", "--base-url", "http://127.0.0.1:9"])
    assert qa._TOKEN_ENV in str(excinfo.value)


# ── run CLI: regen measurement (HTTP layer fully faked, still no network) ────


def test_run_regen_attempt_failure_records_error_code_and_rates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One regen attempt fails (workflow error_code), the next completes: the
    run must record both truthfully and report attempt/clean rates of 0.5 —
    the old "success = nothing left flagged" reading said 1.0 here."""
    workflow_polls = {"count": 0}

    def fake_request_json(base_url, method, path, payload, token, timeout_s=60.0):  # noqa: ANN001
        assert token == SECRET_TOKEN
        if method == "POST" and path == "/api/v1/script-sets":
            return 201, {"id": "set-1"}
        if method == "POST" and path.endswith("/generate-batch"):
            return 202, {"batch_id": "b-1"}
        if method == "GET" and "/generation-batches/" in path:
            return 200, {"status": "completed"}
        if method == "POST" and path.endswith("/regenerate"):
            assert payload["part_id"] == "p2"
            return 202, {"workflow_id": "wf-r1"}
        if method == "GET" and "/workflows/" in path:
            workflow_polls["count"] += 1
            if workflow_polls["count"] == 1:
                return 200, {
                    "status": "failed",
                    "error_code": "script_generation_failed",
                    "error_message": "boom",
                }
            return 200, {"status": "completed"}
        if method == "GET" and path.startswith("/api/v1/script-sets/set-1"):
            return 200, _fake_set_wire(workflow_polls["count"] >= 2)
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(qa, "request_json", fake_request_json)
    monkeypatch.setenv(qa._TOKEN_ENV, SECRET_TOKEN)
    out = tmp_path / "results.json"
    exit_code = qa.main(
        [
            "run",
            "--base-url",
            "http://127.0.0.1:9",
            "--workspace",
            "ZZ-TEST-fake",
            "--rounds",
            "1",
            "--only",
            "zz-serum-moc-lac-dewmist",
            "--regen-attempts",
            "3",
            "--out",
            str(out),
        ]
    )
    assert exit_code == 0  # a failed regen workflow is a recorded outcome, not an api_error
    results = json.loads(out.read_text(encoding="utf-8"))
    round_result = results["products"][0]["rounds"][0]
    attempts = round_result["regen"]["attempts"]
    assert len(attempts) == 2
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["error_code"] == "script_generation_failed"
    assert attempts[0]["error_message"] == "boom"
    assert attempts[0]["gate_state_after"] == "gate_failed"
    assert attempts[0]["gate_error_count_after"] == 1
    assert attempts[1]["status"] == "completed"
    assert attempts[1]["gate_state_after"] == "passed"
    assert "error_code" not in attempts[1]
    assert round_result["regen"]["still_needing"] == []
    summary = results["products"][0]["summary"]
    assert summary["regen_attempts_total"] == 2
    assert summary["regen_attempts_completed"] == 1
    assert summary["regen_attempt_success_rate"] == 0.5
    assert summary["regen_clean_success_rate"] == 0.5
    assert summary["regen_no_part_left_flagged"] is True
    assert SECRET_TOKEN not in out.read_text(encoding="utf-8")


def test_run_regen_trials_round_robin_hits_endpoint_exactly_n_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--regen-trials 3 on a 2-part product: exactly 3 sequential regenerate
    calls rotating segment_index 0 → 1 → 0 (with part_id), independent of the
    gate flags; both rates are 1.0."""
    parts = [
        {
            "part_id": "p1",
            "segment_index": 0,
            "source": "llm",
            "brief": {"role": "intro"},
            "display_text": (
                "Chào cả nhà, mình giới thiệu serum dưỡng ẩm Mộc Lạc Dewmist 30ml, giá niêm yết "
                "389.000đ, giá live 299.000đ, giảm 23% so với giá niêm yết."
            ),
        },
        {
            "part_id": "p2",
            "segment_index": 1,
            "source": "llm",
            "brief": {"role": "highlights"},
            "display_text": (
                "Gel lỏng trong suốt, thấm sau khoảng 30 giây, không bết dính. Cách dùng sau làm "
                "sạch và toner, lấy 2–3 giọt, vỗ lên mặt và cổ, sáng và tối. Bạn nào da khô thì "
                "thả bình luận cho mình tư vấn nhé."
            ),
        },
    ]
    regenerate_calls: list[tuple[str, dict]] = []

    def fake_request_json(base_url, method, path, payload, token, timeout_s=60.0):  # noqa: ANN001
        assert token == SECRET_TOKEN
        if method == "POST" and path == "/api/v1/script-sets":
            return 201, {"id": "set-1"}
        if method == "POST" and path.endswith("/generate-batch"):
            return 202, {"batch_id": "b-1"}
        if method == "GET" and "/generation-batches/" in path:
            return 200, {"status": "completed"}
        if method == "POST" and path.endswith("/regenerate"):
            regenerate_calls.append((path, payload))
            return 202, {"workflow_id": f"wf-{len(regenerate_calls)}"}
        if method == "GET" and "/workflows/" in path:
            return 200, {"status": "completed"}
        if method == "GET" and path.startswith("/api/v1/script-sets/set-1"):
            return 200, _fake_set_wire_custom(parts, {"state": "passed", "violations": []})
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(qa, "request_json", fake_request_json)
    monkeypatch.setenv(qa._TOKEN_ENV, SECRET_TOKEN)
    out = tmp_path / "results.json"
    exit_code = qa.main(
        [
            "run",
            "--base-url",
            "http://127.0.0.1:9",
            "--workspace",
            "ZZ-TEST-fake",
            "--rounds",
            "1",
            "--only",
            "zz-serum-moc-lac-dewmist",
            "--regen-trials",
            "3",
            "--out",
            str(out),
        ]
    )
    assert exit_code == 0
    assert len(regenerate_calls) == 3  # exactly N calls, nothing more
    indices = [
        int(path.rsplit("/segments/", 1)[1].split("/", 1)[0]) for path, _ in regenerate_calls
    ]
    assert indices == [0, 1, 0]  # round-robin over segment_index
    assert [payload.get("part_id") for _, payload in regenerate_calls] == ["p1", "p2", "p1"]
    results = json.loads(out.read_text(encoding="utf-8"))
    round_result = results["products"][0]["rounds"][0]
    assert round_result["regen"]["attempts"] == []  # no part was gate-flagged
    trials = round_result["regen_trials"]["trials"]
    assert [t["segment_index"] for t in trials] == [0, 1, 0]
    assert all(t["status"] == "completed" for t in trials)
    assert all(
        t["gate_state_after"] == "passed" and t["gate_error_count_after"] == 0 for t in trials
    )
    summary = results["products"][0]["summary"]
    assert summary["regen_trials_total"] == 3
    assert summary["regen_trials_completed"] == 3
    assert summary["regen_attempt_success_rate"] == 1.0
    assert summary["regen_clean_success_rate"] == 1.0
    assert SECRET_TOKEN not in out.read_text(encoding="utf-8")


def test_analyze_reports_regen_rates_in_summary_and_table(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    results = {
        "schema": qa.RESULTS_SCHEMA,
        "products": [
            {
                "product_id": "zz-demo",
                "rounds": [
                    {
                        "round": 1,
                        "script": _load("good_script.json"),
                        "regen": {
                            "attempts": [
                                {
                                    "segment_index": 1,
                                    "status": "completed",
                                    "gate_state_after": "passed",
                                    "gate_error_count_after": 0,
                                },
                                {
                                    "segment_index": 1,
                                    "status": "failed",
                                    "error_code": "script_generation_failed",
                                    "gate_state_after": "gate_failed",
                                    "gate_error_count_after": 2,
                                },
                            ]
                        },
                        "regen_trials": {
                            "trials": [
                                {
                                    "segment_index": 0,
                                    "status": "completed",
                                    "gate_state_after": "passed",
                                    "gate_error_count_after": 0,
                                }
                            ]
                        },
                    },
                    {"round": 2, "script": _load("good_script.json")},
                ],
            }
        ],
    }
    source = tmp_path / "run.json"
    source.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    report = tmp_path / "report.json"
    exit_code = qa.main(["analyze", "--input", str(source), "--report", str(report)])
    out = capsys.readouterr().out
    assert exit_code == 0
    summary = json.loads(report.read_text(encoding="utf-8"))["summary"]
    # 3 executions (2 attempts + 1 trial), 2 completed and gate-clean.
    assert summary["regen_attempt_success_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert summary["regen_clean_success_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert "67%" in out  # per-round rates appear in the summary table
    assert "—" in out  # round 2 recorded no regen executions


# ── run CLI real flow (HTTP layer fully faked, still no network) ─────────────


def _fake_set_wire(regenerated: bool) -> dict:
    base_part = {
        "part_id": "p1",
        "segment_index": 0,
        "source": "llm",
        "brief": {"role": "intro"},
        "display_text": (
            "Chào cả nhà, mình giới thiệu serum dưỡng ẩm Mộc Lạc Dewmist 30ml, giá niêm yết "
            "389.000đ, giá live 299.000đ, giảm 23% so với giá niêm yết."
        ),
    }
    flagged = {
        "part_id": "p2",
        "segment_index": 1,
        "source": "fallback",
        "fallback_reason_code": "claim_factual",
        "brief": {"role": "highlights"},
        "display_text": (
            "Gel lỏng trong suốt, thấm sau khoảng 30 giây, không bết dính. Cách dùng sau làm "
            "sạch và toner, lấy 2–3 giọt, vỗ lên mặt và cổ, sáng và tối. Bạn nào da khô thì "
            "thả bình luận cho mình tư vấn nhé."
        ),
        "needs_regeneration": True,
    }
    parts = [base_part, flagged]
    if regenerated:
        parts[1] = {
            **flagged,
            "source": "llm",
            "fallback_reason_code": None,
            "needs_regeneration": False,
        }
    return {
        "id": "set-1",
        "items": {
            "zz-serum-moc-lac-dewmist": {
                "state": "REVIEWABLE",
                "current_version": {"parts": parts},
                "gate": (
                    {"state": "passed", "violations": []}
                    if regenerated
                    else {
                        "state": "gate_failed",
                        "violations": [
                            {
                                "rule_id": "CLAIM_FACTUAL",
                                "severity": "error",
                                "message": "số không có trong claims",
                            }
                        ],
                    }
                ),
            }
        },
    }


def _fake_set_wire_custom(parts: list[dict], gate: dict) -> dict:
    return {
        "id": "set-1",
        "items": {
            "zz-serum-moc-lac-dewmist": {
                "state": "REVIEWABLE",
                "current_version": {"parts": parts},
                "gate": gate,
            }
        },
    }


def test_run_real_flow_with_faked_http_writes_results_without_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"regenerated": False}

    def fake_request_json(base_url, method, path, payload, token, timeout_s=60.0):  # noqa: ANN001
        assert token == SECRET_TOKEN  # the token reaches the header layer only
        if method == "POST" and path == "/api/v1/script-sets":
            assert payload["name"].startswith("ZZ-TEST-")
            assert payload["brief"]["product_facts"]["zz-serum-moc-lac-dewmist"]["allowed_claims"]
            return 201, {"id": "set-1"}
        if method == "POST" and path.endswith("/generate-batch"):
            assert payload["ordered_units"] is True
            return 202, {"batch_id": "b-1", "status": "queued"}
        if method == "GET" and "/generation-batches/" in path:
            return 200, {"status": "completed"}
        if method == "POST" and path.endswith("/regenerate"):
            state["regenerated"] = True
            return 202, {"workflow_id": "wf-2", "status": "queued"}
        if method == "GET" and "/workflows/" in path:
            return 200, {"status": "completed"}
        if method == "GET" and path.startswith("/api/v1/script-sets/set-1"):
            return 200, _fake_set_wire(state["regenerated"])
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(qa, "request_json", fake_request_json)
    monkeypatch.setenv(qa._TOKEN_ENV, SECRET_TOKEN)
    out = tmp_path / "results.json"
    exit_code = qa.main(
        [
            "run",
            "--base-url",
            "http://127.0.0.1:9",
            "--workspace",
            "ZZ-TEST-fake",
            "--rounds",
            "1",
            "--only",
            "zz-serum-moc-lac-dewmist",
            "--out",
            str(out),
        ]
    )
    assert exit_code == 0
    results = json.loads(out.read_text(encoding="utf-8"))
    assert SECRET_TOKEN not in out.read_text(encoding="utf-8")
    assert results["schema"] == qa.RESULTS_SCHEMA
    assert results["workspace"] == "ZZ-TEST-fake"
    round_result = results["products"][0]["rounds"][0]
    assert round_result["set_id"] == "set-1"
    assert round_result["workflow"]["status"] == "completed"
    assert round_result["gate"]["state"] == "passed"
    assert round_result["regen"]["success"] is True
    assert len(round_result["regen"]["attempts"]) == 1
    attempt = round_result["regen"]["attempts"][0]
    assert attempt["segment_index"] == 1
    assert attempt["part_id"] == "p2"
    assert attempt["status"] == "completed"
    assert attempt["gate_state_after"] == "passed"
    assert attempt["gate_error_count_after"] == 0
    assert round_result["script"]["sections"][1]["text"].startswith("Gel lỏng")
    metrics = round_result["metrics"]
    assert metrics["totals"]["sections"] == 2
    assert metrics["numbers"]["violation_count"] == 0
    summary = results["products"][0]["summary"]
    assert summary["regen_no_part_left_flagged"] is True
    assert summary["gate_passed_all_rounds"] is True
    assert summary["regen_attempts_total"] == 1
    assert summary["regen_attempt_success_rate"] == 1.0
    assert summary["regen_clean_success_rate"] == 1.0
    assert summary["regen_trials_total"] == 0  # --regen-trials not requested
    assert SECRET_TOKEN not in out.read_text(encoding="utf-8")


def test_run_records_api_errors_and_reports_exit_code_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_request_json(base_url, method, path, payload, token, timeout_s=60.0):  # noqa: ANN001
        if method == "POST" and path == "/api/v1/script-sets":
            return 409, {"error": {"code": "stale_revision", "message": "x"}}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(qa, "request_json", fake_request_json)
    monkeypatch.setenv(qa._TOKEN_ENV, SECRET_TOKEN)
    out = tmp_path / "results.json"
    exit_code = qa.main(
        [
            "run",
            "--base-url",
            "http://127.0.0.1:9",
            "--workspace",
            "ZZ-TEST-fake",
            "--rounds",
            "1",
            "--only",
            "zz-serum-moc-lac-dewmist",
            "--out",
            str(out),
        ]
    )
    assert exit_code == 3
    round_result = json.loads(out.read_text(encoding="utf-8"))["products"][0]["rounds"][0]
    assert round_result["api_errors"][0]["status"] == 409
    assert round_result["set_id"] is None
