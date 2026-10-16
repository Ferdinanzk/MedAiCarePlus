"""A medication event family can judge for themselves: what happened, what the AI made of it, and the camera's
clip of that moment, sent on LINE whatever the AI's confidence (14 Oct 2026).

Until now family heard from the system only when a dose was recorded (the "dose taken" message with its clip) or
when the robot asked them to confirm one (the request with its clip). Everything the AI could not settle went
unreported: a label that did not match the expected medicine, a hand-to-mouth moment it was not sure of, a watch
that saw nothing, a chat that ended mid-watch, a dose overdose protection refused. Those are the events family
most need to see, so each of them is now one message here: the expected medicine, the label check, the intake
check with the AI's estimate, the time, a line when the camera read a clear negative mood, and the clip of the
moments before the event (services/dose_video.py's buffer; only with the dose-video consent, which the notice
covers) in the same LINE push, so the clip and the message can never be told apart. The AI's confidence changes the
words, never whether the message goes. Family are told plainly when the event needs their judgement.

One report per event: the caller gives a `dedupe` name (a watch id, a verification, a robot dose and outcome), the
outbox key is built from it per recipient, and a retried call changes nothing. Recipients are the family contacts
who get dose messages (dose_confirmation._eligible_contacts).
"""

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import MEDCARE_TIMEZONE
from app.database import get_pool
from app.services import dose_report, dose_video, outbox

log = logging.getLogger(__name__)

KIND = "dose_event"
PRIORITY = 1
# What each event is, for the message's first line.
HEADINGS = {
    "verification": ("🔍 藥物辨識需要您確認", "🔍 Medicine check needs your review"),
    "intake": ("🔍 服藥情況需要您確認", "🔍 Dose needs your review"),
    "watch": ("🔍 服藥情況需要您確認", "🔍 Dose needs your review"),
    "unresolved": ("🔍 服藥情況需要您確認", "🔍 Dose needs your review"),
    "refused": ("⛔ 用藥未記錄，請您查看", "⛔ Dose not recorded: please check"),
    "taken": ("✅ 用藥事件", "✅ Dose event"),
}
INTAKE_WORDS = {
    "taken": ("偵測到服藥動作", "an intake was detected"),
    "uncertain": ("有手到嘴邊的動作，但 AI 不確定", "a hand-to-mouth moment the AI was not sure of"),
    "not_taken": ("沒有看到服藥動作", "no intake was seen"),
    "not_seen": ("沒有看到服藥動作", "no intake was seen"),
    "patient_left": ("病人離開了鏡頭前，沒有看到服藥", "the patient left the camera before any intake was seen"),
    "still_monitoring": ("對話結束時還在觀察，沒有結果", "the chat ended while still watching: no result"),
    "no_camera": ("當時沒有鏡頭畫面", "the camera was not available"),
    "not_checked": ("未檢查", "not checked"),
}
_TZ = ZoneInfo(MEDCARE_TIMEZONE)
_tasks: set = set()
_sending: set = set()     # scheduled reports past their delay: making or sending their message (sending())
_clock = time.monotonic   # the buffer's clock (tests replace it here, never time.monotonic itself: asyncio's clock)


def _when(event_time) -> str:
    if isinstance(event_time, str):
        return event_time
    moment = event_time if isinstance(event_time, datetime) else datetime.now(_TZ)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_TZ)
    return moment.astimezone(_TZ).strftime("%m/%d %H:%M")


