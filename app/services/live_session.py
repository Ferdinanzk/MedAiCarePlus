"""Live voice check-ins with GPT-Live (spec docs/superpowers/specs/2026-10-12-live-voice-2a-design.md, A2 and A3).

The robot opens the WebSocket /api/device/conversations/live on the device port (api_device.conversation_live), and
one LiveRelay runs the chat on it. JSON text frames carry the messages; binary frames carry audio, PCM16 mono at
16 kHz, both ways.
- robot -> server: {"type": "start", "task_id", "language", "rate": 16000, "bridge_version"} first; later
  {"type": "end", "reason"}, {"type": "playing"} and {"type": "idle"}.
- server -> robot: {"type": "ready", "conversation_id"}, {"type": "flush"}, {"type": "say", "speech_text", "reason"},
  {"type": "fallback", "reason", "conversation_id"} and {"type": "end", "reason", "risk"}.

The start: after `start` the server checks, in this order, and answers `fallback` (conversation_id null; then it
closes the socket) at the first failure, before any conversation row exists: LIVE_VOICE_TEST (live_disabled), the
rate (unsupported_rate), check-in consent (checkin_consent_required), the task this robot leased (task_not_found), the
robot's live switch on the Reachy card (live_disabled), a usable live model on the AI connections card
(live_not_configured). Then GPT-Live must start within CONNECT_TIMEOUT, else live_unavailable. Only then is the
conversation row written (mode 'live', the live model's id), with Reachy's opening as its first turn; the robot gets
`ready`, and GPT-Live says the opening in its own voice. A robot that gets a fallback starts today's text chat with
POST /conversations. A first message that is not a valid start closes the socket with BAD_START.

The chat (spec B2, C1-C3), once the robot has `ready`: the robot's microphone goes to GPT-Live as it arrives, and
GPT-Live's voice goes to the robot, but the start of every reply to the patient is held HOLD_SECONDS first (B2), so
the safety checks on the patient's words can stop it before anything is heard. A reply starts when GPT-Live's audio
turns non-silent (a sample above ONSET_LEVEL) after the patient spoke; after that onset the audio flows through, and
silence between replies never goes to the robot. GPT-Live's transcripts become conversation turns (C1): fragments
are grouped per role, a role switch closes the other role's group and a pause over TURN_GAP_SECONDS closes the
patient's. The chat ends (C2) on a goodbye, SILENCE_END_SECONDS without patient speech, LIVE_MAX_SECONDS, the robot's
`end` or a dropped robot; every ending stores conversation.live and runs the post-chat work as
/conversations/{id}/end does. When GPT-Live fails after the chat started, the robot gets `fallback` with the
conversation id and goes on with today's text chat (C3).
"""

import asyncio
import collections
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

import numpy as np
from starlette.websockets import WebSocketDisconnect

from app import config
from app.database import get_pool
from app.services import after_chat, ai_settings, consent_service, conversation, memory, reachy_tasks
from app.services.live_client import LiveClient, LiveError

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 8.0   # seconds for GPT-Live to start (measured: about 1.2 s); then the robot uses the text chat
START_TIMEOUT = 5.0     # seconds the robot has for its start message once the socket is open
RATE = 16000            # PCM16 mono, both ways: one GPT-Live rate covers both directions; the robot's audio is 16 kHz
BAD_START = 4400        # WebSocket close code: the robot's first message was not a valid start
HOLD_SECONDS = 0.5            # the start of every GPT-Live reply waits this long before it goes to the robot (B2)
CONFIRM_SECONDS = 0.4         # a screening hit is confirmed once the patient's words settled this long (B1)
TURN_GAP_SECONDS = 1.5        # a pause this long closes the patient's turn (C1)
SILENCE_END_SECONDS = 45      # no patient speech this long ends the chat (C2)
LIVE_MAX_SECONDS = 600        # the longest live chat (C2)
GOODBYE_GRACE_SECONDS = 3     # after a goodbye, the chat ends this long after GPT-Live's answer stopped (C2)
ONSET_LEVEL = 120             # a PCM16 sample above this is sound, as the TCU companion detects GPT-Live's speech
TAIL_SECONDS = 0.6            # GPT-Live's audio has stopped this long after its last sound (TCU companion) ...
QUIET_SECONDS = 1.0           # ... or this long after its last chunk of any kind
MAX_GROUP_CHARS = 2000        # a transcript group this long is closed, and the next fragment starts a new one
TICK_SECONDS = 0.1            # how often the hold, the turn gap and the endings are checked
ROBOT_END_REASONS = ("finished", "goodbye", "silence", "patient_left", "stopped", "error")   # anything else: stopped

