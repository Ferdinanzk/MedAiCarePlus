"""Real reminders (14 Oct 2026): 「一分鐘後提醒我吃藥」 fires one minute later, whatever GPT-Live remembers.

GPT-Live cannot keep time (its clock is the session's start, and nothing in a model counts minutes), so a reminder is a
row in the `reminder` table with a due moment computed here, on the server's clock in config.MEDCARE_TIMEZONE, from
what the patient said: "in N minutes" from the moment of the request, "at HH:MM" (today, or tomorrow once that time
has passed) and "tomorrow at HH:MM" on the real calendar. Each pending row is armed as an asyncio timer in this
process (the two uvicorn servers and the scheduler share one loop), and a sweep every minute re-arms what a restart
lost: a reminder found up to LATE_FIRE_SECONDS past its time still fires, an older one expires.

When it fires, Reachy speaks it with the live voice (OpenAI speech through med_clips.render, resampled to the robot's
16 kHz, rendered ahead at creation): through the running live chat's relay when one runs (LiveRelay.speak: binary
audio on the live WebSocket, which the robot plays even while GPT-Live is paused), else through a check-in task whose
detail carries the reminder, so the robot wakes, finds the patient and says it (live_session, api_device); else as a
LINE text to the patient; else it is `undelivered`. A medicine reminder names the medicine from the medication table
(「時間到了，請吃第一種藥，Adapalene。」): `position` is the n-th medicine of the slot due at the reminder's time in the
robot's own prompt order (med_name, med_id), a name the patient said is matched against their medicines
(tool_registry.names_match) and otherwise answered `medicine_not_found` with the real names, never echoed.
"""

import asyncio
import io
import json
import logging
import math
import re
import uuid
import wave
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import config
from app.database import get_pool
from app.services import ai_settings, consent_service, context_info, dose_status, med_clips, outbox, reachy_tasks

log = logging.getLogger(__name__)

RATE = 16000
MIN_SECONDS = 20                   # a reminder sooner than this is refused (it would fire before the answer is said)
MAX_DAYS = 7
LATE_FIRE_SECONDS = 15 * 60        # a pending reminder found this late (a restart, a stalled loop) still fires
ROBOT_WAIT_SECONDS = 20 * 60       # handed to a check-in task that never said it: LINE or undelivered after this
FIRING_STUCK_SECONDS = 5 * 60
CHECKIN_MINUTES = 15               # the reminder's check-in task expires after this (as POST /api/reachy/checkin)
MEDICINE_WORDS = ("藥", "药", "medicine", "medication", "pill", "tablet", "capsule", "drops")
ORDINALS = med_clips.ORDINALS
SENTENCES = {
    "named_position": {"zh-TW": "時間到了，請吃第{ordinal}種藥，{name}。",
                       "en": "It's time to take your {ordinal} medicine, {name}."},
    "named": {"zh-TW": "時間到了，請記得吃藥：{name}。", "en": "It's time to take your medicine, {name}."},
    "medicine": {"zh-TW": "時間到了，請記得吃藥。", "en": "It's time to take your medicine."},
    "plain": {"zh-TW": "提醒您：{what}。", "en": "A reminder: {what}."},
}
SLOT_SQL = ("SELECT i.intk_id, m.med_id, m.med_name FROM intake i JOIN medication m ON m.med_id = i.med_id "
            "WHERE i.u_id = $1 AND i.intk_id = ANY($2::int[]) ORDER BY m.med_name, m.med_id, i.intk_id")
MEDICINES_SQL = "SELECT med_id, med_name FROM medication WHERE u_id = $1 AND COALESCE(is_active, TRUE) ORDER BY med_id"
PATIENT_LINE_SQL = 'SELECT line_id FROM "user" WHERE u_id = $1'
INSERT_SQL = ("INSERT INTO reminder (reminder_id, u_id, text, what, med_id, med_name, position, language, due_at, "
              "conversation_id) VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10::uuid)")
ROW_SQL = ("SELECT reminder_id, u_id, text, med_id, med_name, position, language, due_at, status, task_id, created_at "
           "FROM reminder WHERE reminder_id = $1::uuid")
_TIME = re.compile(r"^\s*(?:(上午|早上|中午|下午|晚上|傍晚|凌晨|半夜|am|pm|a\.m\.|p\.m\.)\s*)?(\d{1,2})(?::|：|點|点)?(\d{2})?"
                   r"(?:分)?\s*(上午|早上|中午|下午|晚上|傍晚|凌晨|半夜|am|pm|a\.m\.|p\.m\.)?\s*$", re.IGNORECASE)
