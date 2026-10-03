"""Usage text to dose times, ROC dates, dose forms and identifier removal (app/services/ocr_parsing.py)."""

import datetime

import pytest

from app.services import ocr_parsing as parsing

TODAY = datetime.date(2026, 10, 4)
M, N, E, B, BEFORE, AFTER = "morning", "noon", "night", "bedtime", "before_meals", "after_meals"


def _on(result):
    return {slot for slot, on in result["schedule"].items() if on}


# ── frequency text → dose times ──

@pytest.mark.parametrize("text, times, slots", [
    # 一天/一日/每日/每天 N 次, Arabic, full-width or Chinese numerals: 1 morning, 2 + night, 3 + noon, 4 + bedtime.
    ("一天3次", 3, {M, N, E}),
    ("一天2次", 2, {M, E}),
    ("一日一次", 1, {M}),
    ("每日四次", 4, {M, N, E, B}),
    ("每天１次", 1, {M}),
    ("1日2次", 2, {M, E}),
    ("每日 服用 3 次", 3, {M, N, E}),
    # Slot words win; a count adds what they leave out.
    ("每日兩次 早晚飯後", 2, {M, E, AFTER}),
    ("每天三次 三餐飯前", 3, {M, N, E, BEFORE}),
    ("每日１次 睡前", 1, {B}),
    ("一天2次 睡前", 2, {M, B}),
    ("早中晚", 3, {M, N, E}),
    ("早、晚各一次", 2, {M, E}),
    ("三餐飯後及睡前", 4, {M, N, E, B, AFTER}),
    ("晚餐後", 1, {E, AFTER}),
    ("早上空腹", 1, {M, BEFORE}),
    # Latin abbreviations, also run together.
    ("QD", 1, {M}),
    ("BID", 2, {M, E}),
    ("TID PC", 3, {M, N, E, AFTER}),
    ("TIDPC", 3, {M, N, E, AFTER}),
    ("QID", 4, {M, N, E, B}),
    ("QIDACHS", 4, {M, N, E, B, BEFORE}),
    ("HS", 1, {B}),
    ("QPM", 1, {E}),
    # Every N hours: by doses per day.
    ("Q12H", 2, {M, E}),
    ("Q8H", 3, {M, N, E}),
    ("Q6H", 4, {M, N, E, B}),
    ("每8小時一次", 3, {M, N, E}),
    ("每十二小時", 2, {M, E}),
    # English.
    ("twice daily after meals", 2, {M, E, AFTER}),
    ("3 times a day", 3, {M, N, E}),
    ("Take one tablet every morning", 1, {M}),
    ("at bedtime", 1, {B}),
    # 晚 right before 睡前 says when the bedtime dose is: one dose, not an evening one as well.
    ("每晚睡前", 1, {B}),
    ("晚上睡前", 1, {B}),
    ("夜間睡前", 1, {B}),
    ("每晚睡前服用一次", 1, {B}),
    ("一天1次 晚上睡前", 1, {B}),
    ("每晚睡覺前", 1, {B}),
    ("一天1次 晚上 睡前", 1, {B}),           # more named times than the count: evening and bedtime are one dose
    # ...but a list keeps every time it names.
    ("早上、晚上、睡前", 3, {M, E, B}),
    ("晚上、睡前", 2, {E, B}),
    ("早晚睡前", 3, {M, E, B}),
    # Dotted Latin abbreviations.
    ("t.i.d.", 3, {M, N, E}),
    ("b.i.d. p.c.", 2, {M, E, AFTER}),
    ("Q.D.", 1, {M}),
    ("h.s.", 1, {B}),
    ("q.6.h.", 4, {M, N, E, B}),
    # A daily amount without 次 is once a day; 分N次 splits a day's amount.
    ("每天1粒", 1, {M}),
    ("每日一顆", 1, {M}),
    ("每日2粒 分2次", 2, {M, E}),
])
def test_frequency_text_gives_dose_times(text, times, slots):
    result = parsing.parse_frequency(text)
    assert (result["times_per_day"], _on(result)) == (times, slots)
    assert result["as_needed"] is False and result["custom_times"] == []
    assert result["max_per_day"] is None


