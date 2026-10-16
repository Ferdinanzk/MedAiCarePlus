"""Live voice check-ins, the start (spec 2026-10-12-live-voice-2a-design.md A2): the robot's WebSocket on the device
port with its key check and one live chat per patient, and LiveRelay's start: every check and its fallback before any
conversation row exists, GPT-Live within 8 s, then the row, the opening turn and `ready`. A fake GPT-Live client, a
fake robot and a fake database: no network."""

import asyncio
import functools
import json
import logging
import sys
import types
import uuid

import pytest
from fastapi.testclient import TestClient

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.config import DEVICE_PORT
from app.routers import api_device
from app.services import after_chat, ai_settings, consent_service, conversation, device_auth, live_session, memory
from app.services.live_client import LiveError, LiveEvent

DEVICE_ID = str(uuid.uuid4())
TASK_ID = str(uuid.uuid4())
FOLLOWUP_ID = str(uuid.uuid4())
TOKEN = device_auth.issue_token(DEVICE_ID, 7)
DEVICE = {"device_id": DEVICE_ID, "u_id": 7, "auto_record": False, "face_label": "pearl", "name": "Pearl"}
CHECKIN = {scope: {"granted": True, "terms_version": config.TERMS_VERSION}
           for scope in ("core", "robot_camera", "robot_microphone", "cloud_voice", "conversation_analysis",
                         "safety_alerts")}
MEMORY_ON = {**CHECKIN, "conversation_memory": {"granted": True, "terms_version": config.TERMS_VERSION}}
KEY = "sk-test-live-key-0000"
TARGET = ai_settings.LiveTarget(KEY, "gpt-live-1", "marin")
START = {"type": "start", "task_id": TASK_ID, "language": "zh-TW", "rate": 16000, "bridge_version": "0.8.0"}
OPENING = "王奶奶，" + conversation.OPENING["zh-TW"]
LIVE_URL = f"ws://testserver:{DEVICE_PORT}/api/device/conversations/live"


class FakeDB:
    """The rows a live start reads and writes. `order` records what happened, in order."""

    def __init__(self, order):
        self.order = order
        self.task = {"task_id": uuid.UUID(TASK_ID), "u_id": 7, "slot_time": None, "intk_ids": [],
                     "status": "in_progress", "reason": "checkin", "lease_owner": DEVICE_ID}
        self.paired = True   # the robot is not unpaired
        self.broken = False   # the conversation insert fails
        self.device_row = {"token_hash": device_auth.hash_token(TOKEN), "device_id": uuid.UUID(DEVICE_ID),
                           "u_id": 7, "auto_record": False, "revoked_at": None}   # device_auth's token row
        self.conversations, self.turns = {}, []

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchrow(self, query, *args):
        if 'FROM reachy_device d JOIN "user" u' in query:   # device_auth._lookup
            row = self.device_row
            return {**row, "face_label": "pearl", "name": "Pearl"} if args[0] == row["token_hash"] else None
        if "FROM reachy_task WHERE task_id = $1::uuid AND u_id = $2 AND lease_owner = $3::uuid" in query:
            task = self.task
            ok = str(task["task_id"]) == args[0] and task["u_id"] == args[1] and task["lease_owner"] == args[2]
            return dict(task) if ok else None
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        if "SELECT TRUE FROM reachy_device WHERE device_id = $1::uuid AND revoked_at IS NULL" in query:
            return True if self.paired and args[0] == DEVICE_ID else None
        if "INSERT INTO conversation_turn" in query:
            cid, u_id, role, text, flagged, metrics = args
            self.turns.append({"turn_id": len(self.turns) + 1, "conversation_id": cid, "u_id": u_id, "role": role,
                               "text": text, "flagged": flagged, "metrics": metrics})
            self.order.append("turn")
            return len(self.turns)
        if "UPDATE conversation SET ended_at = NOW(), end_reason = $2, after_chat_state = 'pending'" in query:
            row = self.conversations.get(args[0])
            if row is None or row["ended_at"] is not None:
                return None
            row.update(ended_at="now", end_reason=args[1], after_chat_state="pending", live=json.loads(args[2]))
            return uuid.UUID(args[0])
        raise AssertionError(query)

    async def execute(self, query, *args):
        if "INSERT INTO conversation " in query:
            assert "'live'" in query   # mode = 'live'
            if self.broken:
                raise RuntimeError("database gone")
            cid, u_id, task_id, language, model, followup = args
            self.conversations[cid] = {"u_id": u_id, "task_id": task_id, "language": language, "model": model,
                                       "followup_memory_id": followup, "mode": "live", "ended_at": None}
            self.order.append("conversation")
            return "INSERT 0 1"
        raise AssertionError(query)


