import re
import cv2
import json
import base64
import time
import requests
import numpy as np
from app.config import YOLO_MODEL_PATH, OLLAMA_URL, OLLAMA_TIMEOUT, OLLAMA_MODELS

_PROMPT_TEXT = """You are reading a Taiwanese hospital prescription (處方箋).
Extract these fields from the prescription image. Read BOTH Chinese (繁體中文) and English text.

Return ONLY a JSON object:
{
  "medication_name": "Full name including code, brand, generic name and strength",
  "quantity": "From 總量/Quantity field including number and unit",
  "administration_text": "EXACT text from 用法用量/Administration section",
  "amount_each_intake": "How many pills per dose from the administration text",
  "warning": "From 注意事項及警語 section. Return 'N/A' if not visible."
}

Rules: copy text exactly, return ONLY valid JSON."""

_PROMPT_ICONS = """This image shows an icon row from a Taiwanese hospital prescription bag.
There are exactly 6 icons: 早上 Morning, 中午 Noon, 晚上 Night, 睡前 Bedtime, 飯前 Before meals, 飯後 After meals.
Icons marked with a large 'V' letter are checked (true). Others are false.

Return ONLY:
{"morning": true/false, "noon": true/false, "night": true/false,
 "bedtime": true/false, "before_meals": true/false, "after_meals": true/false}"""

_CN_NUM = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8}


class OCRService:
    """Singleton wrapping YOLO document detection + Ollama two-pass OCR."""

    _instance: "OCRService | None" = None
    _available: bool = False

    def __init__(self):
        try:
            from ultralytics import YOLO
            self.yolo = YOLO(str(YOLO_MODEL_PATH))
            self.active_model = self._resolve_model()
            OCRService._available = True
            print(f"[OCR] YOLO loaded. Ollama model: {self.active_model}")
        except Exception as exc:
            OCRService._available = False
            print(f"[OCR] Not available: {exc}")

    @classmethod
    def get_instance(cls) -> "OCRService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _resolve_model(self) -> str | None:
        try:
            resp = requests.get("http://localhost:11434/api/tags", timeout=5)
            installed = [m["name"] for m in resp.json().get("models", [])]
            for preferred in OLLAMA_MODELS:
                for name in installed:
                    if preferred in name:
                        return name
        except Exception:
            pass
        return None

    def process_image(self, image_bytes: bytes) -> dict:
        """
        Accepts raw JPEG/PNG bytes, returns structured prescription dict.
        Keys match medication table columns: med_name, pill_prescribed,
        schedule_time (JSONB), total_intake, warning, amount_each_intake.
        """
        if not OCRService._available:
            return {"error": "OCR service not loaded"}

        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return {"error": "Cannot decode image"}

        warped = self._warp_with_yolo(img)
        if warped is None:
            warped = img

        enhanced = self._enhance(warped)
        icon_crop = self._crop_icon_row(enhanced)

        if not self.active_model:
            return {"error": "No Ollama vision model available. Run: ollama pull minicpm-v"}

        print("[OCR] Pass 1/2: text extraction…")
        text_data = self._call_ollama(_PROMPT_TEXT, enhanced)
        print("[OCR] Pass 2/2: icon row…")
        icon_data = self._call_ollama(_PROMPT_ICONS, icon_crop)

        admin_text = text_data.get("administration_text", "")
        amount_str = text_data.get("amount_each_intake", "")
        schedule = self._parse_schedule(icon_data, admin_text)
        total = self._calc_total(admin_text, amount_str)

        return {
            "med_name":           text_data.get("medication_name", "N/A"),
            "quantity":           text_data.get("quantity", "N/A"),
            "amount_each_intake": amount_str or "N/A",
            "total_intake":       total,
            "schedule_time":      schedule,
            "warning":            text_data.get("warning", "N/A"),
            "intake_time_label":  self._schedule_label(schedule, admin_text),
        }

    # ── internal helpers ──────────────────────────────────────────────────────

    def _warp_with_yolo(self, img):
        try:
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

    def _call_ollama(self, prompt: str, img) -> dict:
        _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
        b64 = base64.b64encode(buf).decode("utf-8")
        payload = {
            "model": self.active_model,
            "prompt": prompt,
            "images": [b64],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.0, "num_predict": 1500},
        }
        try:
            resp = requests.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT)
            return self._parse_json(resp.json().get("response", ""))
        except Exception:
            return {}

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
            return json.loads(text)
        except json.JSONDecodeError:
            try:
                return json.loads(text.replace("'", '"'))
            except Exception:
                return {}

    def _parse_schedule(self, icon_data: dict, admin_text: str) -> dict:
        base = {"morning": False, "noon": False, "night": False,
                "bedtime": False, "before_meals": False, "after_meals": False}
        parsed = {}
        if admin_text:
            if "早" in admin_text:            parsed["morning"] = True
            if "晚" in admin_text:            parsed["night"] = True
            if "中午" in admin_text:          parsed["noon"] = True
            if "睡前" in admin_text:          parsed["bedtime"] = True
            if "飯前" in admin_text:          parsed["before_meals"] = True
            if "飯後" in admin_text:          parsed["after_meals"] = True
            if "每天兩次" in admin_text or "一天兩次" in admin_text:
                if parsed.get("morning"):  parsed["night"] = True
                if parsed.get("night"):    parsed["morning"] = True
            if "每天三次" in admin_text or "一天三次" in admin_text:
                parsed.update({"morning": True, "noon": True, "night": True})
        timing = parsed if parsed else icon_data
        base.update({k: bool(v) for k, v in timing.items() if k in base})
        return base

    def _schedule_label(self, schedule: dict, admin_text: str) -> str:
        labels = {"morning": "早上", "noon": "中午", "night": "晚上",
                  "bedtime": "睡前", "before_meals": "飯前", "after_meals": "飯後"}
        parts = [labels[k] for k, v in schedule.items() if v]
        summary = " | ".join(parts) if parts else ""
        if summary and admin_text:
            return f"{admin_text} [{summary}]"
        return admin_text or summary or "N/A"

    def _calc_total(self, admin_text: str, amount_str: str) -> str:
        if not admin_text:
            return "N/A"
        freq = None
        m = re.search(r"(?:每天|每日|一天|1天)([一二兩三四五六七八\d]+)次", admin_text)
        if m:
            v = m.group(1)
            freq = _CN_NUM.get(v) or (int(v) if v.isdigit() else None)
        per_dose = None
        combined = f"{amount_str} {admin_text}"
        m2 = re.search(r"每次(\d+(?:\.\d+)?)\s*(?:顆|粒|錠|片|包|ml|CC)", combined)
        if m2:
            per_dose = float(m2.group(1))
        days = None
        m3 = re.search(r"共(\d+)天", admin_text)
        if m3:
            days = int(m3.group(1))
        if freq and per_dose and days:
            total = int(freq * per_dose * days)
            return f"{total}顆 ({freq}次/天 × {int(per_dose)}顆/次 × {days}天)"
        parts = []
        if freq:     parts.append(f"{freq}次/天")
        if per_dose: parts.append(f"{int(per_dose)}顆/次")
        if days:     parts.append(f"{days}天")
        return f"N/A (partial: {' × '.join(parts)})" if parts else "N/A"