PM_WORDS = ("下午", "晚上", "傍晚", "pm", "p.m.")
AM_WORDS = ("上午", "早上", "凌晨", "半夜", "am", "a.m.")

_timers: dict[str, asyncio.Task] = {}
_renders: set = set()
_audio: dict[str, bytes] = {}
find_relay = lambda u_id: None   # noqa: E731  set by api_device: the patient's running live relay, or None


def _tz() -> ZoneInfo:
    return ZoneInfo(config.MEDCARE_TIMEZONE)


def now_local() -> datetime:
    return datetime.now(_tz())


class ReminderError(Exception):
    """A request that cannot be scheduled; str(error) is the tool's error code."""


def parse_clock(text: str) -> tuple[int, int]:
    """"20:00", "8pm", "8:30 PM", "晚上8點", "下午3點半" -> (hour, minute) on the 24-hour clock; ReminderError otherwise."""
    raw = str(text or "").strip().replace("點半", "點30").replace("点半", "点30").replace("半", ":30")
    match = _TIME.match(raw)
    if not match:
        raise ReminderError("invalid_time")
    period = (match.group(1) or match.group(4) or "").lower()
    hour, minute = int(match.group(2)), int(match.group(3) or 0)
    if minute > 59 or hour > 24:
        raise ReminderError("invalid_time")
    if period in PM_WORDS and hour < 12:
        hour += 12
    elif period in AM_WORDS and hour == 12:
        hour = 0
    elif period == "中午" and hour < 12 and hour != 12:
        hour += 12 if hour < 6 else 0
    if hour == 24:
        hour = 0
    return hour, minute


def due_time(now: datetime, *, in_minutes=None, at_time=None, day=None) -> tuple[datetime, str | None]:
    """The reminder's moment, aware in the home zone, and a note ("tomorrow": the clock time had passed today)."""
    if in_minutes is not None:
        try:
            minutes = float(in_minutes)
        except (TypeError, ValueError):
            raise ReminderError("invalid_time") from None
        if not math.isfinite(minutes) or minutes <= 0:
            raise ReminderError("invalid_time")
        due = now + timedelta(minutes=minutes)
        note = None
    elif at_time is not None:
        hour, minute = parse_clock(at_time)
        when = str(day or "").strip().lower()
        if when in ("", "today", "今天"):
            base = now.date()
        elif when in ("tomorrow", "明天"):
            base = now.date() + timedelta(days=1)
        else:
            try:
                base = date.fromisoformat(when)
            except ValueError:
                raise ReminderError("invalid_date") from None
        due = datetime(base.year, base.month, base.day, hour, minute, tzinfo=now.tzinfo)
        note = None
        if when in ("", "今天") and due <= now:
            due, note = due + timedelta(days=1), "tomorrow"   # 8 PM asked at 9 PM means tomorrow's
    else:
        raise ReminderError("no_time")
    if due <= now + timedelta(seconds=MIN_SECONDS):
        raise ReminderError("in_the_past")
    if due > now + timedelta(days=MAX_DAYS):
        raise ReminderError("too_far")
    return due.replace(microsecond=0), note


def spoken(when: datetime, language: str) -> str:
    """「下午2點26分」 / "2:26 in the afternoon", with the day when it is not today."""
    local = when.astimezone(_tz())
    period_zh, period_en = context_info.time_of_day(local.hour)
    today = now_local().date()
    if language == "zh-TW":
        day = "" if local.date() == today else ("明天" if local.date() == today + timedelta(days=1) else f"{local.month}月{local.day}日")
        return f"{day}{period_zh}{context_info.clock(local.hour, local.minute)}"
    day = "" if local.date() == today else ("tomorrow " if local.date() == today + timedelta(days=1) else f"{local.day} {context_info.MONTHS[local.month - 1]} ")
    return f"{day}{local.hour % 12 or 12}:{local.minute:02d} {period_en}"


def sentence(kind: str, language: str, *, name: str | None = None, position: int | None = None, what: str | None = None) -> str:
    lang = "zh-TW" if language == "zh-TW" else "en"
    name = (name or "").strip()[:med_clips.MAX_NAME]
    if kind == "named_position":
        ordinal = ORDINALS[lang][max(1, min(med_clips.MAX_POSITION, int(position or 1))) - 1]
        return SENTENCES[kind][lang].format(ordinal=ordinal, name=name)
    if kind == "named":
        return SENTENCES[kind][lang].format(name=name)
    if kind == "plain":
        return SENTENCES[kind][lang].format(what=(what or "").strip().rstrip("。.！!")[:120])
    return SENTENCES["medicine"][lang]


