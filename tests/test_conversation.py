"""Reachy check-in conversations: screening, replies, consent, safety alerts, and the device/patient APIs."""

import asyncio
import sys
import time
import types
import uuid

import pytest
from fastapi.testclient import TestClient

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.config import DEVICE_PORT
from app.dependencies import get_current_user
from app.routers import api_conversations, api_device, api_reachy
from app.services import after_chat, consent_service, conversation, deletion_ledger, outbox
from app.services.device_auth import get_device

DEVICE_ID = str(uuid.uuid4())
TASK_ID = str(uuid.uuid4())
CHECKIN = {scope: {"granted": True, "terms_version": config.TERMS_VERSION}
           for scope in ("core", "robot_camera", "robot_microphone", "cloud_voice", "conversation_analysis",
                         "safety_alerts")}


# ── pure rules ──

@pytest.mark.parametrize("text, kind", [
    ("我不想活了", "self_harm"), ("活著沒意思", "self_harm"), ("I want to die", "self_harm"),
    ("我剛剛一次吃了五顆", "overdose"), ("I think I took too many pills", "overdose"),
    ("想死你了", None), ("今天不想活動", None), ("今天天氣很好", None), ("", None),
    # what speech-to-text actually wrote for "我不想活了" in a real session
    ("瑞器,我不要货了。", "self_harm"), ("no, no,我不想火了。", "self_harm"), ("我真的活不下去", "self_harm"),
    ("我今天很无聊，我想出门。", None), ("我想跟你聊。", None),
])
def test_screen_flags_risk_words_but_not_everyday_phrases(text, kind):
    assert conversation.screen(text) == kind


def test_goodbye_and_speech_text():
    assert conversation.wants_to_end("好，再見") and conversation.wants_to_end("OK bye")
    assert not conversation.wants_to_end("我今天去散步")
    assert conversation.speech_text("**您好**😊！今天過得怎麼樣？", "zh-TW") == "您好！今天过得怎么样？"
    assert conversation.speech_text("Hello *there*", "en") == "Hello there"


def test_reply_falls_back_when_the_model_is_unavailable(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    history = [{"role": "patient", "text": "我今天去散步"}]
    assert asyncio.run(conversation.reply(history, "zh-TW")) == conversation.FALLBACK["zh-TW"]


def test_reply_maps_roles_and_cleans_the_answer(monkeypatch):
    seen = {}

    async def complete(messages, max_tokens=200):
        seen["messages"] = messages
        return "  真好！😊 您走了多久呢？ "

    monkeypatch.setattr(conversation, "complete", complete)
    history = [{"role": "reachy", "text": "今天感覺怎麼樣？"}, {"role": "patient", "text": "我去散步了"}]
    assert asyncio.run(conversation.reply(history, "zh-TW")) == "真好！ 您走了多久呢？"
    assert [m["role"] for m in seen["messages"]] == ["system", "assistant", "user"]


@pytest.mark.parametrize("finish_reason, expected", [("stop", "好的。"), ("length", None)])
def test_a_reply_cut_off_by_the_token_limit_is_discarded(monkeypatch, finish_reason, expected):
    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"finish_reason": finish_reason, "message": {"content": "好的。"}}]}

    monkeypatch.setattr(conversation.requests, "post", lambda *args, **kwargs: Response())
    assert conversation._post([{"role": "user", "content": "hi"}], 50) == expected


def test_reasoning_text_is_never_spoken(monkeypatch):
    answers = iter(["The user wants me to reply warmly in Chinese...", "好的呀，今天天氣真好！"])

    async def complete(messages, max_tokens=200):
        return next(answers)

    monkeypatch.setattr(conversation, "complete", complete)
    assert asyncio.run(conversation.reply([{"role": "patient", "text": "天氣很好"}], "zh-TW")) == "好的呀，今天天氣真好！"

    async def always_planning(messages, max_tokens=200):
        return "Let me think about this."

    monkeypatch.setattr(conversation, "complete", always_planning)
    assert asyncio.run(conversation.reply([{"role": "patient", "text": "嗨"}], "zh-TW")) == conversation.FALLBACK["zh-TW"]
    assert conversation.usable_reply("Hello there", "en") and not conversation.usable_reply("Hello there", "zh-TW")


def test_summary_uses_the_last_format_lines_after_a_reasoning_preamble():
    answer = ("I need to output two lines:\nMOOD: one of happy|calm\nSUMMARY: <one sentence>\n\n"
              "MOOD: calm\nSUMMARY: 長者談到散步。")
    assert conversation.parse_summary(answer) == ("長者談到散步。", "calm")
    assert conversation.parse_summary("SUMMARY: <one sentence>") == (None, "unknown")


