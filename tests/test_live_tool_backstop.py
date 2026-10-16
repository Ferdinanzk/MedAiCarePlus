"""The live relay's tool backstop (services/live_session.py with services/tool_intents.py): a clear request to dance,
to stop or to look runs its tool when GPT-Live has not asked for it in time, and GPT-Live is told what happened.
The regression case is the chat of 14 Oct 2026 at 10:04 (「你可以看到我嗎」 got 「我現在看不到你」, 「你可以跳舞嗎」 got
「可以啊，我現在就跳，你想看嗎？」, no tool either time). The patient's own word that the medicine is taken
(「我吃完藥了」) is such a request too (agent tool fixes, Task 4): GPT-Live praised it itself and the claim often never
reached record_medication_taken. The harness is tests/test_live_relay_chat.py's; the registry is the real ToolRegistry
over a fake robot link and a fake care backend."""

import asyncio
import types

import pytest

from app import config
from app.services import gpt_live_config, live_session, tool_intents
from app.services.tool_registry import ToolError, ToolRegistry
from tests import test_live_relay_chat as relay_chat
from tests.test_live_relay_chat import _begin, _live, _over, _reachy, _said, _sound, _step

world = relay_chat.world   # the harness's fixture, registered in this module too

# check_medication_due's answers (ServerCareBackend.check_medication_due) and record_medication_taken's
DUE_ALLOWED = {"status": "pending_allowed", "medication_id": "dose-12", "medication_name": "Allegra",
               "med_name": "Allegra", "last_taken_time": None, "next_allowed_time": None, "doses": []}
ASKED = {"status": "pending_confirmation", "medication_id": "dose-12", "medication_name": "Allegra",
         "request_id": "r-1", "family_asked": True, "source": "patient_claim"}


def _due(status):
    return {**DUE_ALLOWED, "status": status}


class Link:
    """The robot: dances start at once, the camera gives a frame."""

    def __init__(self):
        self.dances, self.stops = [], 0

    async def dance(self, name, repeat):
        self.dances.append((name, repeat))
        return {"status": "started", "dance": name or "groovy_sway_and_roll", "repeat": repeat}

    async def stop_dance(self):
        self.stops += 1
        return {"status": "cancelled"}

    async def capture_jpeg(self):
        return b"jpeg", {"width": 640, "height": 480}

    def close(self):
        pass


class Backend:
    u_id, conversation_id, language = 7, "c-1", "zh-TW"

    def __init__(self, available=True, due=None, record=None):
        self.available, self.questions = available, []
        # the medication tools (tool_registry.ServerCareBackend): what the due check and the record answer (an
        # exception is raised), and the calls in order; `gate`, when set, holds the due check until it is set
        self.due = due if due is not None else DUE_ALLOWED
        self.record = record if record is not None else ASKED
        self.calls: list[tuple] = []
        self.gate: asyncio.Event | None = None

    async def check_medication_due(self):
        self.calls.append(("check_medication_due",))
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.due, Exception):
            raise self.due
        return dict(self.due)

    async def record_medication_taken(self, medication_id, patient_confirmed, medication_name, session_id):
        self.calls.append(("record_medication_taken", medication_id, patient_confirmed, medication_name, session_id))
        if isinstance(self.record, Exception):
            raise self.record
        return dict(self.record)

    async def describe_view(self, jpeg, question):
        self.questions.append(question)
        if not self.available:
            return {"available": False, "reason": "vision_off", "description": None, "answer": None}
        return {"available": True, "description": "一位穿藍色上衣的人坐在桌前。", "answer": "看得到你", "people_visible": 1}

    async def close(self):
        pass


async def _with_tools(world, backend=None):
    relay, running = await _begin(world)
    link = Link()
    relay.tools = ToolRegistry(link, backend or Backend())
    world.clients[-1].requested = []   # what GPT-Live asked for (agent_bridge.AgentLiveClient.requested)
    return relay, running, link


async def _end(world, running):
    world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
    await _over(world, running)


