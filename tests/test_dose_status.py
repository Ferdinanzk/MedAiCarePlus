"""get_today_dose_status: the check-in reply model's tool for today's medicine (services/dose_status.py), the forced
call at the conversation's start and the tool round in a reply (services/conversation.py), and the device API."""

import asyncio
import datetime
import json
import os
import sys
import types
from zoneinfo import ZoneInfo

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

sys.path.insert(0, os.path.dirname(__file__))
import dose_facts  # noqa: E402

from app import config  # noqa: E402
from app.services import conversation, dose_safety, dose_status, schedule  # noqa: E402
from tests.test_conversation import _eventually, _fake_http_session, _openrouter, _start, _turn, robot  # noqa: E402,F401

TZ = ZoneInfo("Asia/Taipei")
NOW = datetime.datetime(2026, 10, 12, 20, 30, tzinfo=TZ)
CALL = {"id": "call_1", "type": "function", "function": {"name": dose_status.TOOL_NAME, "arguments": "{}"}}


def at(hour, minute=0, day=12):
    return datetime.datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def row(intk_id, stats, hour=20, taken_at=None):
    return {"intk_id": intk_id, "intake_stats": stats, "intake_time_stamp": at(hour), "actual_intake_time": taken_at}


# ── the tool's answer ──

def test_the_tool_takes_no_arguments_and_can_be_forced():
    function = dose_status.TOOL["function"]
    assert function["name"] == "get_today_dose_status" and function["parameters"]["properties"] == {}
    assert dose_status.FORCE == {"type": "function", "function": {"name": "get_today_dose_status"}}
    assert set(dose_status.PRECEDENCE) | {"none"} == set(dose_status.STATUSES)


@pytest.mark.parametrize("stats, refused, status", [
    ("taken", None, "taken"),
    ("pending_confirmation", None, "pending_confirmation"),
    ("skipped", None, "skipped"),
    ("pending", None, "pending_allowed"),
    ("missed", None, "pending_allowed"),
    ("pending", dose_safety.DoseTooSoon(last_taken_at=at(19), gap=datetime.timedelta(hours=4)), "refused_too_soon"),
    ("pending", dose_safety.DailyMaxReached(taken=3, limit=3, day=NOW.date()), "refused_max_reached"),
    ("pending", schedule.DoseNotDueYet(at(22)), "refused_not_due"),
    ("missed", dose_safety.DoseExpired(expired_at=at(16)), "refused_expired"),
])
def test_each_dose_gets_its_status(stats, refused, status):
    assert dose_status.dose_status(row(1, stats), refused) == status


def test_a_dose_that_may_be_taken_now_wins_then_waiting_then_taken_then_refusals():
    too_soon = dose_safety.DoseTooSoon(last_taken_at=at(19), gap=datetime.timedelta(hours=4))
    taken = row(1, "taken", taken_at=at(20, 5))
    assert dose_status.summarize([taken, row(2, "pending")], {2: None})["status"] == "pending_allowed"
    assert dose_status.summarize([taken, row(2, "pending_confirmation")], {})["status"] == "pending_confirmation"
    # one medicine taken, another refused only for later: the slot still gets its praise
    assert dose_status.summarize([taken, row(2, "pending")], {2: too_soon})["status"] == "taken"
    assert dose_status.summarize([row(2, "pending")], {2: too_soon})["status"] == "refused_too_soon"
    assert dose_status.summarize([], {}) == {"status": "none", "last_taken_time": None, "next_allowed_time": None}


def test_times_come_from_the_taken_dose_and_the_refusals():
    taken = dose_status.summarize([row(1, "taken", taken_at=at(20, 5))], {})
    assert taken == {"status": "taken", "last_taken_time": "2026-10-12T20:05:00+08:00", "next_allowed_time": None}
    too_soon = dose_safety.DoseTooSoon(last_taken_at=at(19), gap=datetime.timedelta(hours=4))
    assert dose_status.summarize([row(2, "pending")], {2: too_soon}) == {
        "status": "refused_too_soon", "last_taken_time": "2026-10-12T19:00:00+08:00",
        "next_allowed_time": "2026-10-12T23:00:00+08:00"}
    maximum = dose_safety.DailyMaxReached(taken=3, limit=3, day=NOW.date())
    assert dose_status.summarize([row(2, "pending")], {2: maximum})["next_allowed_time"] == "2026-10-13T00:00:00+08:00"
    not_due = schedule.DoseNotDueYet(at(22))
    expected = schedule.local_iso(not_due.due_from)
    assert dose_status.summarize([row(2, "pending", 22)], {2: not_due})["next_allowed_time"] == expected


