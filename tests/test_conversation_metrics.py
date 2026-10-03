"""Check-in timings: the model chain (primary, fallback, deadline) and the patient's response-time endpoints."""

import asyncio
import json
import sys
import threading
import time
import types
import uuid
from datetime import datetime, timezone

import pytest
import requests
from fastapi.testclient import TestClient

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.dependencies import get_current_user
from app.routers import api_conversations
from app.services import consent_service, conversation

PRIMARY, FALLBACK_MODEL = "primary/model:free", "openrouter/free"
HISTORY = [{"role": "reachy", "text": "今天感覺怎麼樣？"}, {"role": "patient", "text": "我去散步了"}]


# ── the model chain ──

class Response:
    def __init__(self, status=200, content="真好，您走了多久呢？", finish="stop", served="primary/model", tokens=14):
        self.status_code, self.content, self.finish, self.served, self.tokens = status, content, finish, served, tokens

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return {"model": self.served, "usage": {"completion_tokens": self.tokens},
                "choices": [{"finish_reason": self.finish, "message": {"content": self.content}}]}


@pytest.fixture
def openrouter(monkeypatch):
    """A fake OpenRouter: answers[model] is a Response, or a callable returning one; every request is kept."""
    answers, sent = {}, []

    def post(url, timeout, headers, json):
        sent.append({"timeout": timeout, **json})
        answer = answers.get(json["model"], Response())
        return answer() if callable(answer) else answer

    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(config, "LLM_MODEL", PRIMARY)
    monkeypatch.setattr(config, "LLM_FALLBACK_MODEL", FALLBACK_MODEL)
    monkeypatch.setattr(config, "LLM_DEADLINE_SECONDS", 8.0)
    monkeypatch.setattr(conversation.requests, "post", post)
    return types.SimpleNamespace(answers=answers, sent=sent)


def _reply(language="zh-TW"):
    return asyncio.run(conversation.reply_with_metrics(HISTORY, language))


def test_every_request_turns_reasoning_off(openrouter):
    info = {}
    assert conversation._post([{"role": "user", "content": "hi"}], 50, PRIMARY, 3, info) == "真好，您走了多久呢？"
    (request,) = openrouter.sent
    assert request["reasoning"] == {"enabled": False} and request["model"] == PRIMARY and request["timeout"] == 3
    assert info == {"status": 200, "model_served": "primary/model", "tokens": 14, "finish_reason": "stop"}


def test_the_primary_model_answers(openrouter):
    text, metrics = _reply()
    assert text == "真好，您走了多久呢？" and metrics["fallback_used"] is False
    (attempt,) = metrics["attempts"]
    assert attempt["model_requested"] == PRIMARY and attempt["model_served"] == "primary/model"
    assert attempt["status"] == 200 and attempt["finish_reason"] == "stop" and attempt["tokens"] == 14
    assert attempt["usable"] is True and isinstance(attempt["ms"], int) and isinstance(metrics["llm_ms"], int)
    assert openrouter.sent[0]["max_tokens"] == conversation.REPLY_MAX_TOKENS
    assert openrouter.sent[0]["timeout"] == pytest.approx(conversation.CALL_TIMEOUT, abs=0.05)   # min(6 s, 8 s left)


@pytest.mark.parametrize("primary, status, finish", [
    (Response(status=429), 429, None),
    (Response(finish="length", content="我"), 200, "length"),   # spent its tokens thinking: cut off
    (Response(content="The user wants a warm reply."), 200, "stop"),   # planning text, never spoken
    (Response(status=404), 404, None),
])
def test_the_fallback_model_takes_over(openrouter, primary, status, finish):
    openrouter.answers[PRIMARY] = primary
    openrouter.answers[FALLBACK_MODEL] = Response(content="您走了多久呢？", served="some/free-model")
    text, metrics = _reply()
    assert text == "您走了多久呢？" and metrics["fallback_used"] is False
    first, second = metrics["attempts"]
    assert (first["model_requested"], first["status"], first["finish_reason"], first["usable"]) == (
        PRIMARY, status, finish, False)
    assert (second["model_requested"], second["model_served"], second["usable"]) == (
        FALLBACK_MODEL, "some/free-model", True)
    assert [request["model"] for request in openrouter.sent] == [PRIMARY, FALLBACK_MODEL]