def message_text(*, patient: str, kind: str, medication_name: str | None, verification: dict | None,
                 intake: str | None, confidence: float | None, event_time, needs_review: bool, emotion: dict | None,
                 video: str, notes: list[tuple[str, str]] | None = None) -> str:
    """The LINE text, Chinese then English. `video`: "attached", "off" (no consent), "none" (no frames)."""
    heading_zh, heading_en = HEADINGS.get(kind, HEADINGS["intake"])
    zh = [heading_zh, f"病人：{patient}　時間：{_when(event_time)}"]
    en = [heading_en, f"Patient: {patient}, at {_when(event_time)}"]
    if medication_name:
        zh.append(f"預期藥物：{medication_name}")
        en.append(f"Expected medicine: {medication_name}")
    words = dose_report.verification_words(verification)
    if words:
        zh.append(f"藥物辨識：{words[0]}")
        en.append(f"Medicine check: {words[1]}")
    intake_words = INTAKE_WORDS.get(str(intake or ""))
    if intake_words:
        zh.append(f"服藥偵測：{intake_words[0]}")
        en.append(f"Intake check: {intake_words[1]}")
    if confidence is not None:
        found = dose_report.ai_estimate(confidence, None)
        zh.append(f"AI 判斷已服藥的可能性：{found['zh']}")
        en.append(f"AI estimate that it was taken: {found['en']}")
    for note_zh, note_en in notes or ():
        zh.append(note_zh)
        en.append(note_en)
    mood = dose_report.emotion_note(emotion)
    if mood:
        zh.append(mood[0])
        en.append(mood[1])
    if needs_review:
        zh.append("AI 無法確定實際情況，請您觀看影片後判斷。此事件尚未記錄為已服藥。")
        en.append("The AI could not determine what happened; please judge from the video. Nothing has been "
                  "recorded as taken.")
    if video == "attached":
        hours = int(dose_video.MAX_AGE.total_seconds() // 3600)
        zh.append(f"📹 下方影片是當時鏡頭拍到的畫面（您的 LINE 下載或播放完後會刪除，最長 {hours} 小時）。")
        en.append(f"📹 The video below is what the camera saw (deleted once your LINE has it, {hours} hours at most).")
    elif video == "off":
        zh.append("（沒有影片：病人未開啟用藥影片）")
        en.append("(No video: the patient has not switched dose videos on.)")
    else:
        zh.append("（沒有影片：當時沒有鏡頭畫面）")
        en.append("(No video: the camera had no frames of that moment.)")
    if confidence is not None:
        zh.append(dose_report.FOOTNOTE_ZH)
        en.append(dose_report.FOOTNOTE_EN)
    return "\n".join(zh) + "\n\n" + "\n".join(en)


def take_frames(u_id: int, event_started_at: float | None = None) -> list:
    """The buffered frames of the moment, taken now (before anything else arrives)."""
    return dose_video.clip_frames(u_id, _clock(), event_started_at)


async def report(u_id: int, *, kind: str, dedupe: str, intk_ids: list[int], frames: list | None = None,
                 medication_name: str | None = None, verification: dict | None = None, intake: str | None = None,
                 confidence: float | None = None, event_time=None, needs_review: bool = True,
                 emotion: dict | None = None, notes: list[tuple[str, str]] | None = None,
                 event_started_at: float | None = None, source: str = "reachy") -> dict:
    """Send the event to family: the text and, with the dose-video consent and frames of the moment, the clip in
    the same push. `frames`: taken by the caller (take_frames) when timing matters; else taken now. Returns
    {"event": dedupe, "video": bool, "contacts": n}. Never raises: a failure is logged and the text still goes."""
    if frames is None:
        frames = take_frames(u_id, event_started_at)
    video_id, video = None, "none"
    try:
        if len(frames) >= dose_video.MIN_FRAMES:
            if await dose_video.enabled(u_id):
                video_id, info = await dose_video.make_clip(frames)
                video = "attached"
            else:
                video = "off"
        base_url, no_link = await dose_video.public_link() if video_id is not None else (None, None)
        if video_id is not None and base_url is None:
            dose_video._unlink(video_id)
            video_id, video = None, "none"
            log.warning("dose event %s: no public link for the clip (%s); the text goes without it", dedupe, no_link)
        async with get_pool().acquire() as conn, conn.transaction():
            from app.services import dose_confirmation   # dose_confirmation -> intake_repository -> dose_video

            patient = await conn.fetchval('SELECT name FROM "user" WHERE u_id = $1', u_id) or "Patient"
            contacts = await dose_confirmation._eligible_contacts(conn, u_id)
            if not contacts:
                if video_id is not None:
                    dose_video._unlink(video_id)
                log.info("dose event %s: nobody to tell (no eligible family contact)", dedupe)
                return {"event": dedupe, "video": False, "contacts": 0}
            links = {}
            if video_id is not None:
                await dose_video.insert_clip(conn, video_id, u_id, intk_ids, source, info)
                links = await dose_video.link_messages(conn, video_id, base_url, contacts)
            text = message_text(patient=patient, kind=kind, medication_name=medication_name,
                                verification=verification, intake=intake, confidence=confidence,
                                event_time=event_time, needs_review=needs_review, emotion=emotion, video=video,
                                notes=notes)
            sent = 0
            for contact in contacts:
                messages = [{"type": "text", "text": text[:5000]}]
                if contact["id"] in links:
                    messages.append(links[contact["id"]])
                sent += await outbox.enqueue(conn, u_id=u_id, recipient_line_id=contact["line_id"], kind=KIND,
                                             priority=PRIORITY, messages=messages,
                                             dedupe_key=f"{KIND}:{dedupe}:{contact['id']}",
                                             recipient_contact_id=contact["id"])
        log.info("dose event %s (%s) for patient %s: %d message(s)%s", dedupe, kind, u_id, sent,
                 " with the clip" if video_id is not None else "")
        return {"event": dedupe, "video": video_id is not None, "contacts": sent}
    except Exception:
        log.exception("dose event %s (%s) for patient %s failed", dedupe, kind, u_id)
        if video_id is not None:
            dose_video._unlink(video_id)
        return {"event": dedupe, "video": False, "contacts": 0, "error": "failed"}


def schedule(u_id: int, *, delay: float = 0.0, **fields) -> asyncio.Task:
    """report() in the background, the frames taken now (the moment is this one, whatever the delay): the
    robot's request or the agent's tool answers at once. `delay`: seconds to wait first, so a caller that settles
    the event meanwhile can cancel the task (a watch the agent records right after) while it still waits
    (sending() is False). The task's result is report()'s answer: its "video" is True only when the clip went
    with the message."""
    frames = fields.pop("frames", None)
    if frames is None:
        frames = take_frames(u_id, fields.get("event_started_at"))

    async def run():
        if delay > 0:
            await asyncio.sleep(delay)
        _sending.add(asyncio.current_task())
        return await report(u_id, frames=frames, **fields)

    task = asyncio.get_running_loop().create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    task.add_done_callback(_sending.discard)
    return task


def sending(task) -> bool:
    """Whether a schedule() task has left its delay and is making or sending its message: cancelling it now could
    cut a message half made, so a caller waits for its outcome instead. False while it waits, and once it is done."""
    return task in _sending
