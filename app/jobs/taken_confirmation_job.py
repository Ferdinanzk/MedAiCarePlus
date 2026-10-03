"""Every minute: tell family that a slot's doses were taken, how each was recorded and what the AI made of it, and
(when the patient switched dose videos on) send the clip of each dose (services/dose_video.py).

As the patient works through a slot's pills, the taken ones are batched per (patient, 5-minute slot) and sent
BATCH_WINDOW_MINUTES after the earliest of them, so several pills make one message. Pills never taken are the
missed-dose job's. Every message goes through the notification outbox.
"""

import datetime
import json
from collections import defaultdict
from zoneinfo import ZoneInfo

from app.config import MEDCARE_TIMEZONE
from app.database import get_pool
from app.jobs.missed_dose_job import _slot_key
from app.services import dose_report, dose_video, outbox

# A batch is sent once this many minutes have passed since its earliest dose, so pills taken close together (it
# takes time to swallow several) make ONE message, and later ones start the next batch.
BATCH_WINDOW_MINUTES = 5

# Only recent doses: no flood of messages for old 'taken' rows, and a small working set. taken_notified persists,
# so the job is restart-safe.
RECENCY_HOURS = 2

_TZ = ZoneInfo(MEDCARE_TIMEZONE)

_METHODS = {
    "auto": ("App 鏡頭自動偵測", "the app's camera, automatically"),
    "reachy_prompted": ("Reachy 鏡頭偵測", "Reachy's camera"),
    "confirmed_by_user": ("鏡頭偵測後由本人確認", "the camera, then confirmed by the patient"),
    "caregiver_confirmed": ("家人在 LINE 確認", "a family member on LINE"),
    "manual": ("本人在 App 手動記錄", "marked taken in the app by the patient"),
}


def _evidence(row) -> dict:
    value = row.get("confirm_evidence")
    value = json.loads(value) if isinstance(value, str) else value
    return value if isinstance(value, dict) else {}


def assessment(row) -> dict:
    """How one dose was recorded and the AI's estimate that it was taken, in Chinese and English."""
    method = row.get("detection_method")
    zh_method, en_method = _METHODS.get(method, _METHODS["manual"])
    if method == "caregiver_confirmed" and row.get("confirmed_by"):
        zh_method, en_method = f"{row['confirmed_by']} 在 LINE 確認", f"{row['confirmed_by']} on LINE"
    evidence = _evidence(row)
    score = next((value for value in (dose_report.number(row.get("detection_confidence")),
                                      dose_report.number(row.get("detector_score")),
                                      dose_report.number(evidence.get("confidence"))) if value is not None), None)
    found = dose_report.ai_estimate(score, evidence, camera_used=method in _METHODS and method != "manual")
    return {"method_zh": zh_method, "method_en": en_method, "ai_zh": found["zh"], "ai_en": found["en"],
            "scored": found["scored"], "notes_zh": found["notes_zh"], "notes_en": found["notes_en"]}


def slot_label(slot) -> str:
    if slot.tzinfo is None:
        slot = slot.replace(tzinfo=datetime.timezone.utc)
    return slot.astimezone(_TZ).strftime("%H:%M")


