"""The GPT-Live agent's tools on the server (reference reachy_gpt_live_agent/tool_registry.py, adapted).

ToolRegistry validates, time-limits and runs the tools of gpt_live_config.TOOLS_SCHEMA and always answers a
JSON-serialisable envelope {ok, tool, result | error}, so a failed tool never ends the voice session. Two things it
talks to:
- a RobotLink: the robot's camera, gestures and multimodal emotion over the live chat's own WebSocket
  (agent_bridge.RelayRobotLink sends {"type": "camera"}, {"type": "emote"} and {"type": "emotion"} and waits for
  the robot's answers);
- a ServerCareBackend: the care services of this server, for one patient and one live conversation, each database
  call on a pooled connection within DB_TIMEOUT_SECONDS (as live_session does), so a database that stopped
  answering costs one tool call, not the chat.

Ids: the tools name a dose "dose-<intk_id>" (check_medication_due, get_medication_schedule) and a medicine
"med-<med_id>" (identify_medicine); verify_prescription takes either, record_medication_taken a dose id or a
medication_name. The policy differences from the reference are listed in gpt_live_config.

The visual medication workflow (get_medication_schedule "now" -> verify_medication -> start_pill_intake_detection
-> get_pill_intake_result -> record_medication_taken) reuses: dose_status and dose_safety (which dose is due and
whether overdose protection allows it), the prescription OCR (packaging and label text; never colour or shape), the
pill-intake detector that already scores the robot's frames in the chat's camera session (monitor_service.registry,
its latest_detector / candidate / extra_events evidence from intake_detection.py), and dose_confirmation.create,
which stores the event (time, detector result and confidence, session id) as the request's evidence and asks the
family to confirm it, as every robot-seen dose is today.
"""

import asyncio
import datetime
import inspect
import logging
import time
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Protocol

from app import config
from app.config import MEDCARE_TIMEZONE
from app.database import get_pool
from app.services import (ai_settings, consent_service, conversation, current_time, dose_confirmation, dose_emotion,
                          dose_event, dose_safety, dose_status, dose_video, memory, monitor_service, outbox, reminders,
                          schedule, weather_lookup, web_search)
from app.services.gpt_live_config import (DANCES, DEFAULT_DANCE_REPEAT, EMOTE_PRESETS, EMOTION_MOVES, MAX_DANCE_REPEAT,
                                          MEMORY_CATEGORIES, POSTURES, SEVERITIES, TIME_SCOPES, TOOL_NAMES, URGENCIES)

log = logging.getLogger(__name__)

DB_TIMEOUT_SECONDS = 5.0
FRAME_FRESHNESS_SECONDS = 2.5    # identify_medicine / verify_medication take a frame this fresh, else a new one
OCR_SECONDS = 30.0               # the OCR's own budget is 45 s (OCR_GEMINI_IMAGE_BUDGET); a tool waits this long
EMOTION_RECENT_SECONDS = 30.0    # get_current_emotion averages the server's uncovered samples this recent
DETECTOR_CONFIDENCE = 0.70       # record without the patient's word needs the detector at least this sure
DEFAULT_WATCH_SECONDS = 30
NAME_MIN_PARTIAL = 3             # a label name containing the expected name (or the reverse) matches from this length
# Medication events to family (dose_event.py): a verification that did not match is reported at most once per
# expected medicine and result in this long; a watch's uncertain or empty result waits this long before its report
# goes, so a record_medication_taken that follows at once sends the clip with the confirmation request instead; a
# review already being made when the record comes is waited for (at most REVIEW_WAIT_SECONDS), never cut half made
VERIFICATION_REPORT_GAP_SECONDS = 120.0
WATCH_REPORT_GRACE_SECONDS = 12.0
REVIEW_WAIT_SECONDS = 5.0
VERIFICATION_MEMORY_SECONDS = 600.0   # a label check this recent is quoted with the dose's events
DANCE_ACTIVE_SECONDS = 25.0      # a dance started this recently may still run (4 repeats of the longest 5 s move)
# A dance asked for this soon after a stop, before the patient asks to dance again (dance_asked), is refused with
# stopped_by_patient: GPT-Live's call for the words before the stop can come after it (agent tool fixes, Task 5)
STOP_HOLD_SECONDS = 10.0
INTENT_SHARE_SECONDS = 8.0       # GPT-Live asking for a tool the relay's backstop ran this recently gets that run
TIMEOUTS = {"camera": 6.0, "identify_medicine": OCR_SECONDS + 6.0, "verify_medication": OCR_SECONDS + 6.0,
            "describe_view": OCR_SECONDS + 6.0,
            "verify_prescription": 6.0, "get_medication_schedule": 6.0, "check_medication_due": 6.0,
            "start_pill_intake_detection": 6.0, "get_pill_intake_result": 3.0,
            # two database calls and the consent read (dose_video.clip_outlook), plus a review being made (5 s)
            "record_medication_taken": 10.0 + REVIEW_WAIT_SECONDS,
            "get_current_emotion": 8.0, "get_emotion_history": 6.0, "trigger_antenna_emote": 6.0, "remember": 8.0,
            "get_patient_memory": 8.0, "notify_family": 8.0, "emergency_alert": 12.0,
            "get_weather": 30.0,   # the geocoder (spaced 1.1 s) and Open-Meteo, 12 s and 15 s each at most
            "dance": 12.0, "stop_dance": 6.0, "play_emotion": 12.0,
            "get_current_time": 3.0, "search_web": web_search.TIMEOUT + 4.0,   # measured 6-12 s (14 Oct 2026)
            "set_reminder": 10.0, "cancel_reminder": 6.0}
# Closing GPT-Live (an ending, a companion pause) never cuts these short: a dose request, a note to family or an alert
# GPT-Live asked for runs to its end within its TIMEOUTS entry (agent_bridge.AgentLiveClient.close), while the other
# tools are cancelled. The companion's quiet pause waits while one runs (ToolRegistry.finishing) and for GPT-Live's
# answer after it (finished_at, live_session.LiveRelay._tool_answer_due), and the chat's end waits for them before the
# tools close, bounded by the calls actually held (ToolRegistry.finish_wait: the client runs its calls one at a time)
FINISH_ON_CLOSE = ("record_medication_taken", "notify_family", "emergency_alert")
# The steps of the visual medication workflow: while GPT-Live's call of one runs (verify_medication's OCR takes up to
# 36 s), and for a moment after it, the companion's quiet pause waits (ToolRegistry.checking), so GPT-Live is not
# closed before it asks for record_medication_taken (agent_bridge counts the same tools as a medication context)
MEDICATION_TOOLS = ("check_medication_due", "get_medication_schedule", "verify_prescription", "verify_medication",
                    "start_pill_intake_detection", "get_pill_intake_result", "record_medication_taken")
# A dose dose_confirmation.create found no longer open (NothingToConfirm) is answered by its state then
ALREADY = {"taken": "already_taken", "skipped": "already_skipped",
           "pending_confirmation": "already_pending_confirmation"}
MEMORY_KIND = {"preference": "like", "routine": "routine", "event": "event", "person": "person"}
NOTE_KIND = "live_agent_note"    # notification_outbox.kind of a notify_family message
NOTE_LIMIT = 3                   # notify_family messages one conversation may send (the model never loops on it)
RESULTS = ("taken", "not_taken", "still_monitoring", "uncertain")   # get_pill_intake_result's results
SCHEDULE_SQL = ("SELECT i.intk_id, i.med_id, m.med_name, i.intake_stats, i.intake_time_stamp, i.actual_intake_time "
                "FROM intake i JOIN medication m ON m.med_id = i.med_id WHERE i.u_id = $1 "
                "AND COALESCE(m.is_active, TRUE) "
                "AND (i.intake_time_stamp AT TIME ZONE $3::text)::date = ($2::timestamptz AT TIME ZONE $3::text)::date "
                "ORDER BY i.intake_time_stamp, i.intk_id")
DOSES_SQL = ("SELECT i.intk_id, i.med_id, m.med_name, i.intake_stats, i.intake_time_stamp, i.actual_intake_time "
             "FROM intake i JOIN medication m ON m.med_id = i.med_id "
             "WHERE i.u_id = $1 AND i.intk_id = ANY($2::int[]) ORDER BY i.intake_time_stamp, i.intk_id")
DOSE_SQL = ("SELECT i.intk_id, i.med_id, m.med_name, i.intake_stats, i.intake_time_stamp, "
            "COALESCE(m.is_active, TRUE) AS active FROM intake i JOIN medication m ON m.med_id = i.med_id "
            "WHERE i.u_id = $1 AND i.intk_id = $2")
MEDICINE_SQL = "SELECT med_id, med_name, COALESCE(is_active, TRUE) AS active FROM medication WHERE u_id = $1 AND med_id = $2"
MEDICINES_SQL = "SELECT med_id, med_name FROM medication WHERE u_id = $1 AND COALESCE(is_active, TRUE) ORDER BY med_id"
EMOTION_HISTORY_SQL = ("SELECT dominant, dominant_score, basis, resolved_at FROM dose_emotion "
                       "WHERE u_id = $1 AND resolved_at >= $2 ORDER BY resolved_at DESC LIMIT 20")