@pytest.fixture(autouse=True)
def tools_on(monkeypatch):
    monkeypatch.setattr(config, "LIVE_TOOLS", True)


def test_can_you_dance_dances_when_gpt_live_only_asks_back(world):
    async def scenario():
        relay, running, link = await _with_tools(world)
        await _live(world, _said("你可以跳舞嗎"))
        await _live(world, _sound(), _reachy("可以啊，我現在就跳，你想看嗎？"))
        await _step(world, relay, live_session.INTENT_REPLY_GRACE_SECONDS - 0.5)
        assert link.dances == []                           # GPT-Live still has time to ask for it itself
        await _step(world, relay, 1.0)
        assert link.dances == [(None, 1)]                  # the robot dances: a move it picks
        assert world.clients[-1].commentaries[-1] == live_session.INTENT_LINES["dance"]["zh-TW"]
        await _end(world, running)

    asyncio.run(scenario())


def test_gpt_lives_own_dance_call_leaves_the_backstop_out_and_a_late_one_shares_its_run(world):
    async def scenario():
        relay, running, link = await _with_tools(world)
        await _live(world, _said("跳舞給我看"), _sound(), _reachy("好，我來跳"))
        world.clients[-1].requested.append("dance")        # GPT-Live delegated the dance, which runs
        await relay.tools.execute("dance", {"dance": None, "repeat": None})
        await _step(world, relay, live_session.INTENT_WAIT_SECONDS + 1)
        assert link.dances == [(None, 1)] and relay._intent is None   # no second one from the backstop
        commentaries = list(world.clients[-1].commentaries)

        await _step(world, relay, 3.0)                     # the next turn: GPT-Live says it but asks for nothing
        await _live(world, _said("再跳一次"), _sound(), _reachy("好，再跳一次"))
        await _step(world, relay, live_session.INTENT_REPLY_GRACE_SECONDS + 0.5)
        assert link.dances == [(None, 1), (None, 1)]       # 「再跳一次」 after a dance: another
        late = await relay.tools.execute("dance", {"dance": None, "repeat": None})   # its delegation, late
        assert late["ok"] and link.dances == [(None, 1), (None, 1)]   # one dance for the turn, not two
        assert world.clients[-1].commentaries == commentaries + [live_session.INTENT_LINES["dance"]["zh-TW"]]
        await _end(world, running)

    asyncio.run(scenario())


def test_can_you_see_me_looks_through_the_camera_and_gpt_live_is_told_what_it_saw(world):
    async def scenario():
        backend = Backend()
        relay, running, link = await _with_tools(world, backend)
        await _live(world, _said("Rishi，你可以看到我嗎"), _sound(), _reachy("我現在看不到你"))
        await _step(world, relay, live_session.INTENT_REPLY_GRACE_SECONDS + 0.5)
        assert backend.questions == ["Rishi，你可以看到我嗎"]
        assert world.clients[-1].commentaries[-1] == "我用相機看了一下：看得到你 一位穿藍色上衣的人坐在桌前。"
        await _end(world, running)

    asyncio.run(scenario())


def test_with_the_cloud_vision_off_the_look_runs_but_nothing_is_said(world):
    async def scenario():
        backend = Backend(available=False)
        relay, running, link = await _with_tools(world, backend)
        before = len(world.clients[-1].commentaries)
        await _live(world, _said("你看得到我嗎"))
        await _step(world, relay, live_session.INTENT_WAIT_SECONDS + 0.5)   # GPT-Live said nothing at all
        assert backend.questions == ["你看得到我嗎"] and len(world.clients[-1].commentaries) == before
        await _end(world, running)

    asyncio.run(scenario())


def test_a_stop_with_nothing_running_says_nothing(world, monkeypatch):
    async def scenario():
        relay, running, link = await _with_tools(world)

        async def idle():
            link.stops += 1
            return {"status": "idle"}

        monkeypatch.setattr(link, "stop_dance", idle)
        before = len(world.clients[-1].commentaries)
        await _live(world, _said("不要跳舞了"))
        await _step(world, relay, live_session.INTENT_WAIT_SECONDS + 0.5)
        assert link.stops == 1 and len(world.clients[-1].commentaries) == before
        await _end(world, running)

    asyncio.run(scenario())