@pytest.mark.parametrize("text, times, most, slots", [
    ("一天3-4次", 3, 4, {M, N, E}),
    ("每日3至4次", 3, 4, {M, N, E}),
    ("一天2～3次 飯後", 2, 3, {M, E, AFTER}),
    ("一日三到四次", 3, 4, {M, N, E}),
    ("2-3 times a day", 2, 3, {M, E}),
])
def test_a_count_range_is_scheduled_at_its_lower_end_and_allowed_to_its_upper(text, times, most, slots):
    result = parsing.parse_frequency(text)
    assert (result["times_per_day"], result["max_per_day"], _on(result)) == (times, most, slots)


@pytest.mark.parametrize("text", ["QOD", "Q.O.D.", "qod 飯後"])
def test_every_other_day_has_an_interval_and_no_daily_times(text):
    result = parsing.parse_frequency(text)
    assert result["interval_hours"] == 48 and result["times_per_day"] is None
    assert not _on(result) - {AFTER}


@pytest.mark.parametrize("text", ["1.5", "F.C.", "e.g. as directed"])
def test_dots_that_are_not_usage_codes_stay(text):
    assert parsing.parse_frequency(text)["times_per_day"] is None


def test_more_than_four_a_day_adds_custom_times():
    q4h = parsing.parse_frequency("Q4H")
    assert (q4h["times_per_day"], q4h["interval_hours"]) == (6, 4)
    assert _on(q4h) == {M, N, E, B} and q4h["custom_times"] == ["16:00", "06:00"]
    assert parsing.parse_frequency("一天5次")["custom_times"] == ["16:00"]
    slots, custom = parsing.slots_for_count(12)
    assert sum(slots.values()) + len(custom) == 8          # the medication API's limit


@pytest.mark.parametrize("text, times", [("需要時", None), ("必要時", None), ("PRN", None), ("疼痛時服用", None),
                                         ("必要時 一天最多3次", 3), ("Q6H PRN", 4), ("as needed", None)])
def test_as_needed_gets_no_fixed_times(text, times):
    result = parsing.parse_frequency(text)
    assert result["as_needed"] is True
    assert result["times_per_day"] == times and _on(result) == set()


@pytest.mark.parametrize("text", ["", "N/A", "Use as directed", "OTOMYX OTIC DROPS", "每次1顆", "飯後"])
def test_text_without_times_gives_none(text):
    result = parsing.parse_frequency(text)
    assert result["times_per_day"] is None
    assert not _on(result) - {AFTER}


def test_meal_marks_alone_are_still_schedule_information():
    result = parsing.parse_frequency("飯後")
    assert _on(result) == {AFTER} and parsing.has_schedule(result["schedule"])
    assert not parsing.has_schedule({slot: False for slot in parsing.SLOTS})
    assert parsing.has_schedule({}, ["13:00"])


@pytest.mark.parametrize("text, number", [("3", 3), ("３", 3), ("三", 3), ("兩", 2), ("十", 10), ("十二", 12),
                                          ("二十四", 24), ("", None), ("次", None), ("十十", None)])
def test_chinese_numbers(text, number):
    assert parsing.cn_number(text) == number


@pytest.mark.parametrize("text, days", [("每日3次 共7天", 7), ("七天份", 7), ("for 14 days", 14), ("一天3次", None)])
def test_days_written_in_usage_text(text, days):
    assert parsing.days_from_text(text) == days


# ── ROC dates ──

@pytest.mark.parametrize("text", ["1150928", "115/09/28", "115.09.28", "115.9.28", "115-09-28", "115年9月28日",
                                  "115年09月28日", "就醫日:1150928", "2026-09-28", "2026/9/28", "20260928"])