MOOD_HISTORY_SQL = ("SELECT mood, ended_at FROM conversation WHERE u_id = $1 AND ended_at >= $2 "
                    "AND mood IS NOT NULL ORDER BY ended_at DESC LIMIT 20")
NAME_SQL = 'SELECT COALESCE(reachy_call_name, name) AS name FROM "user" WHERE u_id = $1'
EMERGENCY_TEXT = ("⚠️ 緊急提醒：Reachy 在與 {name} 聊天時判斷需要立即關心：{reason}。請盡快聯絡；如有立即危險請撥 119。\n"
                  "⚠️ Urgent: while talking with {name}, Reachy judged that they need attention now: {reason}. "
                  "Please contact them as soon as possible; in an emergency call 119.")


class ToolError(RuntimeError):
    """A tool could not do what was asked; its text is a short machine reason the backend model may read."""


class RobotLink(Protocol):
    """The robot's camera, gestures and emotion engine, reached through the live chat (agent_bridge.RelayRobotLink)."""

    async def capture_jpeg(self) -> tuple[bytes, dict]: ...   # (JPEG bytes, {"width", "height", "captured_at"})
    async def emote(self, preset: str) -> dict: ...           # the robot's answer: {"status": "queued", ...}
    async def emotion(self) -> dict | None: ...               # multimodal/engine.py report(); None: no answer
    async def dance(self, name: str | None, repeat: int) -> dict: ...   # {"status": "started", "dance", ...}
    async def stop_dance(self) -> dict: ...                   # {"status": "cancelled" | "idle", ...}
    async def express(self, move: str, posture: str | None) -> dict: ...   # a recorded emotion move (+ posture)


@dataclass
class FrameState:
    frame_id: str
    jpeg: bytes
    meta: dict
    wall_time: float


def fold_name(value: str) -> str:
    """A medicine name as the Scan page compares them (frontend scan.ts sameName): NFKC, upper case, no spaces or
    dots."""
    text = unicodedata.normalize("NFKC", str(value or "")).upper()
    return "".join(ch for ch in text if ch not in " ." and not ch.isspace())


def names_match(expected: str, read: str) -> bool:
    """Label text names the expected medicine: the same folded name, or one inside the other (a label reads
    'Allegra 60mg', the schedule says 'Allegra') when the shorter is at least NAME_MIN_PARTIAL characters."""
    a, b = fold_name(expected), fold_name(read)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= NAME_MIN_PARTIAL and shorter in longer


def names_read(ocr: dict) -> list[str]:
    """The medicine names an OCR answer read (the medications list, else the first-medicine field), without N/A."""
    names = []
    for entry in ocr.get("medications") or []:
        name = entry.get("med_name") if isinstance(entry, dict) else None
        if isinstance(name, str) and name.strip() and name.strip().upper() != "N/A":
            names.append(name.strip())
    first = ocr.get("med_name")
    if not names and isinstance(first, str) and first.strip() and first.strip().upper() != "N/A":
        names.append(first.strip())
    return names


def parse_id(value: str) -> tuple[str, int]:
    """"dose-12" -> ("dose", 12); "med-3" -> ("med", 3); a bare number counts as a dose id."""
    text = str(value or "").strip().lower()
    kind, _, number = text.rpartition("-")
    if not kind:
        kind, number = "dose", text
    if kind not in ("dose", "med") or not number.isdigit():
        raise ToolError("invalid_medication_id")
    return kind, int(number)


def _local(when) -> str | None:
    return schedule.local_iso(when) if when is not None else None


