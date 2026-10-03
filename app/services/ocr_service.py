import re
import cv2
import json
import base64
import datetime
import threading
import time
import requests
import numpy as np
from zoneinfo import ZoneInfo
from app.config import (YOLO_MODEL_PATH, OLLAMA_URL, OLLAMA_TIMEOUT, OLLAMA_MODELS,
                        GEMINI_API_KEY, OCR_MODEL, OCR_GEMINI_FALLBACK_MODEL,
                        OCR_GEMINI_TIMEOUT, OCR_GEMINI_IMAGE_BUDGET, MEDCARE_TIMEZONE)
from app.services import ocr_parsing as parsing

_OCR_REQUEST_CONTEXT = threading.local()
# Gemini failures that the fallback model may fix: the primary is busy or timed out, or it was retired or renamed.
_FALLBACK_CODES = ("ocr_provider_busy", "ocr_model_not_found")
_SCHEDULE_SLOTS = parsing.SLOTS
_NA = "N/A"
# Fields of the old one-medicine answer; a model that ignores "medications" is read through them.
_LEGACY_MEDICINE_FIELDS = {
    "name": ("medication_name", "med_name"),
    "strength": ("dosage",),
    "quantity": ("quantity",),
    "usage": ("administration_text", "instructions"),
    "amount_each_intake": ("amount_each_intake",),
    "clinical_uses": ("clinical_uses",),
    "manufacturer": ("manufacturer",),
    "pill_appearance": ("pill_appearance", "pill_description"),
    "warning": ("warning",),
    "pill_count": ("pill_count",),
}
_MEDICINE_TEXT_FIELDS = ("name", "strength", "quantity", "unit", "usage", "amount_each_intake", "days",
                         "clinical_uses", "manufacturer", "pill_appearance", "warning", "pill_count")
_MEDICINE_ALIASES = {
    "name": ("name", "medication_name", "med_name", "drug_name"),
    "strength": ("strength", "dosage", "dose"),
    "usage": ("usage", "frequency", "frequency_text", "administration_text", "instructions"),
}


class OCRServiceError(Exception):
    """An OCR failure safe to return to the caller without provider details."""

    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

