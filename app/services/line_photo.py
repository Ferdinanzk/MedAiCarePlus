"""Medicine photos over LINE (docs/superpowers/plans/2026-11-08-line-medicine-photo.md, Task 3).

A patient sends a photo of a medicine bag in a one-to-one chat. `receive` (called inline by the webhook) checks who
sent it, the patient's consent and the hourly limit, and writes one `line_photo_draft` row per photo; `process` (a
background task) downloads the photo, reads it with the OCR and answers with the list and a Save all button. Nothing
is saved until the patient presses Save all (`handle_save`, Task 4: what the Scan page would save, once, in one
transaction) or saves on the Scan page.

The photo is never stored or logged. Logs never hold LINE user ids, reply tokens, message ids, OCR text or medicine
names: one line per photo, `[LINE] photo: <outcome>`, and one per Save all, `[LINE] photo save: <outcome>`."""

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from pydantic import ValidationError

from app.config import OCR_GEMINI_IMAGE_BUDGET, OLLAMA_TIMEOUT, SECRET_KEY
from app.database import get_pool
from app.routers.api_medications import MedicationPayload, insert_medication
from app.services import consent_service, memory, outbox, scan_rules
from app.services.line_service import LineService
from app.services.ocr_service import OCRService, OCRServiceError

log = logging.getLogger(__name__)

KIND = "line_photo"
PRIORITY = 1
MAX_BYTES = 10 * 1024 * 1024
PER_HOUR = 10
LOADING_SECONDS = 60
SAVE_ACTION = "line_photo_save"
_SIG_HEX = 16
_MAX_DATA = 300          # LINE's limit on postback data
_MAX_TEXT = 5000         # LINE's limit on a text message

NOT_LINKED_TEXT = "請先在 App 裡連結您的 LINE。/ Please link your LINE in the app first."
FAMILY_TEXT = "只有病人本人可以用照片新增藥物。/ Only the patient can add medicines by photo."
CONSENT_TEXT = "請先打開 App 並同意使用條款。/ Please open the app and accept the terms first."
TOO_MANY_TEXT = "目前照片太多，請稍後再試。/ Too many photos right now, please try again later."
TOO_BIG_TEXT = "這張照片太大了，請傳小一點的照片。/ This photo is too large. Please send a smaller one."
UNREADABLE_TEXT = "我看不清楚這張照片，請在光線好的地方再拍一次。/ I couldn't read this photo. Please take it again in good light."
BUSY_TEXT = "讀取服務現在忙碌中，請過幾分鐘再傳一次。/ The reader is busy right now. Please send the photo again in a few minutes."
FOOTER_TEXT = "要修改的話，請打開 App 的掃描頁。/ To change them, open the Scan page in the app."
NOTHING_TEXT = "沒有可以直接儲存的藥，請打開 App 的掃描頁檢查。/ Nothing can be saved directly. Please check on the Scan page in the app."
SAVED_TEXT = "已儲存 {n} 種藥。/ Saved {n} medicines."
ALREADY_TEXT = "已經儲存過了。/ Already saved."
EXPIRED_TEXT = "這份清單已過期，請重新傳送照片。/ This list has expired. Please send the photo again."
SAVE_FAILED_TEXT = "儲存失敗，請在 App 的掃描頁再試一次。/ Couldn't save. Please try again on the Scan page in the app."
SKIPPED_DUPLICATE = "{name}（已在清單中 / already saved）"
SKIPPED_CHECK = "{name}（請在 App 檢查 / check in the app）"
SAVE_LABEL = "全部儲存 Save all"
BUTTON_TEXT = "按「全部儲存」就會加入您的藥物清單。"

# OCR codes that mean the photo itself could not be read; every other failure is the reader's.
UNREADABLE_CODES = frozenset({"invalid_image", "ocr_no_prescription_text", "ocr_incomplete_schedule"})
SLOT_WORDS = {"morning": "早上", "noon": "中午", "night": "晚上", "bedtime": "睡前"}
DUPLICATE_MARK = "（已在清單中）"
CHECK_MARK = "（請在 App 檢查）"

_tasks: set = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def _logged(outcome: str) -> str:
    log.info("[LINE] photo: %s", outcome)
    return outcome


def _text(text: str) -> dict:
    return {"type": "text", "text": text[:_MAX_TEXT]}