class ServerCareBackend:
    """This server's care services for one patient's live conversation. `pool` is get_pool (tests inject a fake);
    `clock` is time.time (wall time); the monitor registry stamps its samples with time.monotonic, read through
    `monotonic`; `ocr` is the OCR service, found on first use."""

    def __init__(self, u_id: int, conversation_id: str, task_id: str | None, language: str, *, pool=get_pool,
                 clock=time.time, monotonic=time.monotonic, ocr=None):
        self.u_id, self.conversation_id, self.task_id, self.language = u_id, conversation_id, task_id, language
        self._pool, self.clock, self.monotonic, self._ocr = pool, clock, monotonic, ocr
        self._watch: dict | None = None   # start_pill_intake_detection's window (the detector session the tool watches)
        self._notes_sent: list[str] = []  # notify_family texts this conversation sent (NOTE_LIMIT, no repeats)
        self._last_verification: dict | None = None   # verify_medication's last result, quoted with the dose's events
        self._verification_reports: dict[tuple, float] = {}   # (expected, result) -> when family were last told

    async def _db(self, work):
        """`work(conn)` on a pooled connection within DB_TIMEOUT_SECONDS."""
        async def run():
            async with self._pool().acquire() as conn:
                return await work(conn)
        try:
            return await asyncio.wait_for(run(), DB_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise ToolError("database_timeout") from None

    def _session(self):
        """The patient's camera session in the monitor registry (the companion chat keeps one open), or None."""
        registry = monitor_service.registry
        state = registry.sessions.get(registry.by_user.get(self.u_id) or "")
        return None if state is None or state.ended else state

    # ── medication ───────────────────────────────────────────────────────
    async def _slot(self, conn) -> tuple[list[dict], dict]:
        """The reminder task's doses, or today's current slot (dose_status.lookup's choice), with their verdicts."""
        at = schedule.current_time()
        ids: list[int] = []
        if self.task_id is not None:
            ids = [int(i) for i in (await conn.fetchval(dose_status.TASK_SQL, str(self.task_id), self.u_id) or [])]
        if not ids:
            ids = dose_status.slot_of(await conn.fetch(dose_status.TODAY_SQL, self.u_id, at, MEDCARE_TIMEZONE), at)
        if not ids:
            return [], {}
        rows = [dict(row) for row in await conn.fetch(DOSES_SQL, self.u_id, ids)]
        open_ids = [row["intk_id"] for row in rows if row["intake_stats"] in dose_status.OPEN]
        verdicts = await dose_safety.verdicts(conn, self.u_id, open_ids, at=at) if open_ids else {}
        return rows, verdicts

    async def _today(self, conn) -> tuple[list[dict], dict]:
        """Every dose of today with its verdict."""
        at = schedule.current_time()
        rows = [dict(row) for row in await conn.fetch(SCHEDULE_SQL, self.u_id, at, MEDCARE_TIMEZONE)]
        open_ids = [row["intk_id"] for row in rows if row["intake_stats"] in dose_status.OPEN]
        verdicts = await dose_safety.verdicts(conn, self.u_id, open_ids, at=at) if open_ids else {}
        return rows, verdicts

    @staticmethod
    def _dose(row: dict, verdicts: dict) -> dict:
        refused = verdicts.get(row["intk_id"])
        return {"medication_id": f"dose-{row['intk_id']}", "medication_name": row["med_name"],
                "med_name": row["med_name"], "scheduled_time": _local(row["intake_time_stamp"]),
                "taken_time": _local(row.get("actual_intake_time")),
                "status": dose_status.dose_status(row, refused),
                "refusal": getattr(refused, "detail", None) if refused is not None else None}

    async def check_medication_due(self) -> dict:
        async def work(conn):
            rows, verdicts = await self._slot(conn)
            summary = dose_status.summarize(rows, verdicts)
            doses = [self._dose(row, verdicts) for row in rows]
            current = next((dose for dose in doses if dose["status"] == summary["status"]), None)
            return {**summary, "medication_id": current["medication_id"] if current else None,
                    "medication_name": current["med_name"] if current else None,
                    "med_name": current["med_name"] if current else None, "doses": doses}
        return await self._db(work)

    async def get_medication_schedule(self, time_scope: str = "today") -> dict:
        """time_scope "now": the dose due now (the reminder task's slot, else today's current slot) judged by
        overdose protection; "today": every dose of today."""
        if time_scope == "now":
            answer = await self.check_medication_due()
            due = [dose for dose in answer["doses"] if dose["status"] == "pending_allowed"]
            return {"time_scope": "now", **answer, "due_now": due, "anything_due": bool(due)}

        async def work(conn):
            rows, verdicts = await self._today(conn)
            at = schedule.current_time()
            return {"time_scope": "today", "date": schedule.local_iso(at)[:10],
                    "doses": [self._dose(row, verdicts) for row in rows]}
        return await self._db(work)

    async def _dose_by_name(self, conn, medication_name: str) -> dict | None:
        """Today's dose of the medicine named: the open one nearest to now, else today's latest; None when no
        medicine of today matches the name."""
        rows, verdicts = await self._today(conn)
        mine = [row for row in rows if names_match(medication_name, row["med_name"])]
        if not mine:
            return None
        now = schedule.current_time()
        open_rows = [row for row in mine if row["intake_stats"] in dose_status.OPEN]
        if open_rows:
            row = min(open_rows, key=lambda r: (abs((r["intake_time_stamp"] - now).total_seconds()), r["intk_id"]))
        else:
            row = max(mine, key=lambda r: (r["intake_time_stamp"], r["intk_id"]))
        return self._dose(row, verdicts)

    async def _dose_of_medicine(self, conn, med_id: int) -> dict | None:
        """Today's open dose of medicine med_id nearest to now (else today's latest), or None."""
        rows, verdicts = await self._today(conn)
        mine = [row for row in rows if row["med_id"] == med_id]
        if not mine:
            return None
        now = schedule.current_time()
        open_rows = [row for row in mine if row["intake_stats"] in dose_status.OPEN]
        row = (min(open_rows, key=lambda r: (abs((r["intake_time_stamp"] - now).total_seconds()), r["intk_id"]))
               if open_rows else max(mine, key=lambda r: (r["intake_time_stamp"], r["intk_id"])))
        return self._dose(row, verdicts)

    async def verify_prescription(self, medication_id: str) -> dict:
        kind, number = parse_id(medication_id)

        async def work(conn):
            if kind == "dose":
                row = await conn.fetchrow(DOSE_SQL, self.u_id, number)
                if row is None:
                    return {"medication_id": medication_id, "verified": False, "reason": "not_this_patients"}
                return {"medication_id": medication_id, "verified": bool(row["active"]), "med_name": row["med_name"],
                        "scheduled_time": _local(row["intake_time_stamp"]), "status": row["intake_stats"],
                        "reason": None if row["active"] else "medicine_archived"}
            row = await conn.fetchrow(MEDICINE_SQL, self.u_id, number)
            if row is None:
                return {"medication_id": medication_id, "verified": False, "reason": "not_this_patients"}
            return {"medication_id": medication_id, "verified": bool(row["active"]), "med_name": row["med_name"],
                    "reason": None if row["active"] else "medicine_archived"}
        return await self._db(work)

    # ── the camera frame: packaging and label text through the prescription OCR ──
    async def _read_labels(self, jpeg: bytes) -> tuple[list[str], str | None]:
        """The medicine names the OCR read on the frame, or ([], why it could not read any). Nothing else of the
        OCR's answer leaves this function (it may read a patient's name or an institution)."""
        if not config.LIVE_VISION_CLOUD:
            return [], "vision_off"   # the frame would leave the server (config.LIVE_VISION_CLOUD)
        service = self._vision()
        if service is None:
            return [], "ocr_unavailable"
        try:
            answer = await asyncio.wait_for(asyncio.to_thread(service.process_image, jpeg), OCR_SECONDS)
        except asyncio.TimeoutError:
            return [], "ocr_timeout"
        except Exception as error:   # OCRServiceError: no prescription text, no schedule, a provider error
            code = getattr(error, "code", type(error).__name__)
            return [], str(code)[:40]
        return names_read(answer if isinstance(answer, dict) else {}), None

    async def identify_medicine(self, jpeg: bytes, expected_name: str | None) -> dict:
        """Packaging and labels through the prescription OCR, matched by name against the patient's active
        medicines. A bare pill, an unavailable OCR or no readable text: uncertain."""
        names, failure = await self._read_labels(jpeg)
        if failure:
            return {"result": "uncertain", "reason": failure, "names_read": [], "medication_id": None,
                    "consistent_with_expected": None}
        medicines = await self._db(lambda conn: conn.fetch(MEDICINES_SQL, self.u_id))
        match = next(((row, name) for name in names for row in medicines
                      if fold_name(row["med_name"]) and fold_name(row["med_name"]) == fold_name(name)), None)
        consistent = None
        if expected_name:
            consistent = any(fold_name(expected_name) == fold_name(name) for name in names) or None
            if consistent is None and names:
                consistent = False
        if match is None:
            return {"result": "uncertain" if names else "no_label_read", "names_read": names[:5],
                    "medication_id": None, "consistent_with_expected": consistent}
        row, name = match
        return {"result": "match", "names_read": names[:5], "medication_id": f"med-{row['med_id']}",
                "med_name": row["med_name"], "consistent_with_expected": consistent}

    def _vision(self):
        """The OCR service (the prescription OCR's Gemini model) when it is available; found on first use."""
        service = self._ocr
        if service is None:
            from app.services import ocr_service   # heavy import, only when a frame is really read
            service = ocr_service.OCRService.get_instance() if ocr_service.OCRService._available else None
            self._ocr = service
        return service

    async def describe_view(self, jpeg: bytes, question: str) -> dict:
        """What the camera shows, from the OCR's vision model (OCRService._call_gemini: the frame as JPEG plus a
        prompt that asks for JSON), for questions that are not about medicine. The frame leaves the server, so
        only with config.LIVE_VISION_CLOUD; the answer carries the description and the model's answer only."""
        if not config.LIVE_VISION_CLOUD:
            return {"available": False, "reason": "vision_off", "description": None, "answer": None}
        service = self._vision()
        if service is None or not callable(getattr(service, "_call_gemini", None)):
            return {"available": False, "reason": "ocr_unavailable", "description": None, "answer": None}
        prompt = ("You see one frame from a home-care robot's camera. Answer in Traditional Chinese (zh-TW). Return "
                  "JSON only, with exactly these keys: \"description\" (one or two short sentences of what is "
                  "visible: people, clothing colours, objects, the room; no names, no guesses about identity, age or "
                  "health), \"answer\" (a short direct answer to the question, or \"看不清楚\" when the frame does "
                  "not show it), \"people_visible\" (an integer). The question: " + question.strip()[:300])

        def call():
            import cv2
            import numpy as np
            image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ToolError("camera_frame_unreadable")
            return service._call_gemini(prompt, image)

        try:
            parsed = await asyncio.wait_for(asyncio.to_thread(call), OCR_SECONDS)
        except asyncio.TimeoutError:
            return {"available": False, "reason": "vision_timeout", "description": None, "answer": None}
        except ToolError:
            raise
        except Exception as error:
            code = getattr(error, "code", type(error).__name__)
            return {"available": False, "reason": str(code)[:40], "description": None, "answer": None}
        if not isinstance(parsed, dict):
            return {"available": False, "reason": "vision_invalid_response", "description": None, "answer": None}
        people = parsed.get("people_visible")
        return {"available": True, "question": question.strip()[:300],
                "description": str(parsed.get("description") or "")[:400],
                "answer": str(parsed.get("answer") or "")[:300],
                "people_visible": int(people) if isinstance(people, (int, float)) else None}

    async def verify_medication(self, jpeg: bytes, expected_medication: str) -> dict:
        """Does the visible medication appear to be expected_medication? match when the label text the OCR read
        names it (names_match), no_match when labels were read and none does, uncertain when nothing readable was
        found (a bare pill, an unavailable OCR). The evidence is label text only, never colour or shape."""
        names, failure = await self._read_labels(jpeg)
        evidence = {"type": "label_text", "names_read": names[:5], "colour_or_shape_used": False}
        if failure or not names:
            result = {"result": "uncertain", "expected_medication": expected_medication,
                      "reason": failure or "no_label_text_read", "evidence": evidence, "medication_id": None}
        elif not any(names_match(expected_medication, name) for name in names):
            result = {"result": "no_match", "expected_medication": expected_medication,
                      "reason": "label_names_differ", "evidence": evidence, "medication_id": None}
        else:
            async def work(conn):
                medicines = await conn.fetch(MEDICINES_SQL, self.u_id)
                mine = next((row for row in medicines if names_match(expected_medication, row["med_name"])), None)
                if mine is None:
                    return None, None
                dose = await self._dose_of_medicine(conn, mine["med_id"])
                return mine, dose
            mine, dose = await self._db(work)
            medication_id = dose["medication_id"] if dose else (f"med-{mine['med_id']}" if mine else None)
            result = {"result": "match", "expected_medication": expected_medication, "reason": "label_names_match",
                      "evidence": evidence, "medication_id": medication_id,
                      "medication_name": mine["med_name"] if mine else expected_medication,
                      "dose_status": dose["status"] if dose else None,
                      "prescribed": mine is not None}
        self._last_verification = {"result": result["result"], "expected_medication": expected_medication,
                                   "names_read": names[:3], "at": self.monotonic()}
        if result["result"] != "match":
            await self._report_verification(result)
        return result

    async def _report_verification(self, result: dict) -> None:
        """A label that did not match, or could not be read, goes to family with the clip (dose_event): once per
        expected medicine and result in VERIFICATION_REPORT_GAP_SECONDS; the agent may look several times."""
        key = (result["expected_medication"].lower(), result["result"])
        now = self.monotonic()
        if now - self._verification_reports.get(key, float("-inf")) < VERIFICATION_REPORT_GAP_SECONDS:
            return
        self._verification_reports[key] = now
        dose = await self._db(lambda conn: self._dose_by_name(conn, result["expected_medication"]))
        verification = {"result": result["result"], "expected_medication": result["expected_medication"],
                        "names_read": result["evidence"]["names_read"][:3]}
        result["family_notified"] = "queued"
        dose_event.schedule(self.u_id, kind="verification",
                            dedupe=f"verify:{self.conversation_id}:{key[0]}:{key[1]}:{int(now)}",
                            intk_ids=[parse_id(dose["medication_id"])[1]] if dose else [],
                            medication_name=dose["med_name"] if dose else result["expected_medication"],
                            verification=verification, intake="not_checked", event_time=schedule.current_time(),
                            needs_review=True, emotion=self._emotion_now())

    def _emotion_now(self) -> dict | None:
        """The server's face reading of the moment ({top, confidence}) for a family message, or None."""
        try:
            face = self._server_face()
        except Exception:
            return None
        return {"top": face["top"], "confidence": face["confidence"]} if face.get("available") else None

    def _verification_for(self, medication_name: str | None) -> dict | None:
        """The last label check of this medicine, when recent enough to describe the dose's events."""
        last = self._last_verification
        if last is None or self.monotonic() - last["at"] > VERIFICATION_MEMORY_SECONDS:
            return None
        if medication_name and not names_match(medication_name, last["expected_medication"]):
            return None
        return {key: last[key] for key in ("result", "expected_medication", "names_read")}

    # ── pill intake (the server's detector on the robot's frames) ────────
    async def start_pill_intake_detection(self, timeout_seconds: int = DEFAULT_WATCH_SECONDS,
                                          medication_name: str | None = None) -> dict:
        state = self._session()
        if state is None:
            raise ToolError("no_camera_session")
        dose = await self._db(lambda conn: self._dose_by_name(conn, medication_name)) if medication_name else None
        now = self.monotonic()
        # The events already in the session are remembered by id, never by position: monitor_service trims the
        # list to EXTRA_EVENT_CAP from the front, so a position would hide every event once it is full
        self._cancel_watch_report()   # a new watch replaces the old one; its unsent review message is dropped
        self._watch = {"id": uuid.uuid4().hex, "since": now, "until": now + timeout_seconds,
                       "seen_events": {event.get("event_id") for event in state.extra_events},
                       "recorded_before": state.recorded, "started_at": schedule.current_time(),
                       "medication_name": medication_name, "dose": dose,
                       "verification": self._verification_for(medication_name),
                       "reported": None, "report_task": None}
        return {"session_id": self._watch["id"], "detector_session_id": self._watch["id"], "state": "watching",
                "result": "still_monitoring", "timeout_seconds": timeout_seconds,
                "medication_name": dose["med_name"] if dose else medication_name,
                "medication_id": dose["medication_id"] if dose else None,
                "medication_known": dose is not None if medication_name else None,
                "camera_fps": round(float(state.landmark_fps), 1), "degraded": bool(state.degraded),
                "note": "supporting evidence only: the chat's camera runs at 2 frames a second"}

    @staticmethod
    def _score(value) -> float | None:
        if not isinstance(value, dict):
            return None
        for key in ("confidence", "score", "detector_score", "event_confidence"):
            if isinstance(value.get(key), (int, float)):
                return round(float(value[key]), 3)
        return None

    async def get_pill_intake_result(self, session_id: str | None = None) -> dict:
        """The watch's result: taken (the detector confirmed an intake, or the session recorded the dose),
        uncertain (a hand-to-mouth moment the detector was not sure of, or a candidate still being judged),
        not_taken (the watch timed out and saw nothing), still_monitoring; with the detector's evidence."""
        watch = self._watch
        if session_id and (watch is None or session_id != watch["id"]):
            raise ToolError("unknown_session")
        if watch is None:
            return {"result": "not_taken", "state": "not_started", "confidence": None, "recorded": False,
                    "evidence": {}}
        state = self._session()
        if state is None:
            return {"session_id": watch["id"], "result": "uncertain", "state": "no_camera_session",
                    "confidence": None, "recorded": False, "evidence": {}}
        now = self.monotonic()
        latest = state.latest_detector if isinstance(state.latest_detector, dict) else {}
        base = {"session_id": watch["id"], "identity": state.public()["identity_status"],
                "camera_fps": round(float(state.landmark_fps), 1), "degraded": bool(state.degraded),
                "medication_name": watch.get("medication_name"),
                "medication_id": (watch.get("dose") or {}).get("medication_id"),
                "detector": {key: latest.get(key) for key in ("stage", "decision", "hand_near_mouth", "mouth_open")},
                "seconds_watched": round(now - watch["since"], 1)}

        def answer(result: str, state_name: str, source: dict | None, recorded: bool) -> dict:
            evidence = (source or {}).get("evidence") if isinstance(source, dict) else None
            return {**base, "result": result, "state": state_name, "confidence": self._score(source),
                    "recorded": recorded, "evidence": evidence or latest.get("evidence") or {}}

        if state.recorded is not None and state.recorded is not watch["recorded_before"]:
            result = {**answer("taken", "recorded", state.recorded, True),
                      "confidence": self._score(state.recorded) or 1.0}
        else:
            events = [event for event in state.extra_events if event.get("event_id") not in watch["seen_events"]]
            if events:
                event = events[-1]
                confirmed = event.get("decision") == "confirmed"
                result = answer("taken" if confirmed else "uncertain", "hand_to_mouth_seen", event, False)
            elif state.candidate is not None:
                result = answer("uncertain", "candidate", state.candidate, False)
            elif now > watch["until"]:
                result = {**answer("not_taken", "timeout", None, False), "confidence": 0.0}
            else:
                result = answer("still_monitoring", "watching", None, False)
        if result["result"] in ("uncertain", "not_taken") and result["state"] != "candidate":
            self._report_watch(watch, result, state)   # a candidate is still being judged: wait for its verdict
        # The watch's last reading: a record that settles the watch without naming it tells family what it was
        watch["reading"] = {"result": result["result"], "confidence": result.get("confidence")}
        return result

    def _report_watch(self, watch: dict, result: dict, state) -> None:
        """The watch ended without a confirmed intake: family get the clip and the AI's reading (dose_event), after
        WATCH_REPORT_GRACE_SECONDS, unless record_medication_taken comes first and sends the clip with its request.
        Once per watch."""
        if watch.get("reported") is not None or watch.get("report_task") is not None:
            return
        watch["reported"] = {"result": result["result"], "state": result["state"]}
        watch["report_task"] = dose_event.schedule(
            self.u_id, delay=WATCH_REPORT_GRACE_SECONDS, kind="intake", dedupe=f"watch:{watch['id']}",
            intk_ids=[parse_id(watch["dose"]["medication_id"])[1]] if watch.get("dose") else [],
            medication_name=(watch.get("dose") or {}).get("med_name") or watch.get("medication_name"),
            verification=watch.get("verification"), intake=result["result"], confidence=result.get("confidence"),
            event_time=schedule.current_time(), needs_review=True, emotion=self._emotion_now(),
            event_started_at=getattr(state, "event_started_at", None) if result["state"] != "timeout" else None)
        result["family_notified"] = "queued"

    def _cancel_watch_report(self) -> bool:
        """Drop the watch's pending review message (its clip then goes with the confirmation request instead);
        True when one was still waiting."""
        watch = self._watch
        task = watch.get("report_task") if watch else None
        if task is not None and not task.done():
            task.cancel()
            watch["report_task"], watch["reported"] = None, None
            return True
        return False

    async def _review_carried_clip(self) -> bool:
        """Whether the current watch's review message (dose_event, after WATCH_REPORT_GRACE_SECONDS) went to family
        with the clip, from the review task's outcome. A review still in its grace is dropped, so the clip goes with
        the confirmation request instead; one already being made (dose_event.sending) is waited for, at most
        REVIEW_WAIT_SECONDS, and never cut half made. A review cancelled, failed, sent without a clip or not finished
        in time did not carry it."""
        task = (self._watch or {}).get("report_task")
        if task is None:
            return False
        if not task.done() and not dose_event.sending(task):
            self._cancel_watch_report()
            return False
        if not task.done():
            await asyncio.wait({task}, timeout=REVIEW_WAIT_SECONDS)   # never cancels it
            if not task.done():
                log.info("live agent: the watch's review message is still being made; the request gets its own clip")
                return False
        if task.cancelled() or task.exception() is not None:
            return False
        outcome = task.result()
        return isinstance(outcome, dict) and outcome.get("video") is True

    def watching(self) -> bool:
        """Whether a watch is open: started (start_pill_intake_detection), neither settled by a record nor closed."""
        return self._watch is not None

    def _watch_for(self, dose: dict) -> dict | None:
        """The open watch when it watches exactly this dose (the dose its medicine named when it started): a record
        settles it whether or not the call names it. Another dose of the same medicine never does, nor a watch
        whose medicine named no dose of today, nor one started without a medicine (no dose and no name)."""
        watch = self._watch
        watched = (watch.get("dose") or {}).get("medication_id") if watch is not None else None
        return watch if watched is not None and watched == dose["medication_id"] else None

    async def close(self) -> None:
        """The chat is over: a watch still open with no result reported goes to family as unresolved, with the clip
        (the agent never got to a verdict, so nobody else would tell them)."""
        watch, self._watch = self._watch, None
        if watch is None or watch.get("reported") is not None:
            return
        self._cancel_watch_report()
        dose_event.schedule(
            self.u_id, kind="unresolved", dedupe=f"watch:{watch['id']}",
            intk_ids=[parse_id(watch["dose"]["medication_id"])[1]] if watch.get("dose") else [],
            medication_name=(watch.get("dose") or {}).get("med_name") or watch.get("medication_name"),
            verification=watch.get("verification"), intake="still_monitoring", event_time=schedule.current_time(),
            needs_review=True, emotion=self._emotion_now())

    async def record_medication_taken(self, medication_id: str | None = None, patient_confirmed: bool | None = None,
                                      medication_name: str | None = None, session_id: str | None = None) -> dict:
        """Store the event and ask the family to confirm it (dose_confirmation.create: the request's evidence keeps
        the time, the detector's result and confidence and the session id). The dose: medication_id (a dose id, or a
        medicine id resolved to today's dose), else medication_name resolved to today's dose. Confirmation: the
        patient's word (patient_confirmed) or the detector's taken result, or its confidence of at least
        DETECTOR_CONFIDENCE, from the watch session_id names. With the patient's word, a session_id that names no open
        watch (unknown, or spent by an earlier record) is no detector evidence, never an error. The open watch of
        exactly this dose is settled whether or not the call names it (_watch_for): its review waiting its grace is
        dropped, the watch is spent, and the clip goes with the request. A watch settled without being named
        authorises nothing, but its last reading (camera_watch: its result and confidence, or not_read_yet) goes into
        the request's evidence, so family see when the camera disagrees with the patient's word. A dose no longer
        open answers its state (ALREADY)."""
        # The detector's verdict counts only for the watch the call names (session_id): a watch started for an
        # earlier dose never authorises a later one, and the patient's word alone is judged now (started_at None)
        intake = None
        if session_id:
            try:
                intake = await self.get_pill_intake_result(session_id)
            except ToolError as error:
                if str(error) != "unknown_session" or not patient_confirmed:
                    raise
                log.info("live agent: record_medication_taken named a watch that is not open (unknown or spent); "
                         "the patient's word counts, with no detector evidence")
        detector_sure = intake is not None and (
            intake.get("result") == "taken"
            or (intake.get("confidence") is not None and intake["confidence"] >= DETECTOR_CONFIDENCE))
        if not patient_confirmed and not detector_sure:
            raise ToolError("dose_not_confirmed_by_patient_or_detector")

        async def resolve(conn) -> dict | None:
            if medication_id:
                kind, number = parse_id(medication_id)
                if kind == "dose":
                    row = await conn.fetchrow(DOSE_SQL, self.u_id, number)
                    return None if row is None else self._dose(dict(row), {})
                return await self._dose_of_medicine(conn, number)
            return await self._dose_by_name(conn, medication_name)

        dose = await self._db(resolve)
        if dose is None:
            raise ToolError("medicine_not_found_today" if medication_name else "not_this_patients_dose")
        number = parse_id(dose["medication_id"])[1]
        recorded_at = schedule.current_time()
        # The clip of the moment goes with the confirmation request (dose_video.capture), unless the watch's review
        # message already carried it (its outcome says so): a report still waiting its grace is dropped in favour of
        # the request. That is the watch the call names, else the open watch of this dose (16 Oct 2026: a record
        # without session_id left it open, so its review and a second clip went out beside the request). What the
        # request says about its video is known before it is made (dose_video.clip_outlook).
        named = self._watch if intake is not None else None
        settled = None if named is not None else self._watch_for(dose)   # the open watch of this dose, not named
        watch = named or settled
        clip_sent_earlier = watch is not None and await self._review_carried_clip()
        state = self._session()
        event_started_at = getattr(state, "event_started_at", None) if (state is not None and intake is not None
                                                                         and intake.get("state") != "timeout") else None
        video, frames = ((dose_video.SENT_EARLIER, []) if clip_sent_earlier
                         else await dose_video.clip_outlook(self.u_id, event_started_at=event_started_at))
        evidence = {"source": "live_agent", "conversation_id": self.conversation_id,
                    "session_id": intake.get("session_id") if intake else None,
                    "recorded_at": _local(recorded_at), "patient_confirmed": bool(patient_confirmed),
                    "detector_result": intake.get("result") if intake else None,
                    "detector_state": intake.get("state") if intake else None,
                    "detector_evidence": intake.get("evidence") if intake else None,
                    "confidence": intake.get("confidence") if intake else None,
                    "landmark_fps": intake.get("camera_fps") if intake else None,
                    "degraded": intake.get("degraded") if intake else None,
                    "verification": ((watch.get("verification") if watch else None)
                                     or self._verification_for(medication_name or dose["med_name"])),
                    "emotion": self._emotion_now(),
                    "video": video}
        if settled is not None:   # dose_confirmation._request_messages says what the camera read
            evidence["camera_watch"] = settled.get("reading") or {"result": "not_read_yet", "confidence": None}
        source = "patient_claim" if patient_confirmed else "degraded"

        async def work(conn):
            try:
                request_id = await dose_confirmation.create(
                    conn, u_id=self.u_id, task_id=self.task_id, intk_ids=[number], source=source,
                    evidence=evidence, started_at=named["started_at"] if named is not None else None)
            except schedule.DoseRefused as refused:
                return refused
            except dose_confirmation.NothingToConfirm:
                # Taken, skipped or waiting for family: its state says which. Any other error is an error
                row = await conn.fetchrow(DOSE_SQL, self.u_id, number)
                status = ALREADY.get(row["intake_stats"]) if row is not None else None
                if status is None:
                    raise ToolError("dose_changed_meanwhile")   # open again, or gone, since the request looked
                return {"status": status, "medication_id": dose["medication_id"], "medication_name": dose["med_name"],
                        "family_asked": False}
            return {"status": "pending_confirmation", "medication_id": dose["medication_id"],
                    "medication_name": dose["med_name"], "request_id": request_id, "family_asked": True,
                    "recorded_at": evidence["recorded_at"], "session_id": evidence["session_id"],
                    "detector_result": evidence["detector_result"], "detector_confidence": evidence["confidence"],
                    "source": source}

        outcome = await self._db(work)
        if isinstance(outcome, schedule.DoseRefused):
            body = dose_safety.refusal_body(outcome)
            try:
                await dose_safety.alert_after(outcome)   # a suspected double dose alerts family (never silent)
            except Exception:
                log.exception("live agent: the double-dose alert after a refusal failed")
            # Family see what happened even though nothing was recorded (the double-dose alert carries no clip)
            dose_event.schedule(self.u_id, kind="refused", dedupe=f"refused:{number}:{int(self.monotonic() // 600)}",
                                intk_ids=[number], medication_name=dose["med_name"],
                                verification=evidence["verification"], intake=evidence["detector_result"],
                                confidence=evidence["confidence"], event_time=recorded_at, needs_review=True,
                                emotion=evidence["emotion"], event_started_at=event_started_at,
                                notes=[(f"未記錄的原因：{body.get('reply') or body.get('detail')}",
                                        f"Not recorded because: {body.get('detail')}")])
            if watch is not None:
                watch["reported"] = {"result": "refused"}
            return {"status": "refused", "medication_id": dose["medication_id"], "medication_name": dose["med_name"],
                    "reason": body.get("detail"), "reply": body.get("reply"), "family_asked": False,
                    "family_notified": "queued"}
        if outcome.get("status") == "pending_confirmation":
            if frames:
                # The clip rides with the request (dose_video.send_with_confirmation); consent is re-checked there
                dose_video.capture(self.u_id, [number], frames=frames)
            outcome["video"] = video
        if watch is not None:
            watch["reported"] = watch.get("reported") or {"result": outcome.get("status")}
            if self._watch is watch:
                self._watch = None   # the watch is settled: its verdict never authorises a second record
        return outcome

    # ── emotion ──────────────────────────────────────────────────────────
    def _server_face(self) -> dict:
        """The server's face result from the chat's camera frames: the latest scored uncovered face, else the mean
        of the uncovered samples of the last EMOTION_RECENT_SECONDS."""
        state = self._session()
        if state is None:
            return {"available": False, "reason": "no_camera_session"}
        public = state.public()
        # MonitorSession.emotion is the EmotionService report ({detected, emotion_type, emotion_score,
        # probabilities, error, source}); the per-label map is its "probabilities"
        report = public.get("emotion")
        probabilities = report.get("probabilities") if isinstance(report, dict) else None
        basis = "latest_frame"
        if not probabilities:
            probabilities = dose_emotion.unoccluded_mean(state.emotion_samples,
                                                         self.monotonic() - EMOTION_RECENT_SECONDS)
            basis = f"mean_last_{int(EMOTION_RECENT_SECONDS)}s"
        if not probabilities:
            return {"available": False, "reason": "no_uncovered_face_lately",
                    "identity": public["identity_status"], "mouth_covered": bool(public.get("emotion_occluded"))}
        top = max(probabilities, key=probabilities.get)
        return {"available": True, "top": top, "confidence": round(float(probabilities[top]), 3),
                "probabilities": {k: round(float(v), 3) for k, v in probabilities.items()}, "basis": basis,
                "identity": public["identity_status"]}

    async def get_current_emotion(self, robot_report: dict | None = None) -> dict:
        """The fused emotion with its confidence and the FER / SER / TER outputs: the robot's multimodal engine's
        report when the robot answered (`robot_report`: multimodal/engine.py report()), else the server's face
        result alone (the robot's voice and text branches then have no output here)."""
        face = self._server_face()
        robot = robot_report if isinstance(robot_report, dict) else None
        fused = robot.get("fused") if robot and robot.get("available") else None
        if fused:
            modalities = sorted(fused.get("used") or {})
            source = "robot_multimodal"
        elif face.get("available"):
            fused = {"top": face["top"], "confidence": face["confidence"], "probabilities": face["probabilities"],
                     "used": {"face": 1.0}, "age_seconds": 0.0}
            modalities, source = ["face"], "server_face"
        else:
            modalities, source = [], None
        unavailable = {"status": "robot_unavailable"}
        return {"available": fused is not None, "source": source, "fused": fused, "modalities": modalities,
                "fer": {"server": face, "robot": robot.get("fer") if robot else unavailable},
                "ser": robot.get("ser") if robot else unavailable,
                "ter": robot.get("ter") if robot else unavailable,
                "robot_branches": robot.get("branches") if robot else None,
                "identity": face.get("identity")}

    async def get_emotion_history(self, time_window_minutes: int) -> dict:
        since = schedule.current_time() - datetime.timedelta(minutes=time_window_minutes)

        async def work(conn):
            doses = await conn.fetch(EMOTION_HISTORY_SQL, self.u_id, since)
            moods = await conn.fetch(MOOD_HISTORY_SQL, self.u_id, since)
            return {"window_minutes": time_window_minutes,
                    "dose_emotions": [{"dominant": r["dominant"], "score": r["dominant_score"], "basis": r["basis"],
                                       "at": _local(r["resolved_at"])} for r in doses],
                    "conversation_moods": [{"mood": r["mood"], "at": _local(r["ended_at"])} for r in moods]}
        return await self._db(work)

    # ── memory ───────────────────────────────────────────────────────────
    async def _memory_on(self) -> bool:
        return memory.consent_current(await consent_service.get_state(self.u_id))

    async def remember(self, fact: str, category: str) -> dict:
        kind = MEMORY_KIND.get(category)
        if kind is None:
            return {"stored": False, "reason": "category_not_stored"}   # care notes and the like: never memory
        if not await self._memory_on():
            return {"stored": False, "reason": "memory_consent_off"}
        clean = memory.validate_fact({"kind": kind, "text": fact[:memory.TEXT_MAX]}, today=memory.local_today(),
                                     source="chat")
        if clean is None:
            return {"stored": False, "reason": "invalid_fact"}

        async def work(conn):
            async with conn.transaction():
                return await memory.store_facts(conn, self.u_id, self.conversation_id, [clean])
        stored = await self._db(work)
        return {"stored": bool(stored), "kind": kind, "subject": clean["subject"],
                "reason": None if stored else "not_stored"}

    async def get_patient_memory(self, query: str) -> dict:
        if not await self._memory_on():
            return {"available": False, "reason": "memory_consent_off", "facts": []}
        facts = await self._db(lambda conn: memory.current_facts(conn, self.u_id))
        needle = memory._fold(query)
        words = [w for w in needle.replace("_", " ").split() if len(w) >= 2]
        found = [f for f in facts if needle in memory._fold(f["subject"]) or needle in memory._fold(f["text"])
                 or any(w in memory._fold(f["text"]) for w in words)]
        return {"available": True, "total_facts": len(facts),
                "facts": [{"kind": f["kind"], "subject": f["subject"], "text": f["text"],
                           "event_date": f["event_date"].isoformat() if f.get("event_date") else None}
                          for f in found[:10]]}

    # ── family ───────────────────────────────────────────────────────────
    async def notify_family(self, message: str, urgency: str | None = None, reason: str | None = None,
                            severity: str | None = None) -> dict:
        """A text note to the patient's verified family contacts, through the outbox (outbox.enqueue_to_contacts
        picks the recipients from family_contacts; nothing from the conversation names one)."""
        level = severity or urgency or "normal"
        text = f"🤖 Reachy（{reason}）：{message}" if reason else f"🤖 Reachy：{message}"
        priority = 0 if level in ("high", "emergency") else 1
        base = {"severity": level, "urgency": level, "reason": reason, "recipients": "verified_family_contacts"}
        if text in self._notes_sent:
            return {**base, "queued": False, "contacts": 0, "failure": "already_sent_this_conversation"}
        if len(self._notes_sent) >= NOTE_LIMIT:
            return {**base, "queued": False, "contacts": 0, "failure": "limit_reached_this_conversation"}

        async def work(conn):
            return await outbox.enqueue_to_contacts(
                conn, self.u_id, kind=NOTE_KIND, priority=priority, messages=[{"type": "text", "text": text}],
                dedupe_prefix=f"{NOTE_KIND}:{self.conversation_id}:{len(self._notes_sent)}", contact_flag=None)
        count = await self._db(work)
        self._notes_sent.append(text)
        return {**base, "queued": count > 0, "contacts": count,
                "failure": None if count else "no_verified_family_contact"}

    async def emergency_alert(self, severity: str, reason: str) -> dict:
        async def work(conn):
            name = await conn.fetchval(NAME_SQL, self.u_id) or ""
            text = EMERGENCY_TEXT.format(name=name, reason=reason.strip()[:120])
            async with conn.transaction():
                return await conversation.alert_family(conn, self.u_id, self.conversation_id, text,
                                                       f"tool:{uuid.uuid4().hex}")
        first = await self._db(work)
        return {"accepted": True, "severity": severity, "alerted": bool(first),
                "reason": None if first else "already_alerted_this_conversation"}


class ToolRegistry:
    """Validate, dispatch, time-limit and serialise the tools. execute(name, args) and run_intent never raise."""

    def __init__(self, robot: RobotLink, backend: ServerCareBackend, *, clock=time.time, monotonic=time.monotonic):
        self.robot, self.backend, self.clock, self.monotonic = robot, backend, clock, monotonic
        self._frame: FrameState | None = None
        self._handlers = {name: getattr(self, name) for name in TOOL_NAMES}
        self._dance_at = float("-inf")                          # when the last dance started
        self._danced = False                                    # this chat had a dance
        self._stopped_at = float("-inf")                        # the last stop, while its hold lasts (dance_asked)
        self._starting = 0                                      # dance calls the robot has not answered yet
        self._stopping: set[asyncio.Future] = set()             # stops sent again (_stop_again), each its own task
        self.stops_told = 0                                     # GPT-Live's dance calls answered stopped_by_patient
                                                                # (execute): it says Reachy stopped (the relay's _told)
        self._intent_runs: dict[str | tuple, tuple[float, asyncio.Future]] = {}   # the backstop's runs (_shared_as)
        # FINISH_ON_CLOSE calls still running (hold), in the order they were held: (when, on the event loop's clock,
        # and the seconds their TIMEOUTS entries allow)
        self._held: dict[asyncio.Future, tuple[float, float]] = {}
        self._held_ended = float("-inf")                        # when the last held call ended (the loop's clock)
        self._steps: dict[object, float] = {}                   # MEDICATION_TOOLS calls running -> until (checking)
        self.finished_at = float("-inf")                        # when the last held call or medication step ended
                                                                # (monotonic)

    def hold(self, run: asyncio.Future, *names: str) -> None:
        """A FINISH_ON_CLOSE call (a record, a note to family or an alert) that closing GPT-Live leaves to finish
        (agent_bridge.AgentLiveClient), or the backstop's claim of a dose: the companion's quiet pause waits for it
        (finishing), then gives GPT-Live time to start its answer (finished_at, on the relay's clock: make_registry
        passes it as `monotonic`), and the chat's end waits for it (close). `names`: the tools it runs, whose
        TIMEOUTS entries, counted from now, bound that wait (finish_wait)."""
        self._held[run] = (run.get_loop().time(), sum(TIMEOUTS.get(name, 10.0) for name in names))
        run.add_done_callback(self._release)

    def _release(self, run: asyncio.Future) -> None:
        self._held.pop(run, None)
        self._held_ended = run.get_loop().time()
        self.finished_at = self.monotonic()

    def finishing(self) -> bool:
        """Whether a held call (a record, a note to family or an alert) is still running. A call that has just ended
        counts until its done callback stamped finished_at, so no moment falls between the two."""
        return bool(self._held)

    def checking(self) -> bool:
        """Whether a step of the medication workflow (MEDICATION_TOOLS: a label check, a watch's result, a record)
        still runs, within its TIMEOUTS entry counted from its start: the companion's quiet pause waits for it."""
        now = self.monotonic()
        return any(now < until for until in self._steps.values())

    def finish_wait(self) -> float:
        """How long the held calls still running may yet take: each its TIMEOUTS entries, counted from when it was
        held, one after the other in the order they were held (GPT-Live's client runs its calls one at a time, so a
        call held behind another starts only when that one ends), and never from before the last one ended."""
        end = self._held_ended
        for held_at, seconds in self._held.values():
            end = max(end, held_at) + seconds
        return max(0.0, end - asyncio.get_running_loop().time())

    async def close(self) -> None:
        """The chat is over: the held calls still running finish first (a record settles its watch), bounded by
        finish_wait, which is read again whenever one ends or the bound passes, because a claim of a dose holds each
        record as it starts (run_intent); then the backend reports a watch left without a verdict
        (ServerCareBackend.close, within DB_TIMEOUT_SECONDS)."""
        while True:
            running = [run for run in self._held if not run.done()]
            wait = self.finish_wait()
            if not running or wait <= 0:
                break
            await asyncio.wait(running, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
        await asyncio.wait_for(self.backend.close(), DB_TIMEOUT_SECONDS)

    def watching(self) -> bool:
        """Whether a camera watch is open (ServerCareBackend.watching): GPT-Live starting or reading it then takes the
        patient's claim of a dose (live_session.LiveRelay._told)."""
        return self.backend.watching()

    def dancing(self) -> bool:
        """Whether a dance started recently enough that it may still run (a bare "stop" then stops it)."""
        return self.clock() - self._dance_at < DANCE_ACTIVE_SECONDS

    def danced(self) -> bool:
        """Whether this chat had a dance (「再跳一次」 then asks for another)."""
        return self._danced

    def dance_asked(self) -> bool:
        """The patient asks to dance (the relay read it in their words): a stop's hold is over. Returns whether one
        was still on (a dance asked for meanwhile was refused)."""
        held = self.clock() - self._stopped_at < STOP_HOLD_SECONDS
        self._stopped_at = float("-inf")
        return held

    async def run_intent(self, name: str, args: dict) -> dict:
        """Run a tool for the relay's backstop (live_session, services/tool_intents.py): GPT-Live did not ask for it.
        A call GPT-Live makes for the same tool within INTENT_SHARE_SECONDS gets this run's answer instead of a
        second run (a second dance, a second camera look); for record_medication_taken, only a call for the same dose
        (_shared_as). A FINISH_ON_CLOSE run (a claim's record) is held as it starts, so the chat's end waits for every
        record of a claim (finish_wait)."""
        run = asyncio.ensure_future(self._run(name, args))
        self._intent_runs[self._shared_as(name, args)] = (self.clock(), run)
        if name in FINISH_ON_CLOSE:
            self.hold(run, name)
        return await asyncio.shield(run)

    @staticmethod
    def _shared_as(name: str, args) -> str | tuple:
        """Which backstop run a call shares: the tool's, except record_medication_taken, which the claim of a slot
        runs once per dose, so a call shares only the run of the same dose argument (round 3 of 17 Oct 2026:
        GPT-Live's call for one dose got another dose's answer and was never filed). A call with no matching run
        executes normally."""
        if name != "record_medication_taken":
            return name
        dose = args.get("medication_id") if isinstance(args, dict) else None
        return name, str(dose).strip() if dose else None

    def forget_intents(self) -> None:
        """A new patient turn: GPT-Live's next call is for new words ("dance again"), never the backstop's run. A stop
        stays shared until a dance starts (dance): GPT-Live's call for it can come after the patient spoke again, and
        a second stop would answer idle."""
        stop = self._intent_runs.get("stop_dance")
        self._intent_runs = {"stop_dance": stop} if stop is not None else {}

    async def execute(self, name: str, args: dict | None) -> dict:
        """GPT-Live's own call. A dance call answered stopped_by_patient is counted (stops_told): BACKEND_INSTRUCTION
        has GPT-Live say Reachy stopped then, so the relay's stop line is not said as well (round 3 of 17 Oct 2026)."""
        shared = self._intent_runs.get(self._shared_as(name, args))
        if shared is not None and self.clock() - shared[0] < INTENT_SHARE_SECONDS:
            log.info("live agent tool %s: the backstop's run answers it", name)
            result = await asyncio.shield(shared[1])
        else:
            result = await self._run(name, args)
        if name == "dance" and result.get("error") == "stopped_by_patient":
            self.stops_told += 1
        return result

    async def _run(self, name: str, args: dict | None) -> dict:
        if name not in MEDICATION_TOOLS:
            return await self._call(name, args)
        # A step of the medication workflow: the companion's quiet pause waits while it runs, at most its TIMEOUTS
        # entry (checking), then gives GPT-Live time to take the next step (finished_at, as after a held call)
        step = object()
        self._steps[step] = self.monotonic() + TIMEOUTS.get(name, 10.0)
        try:
            return await self._call(name, args)
        finally:
            del self._steps[step]
            self.finished_at = self.monotonic()

    async def _call(self, name: str, args: dict | None) -> dict:
        handler = self._handlers.get(name)
        if handler is None:
            return {"ok": False, "tool": name, "error": "unknown_tool"}
        if not isinstance(args, dict):
            return {"ok": False, "tool": name, "error": "arguments_must_be_object"}
        args = {key: value for key, value in args.items() if value is not None}   # strict schemas send null
        started = self.clock()
        try:
            inspect.signature(handler).bind(**args)   # missing or unexpected arguments fail fast
            result = await asyncio.wait_for(handler(**args), timeout=TIMEOUTS.get(name, 10.0))
            return {"ok": True, "tool": name, "result": result}
        except TypeError as error:
            return {"ok": False, "tool": name, "error": f"invalid_arguments: {error}"}
        except asyncio.TimeoutError:
            log.warning("live agent tool %s timed out", name)
            if name == "dance" and self._stopped_at >= started:
                # the patient's stop came while the robot loaded the move: the dance sent it again (dance), and the
                # answer is the stop's, never a failure the backstop would say (round 3 of 17 Oct 2026)
                return {"ok": False, "tool": name, "error": "stopped_by_patient"}
            return {"ok": False, "tool": name, "error": "tool_timeout"}
        except ToolError as error:
            return {"ok": False, "tool": name, "error": str(error)}
        except Exception as error:
            log.exception("live agent tool %s failed", name)
            return {"ok": False, "tool": name, "error": f"internal_tool_error: {type(error).__name__}"}

    async def _fresh_frame(self) -> FrameState:
        frame = self._frame
        if frame is None or self.clock() - frame.wall_time > FRAME_FRESHNESS_SECONDS:
            await self.camera()
            frame = self._frame
        return frame

    @staticmethod
    def _text(value, name: str, limit: int) -> str:
        text = str(value).strip() if isinstance(value, str) else ""
        if not text:
            raise ToolError(f"{name}_required")
        return text[:limit]

    async def camera(self) -> dict:
        jpeg, meta = await self.robot.capture_jpeg()
        self._frame = FrameState(uuid.uuid4().hex, jpeg, dict(meta), self.clock())
        return {"frame_id": self._frame.frame_id, "captured_at_unix": self._frame.wall_time, "fresh": True,
                "width": meta.get("width"), "height": meta.get("height"), "bytes": len(jpeg)}

    async def identify_medicine(self, expected_name: str | None = None) -> dict:
        expected = expected_name.strip() if isinstance(expected_name, str) and expected_name.strip() else None
        frame = await self._fresh_frame()
        result = await self.backend.identify_medicine(frame.jpeg, expected)
        return {**result, "frame_id": frame.frame_id, "prescription_verified": False}

    async def verify_medication(self, expected_medication: str) -> dict:
        expected = self._text(expected_medication, "expected_medication", 120)
        frame = await self._fresh_frame()
        result = await self.backend.verify_medication(frame.jpeg, expected)
        return {**result, "frame_id": frame.frame_id}

    async def describe_view(self, question: str) -> dict:
        asked = self._text(question, "question", 300)
        frame = await self._fresh_frame()
        result = await self.backend.describe_view(frame.jpeg, asked)
        return {**result, "frame_id": frame.frame_id}

    async def verify_prescription(self, medication_id: str) -> dict:
        return await self.backend.verify_prescription(self._text(medication_id, "medication_id", 40))

    async def get_medication_schedule(self, time_scope: str | None = None) -> dict:
        scope = time_scope or "today"
        if scope not in TIME_SCOPES:
            raise ToolError("invalid_time_scope")
        return await self.backend.get_medication_schedule(scope)

    async def check_medication_due(self) -> dict:
        return await self.backend.check_medication_due()

    async def start_pill_intake_detection(self, medication_name: str | None = None,
                                          timeout_seconds: int | None = None) -> dict:
        seconds = DEFAULT_WATCH_SECONDS if timeout_seconds is None else int(timeout_seconds)
        if not 5 <= seconds <= 120:
            raise ToolError("timeout_seconds_out_of_range")
        name = self._text(medication_name, "medication_name", 120) if medication_name else None
        return await self.backend.start_pill_intake_detection(seconds, name)

    async def get_pill_intake_result(self, session_id: str | None = None) -> dict:
        return await self.backend.get_pill_intake_result(str(session_id).strip() if session_id else None)

    async def record_medication_taken(self, medication_name: str | None = None, session_id: str | None = None,
                                      medication_id: str | None = None,
                                      patient_confirmed: bool | None = None) -> dict:
        name = self._text(medication_name, "medication_name", 120) if medication_name else None
        dose_id = self._text(medication_id, "medication_id", 40) if medication_id else None
        if not name and not dose_id:
            raise ToolError("medication_name_or_id_required")
        return await self.backend.record_medication_taken(dose_id, bool(patient_confirmed), name,
                                                          str(session_id).strip() if session_id else None)

    async def get_current_emotion(self) -> dict:
        try:
            report = await self.robot.emotion()
        except ToolError as error:
            log.info("live agent: the robot gave no emotion report (%s)", error)
            report = None
        return await self.backend.get_current_emotion(report)

    async def get_emotion_history(self, time_window_minutes: int = 60) -> dict:
        if not 1 <= int(time_window_minutes) <= 1440:
            raise ToolError("time_window_minutes_out_of_range")
        return await self.backend.get_emotion_history(int(time_window_minutes))

    async def trigger_antenna_emote(self, preset: str) -> dict:
        if preset not in EMOTE_PRESETS:
            raise ToolError("invalid_emote_preset")
        return await self.robot.emote(preset)

    async def dance(self, dance: str | None = None, repeat: int | None = None) -> dict:
        """One of the robot's recorded dances, started on the robot (its motion worker) and answered at once;
        DEFAULT_DANCE_REPEAT moves without a repeat. Refused while a stop's hold lasts (STOP_HOLD_SECONDS). It may run
        from the call on (dancing): the robot answers as the move starts. The robot loads the move first (off its event
        loop, from disk on the first dance), and a stop that reaches it meanwhile finds no move, answers idle, and the
        move starts after it: such a stop is sent again when the dance answers, fails or times out (_stop_again, a task
        of its own, so its error or lateness never changes this answer), and the dance is stopped_by_patient. A dance
        the robot refuses or fails is no dance running (round 3 of 17 Oct 2026: dancing() stayed true for 25 s, and a
        bare 「好了」 sent a stop to an idle robot and started the hold); one that times out may still start."""
        if dance is not None and dance not in DANCES:
            raise ToolError("unknown_dance")
        times = DEFAULT_DANCE_REPEAT if repeat is None else int(repeat)
        if not 1 <= times <= MAX_DANCE_REPEAT:
            raise ToolError("repeat_out_of_range")
        if self.clock() - self._stopped_at < STOP_HOLD_SECONDS:
            log.info("[TOOL] dance refused: the patient asked to stop %.1f s ago", self.clock() - self._stopped_at)
            raise ToolError("stopped_by_patient")
        log.info("[TOOL] dance called: %s x%d", dance or "any", times)
        called_at = self._dance_at = self.clock()
        self._intent_runs.pop("stop_dance", None)   # a stop GPT-Live asks for now is for this dance
        self._starting += 1
        try:
            result = await self.robot.dance(dance, times)
        except Exception as error:
            if self._dance_at == called_at:
                self._dance_at = float("-inf")   # the robot refused or failed it (a later call may have changed it)
            if self._stopped_at >= called_at:   # a stop came while the robot loaded the move
                log.info("[TOOL] dance failed after the patient's stop (%s): stopping it again", error)
                self._stop_again()
                raise ToolError("stopped_by_patient") from None
            raise
        except asyncio.CancelledError:
            if self._stopped_at >= called_at:   # timed out (TIMEOUTS) after the patient's stop: the move may still start
                log.info("[TOOL] dance timed out after the patient's stop: stopping it again")
                self._stop_again()
            raise
        finally:
            self._starting -= 1
        self._danced = True
        if self._stopped_at >= called_at:   # a stop came while the robot started the move (dance_asked clears it)
            log.info("[TOOL] dance started after the patient's stop: stopping it again")
            self._stop_again()
            raise ToolError("stopped_by_patient")
        return result

    def _stop_again(self) -> None:
        """Send the robot the patient's stop again, for a dance a stop came during, as a task of its own within
        TIMEOUTS['stop_dance']: its error or lateness is logged and never replaces the dance's stopped_by_patient
        (round 3 of 17 Oct 2026: inside the dance's own time limit, a late or failed second stop made the dance answer
        tool_timeout or stop_dance_failed, and the backstop said it could not dance right after the stop line)."""
        async def stop() -> None:
            try:
                await asyncio.wait_for(self.robot.stop_dance(), TIMEOUTS["stop_dance"])
            except Exception as error:
                log.warning("[TOOL] the second stop of a dance the patient stopped failed: %s",
                            error or type(error).__name__)

        task = asyncio.ensure_future(stop())
        self._stopping.add(task)
        task.add_done_callback(self._stopping.discard)

    async def stop_dance(self) -> dict:
        """Stop the robot's dance. A dance asked for in the next STOP_HOLD_SECONDS is refused (dance), and so is
        GPT-Live's late call for a dance the relay's backstop ran before the stop, which no longer shares that run.
        While a dance call waits for the robot's answer, the robot answers idle (it still loads the move), but that
        dance is stopped as it starts (dance): the answer is then cancelled, with `starting` true."""
        log.info("[TOOL] stop_dance called")
        self._dance_at, self._stopped_at = float("-inf"), self.clock()
        self._intent_runs.pop("dance", None)
        starting = self._starting > 0
        result = await self.robot.stop_dance()
        if starting and isinstance(result, dict) and result.get("status") == "idle":
            return {"status": "cancelled", "dance": None, "starting": True}
        return result

    async def play_emotion(self, emotion: str, posture: str | None = None) -> dict:
        """One of the robot's recorded emotion moves, with a posture to hold after it (the live chat's `express`)."""
        if emotion not in EMOTION_MOVES:
            raise ToolError("unknown_emotion")
        if posture is not None and posture not in POSTURES:
            raise ToolError("invalid_posture")
        log.info("[TOOL] play_emotion called: %s posture=%s", emotion, posture or "-")
        return await self.robot.express(emotion, posture)

    async def get_weather(self, location: str | None = None) -> dict:
        """The weather for a place in Taiwan (weather_lookup: geocoded, then Open-Meteo), fetched at the call."""
        place = str(location).strip()[:80] if isinstance(location, str) and str(location).strip() else None
        return await asyncio.to_thread(weather_lookup.lookup, place, self.backend.language)

    async def get_current_time(self, place: str | None = None) -> dict:
        """The time now, from the clock at this call (services/current_time.py), here or in the place named."""
        asked = str(place).strip()[:80] if isinstance(place, str) and str(place).strip() else None
        log.info("[TOOL] get_current_time called: %s", asked or "home")
        return current_time.answer(asked, self.backend.language)

    async def search_web(self, query: str, max_results: int | None = None) -> dict:
        """One web search (services/web_search.py: OpenAI's hosted search through the live connection), run in a
        thread; unavailable without a usable live connection."""
        asked = self._text(query, "query", 300)
        target = ai_settings.live_target()
        if target is None:
            return {"status": "unavailable", "query": asked, "reason": "no_live_connection"}
        log.info("[TOOL] search_web called: %s", asked[:80])
        result = await asyncio.to_thread(web_search.search, asked, self.backend.language, target.api_key)
        if max_results is not None and result.get("sources"):
            result["sources"] = result["sources"][:max(1, min(10, int(max_results)))]
        return result

    async def set_reminder(self, what: str | None = None, medication_name: str | None = None, position: int | None = None,
                           in_minutes=None, at_time: str | None = None, day: str | None = None) -> dict:
        """A reminder on the server's clock (services/reminders.py), said by Reachy's voice when it is due."""
        log.info("[TOOL] set_reminder called: what=%r medicine=%r position=%r in=%r at=%r day=%r",
                 (what or "")[:40], medication_name, position, in_minutes, at_time, day)
        return await reminders.create(self.backend.u_id, what=what, medication_name=medication_name,
                                      position=int(position) if position is not None else None, in_minutes=in_minutes,
                                      at_time=at_time, day=day, language=self.backend.language,
                                      conversation_id=self.backend.conversation_id)

    async def cancel_reminder(self, reminder_id: str | None = None) -> dict:
        log.info("[TOOL] cancel_reminder called: %s", reminder_id or "all")
        return await reminders.cancel(self.backend.u_id, reminder_id)

    async def remember(self, fact: str, category: str) -> dict:
        text = self._text(fact, "fact", 1000)
        if category not in MEMORY_CATEGORIES:
            raise ToolError("invalid_memory_category")
        return await self.backend.remember(text, category)

    async def get_patient_memory(self, query: str) -> dict:
        return await self.backend.get_patient_memory(self._text(query, "query", 500))

    async def notify_family(self, message: str = "", urgency: str | None = None, reason: str | None = None,
                            severity: str | None = None) -> dict:
        text = self._text(message, "notification_message", 1000)
        level = severity or urgency
        if level is not None and level not in URGENCIES:
            raise ToolError("invalid_notification_severity")
        why = self._text(reason, "reason", 60) if reason else None
        return await self.backend.notify_family(text, urgency, why, severity)

    async def emergency_alert(self, severity: str, reason: str) -> dict:
        if severity not in SEVERITIES:
            raise ToolError("invalid_emergency_severity")
        why = self._text(reason, "emergency_reason", 1000)
        gesture = asyncio.create_task(self.robot.emote("alert_attention"))
        try:
            alert = await self.backend.emergency_alert(severity, why)
        finally:
            try:
                gesture_result = await asyncio.wait_for(gesture, TIMEOUTS["trigger_antenna_emote"])
            except Exception as error:
                gesture_result = {"status": "failed", "error": type(error).__name__}
        return {"alert": alert, "gesture": gesture_result}
