"""GPT-Live delegation and tool dispatch for the live relay (reference reachy_gpt_live_agent/agent_bridge.py,
adapted to the server's relay).

The reference runs one GPT-Live session on the robot; here the server's LiveRelay (live_session.py) already owns the
session, so this module gives it two pieces:

- AgentLiveClient, a LiveClient whose session.start carries the reference's `delegation` block (a Responses backend
  with gpt_live_config.TOOLS_SCHEMA) and which runs the delegated tool loop in the background: a `response.event`
  envelope whose nested event is `response.output_item.done` with a `function_call` item is executed through the
  ToolRegistry (one call at a time: medication and safety flows depend on each other), and the result goes back as
  `response.item.create` (a `function_call_output` item) followed by `response.create`. Everything else the client
  does (audio, transcripts, mute, close, the error codes) is LiveClient's, unchanged, so the relay's safety layers
  see the same events as before.
- RelayRobotLink, the ToolRegistry's RobotLink over the live chat's own WebSocket: `camera`, `emote`, `emotion`,
  `dance` and `stop_dance` requests go to the robot as JSON ({"type": "camera", "request_id"}, {"type": "emote",
  "preset", "request_id"}, {"type": "emotion", "request_id"}, {"type": "dance", "dance", "repeat", "request_id"},
  {"type": "stop_dance", "request_id"}), and the robot's `frame` (JPEG as base64), `emote_result`, `emotion_result`
  (its multimodal engine's report) and `dance_result` answers, which the relay's uplink hands to answer(), resolve
  them. The robot app (bridge/live.py with reachy_hardware.py) answers camera, emote and emotion from 0.11.0 and
  the dances from 0.12.0; an older robot ignores the request, and the tool times out (the emotion tool then reports
  the server's face result alone).

Event names: session.start, session.input_audio.append, session.output_audio.delta, session.instructions.append and
session.close are the ones the Phase 2 spike measured (live_client.py). The delegation events (`response.event`,
`response.output_item.done`, `response.item.create`, `response.create`) are the reference agent's, and were measured
against the live service on 13 Oct 2026 (a probe with synthesised speech): session.started echoes the delegation,
`session.delegation.created` announces each delegated response, its `response.event` envelopes carry the Responses
stream (reasoning, function_call_arguments, output_text and completed events besides the function calls), and our
`response.item.create` plus `response.create` are accepted. Those envelopes come by the dozen per reply, so they are
logged at DEBUG; a session whose delegation sends something else still runs as a plain live chat.
"""

import asyncio
import base64
import binascii
import json
import logging
import time
import uuid
from collections import defaultdict

from app.services import gpt_live_config
from app.services.live_client import LiveClient, LiveError
from app.services.tool_registry import FINISH_ON_CLOSE, ServerCareBackend, ToolError, ToolRegistry

log = logging.getLogger(__name__)

CAMERA_SECONDS = 5.0    # the robot's frame must come within this (it captures and encodes one JPEG)
EMOTE_SECONDS = 5.0     # the robot's emote answer (it only queues the gesture)
EMOTION_SECONDS = 4.0   # the robot's emotion report (its engine's last result; no model runs for it)
DANCE_SECONDS = 10.0    # the robot's dance answer (it reads its dance library from disk on the first call, then queues)
MEDICATION_TOOLS = ("check_medication_due", "get_medication_schedule", "verify_prescription", "verify_medication",
                    "start_pill_intake_detection", "get_pill_intake_result", "record_medication_taken")
# The events the Phase 2 spike measured (live_client.py): anything else is logged whole by AgentLiveClient._handle
KNOWN_EVENTS = frozenset(("session.started", "session.output_audio.delta", "session.input_transcript.delta",
                          "session.output_transcript.delta", "session.input_audio.muted", "session.input_audio.unmuted",
                          "session.usage.updated", "session.closed", "session.commentary.appended",
                          "session.instructions.appended"))


