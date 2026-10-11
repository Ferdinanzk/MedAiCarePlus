"""GPT-Live's Live WebSocket, for live voice check-ins (spec 2026-10-12-live-voice-2a-design.md, A1).

A port of the TCU companion's reachy-mini-tcu/companion/ai/openai_live.py without its Responses backend, tools and
web search. The server opens the session with the OpenAI key from the AI connections card (the robot never sees it),
sends the robot's microphone audio, and hands GPT-Live's events to the relay (live_session.py) as LiveEvents.

Event names and payloads as measured in the Phase 2 spike (11 Oct 2026, 51 sessions):
- session.started {session: {id, expires_at}} answers session.start, about 1.2 s after connecting;
- session.output_audio.delta {delta: base64 PCM16} every 100 ms, the first ones silent, at the session's rate;
- session.input_transcript.delta / session.output_transcript.delta {delta, start_ms, end_ms}; no "done" event;
- session.input_audio.muted / unmuted {client_event_id} acknowledge mute and unmute (measured 0.25 s);
- error {error: {type, code, message, param, client_event_id?}}, code content_filter when moderation cut a reply;
- session.usage.updated {usage: {seconds}} on the way, and session.closed {reason, usage: {seconds}} at the end,
  reason close_requested, expired, content, remote_hangup or connection_lost.
GPT-Live has no event that stops its speech: the relay stops a reply by not forwarding its audio.
The key goes only into the Authorization header: never into a log, an exception or a LiveEvent.
"""

import asyncio
import base64
import binascii
import json
import logging
import time
from dataclasses import dataclass

from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from app.services.ai_crypto import redact

log = logging.getLogger(__name__)

LIVE_URL = "wss://api.openai.com/v1/live/sessions"
CODES = ("live_auth", "live_unavailable", "rate_limit_exceeded", "content_filter", "live_closed")
ACK_SECONDS = 5.0          # longest wait for a mute or unmute acknowledgement
SOCKET_CLOSE_SECONDS = 1.0  # websockets' close_timeout: it drops the connection itself after this
CHECK_INSTRUCTIONS = "This is a connection check. Stay silent."
_AUTH_CODES = ("invalid_api_key", "authentication_error", "permission_denied")
_RATE_CODES = ("rate_limit_exceeded", "insufficient_quota")


class LiveError(Exception):
    """A GPT-Live failure as a machine code (one of CODES). Its text is the code alone: never OpenAI's message."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class LiveEvent:
    """One event for the relay.
    - "started": the session is open (text: its id). Always the first event.
    - "audio": GPT-Live's voice (data: PCM16 mono at the session's rate).
    - "input_text" / "output_text": a transcript fragment of the patient / of GPT-Live's speech (text).
    - "error": code is one of CODES; text is OpenAI's own code, for logs. The session may go on after it.
    - "closed": reason is OpenAI's close reason, "connection_lost" when the socket dropped without one, or
      "close_requested" when close() gave up waiting; usage_seconds is the billed time when known. Always the last."""

    type: str
    data: bytes = b""
    text: str = ""
    code: str = ""
    reason: str = ""
    usage_seconds: float | None = None


def _status_code(status: int) -> str:
    """The code for an HTTP status that refused the WebSocket upgrade."""
    if status in (401, 403):
        return "live_auth"
    if status == 429:
        return "rate_limit_exceeded"
    return "live_unavailable"


def _upstream_code(event: dict) -> str:
    error = event.get("error") if isinstance(event.get("error"), dict) else {}
    return str(error.get("code") or event.get("code") or error.get("type") or "")


def _error_code(event: dict) -> str:
    """The code for an `error` event: OpenAI's own code mapped onto CODES."""
    upstream = _upstream_code(event)
    if upstream == "content_filter":
        return "content_filter"
    if upstream in _AUTH_CODES:
        return "live_auth"
    if upstream in _RATE_CODES:
        return "rate_limit_exceeded"
    return "live_unavailable"


def _parse(raw) -> dict:
    try:
        event = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return event if isinstance(event, dict) else {}


def _seconds(event: dict) -> float | None:
    usage = event.get("usage")
    seconds = usage.get("seconds") if isinstance(usage, dict) else None
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    return float(seconds)


def _audio(delta) -> bytes | None:
    """The PCM16 bytes of an output audio delta, or None when it is not base64 of whole 16-bit samples."""
    if not isinstance(delta, str):
        return None
    try:
        pcm = base64.b64decode(delta, validate=True)
    except (binascii.Error, ValueError):
        return None
    return pcm if len(pcm) % 2 == 0 else None