# ── the Save all postback data ──

def _save_signature(draft_id: str) -> str:
    message = f"{SAVE_ACTION}|{draft_id}".encode("utf-8")
    return hmac.new(SECRET_KEY.encode("utf-8"), message, hashlib.sha256).hexdigest()[:_SIG_HEX]


def sign_save(draft_id: str) -> str:
    return f"action={SAVE_ACTION}&id={draft_id}&s={_save_signature(draft_id)}"


def verify_save(data: str) -> str | None:
    """The draft id of an untampered Save all postback, else None."""
    if not isinstance(data, str) or len(data) > _MAX_DATA:
        return None
    try:
        pairs = parse_qsl(data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    fields = dict(pairs)
    if len(pairs) != len(fields) or set(fields) != {"action", "id", "s"} or fields["action"] != SAVE_ACTION:
        return None
    draft_id = fields["id"]
    try:
        if str(uuid.UUID(draft_id)) != draft_id:
            return None
    except ValueError:
        return None
    if not hmac.compare_digest(_save_signature(draft_id).encode(), fields["s"].encode("utf-8")):
        return None
    return draft_id


# ── the list ──

def _line(index: int, draft: dict) -> str:
    med = draft["source"]
    amount = med.get("amount_each_intake")
    if not isinstance(amount, str) or amount.strip() in ("", "N/A"):
        amount = f"每次 {draft['unitsPerDose']}"
    if draft["asNeeded"]:
        times = "需要時服用"
    else:
        names = [SLOT_WORDS[slot] for slot in scan_rules.TIME_SLOTS if draft["slots"].get(slot)]
        names += draft["customTimes"]
        times = f"一天 {len(names)} 次（{'、'.join(names)}）" if names else ""
    days = f"{draft['days']} 天" if draft["days"] else ""
    parts = [part for part in (amount, times, days) if part]
    return f"{index}. {draft['name']}" + (f"：{'，'.join(parts)}" if parts else "")


def list_messages(result: dict, today: str, active_names: list[str], draft_id: str) -> list[dict]:
    drafts = scan_rules.mark_duplicates(
        [scan_rules.draft_from(med, result, today) for med in scan_rules.medicines_of(result)], active_names)
    lines = [f"我讀到 {len(drafts)} 種藥：/ I read {len(drafts)} medicines:"]
    savable = 0
    for index, draft in enumerate(drafts, 1):
        line = _line(index, draft)
        if draft.get("duplicate"):
            line += DUPLICATE_MARK
        elif not draft["include"] or scan_rules.draft_problem(draft, today) is not None:
            line += CHECK_MARK
        else:
            savable += 1
        lines.append(line)
    lines.append(FOOTER_TEXT)
    messages = [_text("\n".join(lines))]
    if savable:
        messages.append({"type": "template", "altText": BUTTON_TEXT, "template": {
            "type": "buttons", "text": BUTTON_TEXT,
            "actions": [{"type": "postback", "label": SAVE_LABEL, "displayText": SAVE_LABEL,
                         "data": sign_save(draft_id)}]}})
    else:
        messages.append(_text(NOTHING_TEXT))
    return messages


# ── answers ──

async def _answer(conn, u_id: int, sender: str, messages: list[dict], dedupe_key: str,
                  reply_token: str | None, received_at: datetime) -> None:
    await outbox.enqueue(conn, u_id=u_id, recipient_line_id=sender, kind=KIND, priority=PRIORITY,
                         messages=messages, dedupe_key=dedupe_key, reply_token=reply_token,
                         received_at=received_at)


async def _fail(draft_id: str, u_id: int, sender: str, reply_token: str | None, received_at: datetime,
                outcome: str, error_code: str, text: str, status: str = "failed") -> str:
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE line_photo_draft SET status = $2, error_code = $3 WHERE draft_id = $1",
                               draft_id, status, error_code)
            await _answer(conn, u_id, sender, [_text(text)], f"line_photo:{draft_id}:{outcome}",
                          reply_token, received_at)
    outbox.wake()
    return _logged(outcome)


# ── receiving ──