def test_the_line_waits_out_a_screening_hold(world):
    async def scenario():
        relay, running, link = await _with_tools(world)
        before = len(world.clients[-1].commentaries)
        await _live(world, _said("你可以跳舞嗎"))
        later = world.clock.now + live_session.INTENT_WAIT_SECONDS + 0.5
        relay._hit_at = world.clock.now                    # a screening hit holds everything (the ticker's
        relay._intent_due(later)                           # confirmation is left out here)
        await asyncio.sleep(0)
        assert link.dances == [] and relay._intent is not None
        relay._hit_at = None                               # cleared: the request still runs
        relay._intent_due(later)
        await relay_chat._settle()
        assert link.dances == [(None, 1)] and len(world.clients[-1].commentaries) == before + 1
        await _end(world, running)

    asyncio.run(scenario())


def test_a_bare_stop_stops_a_running_dance(world):
    async def scenario():
        relay, running, link = await _with_tools(world)
        await relay.tools.execute("dance", {"dance": "simple_nod", "repeat": 2})   # GPT-Live's dance runs
        await _live(world, _said("停"))
        await _step(world, relay, live_session.INTENT_WAIT_SECONDS + 0.5)
        assert link.stops == 1 and not relay.tools.dancing()
        assert world.clients[-1].commentaries[-1] == live_session.INTENT_LINES["stop_dance"]["zh-TW"]
        await _end(world, running)

    asyncio.run(scenario())


@pytest.mark.parametrize("words", ["我年輕的時候很喜歡跳舞", "我昨天看到一隻很可愛的狗", "停"])
def test_talk_about_dancing_or_seeing_and_a_stop_with_no_dance_run_nothing(world, words):
    async def scenario():
        backend = Backend()
        relay, running, link = await _with_tools(world, backend)
        await _live(world, _said(words), _sound(), _reachy("聽起來真好"))
        await _step(world, relay, live_session.INTENT_WAIT_SECONDS + 1)
        assert link.dances == [] and link.stops == 0 and backend.questions == []
        await _end(world, running)

    asyncio.run(scenario())


def test_without_the_agents_tools_there_is_no_backstop(world, monkeypatch):
    async def scenario():
        relay, running, link = await _with_tools(world)
        monkeypatch.setattr(config, "LIVE_TOOLS", False)
        await _live(world, _said("你可以跳舞嗎"))
        await _step(world, relay, live_session.INTENT_WAIT_SECONDS + 1)
        assert link.dances == [] and relay._intent is None
        await _end(world, running)

    asyncio.run(scenario())


# ── the request forms (services/tool_intents.py) ──