class Pool:
    def __init__(self, db):
        self.db = db

    def acquire(self):
        return self.db


class FakeLive:
    """GPT-Live as the relay uses it (live_client.LiveClient). world["fail"] makes start() raise that LiveError. Once
    started it stays quiet: no events, and the robot's audio goes nowhere."""

    def __init__(self, world, api_key, model, voice, instructions, *, rate=16000, connect=None):
        self.world = world
        self.args = {"api_key": api_key, "model": model, "voice": voice, "instructions": instructions, "rate": rate}
        self.timeout, self.commentaries, self.closes = None, [], 0
        self.close_reason = ""
        world["clients"].append(self)

    async def start(self, timeout=8.0):
        self.timeout = timeout
        self.world["order"].append("start")
        if self.world.get("fail"):
            raise LiveError(self.world["fail"])

    async def commentary(self, text):
        self.commentaries.append(text)
        self.world["order"].append("commentary")

    async def send_audio(self, pcm):
        pass

    async def events(self):
        await asyncio.Event().wait()
        yield LiveEvent("started")

    async def close(self, wait=2.0):
        self.closes += 1
        self.close_reason = self.close_reason or "close_requested"
        return 12.0


class FakeRobot:
    """The robot's end of the WebSocket (live_session.WebSocketRobot). It sends `inbox` in order and then hangs up
    (None); with hang=True it never says anything."""

    def __init__(self, *inbox, order=None, hang=False):
        self.inbox, self.order, self.hang = list(inbox), order, hang
        self.sent, self.closed = [], None

    async def receive(self):
        if self.hang:
            await asyncio.Event().wait()
        return self.inbox.pop(0) if self.inbox else None

    async def send_json(self, message):
        self.sent.append(message)
        if self.order is not None:
            self.order.append(message["type"])

    async def send_bytes(self, data):
        self.sent.append(bytes(data))

    async def close(self, code=1000):
        self.closed = code


@pytest.fixture
def live(monkeypatch):
    """A live start that passes every check: LIVE_VOICE_TEST on, check-in consent, the task leased by this robot, the
    robot's live switch on, a usable live model. Tests break one thing at a time."""
    order = []
    db = FakeDB(order)
    world = {"order": order, "clients": [], "consent": dict(CHECKIN), "target": TARGET, "name": "王奶奶",
             "names": [], "followups": [], "blocks": [], "after_chat": [], "block_fails": False}

    async def get_state(u_id):
        assert u_id == 7
        return world["consent"]

    async def call_name(conn, u_id, memory_on):
        world["names"].append(memory_on)
        return world["name"]

    async def pick_followup(conn, u_id, today):
        world["followups"].append(u_id)
        return {"memory_id": uuid.UUID(FOLLOWUP_ID), "text": "孫子下週要來", "event_date": None}

    async def build_block(conn, u_id, language, followup_memory_id, today):
        world["blocks"].append(followup_memory_id)
        if world["block_fails"]:
            raise RuntimeError("memory table unreadable")
        return "MEMORY BLOCK"

    async def process(conversation_id, u_id):
        world["after_chat"].append((conversation_id, u_id))

    monkeypatch.setattr(config, "LIVE_VOICE_TEST", True)
    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(live_session, "get_pool", lambda: Pool(db))
    monkeypatch.setattr(device_auth, "get_pool", lambda: Pool(db))
    monkeypatch.setattr(ai_settings, "live_target", lambda snapshot=None: world["target"])
    monkeypatch.setattr(memory, "call_name", call_name)
    monkeypatch.setattr(memory, "pick_followup", pick_followup)
    monkeypatch.setattr(memory, "build_block", build_block)
    monkeypatch.setattr(after_chat, "process", process)
    monkeypatch.setattr(conversation, "reply_prompt", lambda language, tools=False: f"REPLY PROMPT {language}")
    monkeypatch.setattr(api_device, "_live_chats", {})
    return types.SimpleNamespace(db=db, world=world, order=order)