class RelayRobotLink:
    """The robot's camera and gestures through the live chat: `send(message)` is the relay's sender to the robot
    (LiveRelay._to_robot); answer(message) resolves the request the robot answered; close() fails what is left."""

    def __init__(self, send):
        self._send = send
        self._pending: dict[str, asyncio.Future] = {}

    async def _ask(self, message: dict, timeout: float) -> dict:
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({**message, "request_id": request_id})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise ToolError("robot_did_not_answer") from None
        finally:
            self._pending.pop(request_id, None)

    async def capture_jpeg(self) -> tuple[bytes, dict]:
        answer = await self._ask({"type": "camera"}, CAMERA_SECONDS)
        if answer.get("error"):
            raise ToolError(f"camera_failed: {str(answer['error'])[:60]}")
        try:
            jpeg = base64.b64decode(answer.get("jpeg") or "", validate=True)
        except (binascii.Error, ValueError):
            raise ToolError("camera_frame_unreadable") from None
        if not jpeg:
            raise ToolError("camera_frame_empty")
        return jpeg, {"width": answer.get("width"), "height": answer.get("height"),
                      "captured_at": answer.get("captured_at")}

    async def emote(self, preset: str) -> dict:
        answer = await self._ask({"type": "emote", "preset": preset}, EMOTE_SECONDS)
        if not answer.get("ok"):
            raise ToolError(f"emote_failed: {str(answer.get('error') or 'unknown')[:60]}")
        result = answer.get("result")
        return result if isinstance(result, dict) else {"status": "queued", "preset": preset}

    async def emotion(self) -> dict | None:
        """The robot's multimodal emotion report ({"type": "emotion"} -> emotion_result: multimodal/engine.py
        report() plus `available`); None when the robot has no engine or answered an error."""
        answer = await self._ask({"type": "emotion"}, EMOTION_SECONDS)
        if not answer.get("available") and answer.get("error"):
            log.info("live agent: the robot has no emotion report (%s)", str(answer["error"])[:60])
            return None
        return {key: value for key, value in answer.items() if key not in ("type", "request_id")}

    async def dance(self, name: str | None, repeat: int) -> dict:
        """Start a recorded dance on the robot ({"type": "dance"} -> dance_result): its answer as the move starts
        ({"status": "started", "dance", "repeat", "available", ...}); ToolError with the robot's reason (an unknown
        move with the names it has, no motion, gestures off) when it refused."""
        answer = await self._ask({"type": "dance", "dance": name, "repeat": int(repeat)}, DANCE_SECONDS)
        if not answer.get("ok"):
            raise ToolError(f"dance_failed: {str(answer.get('error') or 'unknown')[:160]}")
        result = answer.get("result")
        return result if isinstance(result, dict) else {"status": "started", "dance": name, "repeat": repeat}

    async def stop_dance(self) -> dict:
        """Stop the robot's dance ({"type": "stop_dance"} -> dance_result): {"status": "cancelled", "dance"} or
        {"status": "idle"}."""
        answer = await self._ask({"type": "stop_dance"}, EMOTE_SECONDS)
        if not answer.get("ok"):
            raise ToolError(f"stop_dance_failed: {str(answer.get('error') or 'unknown')[:160]}")
        result = answer.get("result")
        return result if isinstance(result, dict) else {"status": "idle"}

    async def express(self, move: str, posture: str | None) -> dict:
        """Play a recorded emotion move on the robot, with a posture to hold ({"type": "express", "move", "affect",
        "request_id"} -> express_result): the answer as the move starts; ToolError with the robot's reason."""
        message = {"type": "express", "move": move, "reason": "tool"}
        if posture is not None:
            message["affect"] = posture
        answer = await self._ask(message, DANCE_SECONDS)
        if not answer.get("ok"):
            raise ToolError(f"play_emotion_failed: {str(answer.get('error') or 'unknown')[:160]}")
        result = answer.get("result")
        return result if isinstance(result, dict) else {"status": "started", "move": move}

    def answer(self, message: dict) -> bool:
        """The robot's `frame`, `emote_result`, `emotion_result`, `dance_result` or `express_result`; True when a
        request was waiting for it."""
        future = self._pending.get(str(message.get("request_id") or ""))
        if future is None or future.done():
            return False
        future.set_result(message)
        return True

    def close(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ToolError("robot_gone"))
        self._pending.clear()