class LiveClient:
    """One GPT-Live session. start() connects and opens it; events() yields what GPT-Live sends (one reader only);
    send_audio, mute, unmute, commentary and instructions_append talk to it; close() ends it and returns the billed
    seconds. A background task reads the socket from start() on, so mute() gets its acknowledgement even while
    nobody is reading events(). connect(url, **options) -> socket is websockets' client connect; tests pass a fake."""

    def __init__(self, api_key: str, model: str, voice: str, instructions: str, *, rate: int = 16000,
                 connect=None):
        self._api_key = api_key
        self.model, self.voice, self.instructions, self.rate = model, voice, instructions, rate
        self._connect = connect or websocket_connect
        self._socket = None
        self._reader: asyncio.Task | None = None
        self._queue: asyncio.Queue[LiveEvent] = asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self._acks: dict[str, asyncio.Future] = {}
        self._sequence = 0
        self._ended = asyncio.Event()   # the "closed" event is queued: nothing more is sent
        self._closing = False
        self._delivered = False         # events() has handed out the "closed" event
        self.session_id = ""
        self.muted = False
        self.close_reason = ""
        self.usage_seconds: float | None = None

    def __repr__(self) -> str:
        return f"LiveClient(model={self.model!r}, voice={self.voice!r}, rate={self.rate})"

    def _options(self, timeout: float) -> dict:
        # As the TCU companion connects (openai_live.py:71-75), with a shorter close_timeout so a failed start
        # stays near its budget. The key is only ever in this header.
        return {"additional_headers": {"Authorization": "Bearer " + self._api_key}, "open_timeout": timeout,
                "ping_interval": 20, "ping_timeout": 20, "close_timeout": SOCKET_CLOSE_SECONDS,
                "max_size": 2 ** 22, "compression": None, "proxy": None}

    def _session_start(self) -> dict:
        return {"type": "session.start", "session": {
            "model": self.model, "store": False, "instructions": self.instructions,
            "audio": {"format": {"type": "audio/pcm", "rate": self.rate}, "output": {"voice": self.voice}}}}

    async def start(self, timeout: float = 8.0) -> None:
        """Connect and open the session; returns once GPT-Live sent session.started. Raises LiveError, with the
        socket closed: live_auth (key refused), rate_limit_exceeded, content_filter, live_closed (the session closed
        before it started), live_unavailable (anything else: no answer within `timeout` s, network, HTTP 5xx)."""
        try:
            async with asyncio.timeout(timeout):
                self._socket = await self._connect(LIVE_URL, **self._options(timeout))
                await self._socket.send(json.dumps(self._session_start(), ensure_ascii=False))
                while True:
                    event = _parse(await self._socket.recv())
                    kind = event.get("type")
                    if kind == "session.started":
                        session = event.get("session") if isinstance(event.get("session"), dict) else {}
                        self.session_id = str(session.get("id") or "")
                        break
                    if kind == "error":
                        raise LiveError(_error_code(event))
                    if kind == "session.closed":
                        self.close_reason = str(event.get("reason") or "")
                        self.usage_seconds = _seconds(event)
                        raise LiveError("live_closed")
        except LiveError as error:
            await self._drop_socket()
            log.warning("GPT-Live did not start: %s", error.code)
            raise
        except InvalidStatus as error:
            await self._drop_socket()
            code = _status_code(error.response.status_code)
            log.warning("GPT-Live refused the connection: HTTP %s (%s)", error.response.status_code, code)
            raise LiveError(code) from None
        except (OSError, TimeoutError, WebSocketException) as error:
            await self._drop_socket()
            log.warning("GPT-Live did not start: live_unavailable (%s)", type(error).__name__)
            raise LiveError("live_unavailable") from None
        except BaseException:
            await self._drop_socket()
            raise
        self._queue.put_nowait(LiveEvent("started", text=self.session_id))
        self._reader = asyncio.create_task(self._read(self._socket))

    async def _drop_socket(self) -> None:
        socket, self._socket = self._socket, None
        if socket is None:
            return
        try:
            await socket.close()
        except (OSError, WebSocketException):
            pass

    async def _send(self, event: dict) -> None:
        if self._socket is None or self._ended.is_set() or self._closing:
            raise LiveError("live_closed")
        async with self._send_lock:
            try:
                await self._socket.send(json.dumps(event, ensure_ascii=False))
            except (ConnectionClosed, OSError):
                raise LiveError("live_closed") from None

    async def send_audio(self, pcm: bytes) -> None:
        """Append microphone audio: PCM16 mono at the session's rate, sent as base64. Raises LiveError live_closed
        once the session or the socket has closed (so do mute, unmute, commentary and instructions_append)."""
        await self._send({"type": "session.input_audio.append", "audio": base64.b64encode(pcm).decode("ascii")})

    async def mute(self) -> None:
        """Mute GPT-Live's input and wait for session.input_audio.muted. Muting does not stop GPT-Live's speech, and
        muted time with audio still appended is billed (measured): stop sending audio too."""
        await self._set_muted(True)

    async def unmute(self) -> None:
        """Unmute GPT-Live's input and wait for session.input_audio.unmuted."""
        await self._set_muted(False)

    async def _set_muted(self, muted: bool) -> None:
        if muted == self.muted:
            return
        self._sequence += 1
        action = "mute" if muted else "unmute"
        event_id = f"{action}-{self._sequence}"
        future = asyncio.get_running_loop().create_future()
        self._acks[event_id] = future
        try:
            await self._send({"type": "session.input_audio." + action, "event_id": event_id})
            await asyncio.wait_for(future, ACK_SECONDS)
        except TimeoutError:
            raise LiveError("live_unavailable") from None
        finally:
            self._acks.pop(event_id, None)
        self.muted = muted

    async def commentary(self, text: str) -> None:
        """Ask GPT-Live to say `text` in its own voice. It paraphrases and may drop a sentence (measured), so never
        use this for the help line. Acknowledged only once spoken, so this does not wait."""
        self._sequence += 1
        await self._send({"type": "session.commentary.append", "event_id": f"commentary-{self._sequence}",
                          "delegation_id": None, "content": text})

    async def instructions_append(self, text: str) -> None:
        """Add an instruction for the rest of the session (at most 500 tokens). Acknowledged by
        session.instructions.appended, which this does not wait for."""
        self._sequence += 1
        await self._send({"type": "session.instructions.append", "event_id": f"instructions-{self._sequence}",
                          "delegation_id": None, "content": text})

    async def events(self):
        """GPT-Live's events in order, "started" first and "closed" last. Ends at once when the session never
        started or its "closed" event was already handed out."""
        if self._delivered or (self._reader is None and self._queue.empty()):
            return
        while True:
            event = await self._queue.get()
            yield event
            if event.type == "closed":
                self._delivered = True
                return

    async def _read(self, socket) -> None:
        try:
            async for raw in socket:
                if self._handle(_parse(raw)):
                    return
        except ConnectionClosed:
            pass
        finally:
            self._finish("connection_lost")

    def _handle(self, event: dict) -> bool:
        """Turn one GPT-Live event into a LiveEvent or an acknowledgement; True once the session has closed."""
        kind = event.get("type")
        if kind == "session.output_audio.delta":
            pcm = _audio(event.get("delta"))
            if pcm is None:
                log.warning("GPT-Live sent an output audio delta that is not PCM16; dropped")
            elif pcm:
                self._queue.put_nowait(LiveEvent("audio", data=pcm))
        elif kind in ("session.input_transcript.delta", "session.output_transcript.delta"):
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                role = "input_text" if kind == "session.input_transcript.delta" else "output_text"
                self._queue.put_nowait(LiveEvent(role, text=delta))
        elif kind in ("session.input_audio.muted", "session.input_audio.unmuted"):
            future = self._acks.get(str(event.get("client_event_id") or ""))
            if future is not None and not future.done():
                future.set_result(True)
        elif kind == "error":
            code = _error_code(event)
            upstream = redact(_upstream_code(event), self._api_key)[:80]
            error = event.get("error") if isinstance(event.get("error"), dict) else {}
            future = self._acks.get(str(error.get("client_event_id") or event.get("client_event_id") or ""))
            if future is not None and not future.done():
                future.set_exception(LiveError(code))
            log.warning("GPT-Live error: %s (%s)", code, upstream)
            self._queue.put_nowait(LiveEvent("error", code=code, text=upstream))
        elif kind == "session.usage.updated":
            seconds = _seconds(event)
            if seconds is not None:
                self.usage_seconds = seconds
        elif kind == "session.closed":
            self.close_reason = str(event.get("reason") or "")
            seconds = _seconds(event)
            if seconds is not None:
                self.usage_seconds = seconds
            log.info("GPT-Live session closed: %s, %s s billed", self.close_reason, self.usage_seconds)
            self._finish(self.close_reason)
            return True
        return False

    def _finish(self, reason: str) -> None:
        """Queue the one "closed" event (the first call wins) and fail any mute still waiting."""
        if self._ended.is_set():
            return
        self._ended.set()
        self._queue.put_nowait(LiveEvent("closed", reason=reason, usage_seconds=self.usage_seconds))
        for future in self._acks.values():
            if not future.done():
                future.set_exception(LiveError("live_closed"))

    async def close(self, wait: float = 2.0) -> float | None:
        """End the session: send session.close, wait up to `wait` s for session.closed (it carries the billed
        seconds), then close the socket. Never raises; a second call returns at once. Returns the billed seconds:
        from session.closed, else the last session.usage.updated, else None."""
        socket = self._socket
        if socket is None or self._closing:
            return self.usage_seconds
        self._closing = True
        if not self._ended.is_set():
            try:
                async with self._send_lock:
                    await socket.send(json.dumps({"type": "session.close"}))
                await asyncio.wait_for(self._ended.wait(), wait)
            except (TimeoutError, OSError, WebSocketException):
                pass
        self._finish("close_requested")
        reader, self._reader = self._reader, None
        if reader is not None and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await self._drop_socket()
        return self.usage_seconds


async def check(api_key: str, model: str, voice: str, *, connect=None) -> tuple[bool, int | None, str | None]:
    """The AI card's Check: open a session, wait for session.started, close it; no audio is sent.
    (True, milliseconds to session.started, None), or (False, None, the LiveError code)."""
    client = LiveClient(api_key, model, voice, CHECK_INSTRUCTIONS, connect=connect)
    began = time.monotonic()
    try:
        await client.start()
    except LiveError as error:
        return False, None, error.code
    ms = round((time.monotonic() - began) * 1000)
    await client.close()
    return True, ms, None