def test_a_conversation_only_task_reports_the_latest_due_slot_else_the_next():
    rows = [{"intk_id": 1, "intake_time_stamp": at(8)}, {"intk_id": 2, "intake_time_stamp": at(8)},
            {"intk_id": 3, "intake_time_stamp": at(12)}, {"intk_id": 4, "intake_time_stamp": at(20)},
            {"intk_id": 5, "intake_time_stamp": at(20, 3)}]
    assert dose_status.slot_of(rows, at(13)) == [3]
    assert dose_status.slot_of(rows, at(18, 30)) == [4, 5]          # due from two hours early (DOSE_EARLY)
    assert dose_status.slot_of(rows, at(7)) == [1, 2]                # due (early) already at 06:00
    assert dose_status.slot_of(rows, at(5)) == [1, 2]                # nothing due yet: today's first slot
    assert dose_status.slot_of([], at(13)) == []


class Conn:
    """A fake connection: the task's doses, today's doses, their rows and dose_safety's facts (tests/dose_facts)."""

    def __init__(self, task_ids, intakes, meds):
        self.task_ids, self.intakes, self.meds = task_ids, intakes, meds

    async def fetchval(self, query, *args):
        if query == dose_status.TASK_SQL:
            return self.task_ids
        if query == dose_status.CONVERSATION_TASK_SQL:
            return "task-1"
        raise AssertionError(query)

    async def fetch(self, query, *args):
        if query == dose_status.TODAY_SQL:
            return [{"intk_id": i, "intake_time_stamp": r["intake_time_stamp"]}
                    for i, r in sorted(self.intakes.items(), key=lambda item: item[1]["intake_time_stamp"])]
        if query == dose_status.DOSES_SQL:
            return [{"intk_id": i, **{k: r.get(k) for k in ("intake_stats", "intake_time_stamp", "actual_intake_time")}}
                    for i, r in self.intakes.items() if i in args[1]]
        if dose_facts.is_facts(query):
            return dose_facts.rows(args, self.intakes, self.meds)
        raise AssertionError(query)


MEDS = {10: {"med_name": "allegra", "schedule_time": {"morning": True, "night": True}, "min_interval_minutes": None,
             "max_daily_doses": None}}


def test_lookup_reports_the_tasks_doses_judged_by_overdose_protection():
    intakes = {1: {"med_id": 10, "intake_stats": "taken", "intake_time_stamp": at(8), "actual_intake_time": at(8, 10)},
               2: {"med_id": 10, "intake_stats": "pending", "intake_time_stamp": at(20), "actual_intake_time": None}}
    answer = asyncio.run(dose_status.lookup(Conn([2], intakes, MEDS), 7, "task-1", at=at(20, 15)))
    assert answer == {"status": "pending_allowed", "last_taken_time": None, "next_allowed_time": None}
    # taken at 20:10: a second 20:00 dose would be too soon
    intakes[2].update(intake_stats="taken", actual_intake_time=at(20, 10))
    answer = asyncio.run(dose_status.lookup(Conn([2], intakes, MEDS), 7, "task-1", at=at(20, 15)))
    assert answer["status"] == "taken" and answer["last_taken_time"] == "2026-10-12T20:10:00+08:00"


def test_a_conversation_only_task_looks_at_todays_slot():
    intakes = {1: {"med_id": 10, "intake_stats": "taken", "intake_time_stamp": at(8), "actual_intake_time": at(8, 10)},
               2: {"med_id": 10, "intake_stats": "pending", "intake_time_stamp": at(20), "actual_intake_time": None}}
    answer = asyncio.run(dose_status.for_conversation(Conn([], intakes, MEDS), 7, "c-1", at=at(12)))
    assert answer["status"] == "taken"             # 12:00: the 08:00 slot is the latest due
    answer = asyncio.run(dose_status.for_conversation(Conn([], intakes, MEDS), 7, "c-1", at=at(19)))
    assert answer["status"] == "pending_allowed"   # 19:00: the 20:00 dose may start (two hours early)


# ── the prompt ──

def test_the_prompt_praises_a_taken_dose_with_the_fixed_line_and_never_encourages_a_refused_one():
    zh, en = conversation.SYSTEM_PROMPT["zh-TW"], conversation.SYSTEM_PROMPT["en"]
    assert "已經吃過藥了" in zh and "拍拍手" in zh and zh.count("「您越來越健康了！」") == 2
    assert "絕對不要鼓勵他吃藥" in zh and "不要命令、催促或說教" in zh and "不要給醫療建議" in zh
    assert "不要講大道理" in zh and "陪著他" in zh
    assert "has already taken their medicine" in en and en.count('"You are getting healthier and healthier!"') == 2
    assert "never encourage taking medicine" in en and "Never give medical advice" in en
    for status in dose_status.STATUSES:
        assert status in zh and status in en, status