@pytest.mark.parametrize("words, intent", [
    ("你可以看到我嗎", "look"), ("Rishi，你可以看到我嗎", "look"), ("你看得到我穿什麼衣服嗎", "look"), ("看得到我嗎", "look"),
    ("你看看我", "look"), ("你看不到我嗎", "look"), ("can you see me?", "look"), ("what do you see", "look"),
    ("你可以跳舞嗎", "dance"), ("跳舞給我看", "dance"), ("你会跳舞吗", "dance"), ("要不要跳個舞", "dance"),
    ("再跳一支舞", "dance"), ("跳舞吧", "dance"), ("can you dance?", "dance"), ("dance for me", "dance"),
    ("日希，可以跳舞嗎", "dance"), ("我想看你跳舞", "dance"), ("please dance", "dance"),
    ("stop dancing", "stop_dance"), ("不要跳舞了", "stop_dance"),
    ("我看看我的藥", None), ("我昨天看到一隻很可愛的狗", None), ("你有看到新聞嗎", None), ("我看不到你", None),
    ("我年輕的時候很喜歡跳舞", None), ("我可以跳舞嗎", None), ("我們要不要去跳舞", None), ("你喜歡跳舞嗎", None),
    ("你昨天跳舞了嗎", None), ("I love dancing", None), ("I saw a dog", None), ("請問我今天要吃什麼藥", None),
    ("現在幾點了", "time"), ("Richy,現在幾點了", "time"), ("東京現在幾點", "time"), ("what time is it in Tokyo", "time"),
    ("今天星期幾", "time"), ("今天幾號", "time"), ("what's the date today", "time"), ("幫我上網查一下 Reachy Mini", "search"),
    ("搜尋最新的AI新聞", "search"), ("今天AI發生了什麼事", "search"), ("search the web for Reachy Mini", "search"),
    ("what is the latest AI news", "search"), ("look up the latest OpenAI news", "search"),
    ("最新的人形機器人新聞", "search"), ("what happened today in AI", "search"),
    ("我八點要吃藥", None), ("幫我查一下我今天要吃什麼藥", None), ("查一下天氣", None), ("我昨天在網路上看到一隻狗", None),
    ("我在網路上找到一張照片", None), ("一分鐘後提醒我吃藥", None), ("你幾點來的", None),
    ("停", None), ("再跳一次", None), ("他會跳舞嗎", None), ("孫子可以跳舞嗎", None), ("你跳舞跳得很好", None),
    ("你跳舞的時候好可愛", None), ("他看得到我嗎", None), ("他看看我", None), ("can you see what I mean", None),
    ("dance music is nice", None),
])
def test_only_clear_requests_are_tool_intents(words, intent):
    assert tool_intents.detect(words) == intent


def test_what_time_is_it_gets_the_clock_when_gpt_live_answers_from_its_stale_start_time(world):
    async def scenario():
        relay, running, link = await _with_tools(world)
        await _live(world, _said("Richy,現在幾點了"), _sound(), _reachy("現在下午一點五十三分。"))
        await _step(world, relay, live_session.INTENT_REPLY_GRACE_SECONDS + 0.5)
        line = world.clients[-1].commentaries[-1]
        assert line.startswith("我看了一下時鐘：現在是星期") and "點" in line
        await _end(world, running)

    asyncio.run(scenario())


def test_search_the_web_runs_the_search_when_gpt_live_only_promises_to(world, monkeypatch):
    from app.services import ai_settings, web_search
    searched = []
    monkeypatch.setattr(ai_settings, "live_target", lambda snapshot=None: ai_settings.LiveTarget("sk", "gpt-live-1", "marin"))
    monkeypatch.setattr(web_search, "search", lambda query, language, api_key: searched.append(query) or {
        "status": "ok", "query": query, "answer": "Reachy Mini 推出了應用商店。", "sources": [], "queries": [query], "searched": True})

    async def scenario():
        relay, running, link = await _with_tools(world)
        await _live(world, _said("幫我上網查一下 Reachy Mini 的最新消息"), _sound(), _reachy("好，我去查一下。"))
        await _step(world, relay, live_session.INTENT_REPLY_GRACE_SECONDS + 0.5)
        for _ in range(10):
            await relay_chat._settle()
        assert searched == ["幫我上網查一下 Reachy Mini 的最新消息"]
        assert world.clients[-1].commentaries[-1] == "我上網查了一下：Reachy Mini 推出了應用商店。"
        await _end(world, running)

    asyncio.run(scenario())


def test_dance_again_is_a_request_only_after_a_dance_in_this_chat():
    assert tool_intents.detect("再跳一次", danced=True) == "dance"
    assert tool_intents.detect("再跳一次") is None


@pytest.mark.parametrize("words", ["停", "夠了", "stop", "好了"])
def test_while_dancing_a_bare_stop_is_a_stop(words):
    assert tool_intents.detect(words, dancing=True) == "stop_dance"


def test_a_new_turn_lets_gpt_live_dance_again_after_a_backstop_dance():
    async def scenario():
        link = Link()
        registry = ToolRegistry(link, types.SimpleNamespace())
        await registry.run_intent("dance", {"dance": None, "repeat": None})
        await registry.execute("dance", {"dance": None, "repeat": None})   # the same turn's late call: shared
        registry.forget_intents()                                         # 「再跳一次」
        await registry.execute("dance", {"dance": "simple_nod", "repeat": None})
        return link.dances

    assert asyncio.run(scenario()) == [(None, 1), ("simple_nod", 1)]