@pytest.mark.parametrize("answer, expected", [
    ("MOOD: happy\nSUMMARY: 長者談到散步，心情愉快。", ("長者談到散步，心情愉快。", "happy")),
    ("mood: Excited\nsummary: x", ("x", "unknown")),
    (None, (None, "unknown")),
])
def test_summary_parsing(monkeypatch, answer, expected):
    async def complete(messages, max_tokens=200):
        return answer

    monkeypatch.setattr(conversation, "complete", complete)
    assert asyncio.run(conversation.summarize([{"role": "patient", "text": "hi"}], "zh-TW")) == expected


# ── device API ──

class FakeDB:
    def __init__(self):
        self.task = {"task_id": uuid.UUID(TASK_ID), "u_id": 7, "slot_time": None, "intk_ids": [],
                     "status": "in_progress", "lease_owner": DEVICE_ID}
        self.conversations, self.turns, self.notifications = {}, [], []

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchrow(self, query, *args):
        if "FROM reachy_task WHERE task_id" in query:
            ok = str(self.task["task_id"]) == args[0] and self.task["u_id"] == args[1] and self.task["lease_owner"] == args[2]
            return dict(self.task) if ok else None
        if "SET after_chat_attempts = after_chat_attempts + 1" in query:
            row = self.conversations.get(args[0])
            if (row and row["u_id"] == args[1] and row["after_chat_state"] in ("pending", "failed")
                    and row["after_chat_attempts"] < args[2]):
                row["after_chat_attempts"] += 1
                return {"language": row["language"], "risk_flag": row["risk_flag"], "followup_memory_id": None}
            return None
        if "FROM conversation WHERE conversation_id" in query:
            row = self.conversations.get(args[0])
            return dict(row) if row and row["u_id"] == args[1] else None
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        if "INSERT INTO conversation_turn" in query:
            cid, u_id, role, text, flagged = args
            self.turns.append({"turn_id": len(self.turns) + 1, "conversation_id": cid, "role": role, "text": text,
                               "flagged": flagged})
            return len(self.turns)
        raise AssertionError(query)

    async def fetch(self, query, *args):
        if "SELECT role, text FROM conversation_turn" in query:
            return [{"role": t["role"], "text": t["text"]} for t in self.turns if t["conversation_id"] == args[0]]
        raise AssertionError(query)

    async def execute(self, query, *args):
        if "INSERT INTO conversation " in query:
            cid, u_id, task_id, language, model, followup = args
            self.conversations[cid] = {"conversation_id": cid, "u_id": u_id, "language": language,
                                       "ended_at": None, "risk_flag": False, "summary": None, "mood": None,
                                       "after_chat_state": "done", "after_chat_attempts": 0,
                                       "followup_memory_id": None}
        elif "SET risk_flag = TRUE" in query:
            self.conversations[args[0]]["risk_flag"] = True
        elif "SET ended_at = NOW()" in query:
            self.conversations[args[0]].update(ended_at="now", end_reason=args[1], after_chat_state="pending")
        elif "SET after_chat_state" in query:
            self.conversations[args[0]]["after_chat_state"] = args[1]
        elif "SET summary" in query:
            self.conversations[args[0]].update(summary=args[1], mood=args[2])
        elif "INSERT INTO notification" in query:
            self.notifications.append(args)
        else:
            raise AssertionError(query)


class Pool:
    def __init__(self, db):
        self.db = db

    def acquire(self):
        return self.db


@pytest.fixture
def robot(monkeypatch):
    from app.main import app

    db, alerts, prompts = FakeDB(), [], []
    state = {"consent": dict(CHECKIN), "family_contacts": 1}

    async def get_state(u_id):
        return state["consent"]

    async def enqueue_to_contacts(conn, u_id, **kwargs):
        alerts.append({"u_id": u_id, **kwargs})
        return state["family_contacts"]

    async def reply(history, language, memory=""):
        prompts.append(list(history))
        return "真好，您走了多久呢？"

    async def summarize(history, language):
        return "談到散步，心情愉快。", "happy"

    monkeypatch.setattr(app, "dependency_overrides", {get_device: lambda: {
        "device_id": DEVICE_ID, "u_id": 7, "auto_record": True, "face_label": "pearl", "name": "Pearl"}})
    monkeypatch.setattr(api_device, "get_pool", lambda: Pool(db))
    monkeypatch.setattr(after_chat, "get_pool", lambda: Pool(db))
    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(outbox, "enqueue_to_contacts", enqueue_to_contacts)
    monkeypatch.setattr(conversation, "reply", reply)
    monkeypatch.setattr(conversation, "summarize", summarize)
    client = TestClient(app, base_url=f"http://testserver:{DEVICE_PORT}")
    return types.SimpleNamespace(client=client, db=db, alerts=alerts, prompts=prompts, state=state)