def _run(live, robot):
    async def main():
        factory = functools.partial(FakeLive, live.world)
        await live_session.LiveRelay(robot, dict(DEVICE), client_factory=factory).run()
        await asyncio.sleep(0)   # the after-chat work starts in the background
    asyncio.run(main())


def _instructions(language="zh-TW", name="王奶奶", block=""):
    parts = [f"REPLY PROMPT {language}", live_session.LIVE_RULES[language]]
    if name:
        parts.append(live_session.CALL_NAME[language].format(name=name))
    if block:
        parts.append(block)
    return "\n\n".join(parts)


# ── LiveRelay's start ──

def test_a_start_that_passes_every_check_connects_then_stores_the_chat(live):
    robot = FakeRobot(START, {"type": "end", "reason": "finished"}, order=live.order)
    _run(live, robot)
    # GPT-Live started before anything was stored; then the row, the opening turn, ready, the spoken opening.
    assert live.order == ["start", "conversation", "turn", "ready", "commentary", "end"]
    (cid,) = live.db.conversations
    assert robot.sent == [{"type": "ready", "conversation_id": cid},
                          {"type": "end", "reason": "finished", "risk": False}]
    assert robot.closed == 1000
    row = live.db.conversations[cid]
    assert (row["u_id"], row["task_id"], row["language"], row["model"], row["mode"], row["followup_memory_id"]) == (
        7, TASK_ID, "zh-TW", "gpt-live-1", "live", None)
    assert live.db.turns == [{"turn_id": 1, "conversation_id": cid, "u_id": 7, "role": "reachy", "text": OPENING,
                              "flagged": False, "metrics": None}]
    (client,) = live.world["clients"]
    assert client.args == {"api_key": KEY, "model": "gpt-live-1", "voice": "marin", "instructions": _instructions(),
                           "rate": 16000}
    assert client.timeout == live_session.CONNECT_TIMEOUT == 8.0
    assert client.commentaries == [live_session.OPENING_COMMENTARY["zh-TW"] + json.dumps(OPENING, ensure_ascii=False)]
    # The ending: GPT-Live closed once, the conversation ended once with conversation.live, the after-chat work.
    assert client.closes == 1
    assert (row["ended_at"], row["end_reason"], row["after_chat_state"]) == ("now", "finished", "pending")
    assert row["live"] == {"usage_seconds": 12.0, "close_reason": "close_requested", "fallback_reason": None}
    assert live.world["after_chat"] == [(cid, 7)]
    assert live.world["names"] == [False] and live.world["followups"] == [] and live.world["blocks"] == []


