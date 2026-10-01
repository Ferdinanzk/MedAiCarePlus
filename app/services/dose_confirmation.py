"""Caregiver confirmation of doses the robot could not verify (spec 01 §8).

create() moves doses to 'pending_confirmation' without touching stock and queues a
LINE button template to every verified caregiver who receives missed-dose alerts.
The signed postback from a button resolves it: the first valid answer wins.
Every LINE message here goes through the notification outbox.
"""

import datetime
import hashlib
import hmac
import json
import uuid
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from app.config import MEDCARE_TIMEZONE, SECRET_KEY
from app.database import get_pool
from app.services import outbox

SOURCES = ("uncertain_detection", "unsupported_dose", "degraded", "auto_record_off", "patient_claim")
ANSWERS = ("taken", "not_taken")
ACTION = "dose_confirm"
REMINDER_AFTER = datetime.timedelta(minutes=60)
# Caregivers always get at least this long to answer, even when the patient's
# missed-dose window is short (default 10 min x (3 + 1) = 40 min), so the
# 60-minute reminder can actually fire.
MIN_ANSWER_WINDOW = datetime.timedelta(minutes=120)
_SIG_HEX = 16
_TZ = ZoneInfo(MEDCARE_TIMEZONE)

_REASONS = {
    "uncertain_detection": ("Reachy 無法確認服藥動作", "Reachy could not verify the intake"),
    "unsupported_dose": ("此劑型無法自動確認", "this dose type cannot be verified automatically"),
    "degraded": ("影像品質不足", "the camera feed was too poor to verify"),
    "auto_record_off": ("自動記錄未開啟", "automatic recording is off"),
    "patient_claim": ("病人表示已服用", "the patient says the dose was already taken"),
}
_ANSWER_TEXT = {"taken": ("已服用", "taken"), "not_taken": ("未服用", "not taken")}
_RESOLUTION_ANSWER = {"confirmed": "taken", "denied": "not_taken"}


# ── postback signing ──

def _signature(action: str, object_id: str, answer: str, contact_id) -> str:
    message = f"{action}|{object_id}|{answer}|{contact_id}".encode("utf-8")
    return hmac.new(SECRET_KEY.encode("utf-8"), message, hashlib.sha256).hexdigest()[:_SIG_HEX]


def sign_postback(action: str, object_id: str, answer: str, contact_id: int) -> str:
    return (f"action={action}&id={object_id}&a={answer}&c={contact_id}"
            f"&s={_signature(action, object_id, answer, contact_id)}")


