"""The Scan page's save rules in Python (services/scan_rules.py), held equal to frontend_source/src/lib/scan.ts by
the golden file tests/scan_parity.json. scan.unit.ts checks the same file against scan.ts; after a change to scan.ts,
regenerate it with `cd frontend_source && node --experimental-strip-types tests/scan_parity.gen.ts`."""

import json
from pathlib import Path

import pytest

from app.services import scan_rules

GOLDEN = json.loads((Path(__file__).parent / "scan_parity.json").read_text(encoding="utf-8"))["cases"]


def as_json(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


@pytest.mark.parametrize("case", GOLDEN, ids=[case["name"] for case in GOLDEN])
def test_golden_cases_match_scan_ts(case):
    today, result = case["today"], case["result"]
    drafts = scan_rules.mark_duplicates(
        [scan_rules.draft_from(med, result, today) for med in scan_rules.medicines_of(result)], case["active_names"])
    expected = case["expected"]
    assert as_json([{k: v for k, v in draft.items() if k != "source"} for draft in drafts]) == expected["drafts"]
    assert [scan_rules.draft_problem(draft, today) for draft in drafts] == expected["problems"]
    assert as_json([scan_rules.medication_payload(draft, result) for draft in drafts]) == expected["payloads"]


def test_golden_payloads_print_numbers_as_js_does():
    """15 is 15 in the JSON, never 15.0, as JSON.stringify writes it."""
    for case in GOLDEN:
        result = case["result"]
        drafts = [scan_rules.draft_from(med, result, case["today"]) for med in scan_rules.medicines_of(result)]
        for draft, expected in zip(drafts, case["expected"]["payloads"]):
            assert json.dumps(scan_rules.medication_payload(draft, result), ensure_ascii=False, sort_keys=True) \
                == json.dumps(expected, ensure_ascii=False, sort_keys=True)


def test_js_rounding():
    assert scan_rules._js_round(2.5) == 3                  # Python's round(2.5) is 2
    assert scan_rules._js_round(-2.5) == -2
    assert scan_rules._js_round(0.49999999999999994) == 0  # floor(x + 0.5) would give 1
    assert scan_rules._round2(1.005) == 1                  # Math.round(1.005*100)/100: 1.005*100 is 100.49999999999999
    assert scan_rules._round2(0.125) == 0.13               # round(12.5) would give 12


def test_numbers_print_as_js_string_does():
    cases = [(15, "15"), (15.0, "15"), (2.5, "2.5"), (1.005, "1.005"), (0.000001, "0.000001"), (1e-7, "1e-7"),
             (1e21, "1e+21"), (1.5e22, "1.5e+22"), (123456789012345680000.0, "123456789012345680000"), (-0.0, "0"),
             (-2.5, "-2.5"), (True, "true"), ("x", "x")]
    for value, text in cases:
        assert scan_rules._js_str(value) == text


def test_strings_become_numbers_as_js_number_does():
    assert scan_rules._js_number(" 12 ") == 12
    assert scan_rules._js_number("") == 0
    assert scan_rules._js_number("1e3") == 1000
    assert scan_rules._js_number(".5") == 0.5
    assert scan_rules._js_number("0x10") == 16
    assert scan_rules._js_number("-Infinity") == float("-inf")
    for text in ("1_000", "abc", "1,5", "inf", "nan", "١٢"):
        assert scan_rules._js_number(text) != scan_rules._js_number(text)   # NaN, where float() would accept some


def test_absent_stock_differs_from_null():
    result = {"date_dispensed": "2026-10-03", "use_before": "N/A"}
    tablet = {"med_name": "A", "pill_count": 15, "dose_form": "solid_oral", "days": 5,
              "schedule_time": {"morning": True}}
    assert scan_rules.draft_from(tablet, result, "2026-10-04")["stock"] == "15"
    assert scan_rules.draft_from({**tablet, "stock": None}, result, "2026-10-04")["stock"] == ""
    assert scan_rules.draft_from({**tablet, "dose_form": "liquid"}, result, "2026-10-04")["stock"] == ""


def test_impossible_date_rolls_over_like_js():
    assert scan_rules._add_days("2026-02-30", 0) == "2026-03-02"
    assert scan_rules._add_days("2026-13-01", 0) == "2027-01-01"
    assert scan_rules._add_days("2026-12-30", 4) == "2027-01-03"
    assert scan_rules._add_days("2026-10-03", 0.5) == "2026-10-03"   # JS truncates the day
    assert scan_rules._add_days("9999-12-31", 1) == "10000-01-01"    # past Python's date range
    assert scan_rules._add_days("0050-01-01", 0) == "1950-01-01"     # new Date(50, 0, 1) is in 1950
    assert scan_rules.last_day_for("2026-09-28", 5, None) == "2026-10-02"
    assert scan_rules.last_day_for("2026-09-28", None, "2027-03-31") == "2027-03-31"
    assert scan_rules.last_day_for("2026-09-28", 0, "N/A") == ""


def test_dates_are_ascii_and_whole():
    """JS's \\d is ASCII and its $ is the end; Python's \\d takes other digits and its $ allows a final newline."""
    assert scan_rules.course_start({"date_dispensed": "２０２６-10-03", "visit_date": "2026-10-03\n"}, "T") == "T"
    assert scan_rules.course_start({"date_dispensed": "N/A", "visit_date": "2026-10-03"}, "T") == "2026-10-03"


def test_same_name_uses_js_whitespace():
    assert scan_rules.same_name("Gastro F.C. tablets", "GASTRO FC TABLETS")
    assert scan_rules.same_name("ＧＡＳＴＲＯ\u3000ＦＣ", "gastro f.c.")
    assert scan_rules.same_name("VITA\ufeffCOBAL", "vitacobal")             # \ufeff is \s in JS, not in Python
    assert not scan_rules.same_name("VITA\x1cCOBAL", "vitacobal")           # \x1c is \s in Python, not in JS
    assert not scan_rules.same_name("", "")
    assert not scan_rules.same_name(" . ", "")


def test_medicines_of_falls_back_to_the_top_level():
    result = {"med_name": "TOP", "medications": []}
    assert scan_rules.medicines_of(result) == [result]
    assert scan_rules.medicines_of({**result, "medications": [{"med_name": "A"}]}) == [{"med_name": "A"}]