def _start(robot):
    response = robot.client.post("/api/device/conversations", json={"task_id": TASK_ID, "language": "zh-TW"})
    assert response.status_code == 200, response.text
    return response.json()


def _turn(robot, cid, text):
    return robot.client.post(f"/api/device/conversations/{cid}/turn", json={"text": text})


def test_conversation_needs_every_checkin_consent(robot):
    robot.state["consent"] = {k: v for k, v in CHECKIN.items() if k != "safety_alerts"}
    response = robot.client.post("/api/device/conversations", json={"task_id": TASK_ID})
    assert response.status_code == 403 and response.json()["detail"] == "checkin_consent_required"


def test_start_and_a_normal_turn(robot):
    opened = _start(robot)
    assert opened["reply"] == conversation.OPENING["zh-TW"] and opened["speech_text"] == "今天感觉怎么样？想跟我聊聊吗？"
    body = _turn(robot, opened["conversation_id"], "我早上去散步了").json()
    assert body == {"reply": "真好，您走了多久呢？", "speech_text": "真好，您走了多久呢？", "end": False, "risk": False}
    assert [t["role"] for t in robot.db.turns] == ["reachy", "patient", "reachy"]
    assert robot.prompts[0][-1] == {"role": "patient", "text": "我早上去散步了"}
    assert robot.alerts == []


def test_risk_words_never_reach_the_model_and_alert_every_contact(robot):
    cid = _start(robot)["conversation_id"]
    body = _turn(robot, cid, "我覺得活不下去了").json()
    assert body["risk"] is True and body["end"] is True and body["reply"] == conversation.HELPLINE["zh-TW"]
    assert robot.prompts == []
    (alert,) = robot.alerts
    assert alert["kind"] == "safety_alert" and alert["priority"] == 0 and alert["contact_flag"] is None
    assert "活不下去" in alert["messages"][0]["text"] and "119" in alert["messages"][0]["text"]
    assert robot.db.conversations[cid]["risk_flag"] is True
    assert [t["flagged"] for t in robot.db.turns if t["role"] == "patient"] == [True]


def test_a_safety_alert_with_no_family_contact_is_recorded_not_lost(robot):
    robot.state["family_contacts"] = 0   # e.g. the only LINE contact is the patient's own
    cid = _start(robot)["conversation_id"]
    assert _turn(robot, cid, "I want to die").json()["risk"] is True
    assert len(robot.alerts) == 1 and robot.db.notifications == [
        (7, "safety_alert_undelivered", "A check-in safety alert could not be sent: no verified family contact on LINE.")]


def test_goodbye_and_turn_limit_end_the_conversation(robot):
    cid = _start(robot)["conversation_id"]
    assert _turn(robot, cid, "好，拜拜").json()["end"] is True
    cid = _start(robot)["conversation_id"]
    for _ in range(conversation.MAX_PATIENT_TURNS - 1):
        assert _turn(robot, cid, "嗯").json()["end"] is False
    last = _turn(robot, cid, "嗯").json()
    assert last["end"] is True and last["reply"] == conversation.CLOSING["zh-TW"]


def test_end_is_idempotent_writes_a_summary_and_blocks_more_turns(robot):
    cid = _start(robot)["conversation_id"]
    _turn(robot, cid, "我早上去散步了")
    assert robot.client.post(f"/api/device/conversations/{cid}/end", json={"reason": "finished"}).json() == {"ended": True}
    assert robot.client.post(f"/api/device/conversations/{cid}/end", json={"reason": "silence"}).json() == {"ended": True}
    deadline = time.time() + 2
    while robot.db.conversations[cid]["summary"] is None and time.time() < deadline:
        time.sleep(0.02)
    assert robot.db.conversations[cid]["summary"] == "談到散步，心情愉快。"
    assert robot.db.conversations[cid]["mood"] == "happy" and robot.db.conversations[cid]["end_reason"] == "finished"
    assert _turn(robot, cid, "還有").status_code == 409