def make_registry(relay) -> ToolRegistry:
    """The relay's tool registry: the robot link over its WebSocket and the care backend for its patient and
    conversation (the conversation id is set once the row exists; the relay keeps it in step)."""
    backend = ServerCareBackend(relay.u_id, relay.conversation_id, relay.task_id, relay.language)
    return ToolRegistry(RelayRobotLink(relay._to_robot), backend)


class AgentLiveClient(LiveClient):
    """A LiveClient with the Responses delegation and the tool loop. `registry` runs the tools; `language` picks the
    celebration line; `backend_model` is the delegated Responses model."""

    def __init__(self, api_key: str, model: str, voice: str, instructions: str, *, rate: int = 16000,
                 connect=None, registry: ToolRegistry, language: str = "zh-TW",
                 backend_model: str = gpt_live_config.BACKEND_MODEL):
        super().__init__(api_key, model, voice, instructions, rate=rate, connect=connect)
        self.registry = registry
        self.language = language
        self.backend_model = backend_model
        self._tool_tasks: set[asyncio.Task] = set()
        self._finishing: set[asyncio.Task] = set()            # FINISH_ON_CLOSE calls: close() lets them finish
        self._tool_lock = asyncio.Lock()
        self._results: dict[str, dict] = {}                  # call_id -> result (a repeated call is not run twice)
        self._history: dict[str, list[str]] = defaultdict(list)   # delegation_id -> tool names, in order
        self._celebrated: set[str] = set()
        self.calls: list[dict] = []                          # {name, ok, ms} per call, for the chat's log
        self.requested: list[str] = []                       # the tools GPT-Live asked for, as they arrived
        self.delegation_rejected = False                     # the service refused one of our tool events (_handle)

    def _session_start(self) -> dict:
        event = super()._session_start()
        event["session"]["delegation"] = gpt_live_config.delegation(backend_model=self.backend_model)
        return event

    def _handle(self, event: dict) -> bool:
        kind = event.get("type")
        if kind not in KNOWN_EVENTS and log.isEnabledFor(logging.DEBUG):
            # The delegation's stream (dozens of envelopes per reply) and anything else outside LiveClient's
            # measured set, whole, for a debugging session; the function calls themselves are logged by _run_call
            log.debug("GPT-Live event %s: %s", kind, json.dumps(event, ensure_ascii=False)[:600])
        if kind == "response.event":
            self._on_delegated(event)
            return False
        if kind == "error":
            # An error naming one of our tool events (tool-result-N, tool-continue-N, celebrate-N): the service
            # refused the delegation protocol as sent. That is this layer's failure, never the chat's: it is logged,
            # no more tool events go out, and the live chat goes on without tools (the relay never sees it)
            error = event.get("error") if isinstance(event.get("error"), dict) else {}
            named = str(error.get("client_event_id") or event.get("client_event_id") or "")
            if named.startswith(("tool-", "instructions-")):   # instructions-N: the celebration note
                if not self.delegation_rejected:
                    log.warning("GPT-Live refused the agent's tool event %s: %s %s; the chat goes on without "
                                "tools", named, error.get("code"), str(error.get("message") or "")[:160])
                self.delegation_rejected = True
                return False
        return super()._handle(event)

    @staticmethod
    def _preset(arguments) -> str | None:
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
        except ValueError:
            return None
        return args.get("preset") if isinstance(args, dict) else None

    def _on_delegated(self, envelope: dict) -> None:
        if self._closing or self._ended.is_set():
            return   # the session is closing: a call now would run after it (a note to family nobody records)
        nested = envelope.get("event") if isinstance(envelope.get("event"), dict) else {}
        if nested.get("type") != "response.output_item.done":
            return   # reasoning, argument and text deltas, completed: the Responses stream, nothing to run
        item = nested.get("item") if isinstance(nested.get("item"), dict) else {}
        if item.get("type") != "function_call":
            return
        call_id, name = item.get("call_id"), item.get("name")
        if not isinstance(call_id, str) or not isinstance(name, str) or not call_id or not name:
            log.warning("GPT-Live sent a function_call without a call_id or a name; ignored")
            return
        self.requested.append(name)   # the relay's backstop (live_session._intent_due) reads it at once
        task = asyncio.create_task(self._run_call(str(envelope.get("delegation_id") or ""), call_id, name,
                                                  item.get("arguments")), name=f"live-tool-{name}")
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)
        if name in FINISH_ON_CLOSE:   # a record, a note to family or an alert: closing the session never cuts it short
            self._finishing.add(task)
            task.add_done_callback(self._finishing.discard)
            self.registry.hold(task)

    async def _run_call(self, delegation_id: str, call_id: str, name: str, arguments) -> None:
        """One delegated call: run it (serialised), then its output and `response.create` to GPT-Live. A session that
        closed meanwhile (LiveError) ends the loop quietly; the relay handles the close itself."""
        async with self._tool_lock:
            result = self._results.get(call_id)
            if result is None:
                began = time.monotonic()
                result = await self._execute(name, arguments)
                self._results[call_id] = result
                self.calls.append({"name": name, "ok": bool(result.get("ok")),
                                   "ms": int((time.monotonic() - began) * 1000)})
                log.info("live agent tool %s: %s in %d ms", name, "ok" if result.get("ok") else result.get("error"),
                         self.calls[-1]["ms"])
            self._history[delegation_id].append(name)
            if self.delegation_rejected:
                return   # the service refused our tool events (_handle): nothing more goes out for this call
            try:
                if (name == "trigger_antenna_emote" and result.get("ok") and self._preset(arguments) == "applaud_cheer"
                        and self._medication_context(delegation_id) and delegation_id not in self._celebrated):
                    self._celebrated.add(delegation_id)
                    await self.instructions_append(self._celebration_note())
                output = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
                self._sequence += 1
                await self._send({"type": "response.item.create", "event_id": f"tool-result-{self._sequence}",
                                  "item": {"type": "function_call_output", "call_id": call_id, "output": output}})
                self._sequence += 1
                await self._send({"type": "response.create", "event_id": f"tool-continue-{self._sequence}"})
            except LiveError:
                log.info("live agent tool %s: the session closed before its result went out", name)

    async def _execute(self, name: str, arguments) -> dict:
        try:
            args = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else (arguments or {})
        except ValueError as error:
            return {"ok": False, "tool": name, "error": f"invalid_json_arguments: {type(error).__name__}"}
        if not isinstance(args, dict):
            return {"ok": False, "tool": name, "error": "arguments_must_be_object"}
        return await self.registry.execute(name, args)

    def _medication_context(self, delegation_id: str) -> bool:
        return any(tool in MEDICATION_TOOLS for tool in self._history.get(delegation_id, ()))

    def _celebration_note(self) -> str:
        line = gpt_live_config.CELEBRATION.get(self.language, gpt_live_config.CELEBRATION["en"])
        if self.language == "zh-TW":
            return f"拍手動作已經開始。現在請用中文說一次，一字不差：{line} 然後繼續聽，後端會完成剩下的步驟。"
        return (f"The applause gesture has just started. Say exactly once: {line} Then keep listening while the "
                f"backend finishes.")

    async def close(self, wait: float = 2.0) -> float | None:
        """Close the session. The tool calls still running are cancelled, except a record, a note to family or an
        alert (FINISH_ON_CLOSE, also one still waiting its turn): it runs to its end within its tool_registry.TIMEOUTS
        entry, and its result, with no session to go to, is dropped. The registry holds it (ToolRegistry.hold)."""
        await self._cancel_tool_tasks()
        usage = await super().close(wait)
        await self._cancel_tool_tasks()   # a call that slipped in while the close waited (_on_delegated guards too)
        return usage

    async def _cancel_tool_tasks(self) -> None:
        tasks = [task for task in self._tool_tasks if task not in self._finishing]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