def test_the_fixed_line_when_both_models_fail(openrouter):
    openrouter.answers[PRIMARY] = Response(status=429)
    openrouter.answers[FALLBACK_MODEL] = Response(status=503)
    text, metrics = _reply("en")
    assert text == conversation.FALLBACK["en"] and metrics["fallback_used"] is True
    assert [attempt["status"] for attempt in metrics["attempts"]] == [429, 503]


def test_without_a_primary_model_the_fallback_model_is_retried(openrouter, monkeypatch):
    monkeypatch.setattr(config, "LLM_MODEL", "")
    openrouter.answers[FALLBACK_MODEL] = iter([Response(content=""), Response()]).__next__
    text, metrics = _reply()
    assert text == "真好，您走了多久呢？"
    assert [attempt["model_requested"] for attempt in metrics["attempts"]] == [FALLBACK_MODEL, FALLBACK_MODEL]


def test_no_key_means_no_call_and_the_fixed_line(openrouter, monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    text, metrics = _reply()
    assert text == conversation.FALLBACK["zh-TW"] and metrics["fallback_used"] is True
    assert metrics["attempts"] == [] and openrouter.sent == []


def _timed_reply(release: threading.Event):
    async def run():
        started = time.monotonic()
        try:
            result = await conversation.reply_with_metrics(HISTORY, "zh-TW")
            return result, time.monotonic() - started
        finally:
            release.set()   # let the abandoned call's thread finish so the loop can close
    return asyncio.run(run())


def test_a_hanging_model_never_holds_the_robot_past_the_deadline(openrouter, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(config, "LLM_DEADLINE_SECONDS", 0.8)
    openrouter.answers[PRIMARY] = lambda: release.wait(5) and Response()
    (text, metrics), elapsed = _timed_reply(release)
    assert text == conversation.FALLBACK["zh-TW"] and metrics["fallback_used"] is True
    assert elapsed < 1.3 and metrics["llm_ms"] < 1300
    # The whole 0.8 s went to the primary model: no time was left to start the fallback model.
    assert [attempt["status"] for attempt in metrics["attempts"]] == ["timeout"]
    assert openrouter.sent[0]["timeout"] == pytest.approx(0.8, abs=0.05)


def test_a_slow_primary_leaves_the_rest_of_the_deadline_to_the_fallback(openrouter, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(config, "LLM_DEADLINE_SECONDS", 2.0)
    monkeypatch.setattr(conversation, "CALL_TIMEOUT", 0.6)
    openrouter.answers[PRIMARY] = lambda: release.wait(5) and Response()
    openrouter.answers[FALLBACK_MODEL] = Response(content="您走了多久呢？", served="some/free-model")
    (text, metrics), elapsed = _timed_reply(release)
    assert text == "您走了多久呢？" and metrics["fallback_used"] is False and elapsed < 1.5
    assert [attempt["status"] for attempt in metrics["attempts"]] == ["timeout", 200]
    assert metrics["attempts"][0]["ms"] == pytest.approx(600, abs=150)


def test_each_call_gets_at_most_the_time_left(openrouter, monkeypatch):
    monkeypatch.setattr(config, "LLM_DEADLINE_SECONDS", 1.2)

    def slow_rate_limit():
        time.sleep(0.4)
        return Response(status=429)

    openrouter.answers[PRIMARY] = slow_rate_limit
    text, metrics = _reply()
    first, second = (request["timeout"] for request in openrouter.sent)
    assert first == pytest.approx(1.2, abs=0.05) and 0.6 < second < 0.85   # min(6 s, what is left)
    assert text == "真好，您走了多久呢？" and metrics["attempts"][1]["usable"] is True


def test_summaries_use_the_same_chain_with_more_room(openrouter):
    openrouter.answers[PRIMARY] = Response(status=429)
    openrouter.answers[FALLBACK_MODEL] = Response(content="MOOD: calm\nSUMMARY: 長者談到散步。\nRISK: none")
    assert asyncio.run(conversation.summarize(HISTORY, "zh-TW")) == ("長者談到散步。", "calm", None)
    assert [request["model"] for request in openrouter.sent] == [PRIMARY, FALLBACK_MODEL]
    assert all(request["max_tokens"] == conversation.SUMMARY_MAX_TOKENS for request in openrouter.sent)
    assert all(request["reasoning"] == {"enabled": False} for request in openrouter.sent)
    assert openrouter.sent[0]["timeout"] == pytest.approx(conversation.SUMMARY_CALL_TIMEOUT, abs=0.05)


def test_a_summary_without_a_summary_line_keeps_the_mood_and_risk(openrouter):
    openrouter.answers[PRIMARY] = openrouter.answers[FALLBACK_MODEL] = Response(content="MOOD: sad\nRISK: self_harm")
    assert asyncio.run(conversation.summarize(HISTORY, "zh-TW")) == (None, "sad", "self_harm")


def test_the_risk_check_uses_the_same_chain_with_a_short_deadline(openrouter):
    openrouter.answers[PRIMARY] = Response(status=429)
    openrouter.answers[FALLBACK_MODEL] = Response(content="SELF_HARM", tokens=3)
    kind, info = asyncio.run(conversation.classify_risk(HISTORY, "zh-TW"))
    assert kind == "self_harm" and info["risk_result"] == "self_harm"
    assert [(attempt["status"], attempt["usable"]) for attempt in info["risk_attempts"]] == [(429, False), (200, True)]
    assert [request["model"] for request in openrouter.sent] == [PRIMARY, FALLBACK_MODEL]
    assert all(request["max_tokens"] == conversation.RISK_MAX_TOKENS for request in openrouter.sent)
    assert all(request["reasoning"] == {"enabled": False} for request in openrouter.sent)
    assert openrouter.sent[0]["timeout"] == pytest.approx(conversation.RISK_CALL_TIMEOUT, abs=0.05)
    assert openrouter.sent[0]["messages"][0]["content"] == conversation.RISK_PROMPT


def test_a_risk_label_cut_off_by_the_token_limit_is_unknown(openrouter):
    openrouter.answers[PRIMARY] = openrouter.answers[FALLBACK_MODEL] = Response(content="SELF", finish="length")
    kind, info = asyncio.run(conversation.classify_risk(HISTORY, "zh-TW"))
    assert kind is None and info["risk_result"] == "unknown"


def test_a_hanging_risk_check_gives_up_at_its_deadline(openrouter, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(conversation, "RISK_DEADLINE_SECONDS", 0.8)
    monkeypatch.setattr(conversation, "RISK_CALL_TIMEOUT", 0.8)
    openrouter.answers[PRIMARY] = lambda: release.wait(5) and Response(content="NONE")

    async def run():
        started = time.monotonic()
        try:
            return await conversation.classify_risk(HISTORY, "zh-TW"), time.monotonic() - started
        finally:
            release.set()

    (kind, info), elapsed = asyncio.run(run())
    assert kind is None and info["risk_result"] == "unknown" and elapsed < 1.3
    assert [attempt["status"] for attempt in info["risk_attempts"]] == ["timeout"]


def test_reply_stays_a_plain_text_wrapper(openrouter):
    assert asyncio.run(conversation.reply(HISTORY, "zh-TW")) == "真好，您走了多久呢？"


# ── patient side: response times ──

A, B = str(uuid.uuid4()), str(uuid.uuid4())
WHEN = datetime(2026, 10, 2, 9, 30, tzinfo=timezone.utc)


def _attempt(model, served, ms, status, usable):
    return {"model_requested": model, "model_served": served, "ms": ms, "status": status,
            "finish_reason": "stop" if usable else None, "tokens": 12 if usable else None, "usable": usable}


def _row(cid, turn_id, role, metrics, text="嗯"):
    # asyncpg hands JSONB back as text.
    return {"conversation_id": uuid.UUID(cid), "turn_id": turn_id, "role": role, "created_at": WHEN,
            "text_chars": len(text), "metrics": None if metrics is None else json.dumps(metrics)}


ROWS = [   # ordered by conversation, then turn, as the query orders them
    _row(A, 1, "reachy", {"robot": {"tts_first_audio_ms": 1500, "tts_total_ms": 4000, "tts_chunks": 2}}),
    _row(A, 2, "patient", {"robot": {"vad_release_ms": 500, "stt_ms": 600, "handover_ms": 700,
                                     "listen_mode": "chat", "segments": 1}}),
    _row(A, 3, "reachy", {"server": {"llm_ms": 1500, "fallback_used": False, "risk": False,
                                     "attempts": [_attempt(PRIMARY, "ling-served", 1500, 200, True)]},
                          "robot": {"round_trip_ms": 1800, "tts_first_audio_ms": 1400, "tts_total_ms": 3000}}),
    _row(A, 4, "patient", {"robot": {"vad_release_ms": 520, "stt_ms": 800, "handover_ms": 900}}),
    _row(A, 5, "reachy", {"server": {"llm_ms": 8000, "fallback_used": True, "risk": False,
                                     "attempts": [_attempt(PRIMARY, None, 300, 429, False),
                                                  _attempt(FALLBACK_MODEL, None, 6000, "timeout", False)]},
                          "robot": {"round_trip_ms": 8300, "tts_first_audio_ms": 1600}}),
    _row(A, 6, "patient", {"robot": {"handover_ms": 650}}),
    _row(A, 7, "reachy", {"server": {"llm_ms": 0, "fallback_used": False, "risk": False, "attempts": []}}),   # bye
    _row(B, 8, "reachy", None),
    _row(B, 9, "patient", None),   # a robot that sent no timings
    _row(B, 10, "reachy", {"server": {"llm_ms": 2000, "fallback_used": False, "risk": False,
                                      "attempts": [_attempt(PRIMARY, "ling-served", 2000, 200, True)]},
                           "robot": {"round_trip_ms": 2300, "tts_first_audio_ms": 1450}}),
]


@pytest.fixture
def patient(monkeypatch):
    from app.main import app

    seen = []

    class Conn:
        def __init__(self, rows):
            self.rows = rows

        async def fetch(self, query, *args):
            seen.append((query, args))
            assert args[0] == 7   # only this patient's conversations
            return list(reversed(self.rows)) if "DESC" in query else list(self.rows)

    class Pool:
        rows = ROWS

        def acquire(self):
            conn = Conn(self.rows)

            class Ctx:
                async def __aenter__(self):
                    return conn

                async def __aexit__(self, *exc):
                    return False
            return Ctx()

    async def get_state(u_id):
        return {"core": {"granted": True, "terms_version": config.TERMS_VERSION}}

    pool = Pool()
    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(api_conversations, "get_pool", lambda: pool)
    monkeypatch.setattr(app, "dependency_overrides", {get_current_user: lambda: {"u_id": 7, "name": "Pearl"}})
    return types.SimpleNamespace(client=TestClient(app), pool=pool, seen=seen, app=app)


def test_summary_gives_median_and_p90_per_stage(patient):
    body = patient.client.get("/api/conversations/metrics/summary").json()
    assert body["days"] == 7 and body["turns"] == 8 and patient.seen[0][1] == (7, 7)
    assert body["stages"] == {
        "vad_release_ms": {"count": 2, "median_ms": 510, "p90_ms": 520},
        "stt_ms": {"count": 2, "median_ms": 700, "p90_ms": 800},
        "handover_ms": {"count": 3, "median_ms": 700, "p90_ms": 900},
        "round_trip_ms": {"count": 3, "median_ms": 2300, "p90_ms": 8300},
        "llm_ms": {"count": 3, "median_ms": 2000, "p90_ms": 8000},   # the goodbye line asked no model
        "tts_first_audio_ms": {"count": 4, "median_ms": 1475, "p90_ms": 1600},
        "tts_total_ms": {"count": 2, "median_ms": 3500, "p90_ms": 4000},
        # 700 + 1800 + 1400 and 900 + 8300 + 1600; conversation B's patient turn had no timings
        "speech_end_to_first_sound_ms": {"count": 2, "median_ms": 7350, "p90_ms": 10800},
    }
    assert body["fallback_rate"] == 0.333
    assert body["models"] == [{"model": "ling-served", "count": 2, "median_ms": 1750},
                              {"model": FALLBACK_MODEL, "count": 1, "median_ms": 6000},
                              {"model": PRIMARY, "count": 1, "median_ms": 300}]


def test_summary_with_no_timings(patient):
    patient.pool.rows = [_row(A, 1, "reachy", None), _row(A, 2, "patient", None)]
    assert patient.client.get("/api/conversations/metrics/summary?days=30").json() == {
        "days": 30, "turns": 0, "stages": {}, "fallback_rate": None, "models": []}


@pytest.mark.parametrize("days, status", [(0, 422), (91, 422), (1, 200), (90, 200)])
def test_metric_windows_are_1_to_90_days(patient, days, status):
    assert patient.client.get(f"/api/conversations/metrics/summary?days={days}").status_code == status
    assert patient.client.get(f"/api/conversations/metrics/turns?days={days}").status_code == status


def test_windows_past_the_transcript_retention_are_cut_to_it(patient):
    # Only risk-flagged patient turns outlive the 30-day purge, so a 90-day window would be a biased sample.
    assert patient.client.get("/api/conversations/metrics/summary?days=90").json()["days"] == 30
    assert patient.client.get("/api/conversations/metrics/turns?days=45").status_code == 200
    assert [args for _, args in patient.seen] == [(7, 30), (7, 30, api_conversations.MAX_METRIC_TURNS)]
    assert patient.client.get("/api/conversations/metrics/summary?days=30").json()["days"] == 30


def test_absurd_stored_timings_cannot_break_the_summary(patient):
    # Rows stored before the device API bounded numbers: they are left out, never a 500.
    huge = {"robot": {"stt_ms": 10 ** 310, "vad_release_ms": 1.7e308, "handover_ms": 1.7e308}}
    patient.pool.rows = [
        _row(A, 1, "patient", huge), _row(A, 2, "reachy", {"robot": {"round_trip_ms": 1.7e308,
                                                                     "tts_first_audio_ms": 1.7e308}}),
        _row(A, 3, "patient", huge), _row(A, 4, "reachy", {"robot": {"round_trip_ms": 1000,
                                                                     "tts_first_audio_ms": 400}}),
        _row(A, 5, "patient", {"robot": {"stt_ms": 86_400_000}}),   # a day exactly is still kept
    ]
    response = patient.client.get("/api/conversations/metrics/summary")
    assert response.status_code == 200
    assert response.json()["stages"] == {
        "stt_ms": {"count": 1, "median_ms": 86_400_000, "p90_ms": 86_400_000},
        "round_trip_ms": {"count": 1, "median_ms": 1000, "p90_ms": 1000},
        "tts_first_audio_ms": {"count": 1, "median_ms": 400, "p90_ms": 400},
    }


def test_turn_timings_for_the_csv_leave_the_words_out(patient):
    items = patient.client.get("/api/conversations/metrics/turns?days=3").json()["items"]
    query, args = patient.seen[0]
    assert args == (7, 3, api_conversations.MAX_METRIC_TURNS) and "LIMIT $3" in query
    assert [item["turn_id"] for item in items] == list(range(10, 0, -1))   # newest first
    assert set(items[0]) == {"conversation_id", "turn_id", "role", "created_at", "text_chars", "metrics"}
    assert items[0]["conversation_id"] == B and items[0]["text_chars"] == 1
    assert items[0]["metrics"]["robot"] == {"round_trip_ms": 2300, "tts_first_audio_ms": 1450}
    assert items[1]["metrics"] is None and items[0]["created_at"].startswith("2026-10-02T09:30")


def test_metric_routes_are_not_taken_for_a_conversation_id(patient):
    paths = [getattr(route, "path", "") for route in patient.app.routes]
    detail = paths.index("/api/conversations/{conversation_id}")
    assert paths.index("/api/conversations/metrics/summary") < detail
    assert paths.index("/api/conversations/metrics/turns") < detail


# ── conversations the robot never closed, and post-chat work that was lost or failed ──

class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


SPOKEN = [{"role": "reachy", "text": "今天感覺怎麼樣？"}, {"role": "patient", "text": "我去散步了"}]


def _sweep(monkeypatch, summary, risk_flag=False, attempts=0):
    """The after-chat sweep and the real after_chat.process over a fake database holding one conversation the robot
    never closed, which the patient spoke in; summarize gives `summary`. Returns (the job, the world: every query,
    the conversation, the alerts enqueued, the notifications written, the transcripts summarised)."""
    from app.jobs import after_chat_job
    from app.services import after_chat, outbox

    cid = uuid.uuid4()
    chat = {"ended_at": None, "risk_flag": risk_flag, "after_chat_state": "done", "after_chat_attempts": attempts,
            "summary": None, "mood": None}
    world = types.SimpleNamespace(cid=cid, chat=chat, calls=[], alerts=[], notes=[], summarised=[])
    unfinished = ("pending", "failed")

    def due(max_attempts):
        return chat["after_chat_state"] == "pending" or (chat["after_chat_state"] == "failed"
                                                         and chat["after_chat_attempts"] < max_attempts)

    class Conn:
        def transaction(self):
            return _Transaction()

        async def fetch(self, query, *args):
            world.calls.append((query, args))
            if query.startswith("UPDATE conversation c SET ended_at"):
                if chat["ended_at"] is not None:
                    return []
                chat.update(ended_at="now", end_reason="abandoned", after_chat_state="pending")
                return [{"conversation_id": cid}]
            if query.startswith("UPDATE conversation SET after_chat_state = CASE"):   # end_stale
                if chat["ended_at"] != "3 days ago" or not due(args[0]) or str(cid) in args[2]:
                    return []
                chat.update(after_chat_state="skipped" if chat["risk_flag"] else "done" if chat["mood"] else "failed",
                            mood=chat["mood"] or "unknown",
                            after_chat_attempts=max(chat["after_chat_attempts"], args[0]))
                return [{"conversation_id": cid, "u_id": 7, "after_chat_state": chat["after_chat_state"]}]
            if query.startswith("SELECT conversation_id, u_id FROM conversation"):
                fresh = chat["ended_at"] != "3 days ago"
                return [{"conversation_id": cid, "u_id": 7}] if due(args[0]) and fresh else []
            if "FROM conversation_turn" in query:
                return list(SPOKEN)
            raise AssertionError(query)

        async def fetchrow(self, query, *args):   # the claim
            world.calls.append((query, args))
            assert "SET after_chat_attempts = after_chat_attempts + 1" in query and args[0] == str(cid)
            if chat["after_chat_state"] not in unfinished or chat["after_chat_attempts"] >= args[2]:
                return None
            chat.update(after_chat_attempts=chat["after_chat_attempts"] + 1, after_chat_state="pending")
            return {"language": "zh-TW", "risk_flag": chat["risk_flag"], "followup_memory_id": None,
                    "started_at": None, "after_chat_attempts": chat["after_chat_attempts"], "refundable": True}

        async def fetchval(self, query, *args):
            world.calls.append((query, args))
            if 'SELECT name FROM "user"' in query:
                return "Pearl"
            if "RETURNING after_chat_state" in query:   # a last attempt that never finished
                if chat["after_chat_state"] != "pending" or chat["after_chat_attempts"] < args[2]:
                    return None
                chat["after_chat_state"] = ("skipped" if chat["risk_flag"] else "done" if chat["mood"] is not None
                                            else "failed")
                chat["mood"] = chat["mood"] or "unknown"
                return chat["after_chat_state"]
            assert "AND NOT risk_flag RETURNING" in query and args == (str(cid),)
            if chat["risk_flag"]:
                return None
            chat["risk_flag"] = True
            return cid

        async def execute(self, query, *args):
            world.calls.append((query, args))
            if "SET after_chat_state" in query:
                chat["after_chat_state"] = args[1]
                chat["after_chat_attempts"] -= args[2]
            elif "SET summary" in query:
                chat.update(summary=args[1], mood=args[2])
            elif "SET mood = 'unknown'" in query:
                chat["mood"] = chat["mood"] or "unknown"
            elif "INSERT INTO notification" in query:
                world.notes.append(args)
            else:
                raise AssertionError(query)

    class Pool:
        def acquire(self):
            class Ctx:
                async def __aenter__(self):
                    return Conn()

                async def __aexit__(self, *exc):
                    return False
            return Ctx()

    async def get_state(u_id):   # check-ins on, memory off: the summary path
        return {scope: {"granted": True, "terms_version": config.TERMS_VERSION}
                for scope in ("core", "cloud_voice", "conversation_analysis", "robot_microphone", "safety_alerts")}

    async def summarize(history, language, info=None):
        world.summarised.append(history)
        if info is not None:
            info["reason"] = "ok" if summary[0] else "unavailable"
        return summary

    async def enqueue_to_contacts(conn, u_id, **kwargs):
        world.alerts.append({"u_id": u_id, **kwargs})
        return 1

    monkeypatch.setattr(after_chat_job, "get_pool", lambda: Pool())
    monkeypatch.setattr(after_chat, "get_pool", lambda: Pool())
    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(conversation, "summarize", summarize)
    monkeypatch.setattr(outbox, "enqueue_to_contacts", enqueue_to_contacts)
    return after_chat_job, world


def test_abandoned_conversations_are_closed_by_their_last_turn_and_summarised_in_the_same_run(monkeypatch):
    job, world = _sweep(monkeypatch, ("散步", "happy", None))
    asyncio.run(job.run_after_chat_sweep())
    close, args = world.calls[0]
    assert "ended_at IS NULL" in close and "'abandoned'" in close and "after_chat_state = 'pending'" in close
    # Counted from the last turn (or the start), so a live conversation is never closed under the patient.
    assert "GREATEST(c.started_at" in close and "MAX(t.created_at)" in close and args == (job.ABANDON_MINUTES,)
    assert world.summarised == [SPOKEN]
    assert (world.chat["summary"], world.chat["mood"], world.chat["after_chat_state"]) == ("散步", "happy", "done")
    assert world.alerts == [] and world.notes == []
    asyncio.run(job.run_after_chat_sweep())   # done: nothing more to do
    assert world.summarised == [SPOKEN]


def test_lost_or_failed_post_chat_work_is_retried(monkeypatch):
    """/end works in memory: a restart drops it, and an outage leaves the chat pending or failed (review of 2 Oct).
    Any end reason counts a minute after /end; abandoned ones are done at once."""
    from app.services import after_chat

    job, world = _sweep(monkeypatch, ("散步", "happy", None))
    asyncio.run(job.run_after_chat_sweep())
    ((query, args),) = [call for call in world.calls if call[0].startswith("SELECT conversation_id, u_id")]
    # Failed ones until their attempts run out; pending ones at any count (a last attempt that never finished).
    assert ("after_chat_state = 'pending' OR (after_chat_state = 'failed' AND after_chat_attempts < $1)"
            in query)
    assert "end_reason = 'abandoned' OR ended_at < NOW() - INTERVAL '1 minute'" in query
    assert "ended_at > NOW() - make_interval(days => $3)" in query   # older ones: end_stale
    assert args == (after_chat.MAX_ATTEMPTS, job.BATCH, after_chat.RETRY_DAYS)


@pytest.mark.parametrize("risk_flag, noted", [(False, 1), (True, 0)])
def test_post_chat_work_that_fails_on_its_last_try_is_never_silent(monkeypatch, risk_flag, noted):
    from app.services import after_chat

    job, world = _sweep(monkeypatch, (None, "unknown", None), risk_flag, attempts=after_chat.MAX_ATTEMPTS - 1)
    asyncio.run(job.run_after_chat_sweep())
    assert world.chat["summary"] is None and world.chat["mood"] == "unknown"
    assert world.chat["after_chat_attempts"] == after_chat.MAX_ATTEMPTS   # not picked again
    assert len(world.notes) == noted and world.alerts == []
    assert world.summarised == ([] if risk_flag else [SPOKEN])   # a flagged chat makes no model call
    if noted:
        assert world.notes[0][:2] == (7, "safety_check_incomplete") and "safety check" in world.notes[0][2]
    asyncio.run(job.run_after_chat_sweep())
    assert len(world.notes) == noted


@pytest.mark.parametrize("risk_flag, noted", [(False, 1), (True, 0)])
def test_a_last_attempt_that_never_finished_is_ended_by_the_next_sweep(monkeypatch, risk_flag, noted):
    """A restart (or an error) during the last attempt leaves the chat 'pending' with no attempts left: the sweep
    still picks it up and ends it, never silently, without another model call."""
    from app.services import after_chat

    job, world = _sweep(monkeypatch, ("散步", "happy", None), risk_flag, attempts=after_chat.MAX_ATTEMPTS)
    world.chat.update(ended_at="earlier", end_reason="dropped", after_chat_state="pending")
    asyncio.run(job.run_after_chat_sweep())
    assert world.summarised == [] and world.chat["mood"] == "unknown"
    assert world.chat["after_chat_state"] == ("skipped" if risk_flag else "failed")
    assert [note[1] for note in world.notes] == ["safety_check_incomplete"] * noted and world.alerts == []
    asyncio.run(job.run_after_chat_sweep())   # ended: not picked again
    assert len(world.notes) == noted


@pytest.mark.parametrize("risk_flag, alerted", [(False, 1), (True, 0)])
def test_an_abandoned_conversation_whose_summary_finds_risk_alerts_once(monkeypatch, risk_flag, alerted):
    job, world = _sweep(monkeypatch, ("長者表達了想自傷的念頭。", "sad", "self_harm"), risk_flag)
    asyncio.run(job.run_after_chat_sweep())
    assert len(world.alerts) == alerted
    if alerted:
        (alert,) = world.alerts
        assert alert["u_id"] == 7 and alert["kind"] == "safety_alert" and alert["contact_flag"] is None
        assert alert["dedupe_prefix"] == f"safety_alert:{world.cid}:summary"
        assert "Pearl" in alert["messages"][0]["text"] and "長者表達了想自傷的念頭" in alert["messages"][0]["text"]
    world.chat["after_chat_state"] = "pending"   # picked up once more (a retry): still one alert
    asyncio.run(job.run_after_chat_sweep())
    assert len(world.alerts) == alerted


@pytest.mark.parametrize("state, attempts, risk_flag, mood, ended, noted", [
    ("pending", 0, False, None, "failed", 1),      # the app was down for days: the check never ran
    ("failed", 1, False, None, "failed", 1),       # failed, with attempts left that were never used
    ("pending", 3, False, "happy", "done", 0),     # saved, only the state write was lost
    ("pending", 0, True, None, "skipped", 0),      # family were alerted by an earlier layer
])
def test_post_chat_work_still_unfinished_after_two_days_is_ended_never_silently(monkeypatch, state, attempts,
                                                                              risk_flag, mood, ended, noted):
    from app.services import after_chat

    job, world = _sweep(monkeypatch, ("散步", "happy", None), risk_flag, attempts=attempts)
    world.chat.update(ended_at="3 days ago", end_reason="finished", after_chat_state=state, mood=mood)
    asyncio.run(job.run_after_chat_sweep())
    assert world.summarised == [] and world.alerts == []   # no model call that late
    assert world.chat["after_chat_state"] == ended and world.chat["mood"] == (mood or "unknown")
    assert world.chat["after_chat_attempts"] == after_chat.MAX_ATTEMPTS
    assert [note[1] for note in world.notes] == ["safety_check_incomplete"] * noted
    asyncio.run(job.run_after_chat_sweep())   # ended: never again
    assert len(world.notes) == noted


def test_a_failed_attempt_that_used_up_the_attempts_is_not_told_twice(monkeypatch):
    from app.services import after_chat

    job, world = _sweep(monkeypatch, ("散步", "happy", None), attempts=after_chat.MAX_ATTEMPTS)
    world.chat.update(ended_at="3 days ago", end_reason="finished", after_chat_state="failed", mood="unknown")
    asyncio.run(job.run_after_chat_sweep())   # its last attempt told family already
    assert world.chat["after_chat_state"] == "failed" and world.notes == [] and world.summarised == []


def test_one_failed_conversation_does_not_stop_the_sweep(monkeypatch):
    from app.jobs import after_chat_job
    from app.services import after_chat

    ids, processed = [uuid.uuid4(), uuid.uuid4()], []

    class Conn:
        def transaction(self):
            return _Transaction()

        async def fetch(self, query, *args):
            if query.startswith("UPDATE"):
                return []
            return [{"conversation_id": cid, "u_id": 7} for cid in ids]

    class Pool:
        def acquire(self):
            class Ctx:
                async def __aenter__(self):
                    return Conn()

                async def __aexit__(self, *exc):
                    return False
            return Ctx()

    async def process(cid, u_id):
        processed.append(cid)
        if len(processed) == 1:
            raise RuntimeError("database went away")
        return "done"

    monkeypatch.setattr(after_chat_job, "get_pool", lambda: Pool())
    monkeypatch.setattr(after_chat, "process", process)
    asyncio.run(after_chat_job.run_after_chat_sweep())
    assert processed == [str(cid) for cid in ids]