# ── the patient's own word that the medicine is taken (took_medicine; agent tool fixes, Task 4) ──

@pytest.mark.parametrize("words", [
    "我吃完藥了", "我吃過藥了", "藥吃了", "我已經吃藥了", "I took my medicine", "I've taken my pills",
    "我吃完药了", "我剛剛吃藥了", "藥吃好了", "我已經吃過藥了", "日希，我吃完藥了！", "我把藥吃了", "吃完藥了", "我吃藥了",
    "我吃完藥了，你呢？", "我吃完藥了，今天好累", "Reachy, I already took my medication.", "yes, I have taken my pills",
])
def test_the_patients_own_finished_intake_is_a_claim(words):
    assert tool_intents.detect(words) == "took_medicine"


@pytest.mark.parametrize("words", [
    # another person, the future or a plan, a negation, a question, food (the plan's table)
    "他吃完藥了", "我八點要吃藥", "我等一下吃藥", "我還沒吃藥", "我吃藥了嗎", "我吃完飯了",
    "我媽媽吃完藥了", "你吃完藥了嗎", "我要吃藥了", "我去吃藥了", "我沒有吃藥", "我忘了吃藥", "藥還沒吃",
    "吃藥了", "把藥吃了",                        # "medicine time!", "take your medicine!", said to someone
    "我有吃藥",                                 # "I'm on medication"
    "我吃完藥了？", "我吃完藥了，對不對？", "我是不是吃過藥了",
    "我吃完藥了再跟你說", "我吃完藥就去睡覺",      # 吃完藥 as a "when": the claim must end its clause
    "我昨天吃藥了", "昨天我吃完藥了", "早上我吃過藥了",   # another time: the due check would read today's dose
    "我的藥吃完了",                             # ran out
    "did I take my medicine?", "have I taken my pills", "I haven't taken my pills", "I didn't take my medicine",
    "I will take my medicine", "I'm going to take my pills", "he took his medicine", "I took my medicine?",
    "I took my medicine yesterday", "I finished my dinner",
])
def test_no_claim_for_another_person_the_future_a_negation_a_question_or_food(words):
    assert tool_intents.detect(words) is None


async def _claim(world, backend, words="我吃完藥了", reply="好棒！您越來越健康了！"):
    """The patient says `words`, GPT-Live answers `reply` without a tool, and the backstop's grace runs out."""
    relay, running, link = await _with_tools(world, backend)
    await _live(world, _said(words), _sound(), _reachy(reply))
    await _step(world, relay, live_session.INTENT_REPLY_GRACE_SECONDS + 0.5)
    for _ in range(5):
        await relay_chat._settle()
    return relay, running


def test_i_took_my_medicine_asks_family_to_confirm_when_gpt_live_only_praises_it(world):
    async def scenario():
        backend = Backend()
        relay, running, link = await _with_tools(world, backend)
        await _live(world, _said("我吃完藥了"), _sound(), _reachy("好棒！您越來越健康了！"))
        await _step(world, relay, live_session.INTENT_REPLY_GRACE_SECONDS - 0.5)
        assert backend.calls == []                         # GPT-Live still has time to hand it to its backend
        await _step(world, relay, 1.0)
        for _ in range(5):
            await relay_chat._settle()
        # the due check first, then the request to family for the dose it allows, on the patient's word
        assert backend.calls == [("check_medication_due",), ("record_medication_taken", "dose-12", True, None, None)]
        line = world.clients[-1].commentaries[-1]
        assert line == live_session.INTENT_LINES["took_medicine"]["zh-TW"]
        assert gpt_live_config.CELEBRATION["zh-TW"] in line and "家人確認" in line and "記錄" not in line
        await _end(world, running)

    asyncio.run(scenario())


