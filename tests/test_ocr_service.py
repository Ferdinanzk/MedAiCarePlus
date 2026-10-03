import asyncio
import datetime
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import cv2
import numpy as np
import pytest
import requests

from app import config
from app.config import OCR_GEMINI_FALLBACK_MODEL, OCR_GEMINI_TIMEOUT, OCR_MODEL
from app.services.ocr_service import OCRService, OCRServiceError

REPO = Path(__file__).resolve().parent.parent
SLOTS = ("morning", "noon", "night", "bedtime", "before_meals", "after_meals")
LEGACY_KEYS = {
    "med_name", "dosage", "quantity", "pill_count", "amount_each_intake", "total_intake", "schedule_time",
    "instructions", "warning", "pill_description", "intake_time_label", "clinical_uses", "manufacturer",
    "hospital", "prescription_no", "use_before", "physician", "pharmacist", "patient_name", "date_dispensed",
}
RESULT_KEYS = LEGACY_KEYS | {"pharmacy", "visit_date", "document_type", "medications"}
MEDICINE_KEYS = {
    "med_name", "dosage", "quantity", "pill_count", "unit", "frequency_text", "instructions", "times_per_day",
    "max_per_day", "interval_hours", "days", "amount_each_intake", "units_per_dose", "form", "dose_form", "as_needed",
    "schedule_time", "schedule_source", "stock", "total_intake", "intake_time_label", "warning", "pill_description",
    "clinical_uses", "manufacturer",
}


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.body = body

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _image():
    return np.full((80, 120, 3), 255, dtype=np.uint8)


def _jpeg():
    ok, encoded = cv2.imencode(".jpg", _image())
    assert ok
    return encoded.tobytes()