async def receive(event: dict, received_at: datetime) -> str:
    """Handle one image event. Returns its outcome; `reading` means `process` now runs in the background and logs
    the photo's one line when it ends (every other outcome is logged here)."""
    source = event.get("source") or {}
    sender = source.get("userId") or ""
    if source.get("type") != "user" or not sender:
        return _logged("ignored")
    message = event.get("message") or {}
    message_id = str(message.get("id") or "")
    reply_token = event.get("replyToken")
    line_svc = LineService.get_instance()

    async with get_pool().acquire() as conn:
        patients = await conn.fetch('SELECT u_id FROM "user" WHERE line_id = $1 AND user_active = TRUE', sender)
        family = not patients and await conn.fetchval(
            "SELECT 1 FROM family_contacts WHERE line_id = $1 AND relationship IS DISTINCT FROM 'user' LIMIT 1",
            sender) is not None
    if len(patients) != 1:
        outcome, text = ("family", FAMILY_TEXT) if family else ("not_linked", NOT_LINKED_TEXT)
        await asyncio.to_thread(line_svc.reply_text, reply_token, sender, text)
        return _logged(outcome)
    u_id = patients[0]["u_id"]

    if not consent_service.is_current(await consent_service.get_state(u_id), "core"):
        async with get_pool().acquire() as conn:
            await _answer(conn, u_id, sender, [_text(CONSENT_TEXT)], f"line_photo:msg:{message_id}:consent",
                          reply_token, received_at)
        outbox.wake()
        return _logged("consent")

    draft_id = str(uuid.uuid4())
    async with get_pool().acquire() as conn:
        inserted = await conn.fetchval(
            "INSERT INTO line_photo_draft (draft_id, u_id, line_message_id) VALUES ($1::uuid, $2, $3) "
            "ON CONFLICT (line_message_id) DO NOTHING RETURNING draft_id",
            draft_id, u_id, message_id)
        if inserted is None:
            return _logged("duplicate")
        recent = await conn.fetchval(
            "SELECT count(*) FROM line_photo_draft WHERE u_id = $1 AND created_at > NOW() - INTERVAL '1 hour'",
            u_id)
    if recent > PER_HOUR:
        return await _fail(draft_id, u_id, sender, reply_token, received_at, "too_many", "too_many",
                           TOO_MANY_TEXT, status="refused")
    if (message.get("contentProvider") or {}).get("type") != "line":
        return await _fail(draft_id, u_id, sender, reply_token, received_at, "external", "external",
                           UNREADABLE_TEXT)
    _spawn(process(draft_id, u_id, sender, message_id, reply_token, received_at))
    return "reading"


# ── reading ──

async def process(draft_id: str, u_id: int, sender: str, message_id: str, reply_token: str | None,
                  received_at: datetime) -> str:
    """Download, read and answer one photo (a background task). Returns the outcome it logged."""
    try:
        return await _process(draft_id, u_id, sender, message_id, reply_token, received_at)
    except Exception as exc:   # a database failure: logged by type only, never the details
        log.warning("[LINE] photo: error (%s)", type(exc).__name__)
        return "error"


async def _process(draft_id: str, u_id: int, sender: str, message_id: str, reply_token: str | None,
                   received_at: datetime) -> str:
    def fail(outcome: str, error_code: str, text: str):
        return _fail(draft_id, u_id, sender, reply_token, received_at, outcome, error_code, text)

    line_svc = LineService.get_instance()
    await asyncio.to_thread(line_svc.start_loading, sender, LOADING_SECONDS)
    status, photo = await asyncio.to_thread(line_svc.get_content, message_id, MAX_BYTES)
    if status == "too_large":
        return await fail("too_large", "too_large", TOO_BIG_TEXT)
    if status != "ok":
        return await fail("download_failed", "download_failed", BUSY_TEXT)

    svc = OCRService.get_instance()
    timeout = OCR_GEMINI_IMAGE_BUDGET if svc.gemini_api_key else OLLAMA_TIMEOUT
    try:
        result = await asyncio.wait_for(asyncio.to_thread(svc.process_image, photo), timeout)
    except OCRServiceError as exc:
        if exc.code in UNREADABLE_CODES:
            return await fail("unreadable", exc.code, UNREADABLE_TEXT)
        return await fail("busy", exc.code, BUSY_TEXT)
    except asyncio.TimeoutError:
        return await fail("busy", "ocr_timeout", BUSY_TEXT)
    except Exception:
        return await fail("busy", "ocr_error", BUSY_TEXT)

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE line_photo_draft SET status = 'pending', result = $2::jsonb WHERE draft_id = $1",
                draft_id, json.dumps(result, ensure_ascii=False))
            names = await _active_names(conn, u_id)
            messages = list_messages(result, memory.local_today().isoformat(), names, draft_id)
            await _answer(conn, u_id, sender, messages, f"line_photo:{draft_id}:read", reply_token, received_at)
    outbox.wake()
    return _logged("read")