@pytest.mark.parametrize("status, key, praised", [
    ("taken", "took_medicine_taken", True), ("pending_confirmation", "took_medicine_waiting", True),
    ("refused_too_soon", "took_medicine_refused", False), ("refused_max_reached", "took_medicine_refused", False),
    ("refused_not_due", "took_medicine_refused", False), ("refused_expired", "took_medicine_refused", False),
    ("none", "took_medicine_none", False), ("skipped", "took_medicine_none", False),
])
def test_a_dose_the_due_check_does_not_allow_is_never_recorded_and_gpt_live_says_why(world, status, key, praised):
    async def scenario():
        backend = Backend(due=_due(status))
        relay, running = await _claim(world, backend)
        assert backend.calls == [("check_medication_due",)]   # no request to family for it
        line = world.clients[-1].commentaries[-1]
        expected = live_session.INTENT_LINES[key]["zh-TW"]
        if key == "took_medicine_refused":
            expected = expected.format(why=live_session.CLAIM_REASONS[status]["zh-TW"])
            assert "沒有記錄" in line
        assert line == expected and (gpt_live_config.CELEBRATION["zh-TW"] in line) == praised
        await _end(world, running)

    asyncio.run(scenario())


@pytest.mark.parametrize("answer, key, praised", [
    ({"status": "refused", "reason": "dose_too_soon", "reply": "現在還不能吃", "family_asked": False},
     "took_medicine_refused", False),
    ({"status": "already_taken", "family_asked": False}, "took_medicine_taken", True),
    ({"status": "already_pending_confirmation", "family_asked": False}, "took_medicine_waiting", True),
    ({"status": "already_skipped", "family_asked": False}, "took_medicine_none", False),
    (ToolError("database_timeout"), "took_medicine_failed", False),
])
def test_gpt_live_says_what_the_record_answered(world, answer, key, praised):
    async def scenario():
        backend = Backend(record=answer)
        relay, running = await _claim(world, backend)
        assert [call[0] for call in backend.calls] == ["check_medication_due", "record_medication_taken"]
        line = world.clients[-1].commentaries[-1]
        expected = live_session.INTENT_LINES[key]["zh-TW"]
        if key == "took_medicine_refused":
            expected = expected.format(why=live_session.CLAIM_REASONS["refused_too_soon"]["zh-TW"])
        assert line == expected and (gpt_live_config.CELEBRATION["zh-TW"] in line) == praised
        await _end(world, running)

    asyncio.run(scenario())


def test_a_due_check_that_fails_records_nothing_and_says_so(world):
    async def scenario():
        backend = Backend(due=ToolError("database_timeout"))
        relay, running = await _claim(world, backend, words="I took my medicine", reply="Great job!")
        assert backend.calls == [("check_medication_due",)]
        assert world.clients[-1].commentaries[-1] == live_session.INTENT_LINES["took_medicine_failed"]["zh-TW"]
        await _end(world, running)

    asyncio.run(scenario())


@pytest.mark.parametrize("tool", ["check_medication_due", "get_medication_schedule", "record_medication_taken"])
def test_gpt_lives_own_delegation_within_the_grace_means_no_second_run(world, tool):
    async def scenario():
        backend = Backend()
        relay, running, link = await _with_tools(world, backend)
        await _live(world, _said("我吃完藥了"), _sound(), _reachy("好，我幫你問問"))
        world.clients[-1].requested.append(tool)          # GPT-Live's backend took the claim
        await _step(world, relay, live_session.INTENT_WAIT_SECONDS + 1)
        assert backend.calls == [] and relay._intent is None
        await _end(world, running)

    asyncio.run(scenario())


def test_gpt_lives_late_calls_share_the_backstops_runs_so_family_are_asked_once(world):
    async def scenario():
        backend = Backend()
        relay, running = await _claim(world, backend)
        assert len(backend.calls) == 2
        late_check = await relay.tools.execute("check_medication_due", {})
        late = await relay.tools.execute("record_medication_taken", {"medication_id": "dose-12",
                                                                     "patient_confirmed": True})
        assert late_check["ok"] and late["ok"] and late["result"]["status"] == "pending_confirmation"
        assert len(backend.calls) == 2                    # one due check, one request to family
        await _end(world, running)

    asyncio.run(scenario())