# ── the reply's tool round ──

def _reply_post(monkeypatch, answers):
    """A fake OpenRouter for reply_with_metrics: call n gets answers[n]; every request is recorded."""
    calls = []

    def post(messages, max_tokens, model=None, timeout=None, info=None, temperature=0.7, tools=None,
             tool_choice=None):
        calls.append({"messages": [dict(m) for m in messages], "tools": tools, "tool_choice": tool_choice})
        if info is not None:
            info.update(status=200, model_served=model, finish_reason="stop")
        return answers[min(len(calls), len(answers)) - 1]

    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(conversation, "_post", post)
    monkeypatch.setattr(conversation, "reply_prompt", lambda language: conversation.SYSTEM_PROMPT[language])
    return calls


HISTORY = [{"role": "reachy", "text": "今天感覺怎麼樣？"}, {"role": "patient", "text": "我吃完藥了"}]


def test_the_reply_gets_the_openings_tool_exchange_and_the_tool(monkeypatch):
    calls = _reply_post(monkeypatch, [{"content": "拍拍手！好棒！您越來越健康了！", "tool_calls": None}])
    record = {"call_id": "call_open", "arguments": "{}", "result": {"status": "taken", "last_taken_time": None,
                                                                     "next_allowed_time": None}}
    dose = conversation.DoseTool(exchange=conversation.dose_exchange(record), lookup=None)
    dose.lookup = lambda: asyncio.sleep(0, result={"status": "taken"})
    reply, metrics = asyncio.run(conversation.reply_with_metrics(HISTORY, "zh-TW", dose=dose))
    assert reply == "拍拍手！好棒！您越來越健康了！" and "dose_tool" not in metrics
    messages = calls[0]["messages"]
    assert messages[1]["role"] == "assistant" and messages[1]["tool_calls"][0]["id"] == "call_open"
    assert messages[2] == {"role": "tool", "tool_call_id": "call_open",
                           "content": json.dumps(record["result"], ensure_ascii=False)}
    assert messages[3]["content"] == "今天感覺怎麼樣？" and messages[4] == {"role": "user", "content": "我吃完藥了"}
    assert calls[0]["tools"] == [dose_status.TOOL] and calls[0]["tool_choice"] is None


def test_when_the_model_asks_for_the_tool_it_gets_the_answer_and_is_asked_again(monkeypatch):
    calls = _reply_post(monkeypatch, [{"content": None, "tool_calls": [CALL]},
                                      {"content": "太好了，拍拍手！您越來越健康了！", "tool_calls": None}])
    looked = []

    async def lookup():
        looked.append(1)
        return {"status": "taken", "last_taken_time": "2026-10-12T20:05:00+08:00", "next_allowed_time": None}

    reply, metrics = asyncio.run(conversation.reply_with_metrics(
        HISTORY, "zh-TW", dose=conversation.DoseTool(lookup=lookup)))
    assert reply == "太好了，拍拍手！您越來越健康了！" and looked == [1] and len(calls) == 2
    second = calls[1]
    assert second["tool_choice"] == "none" and second["messages"][-2]["tool_calls"] == [CALL]
    assert second["messages"][-1]["role"] == "tool" and second["messages"][-1]["tool_call_id"] == "call_1"
    assert json.loads(second["messages"][-1]["content"])["status"] == "taken"
    assert metrics["dose_tool"][0]["name"] == dose_status.TOOL_NAME and metrics["dose_tool"][0]["result"]["status"] == "taken"
    assert len(metrics["attempts"]) == 2 and metrics["fallback_used"] is False


def test_a_failed_lookup_answers_unknown_and_the_reply_still_comes(monkeypatch):
    calls = _reply_post(monkeypatch, [{"content": None, "tool_calls": [CALL]}, {"content": "我在這裡陪著您。"}])

    async def lookup():
        raise RuntimeError("database down")

    reply, metrics = asyncio.run(conversation.reply_with_metrics(
        HISTORY, "zh-TW", dose=conversation.DoseTool(lookup=lookup)))
    assert reply == "我在這裡陪著您。"
    assert json.loads(calls[1]["messages"][-1]["content"]) == dose_status.UNAVAILABLE