def _vision_response(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def _service(model=OCR_MODEL):
    service = OCRService.__new__(OCRService)
    service.gemini_api_key = "test-key"
    service.active_model = model
    return service


def _ollama_service(model="llava:7b"):
    service = OCRService.__new__(OCRService)
    service.gemini_api_key = ""
    service.active_model = model
    return service


def _scan_ready(monkeypatch, service):
    """Skip the image clean-up so a test drives only the model answers."""
    monkeypatch.setattr(OCRService, "_available", True)
    monkeypatch.setattr(service, "_warp_with_yolo", lambda img: None)
    monkeypatch.setattr(service, "_enhance", lambda img: img)
    monkeypatch.setattr(service, "_crop_icon_row", lambda img: img)
    return service


# ── defaults that every installation gets ──

def _config_in_clean_env(**env):
    """app.config as a fresh process sees it, with no OCR settings except `env`."""
    clean = {key: value for key, value in os.environ.items()
             if not key.startswith("OCR_") and key != "GEMINI_API_KEY"}
    clean.update(env)
    probe = ("import json, app.config as c; print(json.dumps([c.OCR_MODEL, c.OCR_GEMINI_FALLBACK_MODEL, "
             "c.OCR_GEMINI_TIMEOUT, c.OCR_GEMINI_IMAGE_BUDGET, c.GEMINI_API_KEY]))")
    out = subprocess.run([sys.executable, "-c", probe], cwd=REPO, env=clean, capture_output=True, text=True,
                         check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_code_defaults_are_the_models_that_work():
    """gemini-3.8-flash answered 503 'high demand' on every scan on 3-4 Oct 2026; don't let the default drift back.

    gemini-3.5-flash goes first since it read a real receipt more accurately than flash-lite (docs/OCR.md); it took
    up to 11.8 s, so one request may take 25 s and the fallback still has 20 s of the 45 s budget.
    """
    assert config.OCR_DEFAULT_MODEL == "gemini-3.5-flash"
    assert config.OCR_DEFAULT_FALLBACK_MODEL == "gemini-3.5-flash-lite"
    assert _config_in_clean_env() == ["gemini-3.5-flash", "gemini-3.5-flash-lite", 25, 45, ""]


def test_blank_settings_keep_the_defaults_and_a_blank_fallback_turns_it_off():
    assert _config_in_clean_env(OCR_MODEL=" ", OCR_GEMINI_FALLBACK_MODEL="", OCR_GEMINI_TIMEOUT="",
                                OCR_GEMINI_IMAGE_BUDGET="", GEMINI_API_KEY=" k ") == \
        ["gemini-3.5-flash", "", 25, 45, "k"]


def test_env_example_names_the_same_models_and_ships_no_key():
    settings = dict(
        line.split("=", 1) for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    assert settings["OCR_MODEL"] == config.OCR_DEFAULT_MODEL
    assert settings["OCR_GEMINI_FALLBACK_MODEL"] == config.OCR_DEFAULT_FALLBACK_MODEL
    assert settings["GEMINI_API_KEY"] == ""


# ── Gemini requests and the fallback ──

@pytest.mark.parametrize("retry_status", [429, 500, 502, 503, 504])
def test_gemini_transient_failure_retries_once_on_confirmed_fallback(monkeypatch, retry_status):
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return FakeResponse(retry_status, {"error": {"status": "UNAVAILABLE"}})
        return FakeResponse(200, _vision_response('{"medication_name":"Sample"}'))

    monkeypatch.setattr("app.services.ocr_service.requests.post", post)
    result = _service()._call_gemini("prompt", _image())

    assert result == {"medication_name": "Sample"}
    assert len(calls) == 2
    assert calls[0].endswith(f"/{OCR_MODEL}:generateContent")
    assert calls[1].endswith(f"/{OCR_GEMINI_FALLBACK_MODEL}:generateContent")


def test_gemini_timeout_retries_once_on_fallback(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise requests.Timeout()
        return FakeResponse(200, _vision_response('{"medication_name":"Sample"}'))

    monkeypatch.setattr("app.services.ocr_service.requests.post", post)
    result = _service()._call_gemini("prompt", _image())
    assert result == {"medication_name": "Sample"}
    assert len(calls) == 2


def test_a_missing_primary_model_falls_back(monkeypatch):
    """A retired or renamed model answers 404: the same kind of outage as the 3-4 Oct incident."""
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return FakeResponse(404, {"error": {"status": "NOT_FOUND"}})
        return FakeResponse(200, _vision_response('{"medication_name":"Sample"}'))

    monkeypatch.setattr("app.services.ocr_service.requests.post", post)
    assert _service("gemini-retired")._call_gemini("prompt", _image()) == {"medication_name": "Sample"}
    assert calls[1].endswith(f"/{OCR_GEMINI_FALLBACK_MODEL}:generateContent")


@pytest.mark.parametrize("status, code", [(503, "ocr_provider_busy"), (404, "ocr_model_not_found")])
def test_both_models_failing_reports_the_fallback_error(monkeypatch, status, code):
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        return FakeResponse(status, {"error": {}})

    monkeypatch.setattr("app.services.ocr_service.requests.post", post)
    with pytest.raises(OCRServiceError) as caught:
        _service()._call_gemini("prompt", _image())
    assert caught.value.code == code and caught.value.status_code in (502, 503)
    assert len(calls) == 2


def test_no_retry_when_the_primary_is_the_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr("app.services.ocr_service.requests.post",
                        lambda url, **kwargs: calls.append(url) or FakeResponse(503, {}))
    with pytest.raises(OCRServiceError) as caught:
        _service(OCR_GEMINI_FALLBACK_MODEL)._call_gemini("prompt", _image())
    assert caught.value.code == "ocr_provider_busy" and len(calls) == 1


def test_a_blank_fallback_is_never_requested(monkeypatch):
    calls = []
    monkeypatch.setattr("app.services.ocr_service.OCR_GEMINI_FALLBACK_MODEL", "")
    monkeypatch.setattr("app.services.ocr_service.requests.post",
                        lambda url, **kwargs: calls.append(url) or FakeResponse(503, {}))
    with pytest.raises(OCRServiceError) as caught:
        _service()._call_gemini("prompt", _image())
    assert caught.value.code == "ocr_provider_busy"
    assert len(calls) == 1 and "/models/:" not in calls[0]


def test_invalid_api_key_does_not_retry_fallback(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        return FakeResponse(403, {"error": {"status": "PERMISSION_DENIED"}})

    monkeypatch.setattr("app.services.ocr_service.requests.post", post)
    with pytest.raises(OCRServiceError) as caught:
        _service()._call_gemini("prompt", _image())

    assert caught.value.code == "ocr_provider_auth"
    assert caught.value.status_code == 502
    assert len(calls) == 1


def test_the_key_goes_only_in_the_header_and_each_request_is_bounded(monkeypatch):
    sent = []
    monkeypatch.setattr("app.services.ocr_service.requests.post",
                        lambda url, **kwargs: sent.append((url, kwargs)) or
                        FakeResponse(200, _vision_response('{"medication_name":"Sample"}')))
    _service()._call_gemini("prompt", _image())
    url, kwargs = sent[0]
    assert "test-key" not in url and kwargs["headers"] == {"x-goog-api-key": "test-key"}
    assert 0 < kwargs["timeout"] <= OCR_GEMINI_TIMEOUT


def test_an_exhausted_scan_budget_makes_no_request(monkeypatch):
    service = _scan_ready(monkeypatch, _service())
    calls = []
    monkeypatch.setattr("app.services.ocr_service.OCR_GEMINI_IMAGE_BUDGET", 0)
    monkeypatch.setattr("app.services.ocr_service.requests.post", lambda *a, **kw: calls.append(a) or None)
    with pytest.raises(OCRServiceError) as caught:
        service.process_image(_jpeg())
    assert (caught.value.code, caught.value.status_code) == ("ocr_provider_timeout", 504)
    assert calls == []


def test_malformed_provider_response_is_an_error(monkeypatch):
    monkeypatch.setattr(
        "app.services.ocr_service.requests.post",
        lambda *args, **kwargs: FakeResponse(200, {"candidates": []}),
    )
    with pytest.raises(OCRServiceError) as caught:
        _service()._call_gemini("prompt", _image())
    assert caught.value.code == "ocr_invalid_response"


@pytest.mark.parametrize("bad_body", [None, [], {"candidates": [None]}, {"candidates": [{"content": []}]},
                                      ValueError("not json")])
def test_malformed_provider_shapes_raise_safe_ocr_error(monkeypatch, bad_body):
    monkeypatch.setattr(
        "app.services.ocr_service.requests.post",
        lambda *args, **kwargs: FakeResponse(200, bad_body),
    )
    with pytest.raises(OCRServiceError) as caught:
        _service()._call_gemini("prompt", _image())
    assert caught.value.code == "ocr_invalid_response"


# ── Ollama, used when no Gemini key is set ──

@pytest.mark.parametrize("failure, code, status", [
    (requests.Timeout(), "ocr_provider_timeout", 504),
    (requests.ConnectionError(), "ocr_provider_error", 502),
])
def test_ollama_request_failures_are_ocr_errors(monkeypatch, failure, code, status):
    def post(*args, **kwargs):
        raise failure

    monkeypatch.setattr("app.services.ocr_service.requests.post", post)
    with pytest.raises(OCRServiceError) as caught:
        _ollama_service()._call_ollama("prompt", _image())
    assert (caught.value.code, caught.value.status_code) == (code, status)


@pytest.mark.parametrize("response", [
    FakeResponse(200, ValueError("not json")),
    FakeResponse(200, ["not", "a", "dict"]),
    FakeResponse(200, {"response": "I cannot read this image."}),
    FakeResponse(500, {"error": "model crashed"}),
])
def test_ollama_unusable_answers_are_ocr_errors(monkeypatch, response):
    monkeypatch.setattr("app.services.ocr_service.requests.post", lambda *args, **kwargs: response)
    with pytest.raises(OCRServiceError) as caught:
        _ollama_service()._call_ollama("prompt", _image())
    assert caught.value.code in ("ocr_invalid_response", "ocr_provider_error")


def test_ollama_scan_reads_fields_then_icons(monkeypatch):
    service = _scan_ready(monkeypatch, _ollama_service())
    answers = iter([
        {"response": json.dumps({"medication_name": "Allegra 60mg", "administration_text": "Use as directed"})},
        {"response": json.dumps({slot: slot == "bedtime" for slot in SLOTS})},
    ])
    monkeypatch.setattr("app.services.ocr_service.requests.post",
                        lambda *args, **kwargs: FakeResponse(200, next(answers)))
    result = service.process_image(_jpeg())
    assert result["med_name"] == "Allegra 60mg"
    assert [slot for slot, on in result["schedule_time"].items() if on] == ["bedtime"]


def test_ollama_unreadable_icon_pass_still_uses_the_instructions(monkeypatch):
    service = _scan_ready(monkeypatch, _ollama_service())
    answers = iter([
        {"response": json.dumps({"medication_name": "Allegra 60mg", "administration_text": "每天兩次 早晚飯後"})},
        {"response": "null"},
    ])
    monkeypatch.setattr("app.services.ocr_service.requests.post",
                        lambda *args, **kwargs: FakeResponse(200, next(answers)))
    schedule = service.process_image(_jpeg())["schedule_time"]
    assert schedule["morning"] and schedule["night"] and schedule["after_meals"] and not schedule["noon"]


# ── what a scan returns ──

def test_unreadable_text_does_not_return_success_shaped_na_result(monkeypatch):
    service = _scan_ready(monkeypatch, _service())
    monkeypatch.setattr(service, "_call_vision_model", lambda prompt, img: {"warning": "N/A"})

    with pytest.raises(OCRServiceError) as caught:
        service.process_image(_jpeg())
    assert caught.value.code == "ocr_no_prescription_text"
    assert caught.value.status_code == 422


@pytest.mark.parametrize("icons", [None, {"morning": True, "noon": False}, {slot: "maybe" for slot in SLOTS}, []])
def test_unreadable_icons_with_a_clear_text_schedule_still_succeed(monkeypatch, icons):
    """Hospital slips have no six-icon row; the instructions text is enough."""
    service = _scan_ready(monkeypatch, _service())
    monkeypatch.setattr(service, "_call_vision_model", lambda prompt, img: {
        "medication_name": "Allegra 60mg",
        "administration_text": "每天兩次 早晚飯後 每次1顆",
        "schedule_icons": icons,
    })
    result = service.process_image(_jpeg())
    assert {slot for slot, on in result["schedule_time"].items() if on} == {"morning", "night", "after_meals"}


def test_unreadable_icons_and_no_schedule_text_fail_with_a_clear_message(monkeypatch):
    service = _scan_ready(monkeypatch, _service())
    monkeypatch.setattr(service, "_call_vision_model", lambda prompt, img: {
        "medication_name": "Allegra 60mg", "administration_text": "Use as directed", "schedule_icons": None,
    })
    with pytest.raises(OCRServiceError) as caught:
        service.process_image(_jpeg())
    assert (caught.value.code, caught.value.status_code) == ("ocr_incomplete_schedule", 422)
    assert "before saving" not in caught.value.message


def test_as_needed_instructions_need_no_schedule(monkeypatch):
    service = _scan_ready(monkeypatch, _service())
    monkeypatch.setattr(service, "_call_vision_model", lambda prompt, img: {
        "medication_name": "Panadol 500mg", "administration_text": "必要時 每次1顆", "schedule_icons": None,
    })
    assert not any(service.process_image(_jpeg())["schedule_time"].values())


def test_icon_marks_ignore_a_partial_row():
    assert OCRService._icon_marks({"morning": True, "noon": False}) == {slot: False for slot in SLOTS}
    assert OCRService._icon_marks({**{slot: "false" for slot in SLOTS}, "noon": "True"})["noon"] is True


def test_empty_upload_is_a_client_error_not_cv2_exception(monkeypatch):
    monkeypatch.setattr(OCRService, "_available", True)
    with pytest.raises(OCRServiceError) as caught:
        _service().process_image(b"")
    assert caught.value.code == "invalid_image"
    assert caught.value.status_code == 400


def test_tiny_decoded_image_is_rejected_cleanly(monkeypatch):
    monkeypatch.setattr(OCRService, "_available", True)
    ok, encoded = cv2.imencode(".jpg", np.zeros((1, 1, 3), dtype=np.uint8))
    assert ok
    with pytest.raises(OCRServiceError) as caught:
        _service().process_image(encoded.tobytes())
    assert caught.value.code == "invalid_image"


def test_nullable_optional_fields_normalize_and_wrong_types_fail():
    normalized = OCRService._normalize_text_fields({"medication_name": "Sample", "warning": None})
    assert normalized["warning"] == ""
    with pytest.raises(OCRServiceError) as caught:
        OCRService._normalize_text_fields({"medication_name": "Sample", "administration_text": {}})
    assert caught.value.code == "ocr_invalid_response"


def test_numeric_and_list_fields_become_text(monkeypatch):
    """Gemini runs in JSON mode without a schema, so 領藥號 or the quantity may come back as numbers."""
    service = _scan_ready(monkeypatch, _service())
    monkeypatch.setattr(service, "_call_vision_model", lambda prompt, img: {
        "medication_name": "Allegra 60mg", "administration_text": "早晚飯後",
        "quantity": 14, "prescription_no": 1234, "dosage": 60.0, "pill_count": None,
        "warning": ["服藥後避免開車", 2, None], "schedule_icons": None,
    })
    result = service.process_image(_jpeg())
    assert (result["quantity"], result["prescription_no"], result["dosage"]) == ("14", "1234", "60")
    assert result["pill_count"] == 14
    assert result["warning"] == "服藥後避免開車; 2"


@pytest.mark.parametrize("value, count", [(14, 14), (14.0, 14), ("14", 14), ("14 顆", 14), ("1,000", 1000),
                                          (True, None), ("N/A", None), (float("inf"), None), (-3, None)])
def test_pill_count_is_a_whole_number_or_nothing(value, count):
    assert OCRService._count(value) == count


def test_gemini_scan_uses_one_full_image_pass_and_returns_the_same_fields(monkeypatch):
    service = _scan_ready(monkeypatch, _service())
    calls = []

    def call_model(prompt, image):
        calls.append(prompt)
        return {
            "medication_name": "Sample tablet",
            "administration_text": "Take one tablet every morning after meals",
            "schedule_icons": {slot: False for slot in SLOTS},
        }

    monkeypatch.setattr(service, "_call_vision_model", call_model)
    result = service.process_image(_jpeg())
    assert len(calls) == 1
    assert "schedule_icons" in calls[0]
    assert set(result) == RESULT_KEYS
    assert result["schedule_time"]["morning"] is True
    assert result["schedule_time"]["after_meals"] is True
    assert result["hospital"] == "N/A" and result["pill_count"] is None


def test_all_false_marks_without_clear_schedule_instruction_fail(monkeypatch):
    service = _scan_ready(monkeypatch, _service())
    monkeypatch.setattr(service, "_call_vision_model", lambda prompt, img: {
        "medication_name": "Sample tablet",
        "administration_text": "Use as directed",
        "schedule_icons": {slot: False for slot in SLOTS},
    })
    with pytest.raises(OCRServiceError) as caught:
        service.process_image(_jpeg())
    assert caught.value.code == "ocr_incomplete_schedule"


# ── every medicine on the paper ──
# Modelled on a real dot-matrix pharmacy receipt (4 Oct 2026) with three table rows. Every name, number and code
# here is made up; the real receipt's personal data must never be copied into the repository.

TODAY = datetime.date(2026, 10, 4)


def _receipt_answer(**changes):
    answer = {
        "document_type": "pharmacy_receipt",
        "hospital": "N/A",
        "pharmacy": "安心藥局藥品明細收據",
        "physician": "3300110022",               # the prescribing institution's code, not a person
        "pharmacist": "林小青",
        "patient_name": "王小華 身分證:B21****567",
        "prescription_no": "N/A",
        "visit_date": "1150928",
        "date_dispensed": "1150928",
        "use_before": "1150928",                # the visit date copied: not an expiry
        "days": "5",
        "warning": "◎請按時服藥!! 生日:0650101",
        "medications": [
            {"name": "VITACOBAL CAPSULES 0", "strength": "N/A", "quantity": "15 錠", "unit": "錠",
             "usage": "一天3次", "amount_each_intake": "N/A", "days": "5"},
            {"name": "GASTRO F.C. TABLETS", "quantity": "15 TAB", "unit": "TAB", "usage": "一天3次", "days": 5},
            {"name": "OTOMYX OTIC DROPS.", "quantity": "1 瓶", "unit": "瓶", "usage": "一天2次", "days": "5"},
        ],
        "schedule_icons": None,
    }
    answer.update(changes)
    return answer


def _scan(monkeypatch, answer, service=None):
    service = _scan_ready(monkeypatch, service or _service())
    monkeypatch.setattr(OCRService, "_today", staticmethod(lambda: TODAY))
    monkeypatch.setattr(service, "_call_vision_model", lambda prompt, img: json.loads(json.dumps(answer)))
    return service.process_image(_jpeg())


def _on(schedule):
    return {slot for slot in SLOTS if schedule.get(slot)}


def test_a_receipt_returns_every_medicine_and_the_first_one_at_the_top(monkeypatch):
    result = _scan(monkeypatch, _receipt_answer())
    assert set(result) == RESULT_KEYS
    meds = result["medications"]
    assert [med["med_name"] for med in meds] == ["VITACOBAL CAPSULES 0", "GASTRO F.C. TABLETS", "OTOMYX OTIC DROPS."]
    assert all(set(med) == MEDICINE_KEYS for med in meds)
    # Old clients read the top level: it is the first medicine.
    for key in ("med_name", "dosage", "quantity", "pill_count", "amount_each_intake", "total_intake",
                "schedule_time", "instructions", "intake_time_label"):
        assert result[key] == meds[0][key]

    capsule, tablet, drops = meds
    assert (capsule["form"], capsule["dose_form"]) == ("capsule", "solid_oral")
    assert (tablet["form"], tablet["dose_form"]) == ("tablet", "solid_oral")
    assert (drops["form"], drops["dose_form"]) == ("ear_drops", "other")
    assert _on(capsule["schedule_time"]) == _on(tablet["schedule_time"]) == {"morning", "noon", "night"}
    assert _on(drops["schedule_time"]) == {"morning", "night"}
    assert [med["times_per_day"] for med in meds] == [3, 3, 2]
    assert [med["days"] for med in meds] == [5, 5, 5]
    assert [med["pill_count"] for med in meds] == [15, 15, 1]
    # 15 tablets over 3 a day for 5 days is 1 a dose; a bottle has no tablets, its stock counts the course's doses.
    assert [med["units_per_dose"] for med in meds] == [1.0, 1.0, None]
    assert [med["stock"] for med in meds] == [15, 15, 10]
    assert capsule["total_intake"] == "15錠 (3次/天 × 1錠/次 × 5天)"
    assert all(med["schedule_source"] == "text" for med in meds)


def test_receipt_labels_never_become_invented_fields(monkeypatch):
    result = _scan(monkeypatch, _receipt_answer())
    assert result["hospital"] == "N/A"
    assert result["pharmacy"] == "安心藥局"          # without the document title the model appended
    assert result["physician"] == "N/A"             # a code is never a person
    assert result["pharmacist"] == "林小青"
    assert result["date_dispensed"] == result["visit_date"] == "2026-09-28"
    assert result["use_before"] == "N/A"
    assert result["prescription_no"] == "N/A"


def test_no_national_id_or_birth_date_leaves_the_server(monkeypatch):
    result = _scan(monkeypatch, _receipt_answer())
    dump = json.dumps(result, ensure_ascii=False)
    assert "B21" not in dump and "567" not in dump and "0650101" not in dump and "生日" not in dump
    assert result["patient_name"] == "王小華"
    assert result["warning"] == "◎請按時服藥!!"


def test_a_pharmacy_named_as_the_hospital_moves_to_pharmacy(monkeypatch):
    result = _scan(monkeypatch, _receipt_answer(hospital="安心藥局", pharmacy="N/A"))
    assert (result["hospital"], result["pharmacy"]) == ("N/A", "安心藥局")


@pytest.mark.parametrize("field, value", [("physician", "藥事費"), ("pharmacist", "S945001122"),
                                          ("hospital", "3300110022")])
def test_fee_labels_and_codes_are_not_names(monkeypatch, field, value):
    assert _scan(monkeypatch, _receipt_answer(**{field: value}))[field] == "N/A"


@pytest.mark.parametrize("dispensed, expected", [("115/09/28", "2026-09-28"), ("115.9.28", "2026-09-28"),
                                                 ("115年09月28日", "2026-09-28"), ("2026-09-28", "2026-09-28"),
                                                 ("0650101", "N/A"), ("1151231", "N/A"), ("1151340", "N/A")])
def test_dates_are_iso_and_implausible_ones_dropped(monkeypatch, dispensed, expected):
    result = _scan(monkeypatch, _receipt_answer(date_dispensed=dispensed, visit_date="N/A", use_before="N/A"))
    assert result["date_dispensed"] == expected


def test_a_printed_expiry_after_dispensing_is_kept_as_iso(monkeypatch):
    result = _scan(monkeypatch, _receipt_answer(use_before="116年03月31日"))
    assert result["use_before"] == "2027-03-31"


def test_one_medicine_with_a_schedule_is_enough(monkeypatch):
    answer = _receipt_answer()
    answer["medications"][0]["usage"] = "N/A"
    answer["medications"][2]["usage"] = "Use as directed"
    meds = _scan(monkeypatch, answer)["medications"]
    assert [bool(_on(med["schedule_time"])) for med in meds] == [False, True, False]
    assert meds[0]["schedule_source"] == "none" and meds[0]["times_per_day"] is None


def test_an_as_needed_medicine_alone_is_enough(monkeypatch):
    answer = _receipt_answer()
    for med in answer["medications"]:
        med["usage"] = "N/A"
    answer["medications"][1]["usage"] = "必要時 一天最多3次"
    meds = _scan(monkeypatch, answer)["medications"]
    assert meds[1]["as_needed"] is True and meds[1]["times_per_day"] == 3
    assert not any(_on(med["schedule_time"]) for med in meds)


def test_no_medicine_with_a_schedule_fails_with_422(monkeypatch):
    answer = _receipt_answer()
    for med in answer["medications"]:
        med["usage"] = "N/A"
    with pytest.raises(OCRServiceError) as caught:
        _scan(monkeypatch, answer)
    assert (caught.value.code, caught.value.status_code) == ("ocr_incomplete_schedule", 422)


def test_every_other_day_alone_is_schedule_information_not_a_422(monkeypatch):
    answer = _receipt_answer(medications=[{"name": "SLOWDOSE 2.5MG TABLETS", "quantity": "7 錠", "usage": "QOD"}])
    med = _scan(monkeypatch, answer)["medications"][0]
    assert (med["interval_hours"], med["times_per_day"]) == (48, None)
    assert not _on(med["schedule_time"]) and med["schedule_source"] == "none"


def test_a_count_range_keeps_its_upper_end(monkeypatch):
    answer = _receipt_answer(medications=[{"name": "PAINEASE TABLETS", "quantity": "20 TAB", "usage": "一天3-4次"}])
    med = _scan(monkeypatch, answer)["medications"][0]
    assert (med["times_per_day"], med["max_per_day"]) == (3, 4)
    assert _on(med["schedule_time"]) == {"morning", "noon", "night"}
    assert [m["max_per_day"] for m in _scan(monkeypatch, _receipt_answer())["medications"]] == [None, None, None]


def test_a_bedtime_medicine_written_with_the_evening_is_one_dose(monkeypatch):
    answer = _receipt_answer(medications=[{"name": "SLEEPWELL 10MG TABLETS", "quantity": "7 TAB",
                                           "usage": "每晚睡前 每次1顆", "days": "7"}])
    med = _scan(monkeypatch, answer)["medications"][0]
    assert _on(med["schedule_time"]) == {"bedtime"} and med["times_per_day"] == 1
    assert (med["units_per_dose"], med["stock"]) == (1.0, 7)


def test_half_an_hour_before_meals_is_not_half_a_tablet(monkeypatch):
    answer = _receipt_answer(medications=[{"name": "GASTRO F.C. TABLETS", "quantity": "15 TAB",
                                           "usage": "一天3次 飯前半小時", "days": "5"}])
    med = _scan(monkeypatch, answer)["medications"][0]
    assert med["units_per_dose"] == 1.0                      # from the totals: 15 ÷ (3 × 5)
    assert _on(med["schedule_time"]) == {"morning", "noon", "night", "before_meals"}


def test_identifier_removal_leaves_medicine_names_and_warnings_alone(monkeypatch):
    answer = _receipt_answer(warning="Avoid nonsteroidal anti-inflammatory drugs", medications=[
        {"name": "CALCIUM DOBESILATE 500MG CAPSULES", "quantity": "21 CAP", "usage": "TID"},
        {"name": "TRANEXAMIC ACID NORMAL 250MG", "quantity": "9 TAB", "usage": "t.i.d.",
         "warning": "孕婦及新生兒出生後請勿使用"},
    ])
    first, second = _scan(monkeypatch, answer)["medications"]
    assert first["med_name"] == "CALCIUM DOBESILATE 500MG CAPSULES"
    assert first["warning"] == "Avoid nonsteroidal anti-inflammatory drugs"
    assert second["med_name"] == "TRANEXAMIC ACID NORMAL 250MG" and second["times_per_day"] == 3
    assert second["warning"] == "孕婦及新生兒出生後請勿使用"


def test_a_code_in_front_of_the_pharmacy_name_is_removed(monkeypatch):
    result = _scan(monkeypatch, _receipt_answer(pharmacy="健保代碼:S945001122 安心藥局"))
    assert result["pharmacy"] == "安心藥局"


def test_medicines_without_a_name_are_dropped_and_none_left_is_422(monkeypatch):
    answer = _receipt_answer(medications=[{"name": "N/A", "usage": "一天3次"}, {"usage": "一天2次"}])
    with pytest.raises(OCRServiceError) as caught:
        _scan(monkeypatch, answer)
    assert caught.value.code == "ocr_no_prescription_text"


def test_the_icon_row_wins_for_a_bag_with_one_medicine(monkeypatch):
    icons = {"morning": True, "noon": False, "night": True, "bedtime": False,
             "before_meals": False, "after_meals": False}
    answer = _receipt_answer(medications=[{"name": "Amlodipine 5mg", "usage": "一天3次 飯後", "quantity": "28 錠"}],
                             schedule_icons=icons)
    med = _scan(monkeypatch, answer)["medications"][0]
    # Times from the checked icons; the meal mark from the text, since the row marks no meal.
    assert _on(med["schedule_time"]) == {"morning", "night", "after_meals"}
    assert (med["schedule_source"], med["times_per_day"]) == ("icons", 2)


def test_with_several_medicines_the_icon_row_only_fills_one_without_times(monkeypatch):
    icons = {"morning": True, "noon": False, "night": True, "bedtime": False,
             "before_meals": False, "after_meals": False}
    answer = _receipt_answer(schedule_icons=icons, medications=[
        {"name": "Amlodipine 5mg (降壓錠)", "quantity": "28 錠", "usage": "每日一次 早餐後"},
        {"name": "Metformin 500mg (降糖錠)", "quantity": "56 錠", "usage": "每日兩次 早晚餐後"},
        {"name": "Vitamin B 100mg", "quantity": "28 錠", "usage": "N/A"},
    ])
    first, second, third = _scan(monkeypatch, answer)["medications"]
    assert _on(first["schedule_time"]) == {"morning", "after_meals"}
    assert _on(second["schedule_time"]) == {"morning", "night", "after_meals"}
    assert _on(third["schedule_time"]) == {"morning", "night"} and third["schedule_source"] == "icons"


def test_more_than_four_a_day_uses_custom_times(monkeypatch):
    answer = _receipt_answer(medications=[{"name": "Eye gel EYE", "quantity": "1 支", "usage": "Q4H"}])
    med = _scan(monkeypatch, answer)["medications"][0]
    assert med["times_per_day"] == 6 and med["interval_hours"] == 4
    assert med["schedule_time"]["custom_times"] == ["16:00", "06:00"]
    assert med["intake_time_label"] == "Q4H [早上 | 中午 | 晚上 | 睡前 | 16:00 | 06:00]"


def test_custom_times_appear_only_when_needed(monkeypatch):
    result = _scan(monkeypatch, _receipt_answer())
    assert "custom_times" not in result["schedule_time"]
    assert set(result["schedule_time"]) == set(SLOTS)


def test_the_prompt_explains_receipt_labels_and_forbids_guessing():
    from app.services.ocr_service import _PROMPT_GEMINI, _PROMPT_TEXT
    for prompt in (_PROMPT_GEMINI, _PROMPT_TEXT):
        for words in ('"medications"', "就醫序號", "開立處方箋之醫院(診所)/醫師", "藥事費", "調劑者", "1150928",
                      "Never infer a hospital", "A number or code", "身分證", "生日", '"N/A"'):
            assert words in prompt
    assert "schedule_icons" in _PROMPT_GEMINI and "schedule_icons" not in _PROMPT_TEXT


# ── the routes ──

class FakeUpload:
    async def read(self):
        return b"image"


def _routers():
    asyncpg_stub = types.ModuleType("asyncpg")
    asyncpg_stub.Pool = object
    sys.modules.setdefault("asyncpg", asyncpg_stub)
    from app.routers import api_ocr, ocr
    return api_ocr, ocr


class FailingService:
    gemini_api_key = "present"

    def __init__(self, error):
        self.error = error

    def process_image(self, image_bytes):
        raise self.error


@pytest.mark.parametrize("error", [
    OCRServiceError("ocr_provider_busy", "OCR busy", 503),
    OCRServiceError("ocr_incomplete_schedule", "No schedule", 422),
    OCRServiceError("invalid_image", "Not an image", 400),
])
def test_api_ocr_route_returns_structured_errors(monkeypatch, error):
    api_ocr, _ = _routers()
    monkeypatch.setattr(OCRService, "get_instance", lambda: FailingService(error))
    response = asyncio.run(api_ocr.parse_prescription(file=FakeUpload(), user={"u_id": 1}))
    assert response.status_code == error.status_code
    assert json.loads(response.body) == {"error": error.message, "code": error.code}


def test_api_ocr_route_times_out_with_a_code(monkeypatch):
    api_ocr, _ = _routers()

    class SlowService:
        gemini_api_key = "present"

        def process_image(self, image_bytes):
            time.sleep(0.3)
            return {}

    monkeypatch.setattr(OCRService, "get_instance", lambda: SlowService())
    monkeypatch.setattr(api_ocr, "OCR_GEMINI_IMAGE_BUDGET", 0.05)
    response = asyncio.run(api_ocr.parse_prescription(file=FakeUpload(), user={"u_id": 1}))
    assert response.status_code == 504
    assert json.loads(response.body)["code"] == "ocr_provider_timeout"


def test_api_ocr_route_returns_the_scan_unchanged(monkeypatch):
    api_ocr, _ = _routers()

    class GoodService:
        gemini_api_key = "present"

        def process_image(self, image_bytes):
            return {"med_name": "Allegra 60mg"}

    monkeypatch.setattr(OCRService, "get_instance", lambda: GoodService())
    assert asyncio.run(api_ocr.parse_prescription(file=FakeUpload(), user={"u_id": 1})) == {"med_name": "Allegra 60mg"}


def test_legacy_ocr_route_returns_structured_provider_error(monkeypatch):
    _, ocr = _routers()
    monkeypatch.setattr(OCRService, "get_instance",
                        lambda: FailingService(OCRServiceError("ocr_provider_busy", "OCR busy", 503)))
    monkeypatch.setattr(ocr, "current_user", lambda request: {"u_id": 1})
    response = asyncio.run(ocr.upload_prescription(request=object(), file=FakeUpload()))
    assert response.status_code == 503
    assert response.body == b'{"error":"OCR busy","code":"ocr_provider_busy"}'


class _SaveConn:
    def __init__(self):
        self.args = None

    async def fetchval(self, query, *args):
        self.query, self.args = query, args
        return 31


class _SavePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Acquire()


class _JsonRequest:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


@pytest.mark.parametrize("body, form, units", [
    # The first medicine's form and amount from the scan: ear drops are never auto-recorded.
    ({"med_name": "OTOMYX OTIC DROPS.", "medications": [{"dose_form": "other", "units_per_dose": None}]},
     "other", "1"),
    ({"med_name": "GASTRO F.C. TABLETS", "dose_form": "solid_oral", "units_per_dose": 0.5}, "solid_oral", "0.5"),
    # Nothing (or nonsense) about the form: not a tablet the camera may record alone.
    ({"med_name": "Unknown"}, "other", "1"),
    ({"med_name": "X", "dose_form": "capsule", "units_per_dose": "abc"}, "other", "1"),
    ({"med_name": "X", "dose_form": "liquid", "units_per_dose": 250}, "liquid", "1"),
])
def test_legacy_save_stores_the_scanned_dose_form(monkeypatch, body, form, units):
    _, ocr = _routers()
    from decimal import Decimal
    conn = _SaveConn()
    monkeypatch.setattr(ocr, "current_user", lambda request: {"u_id": 1})
    monkeypatch.setattr(ocr, "get_pool", lambda: _SavePool(conn))
    assert asyncio.run(ocr.save_ocr(_JsonRequest({"schedule_time": {"morning": True}, **body}))) == {"med_id": 31}
    assert "dose_form, units_per_dose" in conn.query
    assert conn.args[-2:] == (form, Decimal(units))