def _mentions_medicine(text: str | None) -> bool:
    low = (text or "").lower()
    return any(word in low for word in MEDICINE_WORDS)


async def _resolve_medicine(conn, u_id: int, due: datetime, medication_name: str | None, position: int | None) -> dict:
    """{"kind", "med_id", "med_name", "position"} or {"error": "medicine_not_found", "medicines": [...]}"""
    from app.services.tool_registry import names_match   # here: tool_registry imports this module
    if medication_name:
        rows = await conn.fetch(MEDICINES_SQL, u_id)
        mine = [row for row in rows if names_match(medication_name, row["med_name"])]
        if not mine:
            return {"error": "medicine_not_found", "medicines": [row["med_name"] for row in rows]}
        row = mine[0]
        return {"kind": "named", "med_id": row["med_id"], "med_name": row["med_name"], "position": None}
    if position:
        today = await conn.fetch(dose_status.TODAY_SQL, u_id, due, config.MEDCARE_TIMEZONE)
        ids = dose_status.slot_of(today, due)
        slot = await conn.fetch(SLOT_SQL, u_id, ids) if ids else []
        index = int(position) - 1
        if 0 <= index < len(slot):
            row = slot[index]
            return {"kind": "named_position", "med_id": row["med_id"], "med_name": row["med_name"],
                    "position": int(position)}
        return {"kind": "medicine", "med_id": None, "med_name": None, "position": int(position),
                "note": "no_such_dose_in_that_slot" if slot else "no_dose_scheduled_then"}
    return {"kind": "medicine", "med_id": None, "med_name": None, "position": None}


async def create(u_id: int, *, what: str | None = None, medication_name: str | None = None, position: int | None = None,
                 in_minutes=None, at_time: str | None = None, day: str | None = None, language: str = "zh-TW",
                 conversation_id: str | None = None) -> dict:
    """Schedule one reminder; the tool's answer. Errors come back as {"status": "error", "error": code} (no_time,
    invalid_time, invalid_date, in_the_past, too_far, nothing_to_remind) or {"status": "medicine_not_found",
    "medicines": [...]}; never raises for bad input."""
    lang = "zh-TW" if language == "zh-TW" else "en"
    now = now_local()
    try:
        due, note = due_time(now, in_minutes=in_minutes, at_time=at_time, day=day)
    except ReminderError as error:
        return {"status": "error", "error": str(error)}
    medicine = bool(medication_name) or bool(position) or _mentions_medicine(what)
    async with get_pool().acquire() as conn:
        if medicine:
            found = await _resolve_medicine(conn, u_id, due, medication_name, position)
            if "error" in found:
                return {"status": found["error"], "medicines": found["medicines"]}
            text = sentence(found["kind"], lang, name=found["med_name"], position=found["position"])
            note = found.get("note") or note
        else:
            if not (what or "").strip():
                return {"status": "error", "error": "nothing_to_remind"}
            found = {"med_id": None, "med_name": None, "position": None}
            text = sentence("plain", lang, what=what)
        reminder_id = str(uuid.uuid4())
        await conn.execute(INSERT_SQL, reminder_id, u_id, text, (what or "")[:300] or None, found["med_id"],
                           found["med_name"], found["position"], lang, due, conversation_id)
    arm({"reminder_id": reminder_id, "u_id": u_id, "due_at": due})
    _render_ahead(text, lang)
    log.info("[REMINDER] scheduled %s for patient %s at %s (%s)", reminder_id[:8], u_id, due.isoformat(timespec="seconds"), text)
    return {"status": "scheduled", "reminder_id": reminder_id, "due_at": due.isoformat(timespec="seconds"),
            "in_seconds": int((due - now).total_seconds()), "due_spoken": spoken(due, lang), "text": text,
            "medication_name": found["med_name"], "position": found["position"], "note": note}


async def cancel(u_id: int, reminder_id: str | None = None) -> dict:
    """Cancel one pending reminder of the patient, or all of them."""
    async with get_pool().acquire() as conn:
        if reminder_id:
            try:
                uuid.UUID(str(reminder_id))
            except ValueError:
                return {"status": "error", "error": "invalid_reminder_id"}
            rows = await conn.fetch("UPDATE reminder SET status = 'cancelled' WHERE u_id = $1 AND reminder_id = $2::uuid "
                                    "AND status IN ('pending', 'queued_robot') RETURNING reminder_id", u_id, str(reminder_id))
        else:
            rows = await conn.fetch("UPDATE reminder SET status = 'cancelled' WHERE u_id = $1 "
                                    "AND status IN ('pending', 'queued_robot') RETURNING reminder_id", u_id)
    for row in rows:
        timer = _timers.pop(str(row["reminder_id"]), None)
        if timer is not None:
            timer.cancel()
    return {"status": "cancelled", "count": len(rows)}


