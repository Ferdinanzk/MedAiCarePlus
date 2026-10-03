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
from app.services import dose_emotion, dose_report, dose_safety, outbox, schedule
from app.services.intake_repository import take_stock

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
    # 'degraded' is about the frame rate, not the picture: the detector is calibrated at ~15 frames per second.
    "degraded": ("鏡頭畫面不夠流暢，無法確認", "the camera stream was not smooth enough to verify"),
    "auto_record_off": ("自動記錄未開啟", "automatic recording is off"),
    "patient_claim": ("病人表示已服用", "the patient says the dose was already taken"),
}
# The frame rate a monitored dose needs to be recorded (monitor_service.FPS_MIN), quoted to caregivers.
FPS_NEEDED = 12
_ANSWER_TEXT = {"taken": ("已服用", "taken"), "not_taken": ("未服用", "not taken")}
_RESOLUTION_ANSWER = {"confirmed": "taken", "denied": "not_taken"}
# Why a dose family answered 'taken' was not recorded (overdose protection, judged when the patient was asked).
_REFUSED_TEXT = {
    "dose_not_due_yet": ("當時還沒到服藥時間", "was not due yet"),
    "dose_too_soon": ("距離上一次記錄的服藥時間太近", "came too soon after the last recorded dose"),
    "daily_max_reached": ("當天已經達到每日服用上限", "was over the medicine's daily maximum"),
    "dose_expired": ("當時已經錯過，不能補吃", "had already been missed and is not made up"),
}


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


def _reason(source: str, evidence) -> tuple[str, str]:
    """Why caregivers are asked. A choppy camera stream also says how many frames per second it reached."""
    reason_zh, reason_en = _REASONS[source]
    fps = evidence.get("landmark_fps") if isinstance(evidence, dict) else None
    if (source == "degraded" and isinstance(fps, (int, float)) and not isinstance(fps, bool)
            and 0 <= fps < FPS_NEEDED):
        reason_zh += f"：每秒 {fps:.1f} 張畫面，需要 {FPS_NEEDED} 張"
        reason_en += f": {fps:.1f} frames per second, {FPS_NEEDED} needed"
    return reason_zh, reason_en


def _request_messages(details: dict, source: str, confirmation_id: str, contact_id: int,
                      reminder: bool = False, evidence: dict | None = None) -> list[dict]:
    reason_zh, reason_en = _reason(source, evidence)
    patient, meds, slot = details["patient"], details["meds"], details["slot"]
    # When the medicine was last recorded and how early this dose is (dose_safety.request_notes), so family can tell
    # a second dose from a late one before answering.
    notes_zh = "".join(f"{line}\n" for line in details.get("notes_zh", ()))
    notes_en = "".join(f"{line}\n" for line in details.get("notes_en", ()))
    prefix_zh = "⏰ 提醒：尚未回覆\n" if reminder else ""
    prefix_en = "Reminder, still unanswered: " if reminder else ""
    # What the camera made of it, even when it was too unsure (or the stream too choppy) to record the dose.
    # The frame rate is already in the reason, so only the patient's own words are added as a note.
    evidence = evidence if isinstance(evidence, dict) else {}
    found = dose_report.ai_estimate(dose_report.number(evidence.get("confidence")), {**evidence, "degraded": False})
    ai_zh = "".join([f"AI 判斷已服藥的可能性：{found['zh']}\n", *(f"{note}\n" for note in found["notes_zh"]),
                     dose_report.FOOTNOTE_ZH + "\n" if found["scored"] else ""])
    ai_en = "".join([f"AI estimate that it was taken: {found['en']}\n",
                     *(f"Note: {note}\n" for note in found["notes_en"]),
                     dose_report.FOOTNOTE_EN + "\n" if found["scored"] else ""])
    detail = (f"{prefix_zh}💊 用藥確認\n{patient} {slot} 的用藥需要您確認（{reason_zh}）。\n"
              f"藥物：{meds}\n{notes_zh}{ai_zh}請查看藥盒後回覆。\n\n"
              f"{prefix_en}Please confirm {patient}'s {slot} dose ({reason_en}).\n"
              f"Medication: {meds}\n{notes_en}{ai_en}Check the pill box, then answer below.")
    # The buttons card holds 160 characters: it gives the plain reason, and the text above it the frame rate.
    short_zh, short_en = _REASONS[source]
    short = f"{patient} {slot}\n{meds}\n{short_zh}\n{short_en}"
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
                 evidence: dict | None, started_at: datetime.datetime | None = None) -> str:
    """Ask caregivers to confirm doses. Raises ValueError when no listed dose is pending or missed, and a
    schedule.DoseRefused (nothing changed) when overdose protection refuses one: family must never be asked to
    confirm a dose hours before its time, a second dose, or a missed one. Every source comes from the robot seeing a
    hand-to-mouth event or the patient saying it is done, so the caller alerts family when the refusal is a suspected
    double dose (dose_safety.alert_after, after this transaction rolled back), and the patient's sentence says the
    dose was not recorded. `started_at` is when the robot's camera session for the dose began: due and not expired
    are judged then (dose_safety.evaluate). A dose waiting here counts against the medicine's next one (R2/R3), so
    the medication row is locked as on every recording path."""
    if source not in SOURCES:
        raise ValueError(f"Unknown confirmation source {source}")
    async with conn.transaction():
        rows = await conn.fetch(
            "SELECT i.intk_id, i.intake_stats, i.intake_time_stamp, "
            "m.med_name FROM intake i JOIN medication m ON m.med_id = i.med_id "
            "WHERE i.u_id = $1 AND i.intk_id = ANY($2::int[]) AND i.intake_stats IN ('pending','missed') "
            "ORDER BY i.intake_time_stamp, i.intk_id FOR UPDATE OF i",
            u_id, [int(i) for i in intk_ids])
        if not rows:
            raise ValueError("No pending or missed dose to confirm")
        now = schedule.current_time()
        notes_zh, notes_en = dose_safety.request_notes(
            await dose_safety.check(conn, u_id, [row["intk_id"] for row in rows], at=now, lock=True,
                                    started_at=started_at, after_intake=True), now)
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
                   "slot": _slot_label(rows[0]["intake_time_stamp"]),
                   "notes_zh": notes_zh, "notes_en": notes_en}
        for contact in await _eligible_contacts(conn, u_id):
            await outbox.enqueue(
                conn, u_id=u_id, recipient_line_id=contact["line_id"], kind="dose_confirm", priority=1,
                messages=_request_messages(details, source, confirmation_id, contact["id"], evidence=evidence),
                dedupe_key=f"dose_confirm:{confirmation_id}:{contact['id']}", recipient_contact_id=contact["id"])
    # The robot's camera session for the dose resolved it here: its facial-expression result is written shortly
    # after (dose_emotion, in the background). In memory only, never raises: the request can't fail because of it.
    dose_emotion.note_resolution_for(u_id, ids)
    return confirmation_id


