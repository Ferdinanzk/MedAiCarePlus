"""The GPT-Live agent layer's bridge (services/agent_bridge.py): the delegation block in session.start, the tool
loop (a delegated function_call runs through the registry and its output goes back, then response.create), the
celebration note after the applause gesture of a medication workflow, and the robot link's request and answer
messages. In-memory GPT-Live (tests/test_live_client.py's FakeSocket); no network, no database."""

import asyncio
import base64
import json
import sys
import types

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.services import agent_bridge, gpt_live_config   # noqa: E402
from app.services.agent_bridge import AgentLiveClient, RelayRobotLink   # noqa: E402
from app.services.tool_registry import ToolError   # noqa: E402
from tests.test_live_client import KEY, FakeSocket, connector, starts_only   # noqa: E402


class Registry:
    """A tool registry that answers what the test scripted and records the calls."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    async def execute(self, name, args):
        self.calls.append((name, args))
        return self.answers.get(name, {"ok": True, "tool": name, "result": {"status": "fine"}})


def call_event(name, arguments="{}", call_id="call_1", delegation_id="del_1"):
    return {"type": "response.event", "delegation_id": delegation_id,
            "event": {"type": "response.output_item.done",
                      "item": {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}}}


async def settle(socket, kind, count=1, limit=1.0):
    async with asyncio.timeout(limit):
        while socket.sent_types().count(kind) < count:
            await asyncio.sleep(0)


def client(socket, registry=None, language="zh-TW"):
    return AgentLiveClient(KEY, "gpt-live-1", "marin", "live rules", connect=connector(socket),
                           registry=registry or Registry(), language=language, backend_model="backend-x")


def test_session_start_carries_the_delegation_with_the_tools():
    async def run():
        socket = FakeSocket(starts_only)
        c = client(socket)
        await c.start()
        await c.close()
        return socket.sent[0]

    start = asyncio.run(run())
    assert start["type"] == "session.start" and start["session"]["instructions"] == "live rules"
    delegation = start["session"]["delegation"]
    assert delegation["type"] == "responses" and delegation["responses"]["model"] == "backend-x"
    assert delegation["responses"]["parallel_tool_calls"] is False
    assert [tool["name"] for tool in delegation["responses"]["tools"]] == list(gpt_live_config.TOOL_NAMES)
    assert len(gpt_live_config.TOOLS_SCHEMA) == 25
    assert json.dumps(start, ensure_ascii=False)   # serialisable, as the socket needs it


def test_a_delegated_function_call_runs_the_tool_and_its_output_goes_back():
    async def run():
        socket = FakeSocket(starts_only)
        registry = Registry({"check_medication_due": {"ok": True, "tool": "check_medication_due",
                                                      "result": {"status": "taken", "medication_id": "dose-2"}}})
        c = client(socket, registry)
        await c.start()
        socket.push(call_event("check_medication_due", "{}"))
        await settle(socket, "response.create")
        await c.close()
        return socket.sent, registry.calls, c.calls

    sent, calls, log = asyncio.run(run())
    assert calls == [("check_medication_due", {})]
    output = next(e for e in sent if e["type"] == "response.item.create")
    assert output["item"]["type"] == "function_call_output" and output["item"]["call_id"] == "call_1"
    assert json.loads(output["item"]["output"]) == {"ok": True, "tool": "check_medication_due",
                                                   "result": {"status": "taken", "medication_id": "dose-2"}}
    kinds = [e["type"] for e in sent]
    assert kinds.index("response.item.create") < kinds.index("response.create")
    assert log[0]["name"] == "check_medication_due" and log[0]["ok"] is True


def test_bad_arguments_and_a_repeated_call_id_never_run_the_tool_twice():
    async def run():
        socket = FakeSocket(starts_only)
        registry = Registry()
        c = client(socket, registry)
        await c.start()
        socket.push(call_event("remember", "{not json", call_id="c1"))
        socket.push(call_event("remember", '["list"]', call_id="c2"))
        socket.push(call_event("get_patient_memory", '{"query": "tea"}', call_id="c3"))
        socket.push(call_event("get_patient_memory", '{"query": "tea"}', call_id="c3"))
        await settle(socket, "response.create", 4)
        await c.close()
        return socket.sent, registry.calls

    sent, calls = asyncio.run(run())
    assert calls == [("get_patient_memory", {"query": "tea"})]
    outputs = {e["item"]["call_id"]: json.loads(e["item"]["output"]) for e in sent
               if e["type"] == "response.item.create"}
    assert outputs["c1"]["error"].startswith("invalid_json_arguments")
    assert outputs["c2"]["error"] == "arguments_must_be_object"
    assert outputs["c3"]["ok"] is True


def test_the_applause_of_a_medication_workflow_appends_the_celebration_once():
    async def run():
        socket = FakeSocket(starts_only)
        c = client(socket, Registry())
        await c.start()
        socket.push(call_event("trigger_antenna_emote", '{"preset": "applaud_cheer"}', call_id="a0", delegation_id="d0"))
        socket.push(call_event("check_medication_due", "{}", call_id="a1", delegation_id="d1"))
        socket.push(call_event("trigger_antenna_emote", '{"preset": "applaud_cheer"}', call_id="a2", delegation_id="d1"))
        socket.push(call_event("trigger_antenna_emote", '{"preset": "applaud_cheer"}', call_id="a3", delegation_id="d1"))
        await settle(socket, "response.create", 4)
        await c.close()
        return socket.sent

    sent = asyncio.run(run())
    notes = [e for e in sent if e["type"] == "session.instructions.append"]
    assert len(notes) == 1 and gpt_live_config.CELEBRATION["zh-TW"] in notes[0]["content"]
    assert notes[0]["delegation_id"] is None
    kinds = [e["type"] for e in sent]
    # the note goes out before the applause call's output, so GPT-Live speaks as the gesture starts
    assert kinds.index("session.instructions.append") < [i for i, e in enumerate(sent)
                                                         if e["type"] == "response.item.create"
                                                         and e["item"]["call_id"] == "a2"][0]


def test_other_delegation_events_and_a_malformed_call_are_ignored():
    async def run():
        socket = FakeSocket(starts_only)
        registry = Registry()
        c = client(socket, registry)
        await c.start()
        socket.push({"type": "response.event", "delegation_id": "d", "event": {"type": "response.created"}})
        socket.push({"type": "response.event", "delegation_id": "d",
                     "event": {"type": "response.output_item.done", "item": {"type": "message"}}})
        socket.push({"type": "response.event", "delegation_id": "d",
                     "event": {"type": "response.output_item.done", "item": {"type": "function_call", "name": "x"}}})
        socket.push({"type": "session.delegation.created", "delegation": {"id": "item_1", "target": "client"}})
        await asyncio.sleep(0.05)
        await c.close()
        return socket.sent_types(), registry.calls

    kinds, calls = asyncio.run(run())
    assert calls == [] and "response.item.create" not in kinds and "response.create" not in kinds


def test_the_tool_loop_stops_quietly_when_the_session_closed():
    async def run():
        socket = FakeSocket(starts_only)
        c = client(socket, Registry())
        await c.start()
        socket.push(call_event("get_medication_schedule"))
        socket.push({"type": "session.closed", "reason": "remote_hangup", "usage": {"seconds": 3.0}})
        await asyncio.sleep(0.05)
        await c.close()
        return socket.sent_types()

    kinds = asyncio.run(run())
    assert "session.start" in kinds and kinds.count("session.close") == 0   # closed by OpenAI: nothing more is sent


# ── the robot link ──

def test_the_robot_link_asks_for_a_frame_and_decodes_the_robots_answer():
    async def run():
        sent = []

        async def send(message):
            sent.append(message)
            link.answer({"type": "frame", "request_id": message["request_id"],
                         "jpeg": base64.b64encode(b"\xff\xd8jpeg").decode("ascii"), "width": 640, "height": 480,
                         "captured_at": 12.5})

        link = RelayRobotLink(send)
        jpeg, meta = await link.capture_jpeg()
        return sent, jpeg, meta

    sent, jpeg, meta = asyncio.run(run())
    assert sent[0]["type"] == "camera" and sent[0]["request_id"]
    assert jpeg == b"\xff\xd8jpeg" and meta == {"width": 640, "height": 480, "captured_at": 12.5}


def test_the_robot_link_reports_errors_timeouts_and_a_gone_robot(monkeypatch):
    monkeypatch.setattr(agent_bridge, "CAMERA_SECONDS", 0.05)
    monkeypatch.setattr(agent_bridge, "EMOTE_SECONDS", 0.05)

    async def run():
        async def errors(message):
            if message["type"] == "camera":
                link.answer({"type": "frame", "request_id": message["request_id"], "error": "no_camera_frame"})
            else:
                link.answer({"type": "emote_result", "request_id": message["request_id"], "ok": False,
                             "error": "gestures_off"})

        link = RelayRobotLink(errors)
        with pytest.raises(ToolError, match="camera_failed: no_camera_frame"):
            await link.capture_jpeg()
        with pytest.raises(ToolError, match="emote_failed: gestures_off"):
            await link.emote("curious_tilt")

        async def silent(message):
            pass

        link = RelayRobotLink(silent)
        with pytest.raises(ToolError, match="robot_did_not_answer"):
            await link.emote("curious_tilt")
        assert link.answer({"type": "frame", "request_id": "unknown"}) is False

        async def never(message):
            asyncio.get_running_loop().call_soon(link.close)

        link = RelayRobotLink(never)
        with pytest.raises(ToolError, match="robot_gone"):
            await link.capture_jpeg()

    asyncio.run(run())


def test_the_robot_link_asks_for_the_robots_emotion_report():
    async def run():
        async def send(message):
            link.answer({"type": "emotion_result", "request_id": message["request_id"], "available": True,
                         "fused": {"top": "happy", "confidence": 0.8}, "ser": {"status": "silence"}})

        link = RelayRobotLink(send)
        report = await link.emotion()

        async def none(message):
            link.answer({"type": "emotion_result", "request_id": message["request_id"], "available": False,
                         "error": "no_emotion_engine"})

        link = RelayRobotLink(none)
        return report, await link.emotion()

    report, missing = asyncio.run(run())
    assert report == {"available": True, "fused": {"top": "happy", "confidence": 0.8}, "ser": {"status": "silence"}}
    assert missing is None


def test_the_robot_link_returns_the_queued_emote():
    async def run():
        async def send(message):
            link.answer({"type": "emote_result", "request_id": message["request_id"], "ok": True,
                         "result": {"status": "queued", "preset": "applaud_cheer", "pending_motion_jobs": 1}})

        link = RelayRobotLink(send)
        return await link.emote("applaud_cheer")

    assert asyncio.run(run()) == {"status": "queued", "preset": "applaud_cheer", "pending_motion_jobs": 1}


# ── the review's fixes ──

def test_only_the_applause_gesture_earns_the_celebration_note():
    async def run():
        socket = FakeSocket(starts_only)
        c = client(socket, Registry())
        await c.start()
        socket.push(call_event("check_medication_due", "{}", call_id="b1", delegation_id="d1"))
        socket.push(call_event("trigger_antenna_emote", '{"preset": "sympathy_droop_warm"}', call_id="b2",
                               delegation_id="d1"))
        socket.push(call_event("trigger_antenna_emote", '{"preset": "applaud_cheer"}', call_id="b3", delegation_id="d1"))
        await settle(socket, "response.create", 3)
        await c.close()
        return socket.sent

    sent = asyncio.run(run())
    notes = [e for e in sent if e["type"] == "session.instructions.append"]
    assert len(notes) == 1   # the sympathy gesture earned none; the applause after it did
    kinds = [(e["type"], e.get("item", {}).get("call_id")) for e in sent]
    assert kinds.index(("session.instructions.append", None)) > kinds.index(("response.item.create", "b2"))


def test_a_call_that_arrives_while_the_session_closes_is_not_run():
    async def run():
        socket = FakeSocket(starts_only)   # never answers session.close: close() waits its full `wait`
        registry = Registry()
        c = client(socket, registry)
        await c.start()
        closing = asyncio.ensure_future(c.close(wait=0.2))
        await asyncio.sleep(0.05)
        socket.push(call_event("notify_family", '{"message": "x", "severity": "high"}', call_id="late"))
        await asyncio.sleep(0.05)
        await closing
        await asyncio.sleep(0.05)
        return registry.calls, [t.get_name() for t in c._tool_tasks]

    calls, tasks = asyncio.run(run())
    assert calls == [] and tasks == []


def test_an_error_naming_a_tool_event_stops_the_tools_but_never_the_chat():
    async def run():
        socket = FakeSocket(starts_only)
        c = client(socket, Registry())
        await c.start()
        events = c.events()
        socket.push(call_event("get_medication_schedule", "{}", call_id="e1"))
        await settle(socket, "response.create", 1)
        socket.push({"type": "error", "error": {"type": "invalid_request_error", "code": "unknown_event",
                                                "message": "Unknown event type response.item.create",
                                                "client_event_id": "tool-result-1"}})
        socket.push(call_event("get_medication_schedule", "{}", call_id="e2"))
        await asyncio.sleep(0.1)
        socket.push({"type": "session.output_transcript.delta", "delta": "還在"})
        got = []
        async with asyncio.timeout(1):
            while True:
                event = await anext(events)
                got.append(event.type)
                if event.type == "output_text":
                    break
        await c.close()
        return c.delegation_rejected, got, socket.sent_types().count("response.item.create")

    rejected, got, outputs = asyncio.run(run())
    assert rejected is True and "error" not in got and got[-1] == "output_text"
    assert outputs == 1   # the second call ran but no further tool event went out