async def pending(u_id: int) -> list[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT reminder_id, text, due_at, med_name, position FROM reminder WHERE u_id = $1 "
                                "AND status IN ('pending', 'queued_robot') ORDER BY due_at", u_id)
    return [{"reminder_id": str(row["reminder_id"]), "text": row["text"],
             "due_at": row["due_at"].astimezone(_tz()).isoformat(timespec="seconds"), "medication_name": row["med_name"],
             "position": row["position"]} for row in rows]


# ── timers ──

def arm(row: dict) -> None:
    """Fire `row` at its due moment (an asyncio timer in this loop); armed once."""
    reminder_id = str(row["reminder_id"])
    timer = _timers.get(reminder_id)
    if timer is not None and not timer.done():
        return
    delay = max(0.0, (row["due_at"] - datetime.now(timezone.utc)).total_seconds())
    task = asyncio.get_running_loop().create_task(_wait_and_fire(reminder_id, delay), name=f"reminder-{reminder_id[:8]}")
    _timers[reminder_id] = task
    task.add_done_callback(lambda done: _timers.pop(reminder_id, None) if _timers.get(reminder_id) is done else None)


async def _wait_and_fire(reminder_id: str, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        await fire(reminder_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("[REMINDER] %s failed to fire", reminder_id[:8])


async def fire(reminder_id: str) -> None:
    """Deliver one reminder now: the live chat's relay, else a check-in task for the robot, else LINE, else undelivered."""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow("UPDATE reminder SET status = 'firing', fired_at = NOW() WHERE reminder_id = $1::uuid "
                                  "AND status = 'pending' RETURNING reminder_id, u_id, text, language, due_at", reminder_id)
    if row is None:
        return
    u_id, text, language = row["u_id"], row["text"], row["language"]
    log.info("[REMINDER] firing %s for patient %s: %s", reminder_id[:8], u_id, text)
    pcm = await audio(text, language)
    relay = find_relay(u_id)
    if relay is not None and relay.can_speak():
        try:
            if await relay.speak(text, pcm, reason="reminder"):
                await _mark(reminder_id, "fired", "live")
                return
        except Exception:
            log.exception("[REMINDER] %s: the live chat could not say it", reminder_id[:8])
    task_id = await _queue_checkin(reminder_id, u_id, text)
    if task_id is not None:
        log.info("[REMINDER] %s handed to check-in task %s (the robot says it once it finds the patient)",
                 reminder_id[:8], task_id[:8])
        return
    if await _line(reminder_id, u_id, text):
        await _mark(reminder_id, "fired", "line")
        return
    await _mark(reminder_id, "undelivered", None)
    log.warning("[REMINDER] %s for patient %s could not be delivered (no chat, no robot, no LINE)", reminder_id[:8], u_id)


async def _mark(reminder_id: str, status: str, via: str | None) -> None:
    async with get_pool().acquire() as conn:
        await conn.execute("UPDATE reminder SET status = $2, delivered_via = $3 WHERE reminder_id = $1::uuid",
                           reminder_id, status, via)
    log.info("[REMINDER] %s %s%s", reminder_id[:8], status, f" via {via}" if via else "")


async def _queue_checkin(reminder_id: str, u_id: int, text: str) -> str | None:
    """A check-in task whose detail carries the reminder: the robot wakes, finds the patient and the live relay (or
    the text chat) says it. None without a paired robot, camera consent or check-in consent."""
    now = datetime.now(timezone.utc)
    async with get_pool().acquire() as conn, conn.transaction():
        if not reachy_tasks.checkin_allowed(await consent_service.fetch_state(conn, u_id)):
            return None
        task_id = await reachy_tasks.enqueue_reachy_task(conn, u_id, now, [], "checkin",
                                                         now + timedelta(minutes=CHECKIN_MINUTES))
        if task_id is None:
            return None
        await conn.execute("UPDATE reachy_task SET detail = COALESCE(detail, '{}'::jsonb) || $2::jsonb "
                           "WHERE task_id = $1::uuid", task_id,
                           json.dumps({"reminder": {"reminder_id": reminder_id, "text": text}}))
        await conn.execute("UPDATE reminder SET status = 'queued_robot', task_id = $2::uuid WHERE reminder_id = $1::uuid",
                           reminder_id, task_id)
    return task_id


async def _line(reminder_id: str, u_id: int, text: str) -> bool:
    async with get_pool().acquire() as conn, conn.transaction():
        line_id = await conn.fetchval(PATIENT_LINE_SQL, u_id)
        if not line_id:
            return False
        return await outbox.enqueue(conn, u_id=u_id, recipient_line_id=line_id, kind="reminder", priority=0,
                                    messages=[{"type": "text", "text": text}], dedupe_key=f"reminder:{reminder_id}")


async def take_for_robot(reminder_id: str) -> dict | None:
    """The reminder a check-in task carries, for the relay or the text chat that opens it: its row while it still
    waits (queued_robot), else None (cancelled meanwhile, or already said)."""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(ROW_SQL, reminder_id)
    return dict(row) if row is not None and row["status"] == "queued_robot" else None


async def said_by_robot(reminder_id: str, via: str) -> None:
    await _mark(reminder_id, "fired", via)


# ── the sweep (every minute) and the start ──

async def sweep() -> dict:
    """Arm pending reminders this process does not hold (a restart), fire the ones due, expire the ones found far too
    late, and give up on reminders a check-in task never spoke. Returns the counts {armed, expired, undelivered}."""
    now = datetime.now(timezone.utc)
    counts = {"armed": 0, "expired": 0, "undelivered": 0}
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT reminder_id, u_id, text, due_at, status, fired_at FROM reminder "
                                "WHERE status IN ('pending', 'firing', 'queued_robot')")
    for row in rows:
        reminder_id = str(row["reminder_id"])
        if row["status"] == "pending":
            late = (now - row["due_at"]).total_seconds()
            if late > LATE_FIRE_SECONDS:
                await _mark(reminder_id, "expired", None)
                counts["expired"] += 1
            else:
                arm(row)
                counts["armed"] += 1
        elif row["status"] == "firing" and row["fired_at"] and (now - row["fired_at"]).total_seconds() > FIRING_STUCK_SECONDS:
            await _mark(reminder_id, "undelivered", None)
            counts["undelivered"] += 1
        elif row["status"] == "queued_robot" and row["fired_at"] and (now - row["fired_at"]).total_seconds() > ROBOT_WAIT_SECONDS:
            if await _line(reminder_id, row["u_id"], row["text"]):
                await _mark(reminder_id, "fired", "line")
            else:
                await _mark(reminder_id, "undelivered", None)
                counts["undelivered"] += 1
    return counts


async def reload() -> dict | None:
    """At startup: what a previous process left; the sweep's counts, or None when it failed (logged)."""
    try:
        return await sweep()
    except Exception:
        log.exception("[REMINDER] the start-up sweep failed")
        return None


# ── the voice ──

def to_pcm16k(wav_bytes: bytes) -> bytes:
    """A WAV (any rate, mono or stereo, 16-bit) as the live socket's PCM16 mono 16 kHz."""
    import numpy as np
    from scipy.signal import resample_poly
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
        frames = wav.readframes(wav.getnframes())
    if width != 2:
        raise ValueError("not 16-bit PCM")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != RATE:
        g = math.gcd(RATE, rate)
        samples = resample_poly(samples, RATE // g, rate // g)
    return np.clip(np.round(samples), -32768, 32767).astype("<i2").tobytes()


def _audio_path(key: str):
    return config.MED_CLIP_DIR / "reminders" / f"{key}.pcm"


async def audio(text: str, language: str) -> bytes | None:
    """The reminder as the live voice says it (PCM16 16 kHz), from the cache or rendered now; None without a live
    connection or when rendering failed (the relay then says nothing: a medicine name is never left to Matcha)."""
    target = ai_settings.live_target()
    if target is None:
        return None
    voice = med_clips.voice_for(target)
    key = med_clips.clip_key(text, voice)
    cached = _audio.get(key)
    if cached is not None:
        return cached
    path = _audio_path(key)
    if path.is_file():
        _audio[key] = path.read_bytes()
        return _audio[key]
    try:
        wav = await asyncio.to_thread(med_clips.render, text, voice, target.api_key)
        pcm = to_pcm16k(wav)
    except Exception as error:
        log.warning("[REMINDER] the voice could not render %r (%s)", text[:40], type(error).__name__)
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pcm)
    except OSError:
        pass
    _audio[key] = pcm
    return pcm


def _render_ahead(text: str, language: str) -> None:
    """Render the reminder's voice now, in the background, so nothing waits for the speech API when it fires."""
    task = asyncio.get_running_loop().create_task(audio(text, language))
    _renders.add(task)
    task.add_done_callback(_renders.discard)