def verify_postback(data: str) -> dict | None:
    """Return {action, id, answer, contact_id} for an untampered dose_confirm postback, else None."""
    if not isinstance(data, str) or len(data) > 300:
        return None
    try:
        pairs = parse_qsl(data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    fields = dict(pairs)
    if len(pairs) != len(fields) or set(fields) != {"action", "id", "a", "c", "s"}:
        return None
    action, object_id, answer, contact, sig = (fields[k] for k in ("action", "id", "a", "c", "s"))
    if not hmac.compare_digest(_signature(action, object_id, answer, contact).encode(), sig.encode("utf-8")):
        return None
    if action != ACTION or answer not in ANSWERS or not contact.isdigit():
        return None
    try:
        if str(uuid.UUID(object_id)) != object_id:
            return None
    except ValueError:
        return None
    return {"action": action, "id": object_id, "answer": answer, "contact_id": int(contact)}


# ── message building ──

def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _slot_label(when) -> str:
    if when is None:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when.astimezone(_TZ).strftime("%m/%d %H:%M")


async def _eligible_contacts(conn, u_id: int):
    return await conn.fetch(
        "SELECT id, name, line_id FROM family_contacts WHERE u_id = $1 AND verified = TRUE "
        "AND notify_missed = TRUE AND line_id IS NOT NULL AND relationship IS DISTINCT FROM 'user' ORDER BY id",
        u_id)


async def _details(conn, u_id: int, intk_ids: list[int]) -> dict:
    rows = await conn.fetch(
        "SELECT i.intk_id, i.intake_time_stamp, m.med_name FROM intake i JOIN medication m ON m.med_id = i.med_id "
        "WHERE i.intk_id = ANY($1::int[]) ORDER BY i.intake_time_stamp, i.intk_id",
        list(intk_ids))
    patient = await conn.fetchval('SELECT name FROM "user" WHERE u_id = $1', u_id) or "Patient"
    return {"patient": patient,
            "meds": "、".join(row["med_name"] for row in rows),
            "slot": _slot_label(rows[0]["intake_time_stamp"]) if rows else ""}


def _request_messages(details: dict, source: str, confirmation_id: str, contact_id: int,
                      reminder: bool = False) -> list[dict]:
    reason_zh, reason_en = _REASONS[source]
    patient, meds, slot = details["patient"], details["meds"], details["slot"]
    prefix_zh = "⏰ 提醒：尚未回覆\n" if reminder else ""
    prefix_en = "Reminder, still unanswered: " if reminder else ""
    detail = (f"{prefix_zh}💊 用藥確認\n{patient} {slot} 的用藥需要您確認（{reason_zh}）。\n"
              f"藥物：{meds}\n請查看藥盒後回覆。\n\n"
              f"{prefix_en}Please confirm {patient}'s {slot} dose ({reason_en}).\n"
              f"Medication: {meds}\nCheck the pill box, then answer below.")
    short = f"{patient} {slot}\n{meds}\n{reason_zh}\n{reason_en}"
    actions = []
    for answer, label in (("taken", "已服用 Taken"), ("not_taken", "未服用 Not taken")):
        actions.append({"type": "postback", "label": label, "displayText": label,
                        "data": sign_postback(ACTION, confirmation_id, answer, contact_id)})
    return [
        {"type": "text", "text": _clip(detail, 5000)},
        {"type": "template", "altText": _clip(detail, 400),
         "template": {"type": "buttons", "text": _clip(short, 160), "actions": actions}},
    ]


def _text(text: str) -> list[dict]:
    return [{"type": "text", "text": _clip(text, 5000)}]


# ── state changes ──

async def create(conn, *, u_id: int, task_id: str | None, intk_ids: list[int], source: str,
                 evidence: dict | None) -> str:
    """Ask caregivers to confirm doses. Raises ValueError when no listed dose is pending or missed."""
    if source not in SOURCES:
        raise ValueError(f"Unknown confirmation source {source}")
    async with conn.transaction():
        rows = await conn.fetch(
            "SELECT i.intk_id, i.intake_stats, i.intake_time_stamp, m.med_name "
            "FROM intake i JOIN medication m ON m.med_id = i.med_id "
            "WHERE i.u_id = $1 AND i.intk_id = ANY($2::int[]) AND i.intake_stats IN ('pending','missed') "
            "ORDER BY i.intake_time_stamp, i.intk_id FOR UPDATE OF i",
            u_id, [int(i) for i in intk_ids])
        if not rows:
            raise ValueError("No pending or missed dose to confirm")
        ids = [row["intk_id"] for row in rows]
        previous = {str(row["intk_id"]): row["intake_stats"] for row in rows}
        await conn.execute(
            "UPDATE intake SET intake_stats='pending_confirmation' WHERE intk_id = ANY($1::int[])", ids)
        confirmation_id = str(uuid.uuid4())
        await conn.execute(
            "INSERT INTO dose_confirmation (confirmation_id, u_id, task_id, intk_ids, previous_status, source, "
            "evidence) VALUES ($1::uuid, $2, $3::uuid, $4::int[], $5::jsonb, $6, $7::jsonb)",
            confirmation_id, u_id, task_id, ids, json.dumps(previous), source,
            json.dumps(evidence) if evidence is not None else None)
        patient = await conn.fetchval('SELECT name FROM "user" WHERE u_id = $1', u_id) or "Patient"
        details = {"patient": patient,
                   "meds": "、".join(row["med_name"] for row in rows),
                   "slot": _slot_label(rows[0]["intake_time_stamp"])}
        for contact in await _eligible_contacts(conn, u_id):
            await outbox.enqueue(
                conn, u_id=u_id, recipient_line_id=contact["line_id"], kind="dose_confirm", priority=1,
                messages=_request_messages(details, source, confirmation_id, contact["id"]),
                dedupe_key=f"dose_confirm:{confirmation_id}:{contact['id']}", recipient_contact_id=contact["id"])
    return confirmation_id


async def _lock(conn, confirmation_id: str):
    return await conn.fetchrow(
        "SELECT confirmation_id, u_id, intk_ids, previous_status, source, created_at, reminded_at, "
        "resolution, resolved_by FROM dose_confirmation WHERE confirmation_id = $1::uuid FOR UPDATE",
        str(confirmation_id))


def _previous(row) -> dict:
    previous = row["previous_status"]
    return json.loads(previous) if isinstance(previous, str) else dict(previous or {})


async def _restore(conn, row) -> list[int]:
    previous = _previous(row)
    restored = []
    for intake in await conn.fetch(
            "SELECT intk_id, med_id, intake_stats FROM intake WHERE intk_id = ANY($1::int[]) AND u_id = $2 "
            "ORDER BY intk_id FOR UPDATE", list(row["intk_ids"]), row["u_id"]):
        if intake["intake_stats"] != "pending_confirmation":
            continue
        await conn.execute("UPDATE intake SET intake_stats=$1 WHERE intk_id=$2",
                           previous.get(str(intake["intk_id"]), "pending"), intake["intk_id"])
        restored.append(intake["intk_id"])
    return restored


async def resolve(conn, confirmation_id: str, contact_id: int, answer: str) -> dict:
    """Apply a caregiver's answer once. 'taken' records the dose with a stock decrement;
    'not_taken' restores each dose's exact previous status."""
    if answer not in ANSWERS:
        raise ValueError(f"Unknown answer {answer}")
    async with conn.transaction():
        row = await _lock(conn, confirmation_id)
        if row is None:
            return {"status": "not_found"}
        if row["resolution"] is not None:
            return {"status": "already_resolved", "resolution": row["resolution"], "resolved_by": row["resolved_by"]}
        changed, stock_empty = [], []
        if answer == "taken":
            resolution = "confirmed"
            for intake in await conn.fetch(
                    "SELECT intk_id, med_id, intake_stats FROM intake WHERE intk_id = ANY($1::int[]) AND u_id = $2 "
                    "ORDER BY intk_id FOR UPDATE", list(row["intk_ids"]), row["u_id"]):
                if intake["intake_stats"] != "pending_confirmation":
                    continue
                stock = await conn.fetchval(
                    "UPDATE medication SET pills_remaining=pills_remaining-1 "
                    "WHERE med_id=$1 AND u_id=$2 AND pills_remaining>0 RETURNING pills_remaining",
                    intake["med_id"], row["u_id"])
                if stock is None:
                    # The caregiver saw the dose taken; record it even though stock was already 0.
                    stock_empty.append(intake["intk_id"])
                await conn.execute(
                    "UPDATE intake SET intake_stats='taken', actual_intake_time=NOW(), "
                    "detection_method='caregiver_confirmed', detection_confidence=NULL WHERE intk_id=$1",
                    intake["intk_id"])
                changed.append(intake["intk_id"])
        else:
            resolution = "denied"
            changed = await _restore(conn, row)
        await conn.execute(
            "UPDATE dose_confirmation SET resolution=$2, resolved_by=$3, resolved_at=$4 "
            "WHERE confirmation_id=$1::uuid",
            str(confirmation_id), resolution, contact_id, datetime.datetime.now(datetime.timezone.utc))
    return {"status": "resolved", "resolution": resolution, "u_id": row["u_id"], "intk_ids": changed,
            "stock_empty": stock_empty}


async def handle_postback(conn, data: str, sender_line_id: str) -> dict | None:
    """Webhook entry point. Returns None (and changes nothing) unless the payload HMAC is valid,
    the sender is that verified contact, and the contact belongs to the confirmation's patient."""
    parsed = verify_postback(data)
    if parsed is None or not sender_line_id:
        return None
    async with conn.transaction():
        contact = await conn.fetchrow(
            "SELECT id, u_id, name, line_id, verified, relationship FROM family_contacts WHERE id = $1",
            parsed["contact_id"])
        if (not contact or not contact["verified"] or contact["relationship"] == "user"
                or contact["line_id"] != sender_line_id):
            return None
        owner = await conn.fetchval("SELECT u_id FROM dose_confirmation WHERE confirmation_id = $1::uuid",
                                    parsed["id"])
        if owner is None or owner != contact["u_id"]:
            return None
        result = await resolve(conn, parsed["id"], contact["id"], parsed["answer"])
        await _enqueue_replies(conn, parsed, contact, result)
    outbox.wake()
    return result


async def _enqueue_replies(conn, parsed: dict, contact, result: dict) -> None:
    confirmation_id, u_id = parsed["id"], contact["u_id"]
    row = await _lock(conn, confirmation_id)
    details = await _details(conn, u_id, list(row["intk_ids"]))
    patient, slot = details["patient"], details["slot"]

    if result["status"] == "already_resolved":
        if result["resolution"] == "expired":
            text = (f"此確認請求已逾時（{patient} {slot}）。\n"
                    f"This request for {patient}'s {slot} dose has expired.")
        else:
            by = await conn.fetchval("SELECT name FROM family_contacts WHERE id = $1", result["resolved_by"]) \
                or "another contact"
            zh, en = _ANSWER_TEXT[_RESOLUTION_ANSWER[result["resolution"]]]
            text = (f"此確認已由 {by} 回覆：{zh}（{patient} {slot}）。\n"
                    f"Already answered by {by}: {en} ({patient}'s {slot} dose).")
        await outbox.enqueue(conn, u_id=u_id, recipient_line_id=contact["line_id"], kind="dose_confirm_reply",
                             priority=1, messages=_text(text), recipient_contact_id=contact["id"],
                             dedupe_key=f"dose_confirm_late:{confirmation_id}:{contact['id']}")
        return
    if result["status"] != "resolved":
        return

    zh, en = _ANSWER_TEXT[parsed["answer"]]
    ack = (f"已記錄：{patient} {slot} 的用藥（{details['meds']}）— {zh}。\n"
           f"Recorded: {patient}'s {slot} dose ({details['meds']}) — {en}.")
    if result.get("stock_empty"):
        ack += "\n注意：藥量已為 0，請補充藥物。 / Note: stock was already 0, please refill."
    await outbox.enqueue(conn, u_id=u_id, recipient_line_id=contact["line_id"], kind="dose_confirm_reply",
                         priority=1, messages=_text(ack), recipient_contact_id=contact["id"],
                         dedupe_key=f"dose_confirm_ack:{confirmation_id}:{contact['id']}")
    notice = (f"{contact['name']} 已回覆：{zh}（{patient} {slot}）。\n"
              f"{contact['name']} answered: {en} ({patient}'s {slot} dose).")
    for other in await _eligible_contacts(conn, u_id):
        if other["id"] == contact["id"]:
            continue
        await outbox.enqueue(conn, u_id=u_id, recipient_line_id=other["line_id"], kind="dose_confirm_reply",
                             priority=1, messages=_text(notice), recipient_contact_id=other["id"],
                             dedupe_key=f"dose_confirm_answered:{confirmation_id}:{other['id']}")


async def maintenance(now: datetime.datetime | None = None) -> None:
    """Expire after max(missed window, 2 h); remind once after 60 minutes."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    pool = get_pool()
    async with pool.acquire() as conn:
        open_rows = await conn.fetch(
            "SELECT dc.confirmation_id, dc.created_at, dc.reminded_at, "
            "COALESCE(ns.remind_after_minutes, 10) AS remind_after_minutes, "
            "COALESCE(ns.remind_after_retries, 3) AS remind_after_retries "
            "FROM dose_confirmation dc LEFT JOIN notification_settings ns ON ns.u_id = dc.u_id "
            "WHERE dc.resolution IS NULL ORDER BY dc.created_at")
        for open_row in open_rows:
            confirmation_id = str(open_row["confirmation_id"])
            window = max(MIN_ANSWER_WINDOW, datetime.timedelta(
                minutes=open_row["remind_after_minutes"] * (open_row["remind_after_retries"] + 1)))
            try:
                if open_row["created_at"] + window <= now:
                    await _expire(conn, confirmation_id, now)
                elif open_row["reminded_at"] is None and open_row["created_at"] + REMINDER_AFTER <= now:
                    await _remind(conn, confirmation_id, now)
            except Exception as exc:
                print(f"[DoseConfirmation] maintenance failed for {confirmation_id}: {exc}")


async def _expire(conn, confirmation_id: str, now: datetime.datetime) -> None:
    async with conn.transaction():
        row = await _lock(conn, confirmation_id)
        if row is None or row["resolution"] is not None:
            return
        await _restore(conn, row)
        await conn.execute(
            "UPDATE dose_confirmation SET resolution=$2, resolved_by=$3, resolved_at=$4 "
            "WHERE confirmation_id=$1::uuid",
            confirmation_id, "expired", None, now)


async def _remind(conn, confirmation_id: str, now: datetime.datetime) -> None:
    async with conn.transaction():
        row = await _lock(conn, confirmation_id)
        if row is None or row["resolution"] is not None or row["reminded_at"] is not None:
            return
        details = await _details(conn, row["u_id"], list(row["intk_ids"]))
        for contact in await _eligible_contacts(conn, row["u_id"]):
            await outbox.enqueue(
                conn, u_id=row["u_id"], recipient_line_id=contact["line_id"], kind="dose_confirm", priority=1,
                messages=_request_messages(details, row["source"], confirmation_id, contact["id"], reminder=True),
                dedupe_key=f"dose_confirm_reminder:{confirmation_id}:{contact['id']}",
                recipient_contact_id=contact["id"])
        await conn.execute("UPDATE dose_confirmation SET reminded_at=$2 WHERE confirmation_id=$1::uuid",
                           confirmation_id, now)