def test_roc_and_gregorian_dates_become_iso(text):
    assert parsing.normalize_date(text, "past", TODAY) == "2026-09-28"


@pytest.mark.parametrize("text, kind, expected", [
    ("0650101", "past", None),          # 50 years ago: a birth date, never a dispensing date
    ("1100101", "past", None),          # more than 5 years ago
    ("1151231", "past", None),          # dispensed in the future
    ("1151231", "until", "2026-12-31"),  # an expiry may be ahead
    ("1251231", "until", None),         # 10 years on: misread
    ("1151340", "past", None),          # no such month
    ("1150230", "past", None),          # no such day
    ("113.09.28", "past", "2024-09-28"),
    ("N/A", "past", None),
    ("", "until", None),
    ("15", "past", None),
])
def test_implausible_dates_are_dropped(text, kind, expected):
    assert parsing.normalize_date(text, kind, TODAY) == expected


# ── dose forms and amounts ──

@pytest.mark.parametrize("name, unit, other, form, dose_form", [
    ("VITACOBAL CAPSULES 0", "錠", "", "capsule", "solid_oral"),       # the name wins over the unit
    ("GASTRO F.C. TABLETS", "TAB", "", "tablet", "solid_oral"),
    ("Amlodipine 5mg (降壓錠)", "", "", "tablet", "solid_oral"),
    ("Allegra 60mg", "", "每次1顆", "tablet", "solid_oral"),
    ("OTOMYX OTIC DROPS.", "瓶", "", "ear_drops", "other"),
    ("CLEARVIEW EYE DROPS", "瓶", "", "eye_drops", "other"),
    ("HYDROCORT OINTMENT", "支", "", "topical", "topical"),
    ("皮膚藥膏", "條", "", "topical", "topical"),
    ("COUGH SYRUP", "瓶", "", "oral_liquid", "liquid"),
    ("SALBU INHALER", "支", "", "inhaler", "inhaler"),
    ("INSULIN INJ", "支", "", "injection", "injection"),
    ("GRANULES", "包", "", "powder", "other"),
    ("SOMETHING", "瓶", "", "unknown", "other"),
    ("CLEARSIGHT OPH SOLN", "瓶", "", "eye_drops", "other"),     # an eye solution is not an oral liquid
    ("CLEARSIGHT 點眼液", "瓶", "", "eye_drops", "other"),
    ("CALMFLORA 陰道錠", "錠", "", "suppository", "other"),       # a vaginal tablet is not swallowed
    ("CALMFLORA VAGINAL TABLETS", "TAB", "", "suppository", "other"),
])
def test_dose_form_guess(name, unit, other, form, dose_form):
    assert parsing.guess_form(name, unit, other) == form
    assert parsing.FORM_TO_DOSE_FORM[form] == dose_form


def test_only_tablets_and_capsules_are_solid_oral():
    solid = {form for form, dose_form in parsing.FORM_TO_DOSE_FORM.items() if dose_form == "solid_oral"}
    assert solid == {"tablet", "capsule"}


@pytest.mark.parametrize("text, units", [("每次1顆", 1.0), ("1.5錠", 1.5), ("半顆", 0.5), ("一顆半", 1.5),
                                         ("1/2 TAB", 0.5), ("每次一顆", 1.0), ("2 TAB", 2.0), ("1", 1.0),
                                         ("每日3次 每次2顆", 2.0), ("每次2滴", None), ("5ml", None), ("N/A", None),
                                         ("", None), ("一天3次", None), ("每次半顆", 0.5), ("每次半", 0.5),
                                         ("1粒半", 1.5), ("每次1/2顆", 0.5), ("每日2粒 分2次", 1.0),
                                         # 半小時 and 7點半 are times, not half a tablet.
                                         ("一天3次 飯前半小時", None), ("每次1顆 飯前半小時", 1.0),
                                         ("飯後半小時 每次1顆", 1.0), ("早上7點半", None), ("9/28起", None)])