async def _active_names(conn, u_id: int) -> list[str]:
    """The names the Scan page compares against for duplicates: every medicine not archived."""
    return [row["med_name"] for row in await conn.fetch(
        "SELECT med_name FROM medication WHERE u_id = $1 AND is_active IS NOT FALSE", u_id)]


# ── Save all ──

def _save_logged(outcome: str) -> str:
    log.info("[LINE] photo save: %s", outcome)
    return outcome


async def _save_drafts(conn, u_id: int, result: dict) -> str:
    """Insert every medicine the Scan page would save as it stands; the answer names the ones left out."""
    today = memory.local_today().isoformat()
    drafts = scan_rules.mark_duplicates(
        [scan_rules.draft_from(med, result, today) for med in scan_rules.medicines_of(result)],
        await _active_names(conn, u_id))
    saved, skipped = 0, []
    for draft in drafts:
        name = draft["name"].strip()
        if draft.get("duplicate"):
            skipped.append(SKIPPED_DUPLICATE.format(name=name))
            continue
        if not draft["include"] or scan_rules.draft_problem(draft, today) is not None:
            skipped.append(SKIPPED_CHECK.format(name=name))
            continue
        try:
            payload = MedicationPayload(**scan_rules.medication_payload(draft, result))
        except ValidationError:
            skipped.append(SKIPPED_CHECK.format(name=name))
            continue
        await insert_medication(conn, u_id, payload)
        saved += 1
    return "\n".join([SAVED_TEXT.format(n=saved), *skipped])


async def handle_save(conn, data: str, sender: str, reply_token: str | None, received_at: datetime) -> str | None:
    """Save all, pressed under the list. None when `data` is not a Save all postback (the caller passes it on to
    dose_confirmation); else the outcome it logged. Everything happens in one transaction under the draft's row lock,
    so a second press, at once or later, saves nothing more."""
    draft_id = verify_save(data)
    if draft_id is None:
        return None
    dedupe_key = f"line_photo:{draft_id}:save:{received_at.isoformat()}"
    owner = None
    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                'SELECT d.*, u.line_id, u.user_active FROM line_photo_draft d JOIN "user" u USING (u_id) '
                "WHERE d.draft_id = $1 FOR UPDATE OF d", draft_id)
            if row is None or row["line_id"] != sender or not row["user_active"]:
                outcome, text = "refused", EXPIRED_TEXT
            else:
                owner = row["u_id"]
                if row["status"] == "saved":
                    outcome, text = "already", ALREADY_TEXT
                elif row["status"] != "pending" or row["expires_at"] <= datetime.now(timezone.utc):
                    outcome, text = "expired", EXPIRED_TEXT
                elif not consent_service.is_current(await consent_service.fetch_state(conn, owner), "core"):
                    outcome, text = "consent", CONSENT_TEXT
                else:
                    result = row["result"]
                    text = await _save_drafts(conn, owner, json.loads(result) if isinstance(result, str) else result)
                    await conn.execute(
                        "UPDATE line_photo_draft SET status = 'saved', saved_at = NOW() WHERE draft_id = $1",
                        draft_id)
                    outcome = "saved"
                await _answer(conn, owner, sender, [_text(text)], dedupe_key, reply_token, received_at)
    except Exception as exc:   # rolled back: nothing of this press is stored. Logged by type only.
        log.warning("[LINE] photo save: failed (%s)", type(exc).__name__)
        if owner is None:
            await asyncio.to_thread(LineService.get_instance().reply_text, reply_token, sender, SAVE_FAILED_TEXT)
        else:
            async with conn.transaction():
                await _answer(conn, owner, sender, [_text(SAVE_FAILED_TEXT)], dedupe_key, reply_token, received_at)
            outbox.wake()
        return "failed"
    if owner is None:
        await asyncio.to_thread(LineService.get_instance().reply_text, reply_token, sender, text)
    else:
        outbox.wake()
    return _save_logged(outcome)