# The model copies printed text; the server turns it into dates, dose times and dose forms (ocr_parsing.py).
# On 4 Oct 2026 a real pharmacy receipt with three medicines came back as one medicine, with a hospital guessed
# from the address, the prescriber's institution code as the physician and the visit date as "use before".
# Every rule below answers one of those mistakes; keep them when editing the prompt.
_PROMPT_TEXT = """You are reading a photo of a Taiwanese medicine document: a hospital or clinic medicine bag (藥袋),
a prescription (處方箋) or a pharmacy receipt (藥品明細收據). Read Traditional Chinese and English.

Return ONLY one JSON object with these keys:
{
  "document_type": "medicine_bag", "prescription", "pharmacy_receipt" or "other",
  "hospital": "name of the hospital or clinic printed on the paper, e.g. 臺北市立聯合醫院",
  "pharmacy": "name of the pharmacy printed on the paper, up to and including 藥局 / 藥房, without the document title after it",
  "physician": "person name printed after 醫師 / 處方醫師",
  "pharmacist": "person name printed after 藥師 / 調劑藥師 / 調劑者",
  "patient_name": "person name printed after 姓名",
  "prescription_no": "number printed after 領藥號 / 處方箋號 / 處方號",
  "visit_date": "date printed after 就醫日 / 就診日期 / 看診日期, exactly as printed",
  "date_dispensed": "date printed after 調劑日期 / 調劑日 / 給藥日期, exactly as printed",
  "use_before": "date printed after 處方期限 / 有效期限 / 使用期限 / 用藥期限, exactly as printed",
  "days": "number printed after 日份 / 給藥日數 / 天數 for the whole paper",
  "warning": "text printed under 注意事項 / 警語 for the whole paper",
  "medications": [
    {
      "name": "the medicine name exactly as printed on its line, with brand, generic name and strength when printed there, e.g. Allegra (Fexofenadine) 60mg/tab",
      "strength": "strength per unit if printed, e.g. 60mg",
      "quantity": "total quantity with its unit as printed (總量 + 單位), e.g. 15 錠",
      "unit": "the unit as printed, e.g. 錠, 顆, TAB, CAP, 瓶, 支, 包, ml",
      "usage": "this medicine's usage text exactly as printed (用法 / 用法用量), e.g. 一天3次, 每日兩次 早晚飯後, TID PC, 需要時",
      "amount_each_intake": "amount per dose if printed, e.g. 每次1顆",
      "days": "number of days printed for this medicine (日 / 日數 / 天數)",
      "clinical_uses": "printed 主要用途 / 適應症",
      "manufacturer": "printed 廠牌 / 藥商",
      "pill_appearance": "printed 外觀, e.g. 白色圓形",
      "warning": "printed warnings for this medicine"
    }
  ]
}

Rules:
1. "medications" has one entry for EVERY medicine line on the paper, in printed order. In a table, every row with a
   medicine name is its own entry, even when rows look alike. Never merge, skip or invent rows. Leave the row
   number (N / 序號 column) out of the name. Read each row's own letters: never copy a name from another row.
2. Copy only text that is printed on the paper, character by character. Do not correct, complete, translate or
   guess. Dot-matrix letters are easy to confuse (E and B, I and O, C and G): look at each letter. Receipts cut
   long names off at the column edge: copy a cut-off name as printed (a name ending in "0" stays so), and never
   complete a name or add a strength from your own knowledge of medicines. When a label is absent or its value is
   unreadable, use "N/A".
3. "hospital" is only a hospital or clinic NAME printed on the paper. Never infer a hospital or clinic from an
   address, city, phone number, stamp or code. A pharmacy (藥局 / 藥房) is not a hospital or clinic: put it in
   "pharmacy".
4. "physician", "pharmacist" and "patient_name" are person names printed right after their label. A number or code
   is never a person, and words such as 藥費 or 藥事費 are never names: use "N/A".
5. Taiwanese receipt labels: 健保代碼 is the pharmacy's insurance code. 就醫序號 is a visit sequence number, NOT a
   prescription number. 部分負擔代號 is a co-payment code. A number after 開立處方箋之醫院(診所)/醫師 is the code of
   the prescribing institution, NOT a hospital name and NOT a physician. 藥費, 藥事費, 合計, 健保申請額 and 部分負擔
   are fees. 調劑者 is the dispensing pharmacist. 就醫日 is the visit date and 調劑日期 the dispensing date. Dates are
   often compact ROC dates: 1150928 is ROC year 115, month 09, day 28. Copy dates exactly as printed, do not
   convert them, and never put one date in another date's field. 日份 is the number of days.
6. Never return a national ID number (身分證) or a date of birth (生日 / 出生日期) in any field.
7. Return ONLY valid JSON."""

_PROMPT_GEMINI = _PROMPT_TEXT.replace(
    "7. Return ONLY valid JSON.",
    """7. Medicine bags may have a row of six schedule icons or check boxes (早上, 中午, 晚上, 睡前, 飯前, 飯後). Add a
   top-level "schedule_icons" object with exactly the boolean keys "morning", "noon", "night", "bedtime",
   "before_meals" and "after_meals": true only when that icon or box is clearly checked (a large V or a tick).
   When the paper has no such row (receipts and tables usually have none), or it is cut off or unclear, set
   "schedule_icons" to null.
8. Return ONLY valid JSON.""",
)

_PROMPT_ICONS = """This image shows an icon row from a Taiwanese hospital prescription bag.
There are exactly 6 icons: 早上 Morning, 中午 Noon, 晚上 Night, 睡前 Bedtime, 飯前 Before meals, 飯後 After meals.
Icons marked with a large 'V' letter are checked (true). Others are false.

Return ONLY:
{"morning": true/false, "noon": true/false, "night": true/false,
 "bedtime": true/false, "before_meals": true/false, "after_meals": true/false}"""