def test_conversations_of_other_tasks_and_patients_are_not_found(robot):
    assert robot.client.post("/api/device/conversations", json={"task_id": str(uuid.uuid4())}).status_code == 404
    assert _turn(robot, str(uuid.uuid4()), "hi").status_code == 404
    assert _turn(robot, "not-a-uuid", "hi").status_code == 404


# ── patient side ──

def test_checkin_button_needs_consent(monkeypatch):
    from app.main import app

    async def get_state(u_id):
        return {"core": CHECKIN["core"], "robot_camera": CHECKIN["robot_camera"]}

    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(app, "dependency_overrides", {get_current_user: lambda: {"u_id": 7, "name": "Pearl"}})
    response = TestClient(app).post("/api/reachy/checkin")
    assert response.status_code == 403 and response.json()["detail"] == "checkin_consent_required"
    assert api_reachy.router and api_conversations.router


def test_patient_can_read_and_delete_only_own_conversations(monkeypatch):
    from app.main import app

    cid = str(uuid.uuid4())
    executed, host = [], []

    class Conn:
        def transaction(self):
            class Tx:
                async def __aenter__(self):
                    return None

                async def __aexit__(self, *exc):
                    return False
            return Tx()

        async def execute(self, query, *args):
            executed.append((query, args))
            return "SELECT 1"

        async def fetchrow(self, query, *args):
            assert args[1] == 7
            return {"id": uuid.UUID(cid), "started_at": None, "ended_at": None, "end_reason": "finished",
                    "summary": "s", "mood": "calm", "risk_flag": False, "language": "zh-TW",
                    "model": "openrouter/free"} if args[0] == cid else None

        async def fetch(self, query, *args):
            return [{"role": "reachy", "text": "今天感覺怎麼樣？", "flagged": False, "created_at": None}]

        async def fetchval(self, query, *args):
            return uuid.UUID(cid) if args == (cid, 7) else None

    class P:
        def acquire(self):
            conn = Conn()

            class Ctx:
                async def __aenter__(self):
                    return conn

                async def __aexit__(self, *exc):
                    return False
            return Ctx()

    async def get_state(u_id):
        return {"core": CHECKIN["core"]}

    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(api_conversations, "get_pool", lambda: P())
    monkeypatch.setattr(deletion_ledger, "append_host_file", lambda *entry: host.append(entry))
    monkeypatch.setattr(app, "dependency_overrides", {get_current_user: lambda: {"u_id": 7, "name": "Pearl"}})
    client = TestClient(app)
    detail = client.get(f"/api/conversations/{cid}").json()
    assert detail["id"] == cid and detail["turns"][0]["role"] == "reachy"
    assert client.get(f"/api/conversations/{uuid.uuid4()}").status_code == 404
    assert client.get("/api/conversations/nope").status_code == 404
    assert client.delete(f"/api/conversations/{cid}").json() == {"deleted": cid}
    assert 'FOR UPDATE' in executed[0][0] and executed[0][1] == (7,)
    assert [args for query, args in executed if "INSERT INTO deletion_ledger" in query] == [("conversation", 7, cid)]
    assert host == [("conversation", 7, cid)]
    assert client.delete(f"/api/conversations/{uuid.uuid4()}").status_code == 404
    assert len(host) == 1


def test_end_after_a_risk_turn_makes_no_model_call(robot, monkeypatch):
    calls = []

    async def summarize(history, language):
        calls.append(history)
        return "should not happen", "sad"

    monkeypatch.setattr(conversation, "summarize", summarize)
    cid = _start(robot)["conversation_id"]
    assert _turn(robot, cid, "我覺得活不下去了").json()["risk"] is True
    assert robot.client.post(f"/api/device/conversations/{cid}/end", json={"reason": "risk"}).json() == {"ended": True}
    deadline = time.time() + 1
    while time.time() < deadline and robot.db.conversations[cid]["mood"] is None:
        time.sleep(0.02)
    assert calls == []
    assert robot.db.conversations[cid]["summary"] is None and robot.db.conversations[cid]["mood"] == "unknown"


def test_reply_sends_memory_as_a_second_system_message(monkeypatch):
    seen = {}

    async def complete(messages, max_tokens=200):
        seen["messages"] = messages
        return "好的。"

    monkeypatch.setattr(conversation, "complete", complete)
    asyncio.run(conversation.reply([{"role": "patient", "text": "嗨"}], "zh-TW", "<memory>\n稱呼：王奶奶\n</memory>"))
    assert [m["role"] for m in seen["messages"]] == ["system", "system", "user"]
    assert seen["messages"][0]["content"] == conversation.SYSTEM_PROMPT["zh-TW"]
    assert "王奶奶" in seen["messages"][1]["content"]