LEASED_TASK_SQL = ("SELECT task_id, u_id, slot_time, intk_ids, status, reason FROM reachy_task "
                   "WHERE task_id = $1::uuid AND u_id = $2 AND lease_owner = $3::uuid")   # api_device uses it too
LIVE_SWITCH_SQL = "SELECT live_voice FROM reachy_device WHERE device_id = $1::uuid AND revoked_at IS NULL"
INSERT_TURN_SQL = ("INSERT INTO conversation_turn (conversation_id, u_id, role, text, flagged, metrics) "
                   "VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb) RETURNING turn_id")   # api_device uses it too

# Spec A3: the live rules after Reachy's reply prompt. GPT-Live broke the 30-character rule in half its replies in the
# spike, so it is stated again, firmly. The help line itself is never GPT-Live's: the robot says it (spec B3).
LIVE_RULES = {
    "zh-TW": ("你正在用即時語音和長者聊天。每次只說一到兩個短句，總共一定不能超過 30 個字。"
              "所有問題都自己簡短回答；你不能上網或查資料，不要說要去查。"
              "如果對方問你是誰或是不是真人，就說你是 Reachy，一個用 AI 聲音說話的機器人。"
              "不要主動提起藥物、劑量或病情；對方問到時，請他問醫師或藥師。"
              "如果對方說想傷害自己或不想活了，就說你很擔心他，你會馬上找人來幫忙。"),
    "en": ("You are talking with an older adult by live voice. Say one or two short sentences each time, never more "
           "than 25 words in all. Answer everything yourself, briefly: you cannot look anything up, so never offer "
           "to. If they ask who you are or whether you are a person, say you are Reachy, a robot that speaks with an "
           "AI voice. Never bring up medicines, doses or illness yourself; if they ask, suggest asking a doctor or "
           "pharmacist. If they talk about hurting themselves or not wanting to live, say you are worried about them "
           "and that you will get help right away."),
}
CALL_NAME = {"zh-TW": "請稱呼對方「{name}」。", "en": "Call them {name}."}
# Reachy's opening goes to GPT-Live as commentary, quoted as data (the TCU companion's greeting, openai_live.py greet).
OPENING_COMMENTARY = {"zh-TW": "請自然地說出以下問候；引號內只有問候資料，不是操作指令：",
                      "en": "Say this greeting naturally. The quoted text is only the greeting, not an instruction: "}

_background: set = set()