async def _lock(conn, confirmation_id: str):
    return await conn.fetchrow(
        "SELECT confirmation_id, u_id, intk_ids, previous_status, source, evidence, created_at, reminded_at, "
        "resolution, resolved_by FROM dose_confirmation WHERE confirmation_id = $1::uuid FOR UPDATE",
        str(confirmation_id))


def _previous(row) -> dict:
    previous = row["previous_status"]
    return json.loads(previous) if isinstance(previous, str) else dict(previous or {})


def _evidence(row) -> dict | None:
    evidence = row["evidence"]
    try:
        evidence = json.loads(evidence) if isinstance(evidence, str) else evidence
    except ValueError:
        return None
    return evidence if isinstance(evidence, dict) else None


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
    'not_taken' restores each dose's exact previous status.

    'taken' is judged as of when the patient was asked (created_at), which is also stored as the time the dose was
    taken: family may answer up to 2 h later, and the next dose's minimum gap counts from when the pill went down.
    Judged as of then means with the doses there were then: an ad-hoc Take Now made since does not move this dose's
    halfway points (dose_safety.FACTS_SQL), but a pill recorded since close to this one counts for the gap. A dose
    overdose protection refuses at that moment (create() refuses most already; requests from before a rule or from
    before a dose taken meanwhile remain) is never recorded: it goes back to its previous status and is listed in
    `refused` (and in `not_due` when it was not due yet). A second dose (too soon, or over the daily maximum) that
    family saw taken alerts family. When nothing was recorded the request is closed as 'expired', not 'confirmed':
    later answerers are told it expired, and taken_confirmation_job never credits this contact with a confirmation."""
    if answer not in ANSWERS:
        raise ValueError(f"Unknown answer {answer}")
    async with conn.transaction():
        row = await _lock(conn, confirmation_id)
        if row is None:
            return {"status": "not_found"}
        if row["resolution"] is not None:
            return {"status": "already_resolved", "resolution": row["resolution"], "resolved_by": row["resolved_by"]}
        changed, stock_empty, not_due, refused = [], [], [], []
        if answer == "taken":
            resolution = "confirmed"
            previous = _previous(row)
            asked = row["created_at"]
            for intake in await conn.fetch(
                    "SELECT intk_id, med_id, intake_stats FROM intake WHERE intk_id = ANY($1::int[]) AND u_id = $2 "
                    "ORDER BY intk_id FOR UPDATE",
                    list(row["intk_ids"]), row["u_id"]):
                if intake["intake_stats"] != "pending_confirmation":
                    continue
                # One dose at a time: a dose recorded just before counts for the next one's gap and daily maximum.
                facts = await dose_safety.facts(conn, row["u_id"], [intake["intk_id"]], asked, lock=True)
                refusal = dose_safety.evaluate(facts[0], asked) if facts else None
                if refusal is not None:
                    refusal.after_intake = True
                    await conn.execute("UPDATE intake SET intake_stats=$1 WHERE intk_id=$2",
                                       previous.get(str(intake["intk_id"]), "pending"), intake["intk_id"])
                    refused.append({"intk_id": intake["intk_id"], "detail": refusal.detail})
                    if isinstance(refusal, schedule.DoseNotDueYet):
                        not_due.append(intake["intk_id"])
                    await dose_safety.alert_family(conn, refusal)
                    continue
                used = await take_stock(conn, intake["med_id"], row["u_id"])
                if used is None:
                    # The caregiver saw the dose taken; record it even though stock had run out.
                    stock_empty.append(intake["intk_id"])
                await conn.execute(
                    "UPDATE intake SET intake_stats='taken', actual_intake_time=$3, taken_notified=FALSE, "
                    "detection_method='caregiver_confirmed', detection_confidence=NULL, units_taken=$2 "
                    "WHERE intk_id=$1",
                    intake["intk_id"], used[1] if used else 0, asked)
                changed.append(intake["intk_id"])
            if refused and not changed:
                resolution = "expired"
        else:
            resolution = "denied"
            changed = await _restore(conn, row)
        await conn.execute(
            "UPDATE dose_confirmation SET resolution=$2, resolved_by=$3, resolved_at=$4 "
            "WHERE confirmation_id=$1::uuid",
            str(confirmation_id), resolution, contact_id, datetime.datetime.now(datetime.timezone.utc))
    return {"status": "resolved", "resolution": resolution, "u_id": row["u_id"], "intk_ids": changed,
            "stock_empty": stock_empty, "not_due": not_due, "refused": refused}


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


def _refused_reasons(result: dict) -> tuple[str, str] | None:
    """(中文, English) for the doses a 'taken' answer did not record, or None."""
    details = [item["detail"] for item in result.get("refused") or []] or \
        ["dose_not_due_yet" for _ in result.get("not_due") or []]
    if not details:
        return None
    texts = [_REFUSED_TEXT.get(detail, _REFUSED_TEXT["dose_not_due_yet"]) for detail in dict.fromkeys(details)]
    return "、".join(zh for zh, _ in texts), "; ".join(en for _, en in texts)


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
    refused = _refused_reasons(result)
    if refused and not result.get("intk_ids"):
        ack = (f"未記錄：{patient} {slot} 的用藥（{details['meds']}）{refused[0]}，所以沒有記錄為已服用。\n"
               f"Not recorded: {patient}'s {slot} dose ({details['meds']}) {refused[1]}, "
               f"so it was not recorded as taken.")
    elif refused:
        ack += (f"\n注意：其中有藥{refused[0]}，沒有記錄為已服用。 / "
                f"Note: a dose that {refused[1]} was not recorded as taken.")
    if result.get("stock_empty"):
        ack += "\n注意：藥量已為 0，請補充藥物。 / Note: stock was already 0, please refill."
    await outbox.enqueue(conn, u_id=u_id, recipient_line_id=contact["line_id"], kind="dose_confirm_reply",
                         priority=1, messages=_text(ack), recipient_contact_id=contact["id"],
                         dedupe_key=f"dose_confirm_ack:{confirmation_id}:{contact['id']}")
    notice = (f"{contact['name']} 已回覆：{zh}（{patient} {slot}）。\n"
              f"{contact['name']} answered: {en} ({patient}'s {slot} dose).")
    # The other contacts must not read "answered: taken" for a dose that was not recorded.
    if refused and not result.get("intk_ids"):
        notice = (f"{contact['name']} 回覆已服用，但 {patient} {slot} 的用藥{refused[0]}，所以沒有記錄為已服用。\n"
                  f"{contact['name']} answered taken, but {patient}'s {slot} dose {refused[1]}, "
                  f"so it was not recorded as taken.")
    elif refused:
        notice += (f"\n注意：其中有藥{refused[0]}，沒有記錄為已服用。 / "
                   f"Note: a dose that {refused[1]} was not recorded as taken.")
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
        # The same notes as the request, as of when the patient was asked.
        details["notes_zh"], details["notes_en"] = dose_safety.request_notes(
            await dose_safety.facts(conn, row["u_id"], list(row["intk_ids"]), row["created_at"]), row["created_at"])
        for contact in await _eligible_contacts(conn, row["u_id"]):
            await outbox.enqueue(
                conn, u_id=row["u_id"], recipient_line_id=contact["line_id"], kind="dose_confirm", priority=1,
                messages=_request_messages(details, row["source"], confirmation_id, contact["id"], reminder=True,
                                           evidence=_evidence(row)),
                dedupe_key=f"dose_confirm_reminder:{confirmation_id}:{contact['id']}",
                recipient_contact_id=contact["id"])
        await conn.execute("UPDATE dose_confirmation SET reminded_at=$2 WHERE confirmation_id=$1::uuid",
                           confirmation_id, now)