def test_a_reply_without_dose_context_offers_no_tool(monkeypatch):
    calls = _reply_post(monkeypatch, ["好的。"])
    reply, _ = asyncio.run(conversation.reply_with_metrics(HISTORY, "zh-TW"))
    assert reply == "好的。" and calls[0]["tools"] is None


def test_post_returns_the_whole_message_when_tools_are_offered(monkeypatch):
    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [CALL]}}]}

    sent = []

    def post(*args, **kwargs):
        sent.append(kwargs["json"])
        return Response()

    monkeypatch.setattr(conversation, "_http_session", lambda: _fake_http_session(post))
    answer = conversation._post([{"role": "user", "content": "hi"}], 50, tools=[dose_status.TOOL],
                                tool_choice=dose_status.FORCE)
    assert answer == {"content": None, "tool_calls": [CALL]}
    assert sent[0]["tools"] == [dose_status.TOOL] and sent[0]["tool_choice"] == dose_status.FORCE
    assert conversation._post([{"role": "user", "content": "hi"}], 50) is None   # no tools: text only, as before


# ── the forced call at the start ──

def test_the_opening_forces_the_tool_call_and_keeps_the_models_call_id(monkeypatch):
    calls = _reply_post(monkeypatch, [{"content": None, "tool_calls": [CALL]}])

    async def lookup():
        return {"status": "pending_allowed", "last_taken_time": None, "next_allowed_time": None}

    record = asyncio.run(conversation.open_dose_status("zh-TW", "今天感覺怎麼樣？", lookup))
    assert calls[0]["tool_choice"] == dose_status.FORCE and calls[0]["messages"][-1]["content"] == "今天感覺怎麼樣？"
    assert record["call_id"] == "call_1" and record["forced_by"] == "model"
    assert record["result"]["status"] == "pending_allowed"


def test_without_a_model_the_server_runs_the_lookup_itself(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")

    async def lookup():
        return {"status": "taken", "last_taken_time": None, "next_allowed_time": None}

    record = asyncio.run(conversation.open_dose_status("zh-TW", "今天感覺怎麼樣？", lookup))
    assert record["forced_by"] == "server" and record["result"]["status"] == "taken"
    assert conversation.dose_exchange(record)[1]["tool_call_id"] == record["call_id"]


# ── the device API ──

def test_the_opening_record_is_kept_and_every_reply_gets_it(robot, monkeypatch):
    sent = _openrouter(monkeypatch, robot, dose_open=[{"content": None, "tool_calls": [CALL]}],
                       reply=["今天天氣很好，您想出去走走嗎？"])
    cid = _start(robot)["conversation_id"]
    opening = robot.db.turns[0]
    assert _eventually(lambda: "dose_tool" in ((opening["metrics"] or {}).get("server") or {}))
    record = opening["metrics"]["server"]["dose_tool"]
    assert record["forced_by"] == "model" and record["call_id"] == "call_1"
    assert record["result"] == {"status": "none", "last_taken_time": None, "next_allowed_time": None}
    assert sent.opens[0]["tool_choice"] == dose_status.FORCE

    assert _turn(robot, cid, "我今天去公園散步").status_code == 200
    (reply,) = [request for request in sent if request["kind"] == "reply"]
    roles = [message["role"] for message in reply["messages"]]
    assert roles[:4] == ["system", "assistant", "tool", "assistant"] and roles[-1] == "user"
    assert reply["messages"][1]["tool_calls"][0]["id"] == "call_1" and reply["tools"] == [dose_status.TOOL]
    assert "dose_tool" not in robot.db.turns[-1]["metrics"]["server"]


def test_a_tool_call_in_a_reply_is_kept_on_reachys_turn(robot, monkeypatch):
    sent = _openrouter(monkeypatch, robot, reply=[{"content": None, "tool_calls": [CALL]},
                                                  "太好了，拍拍手！您越來越健康了！"])
    cid = _start(robot)["conversation_id"]
    assert _eventually(lambda: "dose_tool" in ((robot.db.turns[0]["metrics"] or {}).get("server") or {}))
    assert robot.db.turns[0]["metrics"]["server"]["dose_tool"]["forced_by"] == "server"   # no model call came back

    body = _turn(robot, cid, "我剛剛吃完藥了").json()
    assert body["reply"] == "太好了，拍拍手！您越來越健康了！"
    replies = [request for request in sent if request["kind"] == "reply"]
    assert [request["tool_choice"] for request in replies] == [None, "none"]
    server = robot.db.turns[-1]["metrics"]["server"]
    assert server["dose_tool"][0]["result"]["status"] == "none" and len(server["attempts"]) == 2