def taken_message(patient: str, slot: str, rows: list, videos: int) -> str:
    zh = [f"✅ 用藥完成通知\n{patient} 已服用 {slot} 的藥物："]
    en = [f"✅ Dose taken\n{patient} took the {slot} medicine:"]
    scored = False
    for row in rows:
        found = assessment(row)
        scored |= found["scored"]
        zh.append(f"• {row['med_name']}\n  記錄方式：{found['method_zh']}\n  AI 判斷已服藥的可能性：{found['ai_zh']}"
                  + "".join(f"\n  {note}" for note in found["notes_zh"]))
        en.append(f"• {row['med_name']}\n  Recorded by: {found['method_en']}\n"
                  f"  AI estimate that it was taken: {found['ai_en']}"
                  + "".join(f"\n  Note: {note}" for note in found["notes_en"]))
    if scored:
        zh.append(dose_report.FOOTNOTE_ZH)
        en.append(dose_report.FOOTNOTE_EN)
    if videos:
        hours = int(dose_video.MAX_AGE.total_seconds() // 3600)
        count_zh, count_en = ("影片", "video is") if videos == 1 else (f" {videos} 段影片", f"{videos} videos are")
        zh.append(f"📹 下方{count_zh}是當時鏡頭拍到的畫面。您的 LINE 下載或播放完後，影片就會從我們的系統刪除"
                  f"（最長 {hours} 小時），請盡快觀看。")
        en.append(f"📹 The {count_en} what the camera saw at the time. Each is deleted from our system once your "
                  f"LINE app has downloaded or played it ({hours} hours at most), so please watch soon.")
    return "\n".join(zh) + "\n\n" + "\n".join(en)


async def check_taken_confirmations():
    now = datetime.datetime.now(datetime.timezone.utc)
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT i.intk_id AS id, i.u_id, i.intake_time_stamp, i.actual_intake_time,
                   i.detection_method, i.detection_confidence, m.med_name, u.name AS patient_name,
                   COALESCE(ns.notify_family_on_taken, TRUE) AS notify_family_on_taken,
                   ev.detector_score, dc.evidence AS confirm_evidence, fc.name AS confirmed_by
            FROM intake i
            JOIN medication m ON m.med_id = i.med_id
            JOIN "user" u ON u.u_id = i.u_id
            LEFT JOIN notification_settings ns ON ns.u_id = i.u_id
            LEFT JOIN LATERAL (SELECT e.detector_score FROM monitor_event e
                               WHERE e.intk_id = i.intk_id AND e.outcome = 'taken'
                               ORDER BY e.recorded_at DESC LIMIT 1) ev ON TRUE
            -- Only a dose family confirmed has a confirmation to report: one recorded otherwise (a dose taken again
            -- after an earlier record was undone, like 3 Oct's test records) must not borrow an old one's words.
            LEFT JOIN LATERAL (SELECT c.evidence, c.resolved_by, c.resolved_at FROM dose_confirmation c
                               WHERE i.intk_id = ANY(c.intk_ids) AND c.resolution = 'confirmed'
                                 AND i.detection_method = 'caregiver_confirmed'
                               ORDER BY c.resolved_at DESC LIMIT 1) dc ON TRUE
            LEFT JOIN family_contacts fc ON fc.id = dc.resolved_by
            WHERE i.intake_stats = 'taken'
              AND i.taken_notified = FALSE
              AND i.actual_intake_time IS NOT NULL
              -- A caregiver-confirmed dose is stored as taken when the patient was asked, up to hours before family
              -- answered; it is still news when the answer is recent.
              AND GREATEST(i.actual_intake_time, dc.resolved_at) >= $1
            ORDER BY i.intake_time_stamp, i.intk_id
            """,
            now - datetime.timedelta(hours=RECENCY_HOURS),
        )
        if not rows:
            return

        groups: dict[tuple, list] = defaultdict(list)
        for row in rows:
            groups[(row["u_id"], _slot_key(row["intake_time_stamp"]))].append(dict(row))

        for (u_id, slot), group in groups.items():
            intk_ids = [row["id"] for row in group]
            if not group[0]["notify_family_on_taken"]:
                # Master switch off: mark them so they are not looked at again; nobody will see their clips.
                async with conn.transaction():
                    await dose_video.discard(conn, u_id, intk_ids, "notifications_off")
                    await conn.execute("UPDATE intake SET taken_notified = TRUE WHERE intk_id = ANY($1::int[])",
                                       intk_ids)
                continue
            if (now - min(row["actual_intake_time"] for row in group)).total_seconds() < BATCH_WINDOW_MINUTES * 60:
                continue

            contacts = await conn.fetch(
                "SELECT id, line_id, name FROM family_contacts "
                "WHERE u_id = $1 AND notify_taken = TRUE AND verified = TRUE AND line_id IS NOT NULL "
                "AND relationship IS DISTINCT FROM 'user' ORDER BY id",
                u_id)
            label = slot_label(slot)
            # Looked up before the transaction: it may ask LINE and the tunnel (a few seconds at worst).
            base_url = (await dose_video.public_base_url()
                        if contacts and await dose_video.has_unsent(conn, u_id, intk_ids) else None)
            async with conn.transaction():
                videos = await dose_video.unsent_videos(conn, u_id, intk_ids) if contacts else []
                if videos and base_url is None:   # LINE can't download from us now: nobody will get these clips
                    for video in videos:
                        await dose_video.delete_video(conn, str(video["video_id"]), "no_public_link")
                    videos = []
                if not contacts:
                    await dose_video.discard(conn, u_id, intk_ids, "no_recipients")
                links = [await dose_video.link_messages(conn, str(video["video_id"]), base_url, contacts)
                         for video in videos]
                text = taken_message(group[0]["patient_name"], label, group, len(videos))
                # The key names the recording too: a dose undone and taken again (or a test record put back to
                # pending, as on 3 Oct) is news again, and the earlier message's key must not swallow it.
                recorded = int(min(row["actual_intake_time"] for row in group).timestamp())
                for contact in contacts:
                    await outbox.enqueue(
                        conn, u_id=u_id, recipient_line_id=contact["line_id"], kind="taken_confirmation",
                        priority=1, messages=[{"type": "text", "text": text[:5000]}],
                        dedupe_key=f"taken:{u_id}:{min(intk_ids)}:{recorded}:{contact['id']}",
                        recipient_contact_id=contact["id"])
                    for video, messages in zip(videos, links):
                        await outbox.enqueue(
                            conn, u_id=u_id, recipient_line_id=contact["line_id"], kind="dose_video",
                            priority=1, messages=[messages[contact["id"]]],
                            dedupe_key=dose_video.outbox_key(str(video["video_id"]), contact["id"]),
                            recipient_contact_id=contact["id"])
                    await conn.execute(
                        "INSERT INTO notification (u_id, category, type, message) "
                        "VALUES ($1, 'family', 'taken_confirmation', $2)",
                        u_id, f"Taken confirmation for {label} slot ({len(group)} med(s), {len(videos)} video(s)) "
                              f"sent to family: {contact['name']}")
                await conn.execute("UPDATE intake SET taken_notified = TRUE WHERE intk_id = ANY($1::int[])",
                                   intk_ids)
