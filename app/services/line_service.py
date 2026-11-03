import requests
from typing import List
from app.config import LINE_CHANNEL_ACCESS_TOKEN, LINE_API_URL
from app.services import line_pause

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
MAX_REPLY_MESSAGES = 5
REPLY_TIMEOUT_SECONDS = 10


def verification_success_text(patient_name: str) -> str:
    return (
        f"✅ 驗證成功！\n"
        f"您已被設為 {patient_name} 的家人聯絡人。\n"
        f"當有重要健康提醒時，您將收到通知。"
    )


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

    def send_text(self, to: str, message: str, *, kind: str) -> dict:
        """Send a text message to a LINE user by their user ID."""
        if line_pause.blocks(kind):
            return {"sent": False, "paused": True, "error": "reminders_paused"}
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

    def reply_messages(self, reply_token: str, messages: list[dict]) -> dict:
        """Answer an event with its reply token (free, not counted in the monthly quota).

        Status is 'accepted' on 2xx, 'invalid_token' on 400 (used up, expired or wrong), else 'failed'.
        Never raises, and never puts the token, the text or a LINE user id into its result.
        """
        if not LineService._available:
            return {"status": "failed", "http_status": None, "request_id": None, "error": "LINE not configured"}
        if not reply_token or not messages or len(messages) > MAX_REPLY_MESSAGES:
            return {"status": "failed", "http_status": None, "request_id": None, "error": "bad_reply_request"}
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        }
        try:
            resp = requests.post(LINE_REPLY_URL, json={"replyToken": reply_token, "messages": messages},
                                 headers=headers, timeout=REPLY_TIMEOUT_SECONDS)
        except Exception as exc:
            return {"status": "failed", "http_status": None, "request_id": None, "error": type(exc).__name__}
        request_id = resp.headers.get("x-line-request-id") or None
        if 200 <= resp.status_code < 300:
            return {"status": "accepted", "http_status": resp.status_code, "request_id": request_id, "error": None}
        status = "invalid_token" if resp.status_code == 400 else "failed"
        return {"status": status, "http_status": resp.status_code, "request_id": request_id,
                "error": f"LINE reply {resp.status_code}"}

    def reply_text(self, reply_token: str | None, to: str, message: str) -> dict:
        """Reply with the event's token; on anything but 'accepted' (or without a token) push instead."""
        if reply_token:
            result = self.reply_messages(reply_token, [{"type": "text", "text": message}])
            if result.get("status") == "accepted":
                return {"sent": True, "replied": True, "message_id": result.get("request_id") or ""}
        return self.send_text(to, message, kind=line_pause.OTHER)

    def push_messages(self, to: str, messages: list[dict], retry_key: str, *, kind: str) -> dict:
        """Push up to five messages with an idempotency key (used by the outbox dispatcher).

        LINE answers 409 when a request with the same X-Line-Retry-Key was
        already accepted, so a duplicate counts as accepted by the caller.
        """
        if line_pause.blocks(kind):
            return {"status": "paused", "http_status": None, "request_id": None, "error": "reminders_paused"}
        if not LineService._available:
            return {"status": "disabled", "http_status": None, "request_id": None, "error": "LINE not configured"}

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "X-Line-Retry-Key": retry_key,
        }
        try:
            resp = requests.post(LINE_API_URL, json={"to": to, "messages": messages}, headers=headers, timeout=30)
        except Exception as exc:
            return {"status": "failed", "http_status": None, "request_id": None, "error": str(exc)}
        request_id = resp.headers.get("x-line-request-id") or None
        if 200 <= resp.status_code < 300:
            return {"status": "accepted", "http_status": resp.status_code, "request_id": request_id, "error": None}
        if resp.status_code == 409:
            accepted_id = resp.headers.get("x-line-accepted-request-id") or request_id
            return {"status": "duplicate", "http_status": 409, "request_id": accepted_id, "error": None}
        return {"status": "failed", "http_status": resp.status_code, "request_id": request_id,
                "error": f"LINE API {resp.status_code}: {resp.text[:500]}"}

    def send_missed_dose_alert(self, to: str, patient_name: str, medication_name: str, scheduled_time: str,
                               offline: bool = False) -> dict:
        # offline: missed while the system was down, so no reminder went out (missed_dose_job.missed_offline)
        text = (
            f"⚠️ 用藥提醒\n"
            f"{patient_name} 錯過了預定用藥\n"
            f"藥物：\n{medication_name}\n"
            f"預定時間：{scheduled_time}\n"
            + ("（這段時間系統離線，沒能及時提醒。）\n" if offline else "") +
            f"請關心確認狀況。"
        )
        return self.send_text(to, text, kind=line_pause.OTHER)

    def send_taken_confirmation(self, to: str, patient_name: str, medication_names: str, slot_time: str) -> dict:
        text = (
            f"✅ 用藥完成通知\n"
            f"{patient_name} 已服用 {slot_time} 的藥物：\n"
            f"{medication_names}\n"
            f"感謝您的關心。"
        )
        return self.send_text(to, text, kind=line_pause.OTHER)

    def send_emotion_alert(self, to: str, patient_name: str, emotion: str, score: float) -> dict:
        text = (
            f"💙 情緒提醒\n"
            f"{patient_name} 今天的情緒較低落\n"
            f"檢測結果: {emotion} ({score:.0%})\n"
            f"請多加關心。"
        )
        return self.send_text(to, text, kind=line_pause.OTHER)

    def send_weekly_summary(self, to: str, patient_name: str, adherence: float, emotion_summary: str) -> dict:
        text = (
            f"📊 每週健康摘要\n"
            f"{patient_name} 本週用藥順從率: {adherence:.0%}\n"
            f"情緒狀態: {emotion_summary}\n"
            f"保持健康！"
        )
        return self.send_text(to, text, kind=line_pause.OTHER)

    def send_test_notification(self, to: str, patient_name: str) -> dict:
        text = (
            f"🔔 測試通知 / Test Notification\n"
            f"如果您看到這則訊息，代表您的 LINE 已成功連結為 {patient_name} 的家人聯絡人。\n"
            f"If you can see this message, your LINE is synced as a family contact of {patient_name}."
        )
        return self.send_text(to, text, kind=line_pause.OTHER)

    def send_verification_success(self, to: str, patient_name: str) -> dict:
        return self.send_text(to, verification_success_text(patient_name), kind=line_pause.OTHER)

    def send_sync_success_to_patient(self, to: str, contact_name: str) -> dict:
        text = (
            f"✅ 成功連結！\n"
            f"{contact_name} 已成功與您同步，\n"
            f"現在可以接收您的健康提醒通知。"
        )
        return self.send_text(to, text, kind=line_pause.OTHER)