@pytest.mark.parametrize("words", ["我還沒吃藥", "他吃完藥了", "我等一下吃藥", "我吃藥了嗎"])
def test_words_that_claim_no_intake_run_no_medicine_tool(world, words):
    async def scenario():
        backend = Backend()
        relay, running, link = await _with_tools(world, backend)
        before = len(world.clients[-1].commentaries)
        await _live(world, _said(words), _sound(), _reachy("好的"))
        await _step(world, relay, live_session.INTENT_WAIT_SECONDS + 1)
        assert backend.calls == [] and len(world.clients[-1].commentaries) == before
        await _end(world, running)

    asyncio.run(scenario())


@pytest.mark.parametrize("fragments, claimed", [(["我吃完藥了", "再跟你說"], False), (["我吃完藥了", "，今天好累"], True)])
def test_a_claim_is_judged_on_the_whole_turn_its_words_come_in(world, fragments, claimed):
    # GPT-Live's transcript of the patient comes in fragments: 「我吃完藥了」 alone reads as a claim, but the turn
    # 「我吃完藥了再跟你說」 is a plan
    async def scenario():
        backend = Backend()
        relay, running, link = await _with_tools(world, backend)
        await _live(world, *[_said(fragment) for fragment in fragments])
        await _step(world, relay, live_session.INTENT_WAIT_SECONDS + 0.5)
        for _ in range(5):
            await relay_chat._settle()
        assert bool(backend.calls) == claimed
        await _end(world, running)

    asyncio.run(scenario())


def test_the_chats_end_lets_a_claim_already_running_reach_family(world):
    # Like GPT-Live's own record (tool_registry.FINISH_ON_CLOSE), the backstop's claim is never cut off between the
    # due check and the request when the chat ends
    async def scenario():
        backend = Backend()
        backend.gate = asyncio.Event()
        relay, running, link = await _with_tools(world, backend)
        closing = asyncio.Event()
        close = relay.tools.close

        async def closed():
            closing.set()
            await close()

        relay.tools.close = closed
        await _live(world, _said("我吃完藥了"))
        await _step(world, relay, live_session.INTENT_WAIT_SECONDS + 0.5)   # GPT-Live said nothing: the backstop runs
        assert backend.calls == [("check_medication_due",)]
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await asyncio.wait_for(closing.wait(), 5)          # the chat is over: run() closes its tools
        backend.gate.set()
        await _over(world, running)
        assert backend.calls == [("check_medication_due",), ("record_medication_taken", "dose-12", True, None, None)]

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["zh-TW", "en"])
def test_every_claim_line_is_said_in_both_languages_and_praises_only_a_dose_that_stands(language):
    praised = {"took_medicine", "took_medicine_taken", "took_medicine_waiting"}
    keys = [key for key in live_session.INTENT_LINES if key.startswith("took_medicine")]
    assert len(keys) == 6
    for key in keys:
        line = live_session.INTENT_LINES[key][language].format(why="x")
        assert (gpt_live_config.CELEBRATION[language] in line) == (key in praised)
    assert all(reason[language] for reason in live_session.CLAIM_REASONS.values())


def test_the_backends_completion_flow_records_before_it_applauds():
    # As the camera workflow does: the applause (and with it the praise line) only for a dose that stands
    text = gpt_live_config.BACKEND_INSTRUCTION
    flow = text[text.index("MEDICATION COMPLETION"):text.index("\nEMOTION\n")]
    assert flow.index("record_medication_taken(") < flow.index('trigger_antenna_emote(preset="applaud_cheer")')
    applause = flow[flow.index('trigger_antenna_emote(preset="applaud_cheer")'):]
    assert "pending_confirmation" in applause and "already_taken" in applause
    assert "already_pending_confirmation" in applause and "no applause" in applause