class OCRService:
    """Singleton wrapping optional YOLO document detection + vision OCR.

    One process-wide instance serves every account: nothing here depends on who scans. With GEMINI_API_KEY set,
    one Gemini call reads the fields and the icon row (OCR_MODEL, then OCR_GEMINI_FALLBACK_MODEL once); without
    it, a local Ollama vision model makes two passes (fields, then the icon-row crop).
    """

    _instance: "OCRService | None" = None
    _available: bool = False

    def __init__(self):
        self.yolo = None
        self.gemini_api_key = GEMINI_API_KEY.strip()
        self.active_model = OCR_MODEL if self.gemini_api_key else None
        # Boundary detection only improves the crop; OCR itself can work without YOLO.
        try:
            from ultralytics import YOLO
            if YOLO_MODEL_PATH.is_file():
                self.yolo = YOLO(str(YOLO_MODEL_PATH))
        except Exception as exc:
            print(f"[OCR] YOLO document detection unavailable: {exc}")

        if self.gemini_api_key:
            OCRService._available = True
            print(f"[OCR] Gemini vision model: {self.active_model} "
                  f"(fallback: {OCR_GEMINI_FALLBACK_MODEL or 'off'})")
        else:
            self.active_model = self._resolve_model()
            OCRService._available = bool(self.active_model)
            if self.active_model:
                print(f"[OCR] Ollama vision model: {self.active_model}")
            else:
                print("[OCR] Not available: configure GEMINI_API_KEY or start Ollama with a vision model")

    @classmethod
    def get_instance(cls) -> "OCRService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _resolve_model(self) -> str | None:
        ollama_base = OLLAMA_URL.split("/api/")[0]
        try:
            resp = requests.get(f"{ollama_base}/api/tags", timeout=5)
            installed = [m["name"] for m in resp.json().get("models", [])]
            for preferred in OLLAMA_MODELS:
                for name in installed:
                    if preferred in name:
                        return name
        except Exception:
            pass
        return None

    def process_image(self, image_bytes: bytes) -> dict:
        _OCR_REQUEST_CONTEXT.deadline = time.monotonic() + OCR_GEMINI_IMAGE_BUDGET
        _OCR_REQUEST_CONTEXT.model = self.active_model
        try:
            return self._process_image(image_bytes)
        finally:
            for attribute in ("deadline", "model"):
                if hasattr(_OCR_REQUEST_CONTEXT, attribute):
                    delattr(_OCR_REQUEST_CONTEXT, attribute)

    def _process_image(self, image_bytes: bytes) -> dict:
        """
        Accepts raw JPEG/PNG bytes and returns every medicine on the paper.

        "medications" lists one entry per medicine line; the top-level fields are the first medicine plus the
        paper's own fields (hospital, pharmacy, dates, names), so clients that read one medicine keep working.
        """
        if not OCRService._available:
            raise OCRServiceError("ocr_unavailable", "OCR service is not configured.", 503)

        nparr = np.frombuffer(image_bytes or b"", np.uint8)
        try:
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR) if nparr.size else None
        except cv2.error:
            img = None
        if img is None or min(img.shape[:2]) < 32:
            raise OCRServiceError("invalid_image", "The uploaded file is not a readable image.", 400)

        warped = self._warp_with_yolo(img)
        if warped is None:
            warped = img

        enhanced = self._enhance(warped)
        icon_crop = self._crop_icon_row(enhanced)

        if not self.active_model:
            raise OCRServiceError("ocr_unavailable", "No OCR vision model is configured.", 503)

        if self.gemini_api_key:
            print("[OCR] Gemini pass: prescription fields and icon row…")
            text_data = self._clean(self._call_vision_model(_PROMPT_GEMINI, enhanced))
        else:
            print("[OCR] Pass 1/2: text extraction…")
            text_data = self._clean(self._call_vision_model(_PROMPT_TEXT, enhanced))
        if not isinstance(text_data, dict):
            raise OCRServiceError(
                "ocr_invalid_response", "The OCR model returned an unreadable result. Please retry.", 502
            )
        text_data = self._normalize_text_fields(text_data)
        entries = self._medicine_entries(text_data)
        if not entries:
            raise OCRServiceError(
                "ocr_no_prescription_text",
                "OCR could not read a medication name. Try a clearer, well-lit image.",
                422,
            )
        if self.gemini_api_key:
            icon_data = text_data.get("schedule_icons")
        else:
            print("[OCR] Pass 2/2: icon row…")
            try:
                icon_data = self._clean(self._call_vision_model(_PROMPT_ICONS, icon_crop))
            except OCRServiceError as exc:
                if exc.code != "ocr_invalid_response":
                    raise
                icon_data = None   # an unreadable icon answer is the same as an unreadable row
        icon_data = self._icon_marks(icon_data)
        # A readable icon row with at least one dose time checked; all-false marks say nothing (receipts have none).
        icons = icon_data if any(icon_data[slot] for slot in parsing.TIME_SLOTS) else None

        document = self._document_fields(text_data)
        medicines = [
            self._medicine(entry, document, icons, single=len(entries) == 1) for entry in entries
        ]
        # Schedule information: a dose time or meal mark, as-needed use, or a recognised interval (QOD: every 48 h).
        if not any(medicine["has_schedule"] or medicine["as_needed"] or medicine["interval_hours"]
                   for medicine in medicines):
            raise OCRServiceError(
                "ocr_incomplete_schedule",
                "OCR read the medicine but not when to take it: no checked schedule mark and no clear instructions. "
                "Retake the photo with the schedule row and the instructions in view.",
                422,
            )
        for medicine in medicines:
            del medicine["has_schedule"]

        first = medicines[0]
        result = {
            "med_name":           first["med_name"],
            "dosage":             first["dosage"],
            "quantity":           first["quantity"],
            "pill_count":         first["pill_count"],
            "amount_each_intake": first["amount_each_intake"],
            "total_intake":       first["total_intake"],
            "schedule_time":      dict(first["schedule_time"]),
            "instructions":       first["instructions"],
            "warning":            first["warning"],
            "pill_description":   first["pill_description"],
            "intake_time_label":  first["intake_time_label"],
            "clinical_uses":      first["clinical_uses"],
            "manufacturer":       first["manufacturer"],
            "hospital":           document["hospital"],
            "prescription_no":    document["prescription_no"],
            "use_before":         document["use_before"],
            "physician":          document["physician"],
            "pharmacist":         document["pharmacist"],
            "patient_name":       document["patient_name"],
            "date_dispensed":     document["date_dispensed"],
            "pharmacy":           document["pharmacy"],
            "visit_date":         document["visit_date"],
            "document_type":      document["document_type"],
            "medications":        medicines,
        }
        return self._scrub(result)

    # ── turning the model's answer into medicines ─────────────────────────────

    @staticmethod
    def _today() -> datetime.date:
        return datetime.datetime.now(ZoneInfo(MEDCARE_TIMEZONE)).date()

    @staticmethod
    def _value(value) -> str:
        """A text field without ID numbers or birth dates, and with the model's "nothing" (N/A, null, -) as ""."""
        text = parsing.strip_identifiers(value).strip() if isinstance(value, str) else ""
        return "" if text.casefold() in ("n/a", "na", "none", "null", "unknown", "-", "--", "無") else text

    def _medicine_entries(self, text_data: dict) -> list[dict]:
        """One dict of text fields per medicine with a name: the "medications" list, else the old flat fields."""
        listed = text_data.get("medications")
        raw_entries = [item for item in listed if isinstance(item, dict)] if isinstance(listed, list) else []
        if not raw_entries:
            raw_entries = [{key: next((text_data[f] for f in fields if text_data.get(f) not in (None, "")), None)
                            for key, fields in _LEGACY_MEDICINE_FIELDS.items()}]
        entries = []
        for raw in raw_entries:
            entry = {}
            for key in _MEDICINE_TEXT_FIELDS:
                aliases = _MEDICINE_ALIASES.get(key, (key,))
                value = next((raw.get(alias) for alias in aliases if raw.get(alias) not in (None, "")), None)
                if key == "pill_count":
                    entry[key] = value
                    continue
                try:
                    if isinstance(value, list):
                        value = "; ".join(part for part in map(OCRService._text, value) if part.strip())
                    entry[key] = self._value(OCRService._text(value))
                except TypeError:
                    raise OCRServiceError(
                        "ocr_invalid_response", "The OCR model returned fields in an invalid format.", 502
                    ) from None
            if entry["name"]:
                entries.append(entry)
        return entries

    def _document_fields(self, text_data: dict) -> dict:
        """The paper's own fields: names checked to be names, dates as ISO, codes and invented values dropped."""
        today = self._today()
        hospital = parsing.institution_name(self._value(text_data.get("hospital")))
        pharmacy = parsing.institution_name(self._value(text_data.get("pharmacy")))
        if hospital and parsing.is_pharmacy(hospital) and not pharmacy:
            hospital, pharmacy = "", hospital      # a pharmacy is not a hospital or clinic
        visit = parsing.normalize_date(self._value(text_data.get("visit_date")), "past", today)
        dispensed = parsing.normalize_date(self._value(text_data.get("date_dispensed")), "past", today)
        use_before = parsing.normalize_date(self._value(text_data.get("use_before")), "until", today)
        # A "use before" equal to (or before) the visit or dispensing date is that date copied, not an expiry.
        if use_before and any(use_before <= other for other in (visit, dispensed) if other):
            use_before = None
        document_type = self._value(text_data.get("document_type")).lower()
        return {
            "hospital": hospital or _NA,
            "pharmacy": pharmacy or _NA,
            "physician": parsing.person_name(self._value(text_data.get("physician"))) or _NA,
            "pharmacist": parsing.person_name(self._value(text_data.get("pharmacist"))) or _NA,
            "patient_name": parsing.person_name(self._value(text_data.get("patient_name"))) or _NA,
            "prescription_no": self._value(text_data.get("prescription_no")) or _NA,
            "visit_date": visit or _NA,
            "date_dispensed": dispensed or _NA,
            "use_before": use_before or _NA,
            "days": self._days(text_data.get("days")),
            "warning": self._value(text_data.get("warning")),
            "document_type": document_type if document_type in (
                "medicine_bag", "prescription", "pharmacy_receipt", "other") else "other",
        }

    @staticmethod
    def _days(value) -> int | None:
        days = OCRService._count(value)
        return days if days and 1 <= days <= 365 else None

    def _medicine(self, entry: dict, document: dict, icons: dict | None, *, single: bool) -> dict:
        """One medicine: its printed fields plus the dose times, form and amounts worked out from them.

        Dose times come from its own usage text. A readable icon row (a hospital bag) wins for the medicine when it
        is the paper's only medicine; on a paper with several medicines the row only fills in a medicine whose text
        gives no times, because one row can't tell which medicine each mark is for.
        """
        usage = entry["usage"]
        frequency = parsing.parse_frequency(usage)
        schedule = dict(frequency["schedule"])
        custom_times = list(frequency["custom_times"])
        times_per_day = frequency["times_per_day"]
        source = "text" if parsing.has_schedule(schedule, custom_times) else "none"
        own_times = any(schedule[slot] for slot in parsing.TIME_SLOTS) or bool(custom_times)
        if icons and (single or not own_times):
            meals_marked = any(icons[slot] for slot in parsing.MEAL_SLOTS)
            for slot in parsing.SLOTS:
                if slot in parsing.TIME_SLOTS or meals_marked:
                    schedule[slot] = bool(icons[slot])
            custom_times = []
            times_per_day = sum(schedule[slot] for slot in parsing.TIME_SLOTS)
            source = "icons"
        has_schedule = parsing.has_schedule(schedule, custom_times)

        quantity = entry["quantity"]
        unit = entry["unit"]
        pill_count = self._count(entry.get("pill_count"))
        if pill_count is None:
            pill_count = self._count(quantity)
        days = self._days(entry["days"]) or self._days(parsing.days_from_text(usage)) or document["days"]
        form = parsing.guess_form(entry["name"], unit or quantity,
                                  " ".join((quantity, entry["amount_each_intake"], usage)))
        container = parsing.is_container_unit(unit or quantity)
        countable = form in ("tablet", "capsule") and not container
        per_dose = None
        if countable:   # printed (每次1顆), else from the totals (15 tablets, 3 a day, 5 days: 1)
            per_dose = (parsing.units_per_dose(entry["amount_each_intake"]) or parsing.units_per_dose(usage)
                        or parsing.per_dose_from_totals(pill_count, times_per_day, days))
        if countable:
            stock = pill_count
        elif times_per_day and days and not frequency["as_needed"]:
            stock = times_per_day * days       # a bottle or tube: the stock counts doses (one application each)
        else:
            stock = None

        schedule_time = dict(schedule)
        if custom_times:
            schedule_time["custom_times"] = custom_times
        return {
            "med_name": entry["name"],
            "dosage": entry["strength"] or _NA,
            "quantity": quantity or _NA,
            "pill_count": pill_count,
            "unit": unit or _NA,
            "frequency_text": usage or _NA,
            "instructions": usage or _NA,
            "times_per_day": times_per_day,
            "max_per_day": frequency["max_per_day"],
            "interval_hours": frequency["interval_hours"],
            "days": days,
            "amount_each_intake": entry["amount_each_intake"] or _NA,
            "units_per_dose": per_dose,
            "form": form,
            "dose_form": parsing.FORM_TO_DOSE_FORM[form],
            "as_needed": frequency["as_needed"],
            "schedule_time": schedule_time,
            "schedule_source": source,
            "stock": stock,
            "total_intake": self._calc_total(times_per_day, per_dose, days, unit if countable else ""),
            "intake_time_label": self._schedule_label(schedule_time, usage),
            "warning": entry["warning"] or document["warning"] or _NA,
            "pill_description": entry["pill_appearance"] or _NA,
            "clinical_uses": entry["clinical_uses"] or _NA,
            "manufacturer": entry["manufacturer"] or _NA,
            "has_schedule": has_schedule,
        }

    @staticmethod
    def _scrub(value):
        """No national ID number or labelled birth date leaves the server, in any text field."""
        if isinstance(value, str):
            cleaned = parsing.strip_identifiers(value)
            return cleaned if cleaned.strip() else _NA
        if isinstance(value, dict):
            return {key: OCRService._scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [OCRService._scrub(item) for item in value]
        return value

    # ── internal helpers ──────────────────────────────────────────────────────

    def _warp_with_yolo(self, img):
        try:
            if self.yolo is None:
                return None
            results = self.yolo(img, verbose=False, conf=0.80)
            if results and results[0].masks:
                from ultralytics.utils.ops import scale_image
                mask_pts = results[0].masks.xy[0]
                return self._perspective_warp(img, mask_pts)
        except Exception:
            pass
        return None

    def _perspective_warp(self, image, mask_points):
        TARGET_W, TARGET_H = 1000, int(1000 / (3 / 4))
        try:
            contour = np.array(mask_points, dtype=np.int32)
            peri = cv2.arcLength(contour, True)
            approx = None
            for eps in [0.02, 0.05, 0.1]:
                a = cv2.approxPolyDP(contour, eps * peri, True)
                if len(a) == 4:
                    approx = a
                    break
            if approx is None:
                rect = cv2.minAreaRect(contour)
                approx = np.int32(cv2.boxPoints(rect))
            pts = approx.reshape(4, 2).astype("float32")
            s = pts.sum(axis=1)
            diff = np.diff(pts, axis=1)
            ordered = np.array([pts[np.argmin(s)], pts[np.argmin(diff)],
                                 pts[np.argmax(s)], pts[np.argmax(diff)]], dtype="float32")
            dst = np.array([[0, 0], [TARGET_W - 1, 0],
                             [TARGET_W - 1, TARGET_H - 1], [0, TARGET_H - 1]], dtype="float32")
            M = cv2.getPerspectiveTransform(ordered, dst)
            return cv2.warpPerspective(image, M, (TARGET_W, TARGET_H))
        except Exception:
            return None

    def _enhance(self, img):
        out = cv2.fastNlMeansDenoisingColored(img, None, 10, 10, 7, 21)
        lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        l = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l)
        out = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)
        kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.float32)
        return cv2.filter2D(out, -1, kernel)

    def _crop_icon_row(self, img):
        h, w = img.shape[:2]
        crop = img[int(h * 0.78): int(h * 0.92), :]
        return cv2.resize(crop, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)

    def _call_vision_model(self, prompt: str, img) -> dict:
        if self.gemini_api_key:
            return self._call_gemini(prompt, img)
        return self._call_ollama(prompt, img)

    def _call_gemini(self, prompt: str, img) -> dict:
        _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
        payload = {
            "contents": [{"parts": [
                {"text": prompt},
                {"inlineData": {"mimeType": "image/jpeg",
                                "data": base64.b64encode(buf).decode("ascii")}},
            ]}],
            "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
        }
        has_scan_context = hasattr(_OCR_REQUEST_CONTEXT, "model")
        model = getattr(_OCR_REQUEST_CONTEXT, "model", self.active_model or OCR_MODEL)
        fallback = OCR_GEMINI_FALLBACK_MODEL
        try:
            response = self._post_gemini(model, payload)
        except OCRServiceError as exc:
            if exc.code not in _FALLBACK_CODES or not fallback or model == fallback:
                raise
            # Only a busy, timed-out or missing model is worth another try. Auth and request errors
            # should be visible instead of being hidden by a different model.
            print(f"[OCR] Gemini model {model} failed ({exc.code}); trying fallback model {fallback}")
            response = self._post_gemini(fallback, payload)
            if has_scan_context:
                _OCR_REQUEST_CONTEXT.model = fallback

        candidates = response.get("candidates")
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
            raise OCRServiceError(
                "ocr_invalid_response", "The OCR provider returned an unreadable result. Please retry.", 502
            )
        content = candidates[0].get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            raise OCRServiceError(
                "ocr_invalid_response", "The OCR provider returned an unreadable result. Please retry.", 502
            )
        raw = "\n".join(
            part["text"] for part in parts
            if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]
        )
        parsed = self._parse_json(raw)
        if not parsed:
            raise OCRServiceError(
                "ocr_invalid_response",
                "The OCR provider returned an unreadable result. Please retry with a clearer image.",
                502,
            )
        return parsed

    def _post_gemini(self, model: str, payload: dict) -> dict:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        remaining = getattr(
            _OCR_REQUEST_CONTEXT,
            "deadline",
            time.monotonic() + OCR_GEMINI_IMAGE_BUDGET,
        ) - time.monotonic()
        if remaining <= 0:
            raise OCRServiceError("ocr_provider_timeout", "OCR exceeded its time limit. Please retry.", 504)
        timeout = min(OCR_GEMINI_TIMEOUT, remaining)
        try:
            response = requests.post(
                url,
                headers={"x-goog-api-key": self.gemini_api_key},
                json=payload,
                timeout=timeout,
            )
        except requests.Timeout as exc:
            print(f"[OCR] Gemini model {model} did not answer within {timeout:.0f} s")
            raise OCRServiceError(
                "ocr_provider_busy", "The OCR provider is temporarily busy.", 503
            ) from exc
        except requests.RequestException as exc:
            raise OCRServiceError(
                "ocr_provider_error", "Could not connect to the OCR provider. Please retry shortly.", 502
            ) from exc

        if response.status_code in (429, 500, 502, 503, 504):
            print(f"[OCR] Gemini model {model} returned transient HTTP {response.status_code}")
            raise OCRServiceError(
                "ocr_provider_busy", "The OCR provider is temporarily busy.", 503
            )
        if response.status_code in (401, 403):
            raise OCRServiceError(
                "ocr_provider_auth", "The OCR provider rejected its API key. Check the server configuration.", 502
            )
        if response.status_code == 404:
            print(f"[OCR] Gemini model {model} was not found (HTTP 404): check OCR_MODEL and OCR_GEMINI_FALLBACK_MODEL")
            raise OCRServiceError(
                "ocr_model_not_found", "The configured OCR model is not available. Check the server configuration.", 502
            )
        if not response.ok:
            print(f"[OCR] Gemini model {model} returned HTTP {response.status_code}")
            raise OCRServiceError(
                "ocr_provider_error", "The OCR provider could not process this request.", 502
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise OCRServiceError(
                "ocr_invalid_response", "The OCR provider returned an invalid response.", 502
            ) from exc
        if not isinstance(body, dict):
            raise OCRServiceError(
                "ocr_invalid_response", "The OCR provider returned an invalid response.", 502
            )
        return body

    def _call_ollama(self, prompt: str, img) -> dict:
        _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
        b64 = base64.b64encode(buf).decode("utf-8")
        opts: dict = {"temperature": 0.0}
        if ":cloud" not in (self.active_model or ""):
            opts["num_predict"] = -1
        payload = {
            "model": self.active_model,
            "prompt": prompt,
            "images": [b64],
            "stream": False,
            "options": opts,
        }
        try:
            resp = requests.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT)
            resp.raise_for_status()
            r = resp.json()
            if not isinstance(r, dict):
                raise OCRServiceError(
                    "ocr_invalid_response", "The OCR model returned an invalid response.", 502
                )
            # Thinking models (e.g. kimi-k2.6:cloud) put the answer in "thinking" when response is empty
            raw = r.get("response") or r.get("thinking", "")
            parsed = self._parse_json(raw)
            if not parsed:
                raise OCRServiceError(
                    "ocr_invalid_response", "The OCR model returned an unreadable result.", 502
                )
            return parsed
        except OCRServiceError:
            raise
        except requests.Timeout as exc:
            print(f"[OCR] Ollama model {self.active_model} request timed out after {OLLAMA_TIMEOUT} s")
            raise OCRServiceError(
                "ocr_provider_timeout", "The OCR model took too long to respond. Please retry.", 504
            ) from exc
        except requests.RequestException as exc:
            raise OCRServiceError(
                "ocr_provider_error", "Could not connect to the OCR model. Please retry shortly.", 502
            ) from exc
        except ValueError as exc:
            raise OCRServiceError(
                "ocr_invalid_response", "The OCR model returned an invalid response.", 502
            ) from exc

    @staticmethod
    def _icon_marks(icon_data) -> dict:
        """The six schedule marks, or all False when the model could not read the icon row.

        Many prescriptions (hospital slips, other bags) have no six-icon row, so unreadable marks never fail a scan
        on their own: the instructions text can still give the schedule, and the scan fails only when neither does.
        A partial or malformed row is ignored as a whole, so the schedule then comes from the text alone.
        """
        unread = {slot: False for slot in _SCHEDULE_SLOTS}
        if not isinstance(icon_data, dict):
            print("[OCR] Schedule icons unreadable; using the instructions text only")
            return unread
        marks = {}
        for slot in _SCHEDULE_SLOTS:
            value = icon_data.get(slot)
            if isinstance(value, bool):
                marks[slot] = value
            elif isinstance(value, str) and value.strip().lower() in ("true", "false"):
                marks[slot] = value.strip().lower() == "true"
            else:
                print("[OCR] Schedule icons incomplete; using the instructions text only")
                return unread
        return marks

    @staticmethod
    def _text(value) -> str:
        """One text field as a string. Numbers become text (a numeric 領藥號 or quantity is still readable)."""
        if value is None or isinstance(value, bool):
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            return str(int(value)) if value.is_integer() else str(value)
        raise TypeError(type(value).__name__)

    @staticmethod
    def _normalize_text_fields(text_data: dict) -> dict:
        """Every text field as a string ('' when missing); objects in a text field fail the scan."""
        string_fields = (
            "medication_name", "dosage", "quantity", "administration_text",
            "amount_each_intake", "clinical_uses", "manufacturer", "hospital",
            "prescription_no", "use_before", "physician", "pharmacist",
            "patient_name", "date_dispensed", "pill_appearance", "warning",
            "pharmacy", "visit_date", "document_type", "days",
        )
        normalized = dict(text_data)
        for key in string_fields:
            value = normalized.get(key)
            try:
                if isinstance(value, list):   # e.g. several warnings as a list of sentences
                    normalized[key] = "; ".join(part for part in map(OCRService._text, value) if part.strip())
                else:
                    normalized[key] = OCRService._text(value)
            except TypeError:
                raise OCRServiceError(
                    "ocr_invalid_response", "The OCR model returned fields in an invalid format.", 502
                ) from None
        return normalized

    @staticmethod
    def _count(value) -> int | None:
        """A whole pill count from 14, 14.0, "14" or "14 顆"; None when there is no number."""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            try:
                return int(value) if value >= 0 else None
            except (OverflowError, ValueError):   # Infinity / NaN, which json.loads accepts
                return None
        match =re.search(r"\d+", str(value).replace(",", "")) if isinstance(value, str) else None
        return int(match.group()) if match else None

    @staticmethod
    def _clean(value):
        """Remove surrogate characters that Ollama sometimes produces."""
        if isinstance(value, str):
            return value.encode("utf-8", errors="ignore").decode("utf-8")
        if isinstance(value, dict):
            return {k: OCRService._clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [OCRService._clean(v) for v in value]
        return value

    def _parse_json(self, raw: str) -> dict:
        if not raw:
            return {}
        text = raw.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            parts = text.split("```")
            if len(parts) >= 3:
                text = parts[1].strip()
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e > s:
            text = text[s: e + 1]
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            try:
                parsed = json.loads(text.replace("'", '"'))
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}

    @staticmethod
    def _schedule_label(schedule: dict, admin_text: str) -> str:
        labels = {"morning": "早上", "noon": "中午", "night": "晚上",
                  "bedtime": "睡前", "before_meals": "飯前", "after_meals": "飯後"}
        parts = [label for slot, label in labels.items() if schedule.get(slot)]
        parts += list(schedule.get("custom_times") or [])
        summary = " | ".join(parts)
        if summary and admin_text:
            return f"{admin_text} [{summary}]"
        return admin_text or summary or _NA

    @staticmethod
    def _calc_total(times_per_day: int | None, per_dose: float | None, days: int | None, unit: str) -> str:
        """The course's total as text, e.g. 15錠 (3次/天 × 1錠/次 × 5天); N/A (partial: …) when a number is missing."""
        label = unit if unit and unit != _NA else "顆"

        def number(value) -> str:
            return str(int(value)) if float(value).is_integer() else f"{value:g}"

        if times_per_day and per_dose and days:
            total = times_per_day * per_dose * days
            return f"{number(total)}{label} ({times_per_day}次/天 × {number(per_dose)}{label}/次 × {days}天)"
        parts = []
        if times_per_day:
            parts.append(f"{times_per_day}次/天")
        if per_dose:
            parts.append(f"{number(per_dose)}{label}/次")
        if days:
            parts.append(f"{days}天")
        return f"N/A (partial: {' × '.join(parts)})" if parts else _NA
