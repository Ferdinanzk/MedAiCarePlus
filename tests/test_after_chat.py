"""Post-chat work: summary, risk skip, fact storage under the lock, follow-up marking."""

import asyncio
import sys
import types
import uuid
from datetime import date, datetime, timezone

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.services import after_chat, consent_service, conversation, memory

CID = str(uuid.uuid4())
MEMORY_ON = {s: {"granted": True, "terms_version": config.TERMS_VERSION} for s in memory.MEMORY_SCOPES}
NOW = datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc)


class Conn:
    def __init__(self, store):
        self.s = store

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchrow(self, query, *args):
        if "SET after_chat_attempts = after_chat_attempts + 1" in query:
            chat = self.s.chat
            if chat["after_chat_state"] in ("pending", "failed") and chat["after_chat_attempts"] < args[2]:
                chat["after_chat_attempts"] += 1
                return dict(chat)
            return None
        if "SELECT ended_at FROM conversation" in query:
            return {"ended_at": NOW} if not self.s.deleted_chat else None
        if "FROM patient_memory WHERE memory_id" in query:
            return {"subject": "amy_visit"}
        raise AssertionError(query)

    async def fetch(self, query, *args):
        if "FROM conversation_turn" in query:
            return self.s.turns
        if "DISTINCT ON (kind, subject)" in query:
            return [dict(f) for f in self.s.facts]
        if "FROM patient_memory_deleted" in query:
            return self.s.tombstones
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        if "max(followed_up_at)" in query:
            return None
        if "count(*) FROM conversation" in query:
            return self.s.followup_tries
        raise AssertionError(query)

    async def execute(self, query, *args):
        self.s.sql.append(query)
        if 'FROM "user" WHERE u_id = $1 FOR UPDATE' in query:
            return "SELECT 1"
        if "SET after_chat_state" in query:
            self.s.chat["after_chat_state"] = args[1]
            self.s.chat["after_chat_attempts"] -= args[2]      # refund on rate limit
        elif "SET summary" in query:
            self.s.chat.update(summary=args[1], mood=args[2])
        elif "INSERT INTO patient_memory" in query:
            self.s.inserted.append(args)
            return "INSERT 0 1"
        elif "SET followed_up_at = NOW()" in query:
            self.s.followed_up.append(args)
        else:
            raise AssertionError(query)
        return "UPDATE 1"


class Pool:
    def __init__(self, store):
        self.store = store

    def acquire(self):
        return Conn(self.store)


@pytest.fixture
def world(monkeypatch):
    store = types.SimpleNamespace(
        chat={"language": "zh-TW", "risk_flag": False, "followup_memory_id": None, "after_chat_state": "pending",
              "after_chat_attempts": 0, "summary": None, "mood": None},
        turns=[{"role": "reachy", "text": conversation.OPENING["zh-TW"]},
               {"role": "patient", "text": "我孫女 Amy 下週日要來看我"},
               {"role": "reachy", "text": "真好！"},
               {"role": "patient", "text": "我很喜歡在陽台種花"}],
        facts=[], tombstones=[], inserted=[], followed_up=[], sql=[], followup_tries=1, deleted_chat=False,
        model_calls=[], answer=None, reason="ok", consent=dict(MEMORY_ON))

    async def get_state(u_id):
        return store.consent

    async def fetch_state(conn, u_id):
        return store.consent

    async def complete_with_reason(messages, max_tokens=200, temperature=0.7):
        store.model_calls.append(messages)
        return store.answer, store.reason

    async def summarize(history, language):
        store.model_calls.append(history)
        return "談到孫女。", "happy"

    monkeypatch.setattr(after_chat, "get_pool", lambda: Pool(store))
    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(consent_service, "fetch_state", fetch_state)
    monkeypatch.setattr(conversation, "complete_with_reason", complete_with_reason)
    monkeypatch.setattr(conversation, "summarize", summarize)
    monkeypatch.setattr(memory, "local_today", lambda: date(2026, 10, 3))
    store.answer = ("MOOD: happy\nSUMMARY: 長者談到孫女和種花。\n"
                    '{"facts": [{"kind": "event", "subject": "amy_visit", "text": "孫女 Amy 週日來訪", "event_date": "2026-10-11"},'
                    ' {"kind": "like", "subject": "garden", "text": "喜歡在陽台種花", "event_date": null},'
                    ' {"kind": "person", "subject": "meimei", "text": "女兒美美", "event_date": null}]}')
    return store


