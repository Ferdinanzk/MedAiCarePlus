import requests
from typing import List
from app.config import LINE_CHANNEL_ACCESS_TOKEN, LINE_API_URL


class LineService:
    """LINE Messaging API wrapper for sending notifications to family contacts."""

    _instance: "LineService | None" = None
    _available: bool = False

    def __init__(self):
        if LINE_CHANNEL_ACCESS_TOKEN:
            LineService._available = True
            print("[LINE] Service initialized")
        else:
            LineService._available = False
            print("[LINE] CHANNEL_ACCESS_TOKEN not set — notifications disabled")

    @classmethod
    def get_instance(cls) -> "LineService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def send_text(self, to: str, message: str) -> dict:
        """Send a text message to a LINE user by their user ID."""
        if not LineService._available:
            return {"sent": False, "error": "LINE not configured"}

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        }
        payload = {
            "to": to,
            "messages": [{"type": "text", "text": message}],
        }
        try:
            resp = requests.post(LINE_API_URL, json=payload, headers=headers, timeout=30)
            if resp.status_code == 200:
                return {"sent": True, "message_id": resp.headers.get("x-line-request-id", "")}
            return {"sent": False, "error": f"LINE API {resp.status_code}: {resp.text}"}
        except Exception as exc:
            return {"sent": False, "error": str(exc)}

    def send_missed_dose_alert(self, to: str, patient_name: str, medication_name: str, scheduled_time: str) -> dict:
        text = (
            f"⚠️ 用藥提醒\n"
            f"{patient_name} 錯過了預定用藥\n"
            f"藥物: {medication_name}\n"
            f"預定時間: {scheduled_time}\n"
            f"請關心確認狀況。"
        )
        return self.send_text(to, text)

    def send_emotion_alert(self, to: str, patient_name: str, emotion: str, score: float) -> dict:
        text = (
            f"💙 情緒提醒\n"
            f"{patient_name} 今天的情緒較低落\n"
            f"檢測結果: {emotion} ({score:.0%})\n"
            f"請多加關心。"
        )
        return self.send_text(to, text)

    def send_weekly_summary(self, to: str, patient_name: str, adherence: float, emotion_summary: str) -> dict:
        text = (
            f"📊 每週健康摘要\n"
            f"{patient_name} 本週用藥順從率: {adherence:.0%}\n"
            f"情緒狀態: {emotion_summary}\n"
            f"保持健康！"
        )
        return self.send_text(to, text)

    def send_verification_success(self, to: str, patient_name: str) -> dict:
        text = (
            f"✅ 驗證成功！\n"
            f"您已被設為 {patient_name} 的家人聯絡人。\n"
            f"當有重要健康提醒時，您將收到通知。"
        )
        return self.send_text(to, text)