def test_the_checks_run_in_order_and_the_first_failure_is_the_fallback(live, monkeypatch):
    """LIVE_VOICE_TEST first (spec 5: without it no live path is reachable), then spec A2's order: check-in consent,
    the leased task, the robot still paired, a usable live model, GPT-Live itself."""
    monkeypatch.setattr(config, "LIVE_VOICE_TEST", False)
    live.world["consent"] = {scope: value for scope, value in CHECKIN.items() if scope != "safety_alerts"}
    live.db.task["lease_owner"] = str(uuid.uuid4())   # another robot holds the task
    live.db.paired = False
    live.world["target"] = None
    live.world["fail"] = "live_auth"
    fixes = [lambda: monkeypatch.setattr(config, "LIVE_VOICE_TEST", True),
             lambda: live.world.update(consent=dict(CHECKIN)),
             lambda: live.db.task.update(lease_owner=DEVICE_ID),
             lambda: setattr(live.db, "paired", True),
             lambda: live.world.update(target=TARGET),
             lambda: live.world.pop("fail")]
    reasons = []
    for fix in fixes:
        robot = FakeRobot(START)
        _run(live, robot)
        (message,) = robot.sent
        assert (message["type"], message["conversation_id"], robot.closed) == ("fallback", None, 1000)
        reasons.append(message["reason"])
        fix()
    assert reasons == ["live_disabled", "checkin_consent_required", "task_not_found", "live_disabled",
                       "live_not_configured", "live_unavailable"]
    assert live.db.conversations == {} and live.db.turns == []   # nothing stored before GPT-Live runs
    assert [client.timeout for client in live.world["clients"]] == [8.0]   # only the last start reached GPT-Live
    robot = FakeRobot(START)
    _run(live, robot)
    assert robot.sent[0]["type"] == "ready" and len(live.db.conversations) == 1


@pytest.mark.parametrize("code", ["live_auth", "live_unavailable", "rate_limit_exceeded", "content_filter",
                                  "live_closed"])
def test_a_gpt_live_that_does_not_start_is_a_fallback_before_any_row(live, code):
    live.world["fail"] = code
    robot = FakeRobot(START, order=live.order)
    _run(live, robot)
    assert robot.sent == [{"type": "fallback", "reason": "live_unavailable", "conversation_id": None}]
    assert robot.closed == 1000 and live.order == ["start", "fallback"]
    assert live.db.conversations == {} and live.db.turns == [] and live.world["after_chat"] == []
    assert live.world["clients"][0].closes == 0   # start() closed its own socket


@pytest.mark.parametrize("start, reason", [
    ({**START, "task_id": "not-a-uuid"}, "task_not_found"),
    ({**START, "rate": 24000}, "unsupported_rate"),
])
def test_a_start_with_a_bad_task_id_or_rate_is_a_fallback(live, start, reason):
    robot = FakeRobot(start)
    _run(live, robot)
    assert robot.sent == [{"type": "fallback", "reason": reason, "conversation_id": None}] and robot.closed == 1000
    assert live.world["clients"] == [] and live.db.conversations == {}


@pytest.mark.parametrize("first", [b"\x00\x00" * 320, {"type": "end", "reason": "stopped"}, {"type": "start"},
                                   {"type": "start", "task_id": 7}, {**START, "language": ["en"]}, {}, None])
def test_a_robot_that_does_not_start_properly_is_closed_4400(live, first):
    robot = FakeRobot(first, START)   # a start after the first message comes too late
    _run(live, robot)
    assert robot.sent == [] and robot.closed == live_session.BAD_START == 4400
    assert live.world["clients"] == [] and live.db.conversations == {}


def test_a_robot_that_sends_nothing_is_closed_after_the_start_timeout(live, monkeypatch):
    monkeypatch.setattr(live_session, "START_TIMEOUT", 0.05)
    robot = FakeRobot(hang=True)
    _run(live, robot)
    assert robot.sent == [] and robot.closed == 4400


def test_memory_consent_adds_the_followup_and_the_memory_block(live):
    live.world["consent"] = dict(MEMORY_ON)
    robot = FakeRobot(START, {"type": "end", "reason": "goodbye"})
    _run(live, robot)
    (client,) = live.world["clients"]
    assert client.args["instructions"] == _instructions(block="MEMORY BLOCK")
    assert live.world["names"] == [True] and live.world["blocks"] == [uuid.UUID(FOLLOWUP_ID)]
    (row,) = live.db.conversations.values()
    assert row["followup_memory_id"] == FOLLOWUP_ID and row["end_reason"] == "goodbye"