def run(store):
    return asyncio.run(after_chat.process(CID, 7))


def test_risk_chat_makes_no_model_call(world):
    world.chat["risk_flag"] = True
    assert run(world) == "skipped"
    assert world.model_calls == [] and world.chat["summary"] is None and world.chat["mood"] == "unknown"


def test_stores_summary_and_only_grounded_facts_under_the_lock(world):
    assert run(world) == "done"
    assert world.chat["summary"] == "長者談到孫女和種花。" and world.chat["mood"] == "happy"
    assert [args[3] for args in world.inserted] == ["amy_visit", "garden"]      # meimei is not in the patient's words
    lock = next(i for i, q in enumerate(world.sql) if 'FOR UPDATE' in q)
    first_insert = next(i for i, q in enumerate(world.sql) if "INSERT INTO patient_memory" in q)
    assert lock < first_insert and world.chat["after_chat_state"] == "done"


def test_bad_json_still_saves_summary(world):
    world.answer = 'MOOD: calm\nSUMMARY: 長者談到天氣。\n{"facts": [{"kind": "like"'
    assert run(world) == "done"
    assert world.chat["summary"] == "長者談到天氣。" and world.inserted == []


def test_withdrawn_consent_stores_nothing(world, monkeypatch):
    async def fetch_state(conn, u_id):
        return {}            # withdrawn between the model call and the write

    monkeypatch.setattr(consent_service, "fetch_state", fetch_state)
    assert run(world) == "done" and world.inserted == []


def test_without_memory_consent_only_the_old_summary_runs(world):
    world.consent = {k: v for k, v in MEMORY_ON.items() if k != "conversation_memory"}
    assert run(world) == "done"
    assert world.chat["summary"] == "談到孫女。" and world.inserted == []


def test_tombstoned_subject_is_not_relearned(world):
    world.tombstones = [{"kind": "like", "subject": "garden"}]
    run(world)
    assert [args[3] for args in world.inserted] == ["amy_visit"]


def test_deleted_chat_stores_nothing(world):
    world.deleted_chat = True
    assert run(world) == "done" and world.inserted == []


def test_rate_limit_leaves_the_chat_pending_and_does_not_count_the_attempt(world):
    world.answer, world.reason = None, "rate_limited"
    assert run(world) == "pending"
    assert world.chat["after_chat_state"] == "pending" and world.chat["after_chat_attempts"] == 0


def test_short_chat_gets_a_summary_only(world):
    world.turns = [{"role": "reachy", "text": "嗨"}, {"role": "patient", "text": "好"}]
    run(world)
    assert world.chat["summary"] == "談到孫女。" and world.inserted == []


def test_followup_marked_only_after_a_real_reply(world):
    world.chat["followup_memory_id"] = uuid.uuid4()
    world.turns = [{"role": "reachy", "text": conversation.OPENING["zh-TW"]},
                   {"role": "patient", "text": "嗯"}, {"role": "reachy", "text": conversation.FALLBACK["zh-TW"]}]
    run(world)
    assert world.followed_up == []
    world.chat.update(after_chat_state="pending")
    world.turns[-1] = {"role": "reachy", "text": "Amy 來玩得開心嗎？"}
    run(world)
    assert world.followed_up and world.followed_up[0][1] == "amy_visit"


def test_followup_given_up_after_two_chats(world):
    world.chat["followup_memory_id"] = uuid.uuid4()
    world.followup_tries = 2
    world.turns = [{"role": "reachy", "text": conversation.OPENING["zh-TW"]}, {"role": "patient", "text": "嗯"},
                   {"role": "reachy", "text": conversation.CLOSING["zh-TW"]}]
    run(world)
    assert world.followed_up


def test_a_claimed_chat_is_not_processed_twice(world):
    world.chat["after_chat_state"] = "done"
    assert run(world) is None and world.model_calls == []