def test_reply_gives_the_fallback_when_the_model_is_too_slow(monkeypatch):
    async def complete(messages, max_tokens=200):
        await asyncio.sleep(1)
        return "太慢了"

    monkeypatch.setattr(conversation, "complete", complete)
    monkeypatch.setattr(conversation, "REPLY_BUDGET_SECONDS", 0.05)
    assert asyncio.run(conversation.reply([{"role": "patient", "text": "嗨"}], "zh-TW")) == conversation.FALLBACK["zh-TW"]


def test_complete_with_reason(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    assert asyncio.run(conversation.complete_with_reason([])) == (None, "no_key")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "k")

    def limited(*args):
        raise RuntimeError("retryable 429")

    monkeypatch.setattr(conversation, "_post", limited)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(conversation.asyncio, "sleep", lambda s: real_sleep(0))
    assert asyncio.run(conversation.complete_with_reason([])) == (None, "rate_limited")
    monkeypatch.setattr(conversation, "_post", lambda *args: None)
    assert asyncio.run(conversation.complete_with_reason([])) == (None, "empty")
    monkeypatch.setattr(conversation, "_post", lambda *args: "hi")
    assert asyncio.run(conversation.complete_with_reason([])) == ("hi", "ok")


def test_post_sends_temperature_and_optional_provider_routing(monkeypatch):
    sent = {}

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}

    def post(url, timeout, headers, json):
        sent.update(json)
        return Response()

    monkeypatch.setattr(conversation.requests, "post", post)
    monkeypatch.setattr(config, "OPENROUTER_PROVIDER_ONLY", "")
    monkeypatch.setattr(config, "OPENROUTER_DATA_COLLECTION", "")
    conversation._post([], 10, 0)
    assert sent["temperature"] == 0 and "provider" not in sent
    monkeypatch.setattr(config, "OPENROUTER_PROVIDER_ONLY", "deepinfra, together")
    monkeypatch.setattr(config, "OPENROUTER_DATA_COLLECTION", "deny")
    conversation._post([], 10)
    assert sent["provider"] == {"only": ["deepinfra", "together"], "allow_fallbacks": False, "data_collection": "deny"}


def test_block_empty_without_memory_consent(robot, monkeypatch):
    seen = []

    async def reply(history, language, memory=""):
        seen.append(memory)
        return "好。"

    monkeypatch.setattr(conversation, "reply", reply)
    cid = _start(robot)["conversation_id"]
    _turn(robot, cid, "我去散步了")
    assert seen == [""]


def test_memory_on_names_the_patient_and_sends_the_block(robot, monkeypatch):
    from app.services import memory
    seen = []
    robot.state["consent"] = {**CHECKIN, "conversation_memory": {"granted": True, "terms_version": config.TERMS_VERSION}}

    async def current_facts(conn, u_id):
        return [{"kind": "name", "subject": "preferred_name", "text": "王奶奶", "event_date": None, "source": "patient",
                 "created_at": None, "memory_id": "m", "followed_up_at": None}]

    async def pick_followup(conn, u_id, today):
        return None

    async def build_block(conn, u_id, language, followup_memory_id, today):
        return "<memory>\n稱呼：王奶奶\n</memory>"

    async def reply(history, language, memory=""):
        seen.append(memory)
        return "好。"

    monkeypatch.setattr(memory, "current_facts", current_facts)
    monkeypatch.setattr(memory, "pick_followup", pick_followup)
    monkeypatch.setattr(memory, "build_block", build_block)
    monkeypatch.setattr(conversation, "reply", reply)
    opened = _start(robot)
    assert opened["reply"] == "王奶奶，" + conversation.OPENING["zh-TW"]
    _turn(robot, opened["conversation_id"], "我去散步了")
    assert seen == ["<memory>\n稱呼：王奶奶\n</memory>"]


def test_a_turn_after_a_risk_turn_never_reaches_the_model(robot):
    # The robot should call /end after a risk reply, but if it sends another turn first, the history
    # holds the flagged words: no model call, no second alert, the help-line reply again.
    cid = _start(robot)["conversation_id"]
    assert _turn(robot, cid, "我覺得活不下去了").json()["risk"] is True
    body = _turn(robot, cid, "今天天氣很好").json()
    assert body["end"] is True and body["risk"] is True and body["reply"] == conversation.HELPLINE["zh-TW"]
    assert robot.prompts == [] and len(robot.alerts) == 1