@pytest.mark.parametrize("consent, memory_on", [(CHECKIN, False), (MEMORY_ON, True)])
def test_the_start_keeps_whether_memory_was_on_for_the_access_re_checks(live, monkeypatch, consent, memory_on):
    seen = []

    async def chat(relay):
        seen.append(relay._memory_on)

    monkeypatch.setattr(live_session.LiveRelay, "_chat", chat)
    live.world["consent"] = consent
    _run(live, FakeRobot(START))
    assert seen == [memory_on]


def test_a_memory_block_that_fails_costs_only_the_notes(live):
    live.world["consent"] = dict(MEMORY_ON)
    live.world["block_fails"] = True
    robot = FakeRobot(START, {"type": "end", "reason": "finished"})
    _run(live, robot)
    assert live.world["clients"][0].args["instructions"] == _instructions()
    assert robot.sent[0]["type"] == "ready"


def test_without_a_name_and_in_english(live):
    live.world["name"] = None
    robot = FakeRobot({**START, "language": "en"}, {"type": "end", "reason": "finished"})
    _run(live, robot)
    (client,) = live.world["clients"]
    assert client.args["instructions"] == _instructions("en", name=None)
    assert client.commentaries == [live_session.OPENING_COMMENTARY["en"]
                                   + json.dumps(conversation.OPENING["en"], ensure_ascii=False)]
    (row,) = live.db.conversations.values()
    assert row["language"] == "en" and live.db.turns[0]["text"] == conversation.OPENING["en"]


def test_an_unknown_language_is_zh_tw(live):
    robot = FakeRobot({**START, "language": "fr"}, {"type": "end", "reason": "finished"})
    _run(live, robot)
    (row,) = live.db.conversations.values()
    assert row["language"] == "zh-TW" and live.db.turns[0]["text"] == OPENING


@pytest.mark.parametrize("ending, reason", [(None, "disconnected"),
                                            ({"type": "end", "reason": "silence"}, "silence"),
                                            ({"type": "end", "reason": "risk"}, "stopped"),
                                            ({"type": "end"}, "stopped")])
def test_the_chat_ends_on_the_robots_end_or_its_leaving(live, ending, reason):
    """The robot's audio and reports are not relayed yet; the chat runs until the robot ends it or leaves. The robot
    can't end a chat as 'risk': only the server decides that."""
    robot = FakeRobot(START, b"\x00\x00" * 320, {"type": "playing"}, {"type": "idle"}, *([ending] if ending else []))
    _run(live, robot)
    (cid,) = live.db.conversations
    row = live.db.conversations[cid]
    assert row["end_reason"] == reason and live.world["after_chat"] == [(cid, 7)]
    assert robot.sent[-1] == {"type": "end", "reason": reason, "risk": False} and robot.closed == 1000
    assert live.world["clients"][0].closes == 1


def test_an_unexpected_failure_closes_1011_and_never_leaves_gpt_live_open(live):
    live.db.broken = True   # GPT-Live started, then the conversation row could not be written
    robot = FakeRobot(START)
    _run(live, robot)
    assert robot.sent == [] and robot.closed == 1011
    assert live.world["clients"][0].closes == 1 and live.db.conversations == {}


def test_the_openai_key_is_never_logged_or_sent_to_the_robot(live, caplog):
    caplog.set_level(logging.DEBUG)
    robot = FakeRobot(START, {"type": "end", "reason": "finished"})
    _run(live, robot)
    live.world["fail"] = "live_auth"
    refused = FakeRobot(START)
    _run(live, refused)
    assert KEY not in caplog.text and KEY not in json.dumps(robot.sent + refused.sent)


# ── The WebSocket route (/api/device/conversations/live) ──