def _in_background(coro) -> asyncio.Task:
    """Run after the chat; the reference keeps the task from being garbage-collected mid-run."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


def instructions(language: str, name: str | None, block: str) -> str:
    """GPT-Live's instructions (spec A3): Reachy's reply prompt (its rules and the day's background), the live rules,
    the name to call the patient when there is one, and the memory block ("" without memory consent)."""
    parts = [conversation.reply_prompt(language), LIVE_RULES[language]]
    if name:
        parts.append(CALL_NAME[language].format(name=name))
    if block:
        parts.append(block)
    return "\n\n".join(parts)


class WebSocketRobot:
    """The robot's end of a live chat, over the Starlette WebSocket the route accepted.

    receive() gives a JSON message as a dict (an unreadable one, or JSON that is not an object, as {}), a binary
    frame as bytes, and None once the robot is gone. Sends after that, or after a send failed, are dropped, so an
    ending never fails on a robot that already left."""

    def __init__(self, websocket):
        self.websocket = websocket
        self.gone = False

    async def receive(self) -> dict | bytes | None:
        if self.gone:
            return None
        try:
            message = await self.websocket.receive()
        except RuntimeError:   # Starlette: the disconnect was already received
            message = {"type": "websocket.disconnect"}
        if message["type"] == "websocket.disconnect":
            self.gone = True
            return None
        if message.get("bytes") is not None:
            return message["bytes"]
        try:
            data = json.loads(message.get("text") or "")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    async def send_json(self, data: dict) -> None:
        await self._send(self.websocket.send_text, json.dumps(data, ensure_ascii=False))

    async def send_bytes(self, data: bytes) -> None:
        await self._send(self.websocket.send_bytes, data)

    async def close(self, code: int = 1000) -> None:
        await self._send(self.websocket.close, code)
        self.gone = True

    async def _send(self, send, value) -> None:
        if self.gone:
            return
        try:
            await send(value)
        except (WebSocketDisconnect, RuntimeError, OSError):
            self.gone = True


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))


def _loud(pcm: bytes) -> bool:
    """Any PCM16 sample above ONSET_LEVEL."""
    samples = np.frombuffer(pcm[:len(pcm) - len(pcm) % 2], dtype="<i2")
    return bool(samples.size) and int(np.abs(samples.astype(np.int32)).max()) > ONSET_LEVEL


@dataclass
class _Group:
    """Transcript fragments of one role that become one conversation turn."""
    first: float
    last: float
    text: str = ""
    fragments: int = 0

    def add(self, text: str, now: float) -> None:
        self.text += text
        self.last = now
        self.fragments += 1


@dataclass
class _Segment:
    """One stretch of GPT-Live's sound, from its onset until TAIL_SECONDS of silence or QUIET_SECONDS of no audio."""
    onset: float
    last_signal: float
    last_audio: float
    held: bool                       # the start of a reply: forwarded only HOLD_SECONDS after the onset
    released_at: float | None = None
    queue: collections.deque = field(default_factory=collections.deque)


@dataclass(frozen=True)
class _Outcome:
    kind: str      # "end": the conversation ends; "fallback": it goes on as today's text chat
    reason: str
    risk: bool = False


class LiveRelay:
    """One live check-in on one robot WebSocket (spec A2).

    robot: an adapter with async receive() -> dict | bytes | None, send_json(dict), send_bytes(bytes) and
    close(code=1000) (WebSocketRobot; tests pass a fake). device: get_device_ws's dict. client_factory builds the
    GPT-Live client (live_client.LiveClient's signature); clock is the monotonic clock the relay reads.
    After a successful start: client is the running GPT-Live session, conversation_id the conversation row, opening
    and opening_turn_id Reachy's opening line and its stored turn."""

    def __init__(self, robot, device: dict, *, client_factory=LiveClient, clock=time.monotonic):
        self.robot = robot
        self.device = device
        self.u_id = device["u_id"]
        self.client_factory = client_factory
        self.clock = clock
        self.language = "zh-TW"
        self.client = None
        self.conversation_id: str | None = None
        self.opening = ""
        self.opening_turn_id: int | None = None
        self._close_reason: str | None = None
        self.outcome: _Outcome | None = None
        self._done = asyncio.Event()
        self._handler_lock = asyncio.Lock()     # GPT-Live's events and the timers are handled one at a time, and
                                                # an outcome decided meanwhile never cuts one off part way (_chat)
        self._send_lock = asyncio.Lock()        # one sender to the robot at a time, so audio stays in order
        self._turn_lock = asyncio.Lock()        # turns are stored in the order their groups closed
        self._live_closed = False
        self._usage: float | None = None
        self._opening_spoken = False            # GPT-Live's spoken opening replaced the opening turn's text
        self._patient: _Group | None = None
        self._reachy: _Group | None = None
        self._patient_turns = 0
        self._segment: _Segment | None = None
        self._armed = False                     # the patient spoke since GPT-Live's last onset: hold the next one
        self._reply_metrics: dict | None = None  # the open Reachy reply's timings, stored with its turn
        self._started_at = self._last_patient_at = self._reply_due_at = 0.0
        self._goodbye = False
        self._goodbye_replied = False
        self._goodbye_end_at: float | None = None

    async def run(self) -> None:
        """The whole live chat; returns when it is over and never raises. GPT-Live is always closed at the end. A
        failure the relay does not expect closes the robot's socket with 1011; the robot then uses today's chat."""
        try:
            start = await self._receive_start()
            if start is None:
                await self._close_robot(BAD_START)
            elif await self._open(start):
                await self._chat()
        except Exception:
            log.exception("live chat %s of patient %s failed", self.conversation_id, self.u_id)
            await self._close_robot(1011)
        finally:
            await self._close_live()

    async def _receive_start(self) -> dict | None:
        """The robot's first message when it is a start with a task_id (and a language, when given, as text); None
        when it is anything else, when the robot is gone, or when nothing came within START_TIMEOUT."""
        try:
            message = await asyncio.wait_for(self.robot.receive(), START_TIMEOUT)
        except asyncio.TimeoutError:
            return None
        if (not isinstance(message, dict) or message.get("type") != "start"
                or not isinstance(message.get("task_id"), str) or not isinstance(message.get("language", ""), str)):
            return None
        return message

    async def _refuse(self, reason: str) -> bool:
        """Before any conversation row exists (spec C3): the robot starts today's text chat (POST /conversations)."""
        log.info("live chat of patient %s not started: %s", self.u_id, reason)
        await self.robot.send_json({"type": "fallback", "reason": reason, "conversation_id": None})
        await self._close_robot(1000)
        return False

    async def _open(self, start: dict) -> bool:
        """The start checks in order (module docstring), GPT-Live within CONNECT_TIMEOUT, then the conversation row
        with Reachy's opening as its first turn, and `ready`. False when the robot was sent a fallback instead."""
        self.language = language = conversation.language_of(start.get("language"))
        if not config.LIVE_VOICE_TEST:   # spec 5: without the test switch no live path is reachable
            return await self._refuse("live_disabled")
        if start.get("rate", RATE) != RATE:
            return await self._refuse("unsupported_rate")
        state = await consent_service.get_state(self.u_id)
        if not reachy_tasks.checkin_allowed(state):
            return await self._refuse("checkin_consent_required")
        try:
            task_id = str(uuid.UUID(start["task_id"]))
        except ValueError:
            return await self._refuse("task_not_found")
        async with get_pool().acquire() as conn:
            task = await conn.fetchrow(LEASED_TASK_SQL, task_id, self.u_id, self.device["device_id"])
            switched_on = task is not None and bool(await conn.fetchval(LIVE_SWITCH_SQL, self.device["device_id"]))
        if task is None:
            return await self._refuse("task_not_found")
        if not switched_on:
            return await self._refuse("live_disabled")
        target = ai_settings.live_target()
        if target is None:
            return await self._refuse("live_not_configured")

        # As POST /conversations: the name to call the patient (both chat types), and with memory consent the
        # follow-up and the memory block (for GPT-Live's instructions only).
        memory_on = memory.consent_current(state)
        today = memory.local_today()
        block = ""
        async with get_pool().acquire() as conn:
            name = await memory.call_name(conn, self.u_id, memory_on)
            followup = await memory.pick_followup(conn, self.u_id, today) if memory_on else None
            if memory_on:
                try:
                    async with conn.transaction():   # a savepoint: a failed read costs the chat its notes, nothing else
                        block = await memory.build_block(conn, self.u_id, language,
                                                         followup["memory_id"] if followup else None, today)
                except Exception:
                    log.exception("live chat of patient %s: the memory block failed; starting without it", self.u_id)
                    block = ""
        self.opening = memory.opening_line(language, name)

        self.client = self.client_factory(target.api_key, target.model_id, target.voice,
                                          instructions(language, name, block), rate=RATE)
        started = self.clock()
        try:
            await self.client.start(timeout=CONNECT_TIMEOUT)
        except LiveError as error:
            log.warning("live chat of patient %s: GPT-Live did not start (%s)", self.u_id, error.code)
            self.client = None   # start() closed its own socket
            return await self._refuse("live_unavailable")
        connect_ms = int((self.clock() - started) * 1000)

        conversation_id = str(uuid.uuid4())
        async with get_pool().acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO conversation (conversation_id, u_id, task_id, language, model, followup_memory_id, mode) "
                "VALUES ($1::uuid, $2, $3::uuid, $4, $5, $6::uuid, 'live')",
                conversation_id, self.u_id, str(task["task_id"]), language, target.model_id,
                str(followup["memory_id"]) if followup else None)
            self.opening_turn_id = await conn.fetchval(INSERT_TURN_SQL, conversation_id, self.u_id, "reachy",
                                                       self.opening, False, None)
        self.conversation_id = conversation_id
        log.info("live chat %s started for patient %s: %s, voice %s, GPT-Live ready in %d ms", conversation_id,
                 self.u_id, target.model_id, target.voice, connect_ms)
        await self.robot.send_json({"type": "ready", "conversation_id": conversation_id})
        return True

    # ── the chat: three loops until an outcome is decided ────────────────────────────────────────────────────────

    async def _chat(self) -> None:
        """Give GPT-Live the opening, relay until an ending or a fallback is decided (_finish), then wrap up.

        Three loops run meanwhile: the robot's messages (_uplink), GPT-Live's events (_downlink) and the timers
        (_ticker). GPT-Live's events and the timers are handled under _handler_lock, one at a time, and the loops are
        stopped only once the one being handled has finished, so a turn being stored is never cut off part way."""
        now = self.clock()
        self._started_at = self._last_patient_at = self._reply_due_at = now
        try:
            await self.client.commentary(OPENING_COMMENTARY[self.language]
                                         + json.dumps(self.opening, ensure_ascii=False))
        except LiveError:
            pass   # GPT-Live has closed the session: its closed event (_downlink) decides what happens
        loops = [asyncio.create_task(loop) for loop in (self._uplink(), self._downlink(), self._ticker())]
        try:
            await self._done.wait()
            async with self._handler_lock:
                for loop in loops:
                    loop.cancel()
        finally:
            for loop in loops:
                loop.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
        await self._wrap_up()

    def _finish(self, kind: str, reason: str, *, risk: bool = False) -> bool:
        """Decide how the chat ends: "end" (the conversation ends with `reason`) or "fallback" (it goes on as today's
        text chat). The first decision wins and later ones return False, so an ending, a fallback and a block racing
        each other end the conversation once. _chat then stops the loops and runs _wrap_up."""
        if self.outcome is not None:
            return False
        self.outcome = _Outcome(kind, reason, risk)
        self._done.set()
        return True

    async def _uplink(self) -> None:
        """The robot's messages: microphone PCM goes to GPT-Live; `end`, `playing` and `idle` are handled."""
        try:
            while True:
                message = await self.robot.receive()
                if message is None:
                    self._finish("end", "disconnected")
                    return
                if isinstance(message, (bytes, bytearray)):
                    if self.outcome is None and message and len(message) % 2 == 0:
                        await self.client.send_audio(bytes(message))
                elif isinstance(message, dict):
                    self._on_robot_message(message)
        except LiveError:
            # GPT-Live closed the session. Its own closed event (_downlink) decides what happens: deciding here would
            # turn OpenAI's moderation close (spec B4), whose event is still on its way, into a plain fallback.
            return
        except Exception:
            log.exception("live chat %s: the robot's side failed", self.conversation_id)
            self._finish("end", "error")

    async def _downlink(self) -> None:
        """GPT-Live's events: audio through the hold, transcripts into turns, errors and closes into a fallback."""
        try:
            async for event in self.client.events():
                async with self._handler_lock:
                    await self._on_live_event(event)
                if self.outcome is not None:
                    return
            self._finish("fallback", "live_closed")
        except LiveError as error:
            self._finish("fallback", error.code or "live_closed")
        except Exception:
            log.exception("live chat %s: GPT-Live's side failed", self.conversation_id)
            self._finish("fallback", "error")

    async def _ticker(self) -> None:
        try:
            while True:
                await asyncio.sleep(TICK_SECONDS)
                async with self._handler_lock:
                    await self._tick()
        except Exception:
            log.exception("live chat %s: the timers failed", self.conversation_id)
            self._finish("fallback", "error")

    async def _tick(self) -> None:
        """The timers: the end of the hold, the end of GPT-Live's sound, the patient's turn gap, and the endings."""
        now = self.clock()
        segment = self._segment
        if segment is not None:
            await self._drain(segment)
            if self._segment is segment and (now - segment.last_signal > TAIL_SECONDS
                                             or now - segment.last_audio > QUIET_SECONDS):
                self._segment = None
                self._sound_ended(now)
        if self._patient is not None and now - self._patient.last > TURN_GAP_SECONDS:
            await self._close_patient()
        if self._goodbye_end_at is not None and now >= self._goodbye_end_at:
            self._finish("end", "goodbye")
        elif now - self._last_patient_at >= SILENCE_END_SECONDS:
            self._finish("end", "silence")
        elif now - self._started_at >= LIVE_MAX_SECONDS:
            self._finish("end", "limit")

    def _on_robot_message(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "end":
            reason = message.get("reason")
            self._finish("end", reason if reason in ROBOT_END_REASONS else "stopped")
        elif kind == "playing" and self._reply_metrics is not None:
            self._reply_metrics.setdefault("robot_playing_ms", _ms(self.clock() - self._reply_due_at))

    async def _on_live_event(self, event) -> None:
        if event.type == "audio":
            await self._on_audio(event.data)
        elif event.type == "input_text":
            await self._on_input_text(event.text)
        elif event.type == "output_text":
            await self._on_output_text(event.text)
        elif event.type == "error":
            if event.code == "content_filter":
                self._close_reason = "content"
                await self._moderation_end()
            else:
                self._finish("fallback", event.code or "live_closed")
        elif event.type == "closed":
            self._close_reason = event.reason or None
            if event.usage_seconds is not None:
                self._usage = event.usage_seconds
            if event.reason == "content":
                await self._moderation_end()
            else:
                self._finish("fallback", "live_closed")

    async def _moderation_end(self) -> None:
        """OpenAI's moderation ended GPT-Live's speech or the session (spec B4): the chat goes on as today's text
        chat, whose every turn is screened and judged before Reachy answers."""
        self._finish("fallback", "live_ended")

    # ── GPT-Live's voice to the robot, behind the hold (B2) ──────────────────────────────────────────────────────

    async def _on_audio(self, pcm: bytes) -> None:
        if not pcm:
            return
        now = self.clock()
        loud = _loud(pcm)
        segment = self._segment
        if segment is None:
            if not loud:
                return   # silence between replies never goes to the robot
            segment = self._segment = _Segment(onset=now, last_signal=now, last_audio=now, held=self._armed)
            self._armed = False
            if not segment.held:   # nobody spoke since GPT-Live's last sound (its opening, or it goes on): no hold
                segment.released_at = now
                self._reply_metrics = self._reply_metrics or {}
                self._reply_metrics.setdefault("first_audio_ms", _ms(now - self._reply_due_at))
                self._reply_metrics.setdefault("hold_ms", 0)
            self._sound_started()
        segment.last_audio = now
        if loud:
            segment.last_signal = now
        segment.queue.append(pcm)
        await self._drain(segment)

    async def _drain(self, segment: _Segment) -> None:
        """Forward a segment's queued audio, in order, once its hold is over."""
        if segment.released_at is None:
            now = self.clock()
            if now - segment.onset < HOLD_SECONDS:
                return
            segment.released_at = now
            if self._reply_metrics is None:
                self._reply_metrics = {}
            self._reply_metrics.setdefault("first_audio_ms", _ms(now - self._reply_due_at))
            self._reply_metrics.setdefault("hold_ms", _ms(now - segment.onset))
        async with self._send_lock:
            while segment.queue and self.outcome is None:
                await self._robot_bytes(segment.queue.popleft())

    def _sound_started(self) -> None:
        if self._reply_metrics is None:
            self._reply_metrics = {}
        if self._goodbye and not self._goodbye_replied:
            self._goodbye_end_at = None   # GPT-Live answers the goodbye: the chat ends after this answer

    def _sound_ended(self, now: float) -> None:
        if self._goodbye and not self._goodbye_replied:
            self._goodbye_replied = True
            self._goodbye_end_at = now + GOODBYE_GRACE_SECONDS

    # ── transcripts into turns (C1) ──────────────────────────────────────────────────────────────────────────────

    async def _on_input_text(self, text: str) -> None:
        if not text.strip():
            if self._patient is not None:
                self._patient.text += text
            return
        now = self.clock()
        self._last_patient_at = self._reply_due_at = now
        self._armed = True   # GPT-Live's next sound answers these words: hold its start
        if self._reachy is not None:
            await self._close_reachy()
        elif self._segment is None:
            self._reply_metrics = None   # sound without words (a backchannel) is not the next reply's timing
        if self._patient is not None and now - self._patient.last > TURN_GAP_SECONDS:
            await self._close_patient()   # a pause the timers have not closed yet
        if self._patient is None:
            self._patient = _Group(first=now, last=now)
        self._patient.add(text, now)
        if len(self._patient.text) >= MAX_GROUP_CHARS:
            await self._close_patient()

    async def _on_output_text(self, text: str) -> None:
        if not text.strip():
            if self._reachy is not None:
                self._reachy.text += text
            return
        now = self.clock()
        if self._patient is not None:
            await self._close_patient()
        if self._reachy is None:
            self._reachy = _Group(first=now, last=now)
        self._reachy.add(text, now)
        if len(self._reachy.text) >= MAX_GROUP_CHARS:
            await self._close_reachy()

    async def _close_patient(self) -> int | None:
        """Store the patient's open group as a turn; returns its turn id (None when there was nothing to store)."""
        group, self._patient = self._patient, None
        text = group.text.strip() if group is not None else ""
        if not text:
            return None
        turn_id = await self._add_turn("patient", text, {"live": {
            "transcript_ms": _ms(group.last - group.first), "fragments": group.fragments}})
        self._patient_turns += 1
        if conversation.wants_to_end(text):
            self._goodbye_said()
        return turn_id

    async def _close_reachy(self) -> int | None:
        """Store Reachy's open group as a turn with its reply timings; returns its turn id. The first group before
        the patient said anything is GPT-Live's spoken opening: it replaces the opening turn's text, so the opening
        stays the chat's first Reachy turn (memory.finish_followup reads it so)."""
        group, self._reachy = self._reachy, None
        metrics, self._reply_metrics = self._reply_metrics or {}, None
        if group is None:
            return None
        text = group.text.strip()
        live = {"live": {"first_audio_ms": metrics.get("first_audio_ms"), "hold_ms": metrics.get("hold_ms"),
                         "cut": bool(metrics.get("cut")), "robot_playing_ms": metrics.get("robot_playing_ms")}}
        if self.opening_turn_id is not None and not self._opening_spoken and self._patient_turns == 0:
            self._opening_spoken = True
            async with self._turn_lock, get_pool().acquire() as conn:
                await conn.execute("UPDATE conversation_turn SET text = $2, metrics = $3::jsonb WHERE turn_id = $1",
                                   self.opening_turn_id, text or self.opening, json.dumps(live))
            return self.opening_turn_id
        if not text:
            return None
        return await self._add_turn("reachy", text, live)

    async def _add_turn(self, role: str, text: str, metrics: dict, flagged: bool = False) -> int:
        async with self._turn_lock, get_pool().acquire() as conn:
            return await conn.fetchval(INSERT_TURN_SQL, self.conversation_id, self.u_id, role, text, flagged,
                                       json.dumps(metrics))

    async def _store_groups(self) -> None:
        """Store whatever is still open, oldest group first."""
        groups = sorted((group.first, role) for role, group in (("patient", self._patient), ("reachy", self._reachy))
                        if group is not None)
        for _, role in groups:
            await (self._close_patient() if role == "patient" else self._close_reachy())

    def _goodbye_said(self) -> None:
        """A goodbye in a closed patient turn (conversation.wants_to_end): GPT-Live may answer once, and the chat
        ends GOODBYE_GRACE_SECONDS after that answer's sound stopped (or after the goodbye, when it says nothing)."""
        if self._goodbye:
            return
        self._goodbye = True
        self._goodbye_end_at = None if self._segment is not None else self.clock() + GOODBYE_GRACE_SECONDS

    # ── endings (C2) and the fallback (C3) ───────────────────────────────────────────────────────────────────────

    async def _wrap_up(self) -> None:
        if self.outcome.kind == "fallback":
            await self._fall_back(self.outcome.reason)
        else:
            await self._end_chat(self.outcome.reason, self.outcome.risk)

    async def _end_chat(self, reason: str, risk: bool) -> None:
        """Every ending: the open turns stored, session.close, conversation.live stored, the conversation ended with
        its reason and its post-chat work started, as /conversations/{id}/end does; then `end` to the robot."""
        await self._store_groups()
        usage = await self._close_live()
        live = {"usage_seconds": usage, "close_reason": self._close_reason, "fallback_reason": None}
        async with get_pool().acquire() as conn, conn.transaction():
            ended = await conn.fetchval(
                "UPDATE conversation SET ended_at = NOW(), end_reason = $2, after_chat_state = 'pending', "
                "live = $3::jsonb WHERE conversation_id = $1::uuid AND ended_at IS NULL RETURNING conversation_id",
                self.conversation_id, reason, json.dumps(live))
        if ended is not None:
            _in_background(after_chat.process(self.conversation_id, self.u_id))
        log.info("live chat %s ended: %s (GPT-Live %s s, %s)", self.conversation_id, reason, usage,
                 self._close_reason)
        await self._to_robot({"type": "end", "reason": reason, "risk": risk})
        await self._close_robot(1000)

    async def _fall_back(self, reason: str) -> None:
        """GPT-Live failed after the chat started: the stored turns stay, conversation.mode stays 'live',
        conversation.live records why, and the robot goes on with POST /conversations/{id}/turn. That route counts
        its turn limit from here (live_patient_turns)."""
        await self._store_groups()
        usage = await self._close_live()
        live = {"usage_seconds": usage, "close_reason": self._close_reason, "fallback_reason": reason,
                "live_patient_turns": self._patient_turns}
        async with get_pool().acquire() as conn:
            await conn.execute("UPDATE conversation SET live = $2::jsonb "
                               "WHERE conversation_id = $1::uuid AND ended_at IS NULL",
                               self.conversation_id, json.dumps(live))
        log.warning("live chat %s falls back to the text chat: %s (GPT-Live %s s, %s)", self.conversation_id, reason,
                    usage, self._close_reason)
        await self._to_robot({"type": "fallback", "reason": reason, "conversation_id": self.conversation_id})
        await self._close_robot(1000)

    async def _close_live(self) -> float | None:
        """Close the GPT-Live session once (session.close, waiting for its usage). Returns the usage seconds, from
        this close or from a session.closed GPT-Live sent earlier; None when it never said."""
        if self.client is None or self._live_closed:
            return self._usage
        self._live_closed = True
        try:
            usage = await self.client.close()
        except Exception:
            log.exception("live chat %s: closing GPT-Live failed", self.conversation_id)
            usage = None
        if usage is not None:
            self._close_reason = self._close_reason or "close_requested"
            if self._usage is None:
                self._usage = usage
        return self._usage

    # ── the robot's connection ───────────────────────────────────────────────────────────────────────────────────

    async def _to_robot(self, message: dict) -> None:
        try:
            async with self._send_lock:
                await self.robot.send_json(message)
        except Exception:
            log.info("live chat %s: the robot is gone (%s not sent)", self.conversation_id, message.get("type"))
            self._finish("end", "disconnected")

    async def _robot_bytes(self, pcm: bytes) -> None:
        """One audio chunk to the robot; the caller holds _send_lock."""
        try:
            await self.robot.send_bytes(pcm)
        except Exception:
            log.info("live chat %s: the robot is gone (audio not sent)", self.conversation_id)
            self._finish("end", "disconnected")

    async def _close_robot(self, code: int) -> None:
        await self.robot.close(code)