def test_units_per_dose(text, units):
    assert parsing.units_per_dose(text) == units


@pytest.mark.parametrize("count, times, days, per_dose", [(15, 3, 5, 1.0), (14, 2, 7, 1.0), (10, 2, 10, 0.5),
                                                          (1, 2, 5, None), (16, 3, 5, None), (None, 3, 5, None)])
def test_per_dose_from_totals(count, times, days, per_dose):
    assert parsing.per_dose_from_totals(count, times, days) == per_dose


# ── identifiers and names ──

@pytest.mark.parametrize("text, cleaned", [
    ("身分證:B21****567", ""),
    ("身份證字號 A123456789", ""),
    ("王小華 A123456789", "王小華"),
    ("王小華 B21****567", "王小華"),
    ("生日:0650101 王小華", "王小華"),
    ("出生日期 65/01/01", ""),
    ("AC12345100", "AC12345100"),                  # an NHI drug code, not an ID
    ("VITACOBAL CAPSULES 0", "VITACOBAL CAPSULES 0"),
    ("病歷號:A123456", "病歷號:A123456"),
    # A mask a character longer or shorter, as a model may copy it.
    ("王小華 B21*****5678", "王小華"),
    ("B21***567", ""),
    ("B21XXXXX67", ""),
    ("ID No. A123456789", ""),
    ("DOB: 1976-01-01", ""),
    ("生日 民國65年1月1日", ""),
    ("A043298100", "A043298100"),                  # one letter + 9 digits starting 0: not an ID shape
    ("BC*****123", ""),                            # an older two-letter resident certificate, masked
    ("BC12345100", "BC12345100"),                  # the same shape unmasked: an NHI drug code
])
def test_identifiers_are_stripped(text, cleaned):
    assert parsing.strip_identifiers(text) == cleaned


@pytest.mark.parametrize("text", [
    # Medicine names and warnings that contain the letters of a label must come back unchanged.
    "CALCIUM DOBESILATE 500MG", "Doxium (Calcium Dobesilate) 500mg", "Dobutamine 250mg INJ",
    "TRANEXAMIC ACID NORMAL", "ACID number 5", "Avoid nonsteroidal anti-inflammatory drugs",
    "Avoid normal saline", "Take with fluid no less than 200ml", "Valid number of refills: 2",
    "孕婦及新生兒出生後請勿使用", "出生", "身分證", "生日禮物",
])
def test_medicine_text_is_never_taken_for_an_identifier(text):
    assert parsing.strip_identifiers(text) == text


@pytest.mark.parametrize("value, name", [("王大文", "王大文"), ("Dr. Chen", "Dr. Chen"), ("3300110022", ""),
                                         ("藥事費", ""), ("藥事陳", ""), ("安心藥局", ""), ("N/A", ""), ("", "")])
def test_person_names(value, name):
    assert parsing.person_name(value) == name


@pytest.mark.parametrize("value, name", [("臺北市立聯合醫院", "臺北市立聯合醫院"), ("安心藥局藥品明細收據", "安心藥局"),
                                         ("臺北市立聯合醫院 門診處方箋", "臺北市立聯合醫院"), ("S945001122", ""),
                                         ("3300110022", ""), ("N/A", ""),
                                         # Codes, labels, addresses and phones are not part of a name.
                                         ("健保代碼:S945001122", ""), ("S945001122 安心藥局", "安心藥局"),
                                         ("健保代碼: 安心藥局", "安心藥局"),
                                         ("安心藥局 健保代碼:5945001122", "安心藥局"),
                                         ("開立處方箋之醫院(診所)/醫師:3300110022", ""),
                                         ("安心藥局 地址:臺北市中正路1號", "安心藥局"), ("地址:臺北市中正路1號", ""),
                                         ("安心藥局 電話:02-23456789", "安心藥局")])
def test_institution_names(value, name):
    assert parsing.institution_name(value) == name