@pytest.fixture
def live_socket(live, monkeypatch):
    from app.main import app

    relay = live_session.LiveRelay
    monkeypatch.setattr(live_session, "LiveRelay",
                        functools.partial(relay, client_factory=functools.partial(FakeLive, live.world)))
    live.client = TestClient(app)
    return live


def _connect(live, token=TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return live.client.websocket_connect(LIVE_URL, headers=headers)


def test_the_live_socket_checks_the_robot_key(live_socket):
    with _connect(live_socket, token=None) as websocket:
        assert websocket.receive() == {"type": "websocket.close", "code": 4401, "reason": "invalid_token"}
    live_socket.world["consent"] = {"core": CHECKIN["core"]}   # no robot_camera consent
    with _connect(live_socket) as websocket:
        assert websocket.receive() == {"type": "websocket.close", "code": 4403, "reason": "consent_required"}
    assert live_socket.world["clients"] == [] and api_device._live_chats == {}


def test_a_live_chat_through_the_socket(live_socket):
    with _connect(live_socket) as websocket:
        websocket.send_json(START)
        ready = websocket.receive_json()
        (cid,) = live_socket.db.conversations
        assert ready == {"type": "ready", "conversation_id": cid}
        assert api_device._live_chats.keys() == {7}
        websocket.send_bytes(b"\x00\x00" * 320)   # 20 ms of microphone audio
        websocket.send_json({"type": "end", "reason": "finished"})
        assert websocket.receive_json() == {"type": "end", "reason": "finished", "risk": False}
        assert websocket.receive() == {"type": "websocket.close", "code": 1000, "reason": ""}
    assert live_socket.db.conversations[cid]["end_reason"] == "finished" and api_device._live_chats == {}


def test_a_fallback_through_the_socket_frees_the_patient(live_socket, monkeypatch):
    monkeypatch.setattr(config, "LIVE_VOICE_TEST", False)
    with _connect(live_socket) as websocket:
        websocket.send_json(START)
        assert websocket.receive_json() == {"type": "fallback", "reason": "live_disabled", "conversation_id": None}
        assert websocket.receive() == {"type": "websocket.close", "code": 1000, "reason": ""}
    assert api_device._live_chats == {} and live_socket.db.conversations == {}


def test_the_patients_slot_is_free_once_the_chat_is_over_not_after_its_held_calls(live_socket, monkeypatch):
    """Round 3 (F1.5): the relay frees the patient's live-chat slot once its chat is over (on_over), before it waits
    for a record or a note to family still running, so the patient's next live chat is not refused with 4409; a chat
    that took the slot meanwhile keeps it."""
    seen, newer = [], object()

    class Relay:
        def __init__(self, robot, device):
            self.robot, self.on_over = robot, None

        async def run(self):
            seen.append(sorted(api_device._live_chats))
            self.on_over()                                    # GPT-Live is closed: the chat is over
            seen.append(sorted(api_device._live_chats))
            api_device._live_chats[7] = newer                 # the patient's next live chat starts meanwhile
            await self.robot.close(1000)                      # the held calls would be waited for now

    monkeypatch.setattr(live_session, "LiveRelay", Relay)
    with _connect(live_socket) as websocket:
        assert websocket.receive() == {"type": "websocket.close", "code": 1000, "reason": ""}
    assert seen == [[7], []] and api_device._live_chats == {7: newer}


def test_one_live_chat_per_patient(live_socket):
    with _connect(live_socket) as first:
        with _connect(live_socket) as second:   # the same patient's robot, while the first chat runs
            assert second.receive() == {"type": "websocket.close", "code": 4409, "reason": "live_chat_open"}
        first.send_json(START)
        assert first.receive_json()["type"] == "ready"
        first.send_json({"type": "end", "reason": "finished"})
        assert first.receive_json()["type"] == "end"
    assert api_device._live_chats == {}
    with _connect(live_socket) as again:   # once it ended, the next one may start
        again.send_json(START)
        assert again.receive_json()["type"] == "ready"
    assert len(live_socket.db.conversations) == 2 and api_device._live_chats == {}
