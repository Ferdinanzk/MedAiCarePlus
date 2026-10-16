"""The live relay's tool backstop (services/live_session.py with services/tool_intents.py): a clear request to dance
or to look runs its tool when GPT-Live has not asked for it in time, and GPT-Live is told what happened; a stop runs
at once, and no dance starts after it (agent tool fixes, Task 5).
The regression case is the chat of 14 Oct 2026 at 10:04 (「你可以看到我嗎」 got 「我現在看不到你」, 「你可以跳舞嗎」 got
「可以啊，我現在就跳，你想看嗎？」, no tool either time). The patient's own word that the medicine is taken
(「我吃完藥了」) is such a request too (agent tool fixes, Task 4): GPT-Live praised it itself and the claim often never
reached record_medication_taken. The harness is tests/test_live_relay_chat.py's; the registry is the real ToolRegistry
over a fake robot link and a fake care backend."""

import asyncio
import types

import pytest

from app import config
from app.services import gpt_live_config, live_session, tool_intents, tool_registry
from app.services.tool_registry import ToolError, ToolRegistry
from tests import test_live_relay_chat as relay_chat
from tests.test_live_relay_chat import _begin, _live, _over, _reachy, _said, _sound, _step

world = relay_chat.world   # the harness's fixture, registered in this module too

# check_medication_due's answers (ServerCareBackend.check_medication_due: the slot's summary, the first dose in that
# state, and every dose of the slot) and record_medication_taken's
DOSE = {"medication_id": "dose-12", "medication_name": "Allegra", "med_name": "Allegra", "scheduled_time": "08:00",
        "taken_time": None, "status": "pending_allowed", "refusal": None}
SECOND = {**DOSE, "medication_id": "dose-13", "medication_name": "Metformin", "med_name": "Metformin"}
DUE_ALLOWED = {"status": "pending_allowed", "medication_id": "dose-12", "medication_name": "Allegra",
               "med_name": "Allegra", "last_taken_time": None, "next_allowed_time": None, "doses": [DOSE]}
ASKED = {"status": "pending_confirmation", "medication_id": "dose-12", "medication_name": "Allegra",
         "request_id": "r-1", "family_asked": True, "source": "patient_claim"}


def _due(status, *doses):
    """The due check's answer for a slot whose summary is `status`: its doses as given, else Allegra in that state."""
    return {**DUE_ALLOWED, "status": status, "doses": list(doses) or [{**DOSE, "status": status}]}


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

    def __init__(self, available=True, due=None, record=None, records=None):
        self.available, self.questions = available, []
        # the medication tools (tool_registry.ServerCareBackend): what the due check and the record answer (an
        # exception is raised; `records`: a dose's own answer by its id), and the calls in order; `gate`, when set,
        # holds the due check until it is set
        self.due = due if due is not None else DUE_ALLOWED
        self.record = record if record is not None else ASKED
        self.records = records or {}
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
        answer = self.records.get(medication_id, self.record)
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    async def describe_view(self, jpeg, question):
        self.questions.append(question)
        if not self.available:
            return {"available": False, "reason": "vision_off", "description": None, "answer": None}
        return {"available": True, "description": "一位穿藍色上衣的人坐在桌前。", "answer": "看得到你", "people_visible": 1}

    async def close(self):
        pass


async def _with_tools(world, backend=None, clock=None):
    """`clock`: the registry's clock (world.clock, for its dance and stop timings); the real clock by default."""
    relay, running = await _begin(world)
    link = Link()
    relay.tools = ToolRegistry(link, backend or Backend(), **({"clock": clock} if clock else {}))
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
        assert link.dances == [(None, 2)]                  # the robot dances: a move it picks
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
        assert link.dances == [(None, 2)] and relay._intent is None   # no second one from the backstop
        commentaries = list(world.clients[-1].commentaries)

        await _step(world, relay, 3.0)                     # the next turn: GPT-Live says it but asks for nothing
        await _live(world, _said("再跳一次"), _sound(), _reachy("好，再跳一次"))
        await _step(world, relay, live_session.INTENT_REPLY_GRACE_SECONDS + 0.5)
        assert link.dances == [(None, 2), (None, 2)]       # 「再跳一次」 after a dance: another
        late = await relay.tools.execute("dance", {"dance": None, "repeat": None})   # its delegation, late
        assert late["ok"] and link.dances == [(None, 2), (None, 2)]   # one dance for the turn, not two
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
        assert link.dances == [(None, 2)] and len(world.clients[-1].commentaries) == before + 1
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

    assert asyncio.run(scenario()) == [(None, 2), ("simple_nod", 2)]


# ── stop_dance stops at once (agent tool fixes, Task 5): on 16 Oct 2026 Reachy did not stop. A stop reached the robot
# 2-6 s after the word, while a dance of one move lasts 2-5 s, common stop words were missed, and a dance still waiting
# for the backstop started after the stop ──

# While a dance may still run, a stop phrase anywhere in the patient's words is a stop
STOPS = ["停", "停下來", "停止", "不要跳了", "不要再跳了", "別跳了", "不跳了", "好了，停", "日希，停下來", "你停下來",
         "瑞希停下來", "你不要跳了", "停停停", "停一下", "先停一下", "可以停了", "你可以停下來嗎", "暫停",
         "stop", "please stop", "Reachy, stop", "stop it", "okay stop", "can you stop", "stop now please",
         "that's enough, stop", "夠了", "好了", "好了好了",
         # ... read clause by clause, so a "don't" of one clause never takes the stop of another (fix round 2):
         # 「停，不要跳了」 read as one run of words lost 停 and the 不 of 不要跳了 to 「停不」 (「停不下來」), and
         # 「不要，停下來」 read as 「不要停」
         "停，不要跳了", "停！不要跳了", "不要，停下來", "不要！停！", "不，停", "停，不跳了", "好，停，不要跳了",
         "停，不用了", "日希，夠了",
         # ... and without the transcript's comma, 停 followed by 不要 or 不 is still a stop (「停不下來」 is not)
         "停不要跳了", "停不跳了"]
# ... and these at any time, dancing or not
ALWAYS_STOPS = ["stop dancing", "不要跳舞了", "停止跳舞", "Reachy, please stop dancing", "停，不要跳舞了",
                "不要，停止跳舞"]
# Never a stop: "don't stop", "non-stop" and other kinds of stop
NOT_STOPS = ["不要停", "停車", "停電", "不停", "不要停，繼續跳", "你跳個不停", "停車場在哪裡", "昨天停電了", "別停",
             "我停不下來", "我想停藥", "不要停止跳舞", "don't stop", "don't stop dancing", "I can't stop laughing",
             "the bus stop", "non-stop", "我吃好了", "停不了", "我真的停不住"]


@pytest.mark.parametrize("words", STOPS + ALWAYS_STOPS)
def test_while_a_dance_may_run_a_stop_phrase_anywhere_in_the_words_is_a_stop(words):
    assert tool_intents.detect(words, dancing=True) == "stop_dance"


@pytest.mark.parametrize("words", STOPS)
def test_with_no_dance_running_a_bare_stop_phrase_is_no_stop(words):
    assert tool_intents.detect(words) is None


@pytest.mark.parametrize("words", ALWAYS_STOPS)
def test_a_stop_that_names_dancing_is_a_stop_dancing_or_not(words):
    assert tool_intents.detect(words) == "stop_dance"


@pytest.mark.parametrize("words", NOT_STOPS)
def test_dont_stop_and_other_kinds_of_stop_are_never_a_stop(words):
    assert tool_intents.detect(words, dancing=True) != "stop_dance"
    assert tool_intents.detect(words) != "stop_dance"


@pytest.mark.parametrize("fragments", [["不要再跳了"], ["日希，", "停下來"], ["Reachy,", " stop"], ["please stop"],
                                       ["你", "不要跳了"], ["好了，停"], ["你停下來"], ["不要，", "停下來"],
                                       ["停，不要跳了"], ["不，", "停"]])
def test_a_stop_reaches_the_robot_at_the_fragment_that_holds_it(world, fragments):
    # GPT-Live's reply and the backstop's grace are not waited for: a dance of two moves lasts 4-10 s
    async def scenario():
        relay, running, link = await _with_tools(world, clock=world.clock)
        await relay.tools.execute("dance", {"dance": "simple_nod", "repeat": None})   # GPT-Live's dance runs
        await _live(world, *[_said(fragment) for fragment in fragments[:-1]])
        assert link.stops == 0
        await _live(world, _said(fragments[-1]))           # no relay step, nothing from GPT-Live
        assert link.stops == 1 and not relay.tools.dancing()
        await _end(world, running)

    asyncio.run(scenario())


def test_the_stop_runs_even_while_a_screening_hit_settles(world):
    # A screening hit holds the other requests; a dance that goes on helps nobody, so the stop still runs
    async def scenario():
        relay, running, link = await _with_tools(world, clock=world.clock)
        await relay.tools.execute("dance", {"dance": "simple_nod", "repeat": None})
        relay._hit_at = world.clock.now
        await _live(world, _said("停"))
        assert link.stops == 1
        relay._hit_at = None
        await _end(world, running)

    asyncio.run(scenario())


def test_gpt_lives_own_stop_call_shares_the_relays_stop_even_after_the_patients_next_turn(world):
    async def scenario():
        relay, running, link = await _with_tools(world, clock=world.clock)
        await relay.tools.execute("dance", {"dance": "simple_nod", "repeat": None})
        before = len(world.clients[-1].commentaries)
        await _live(world, _said("不要再跳了"))
        assert link.stops == 1
        await _step(world, relay, live_session.TURN_GAP_SECONDS + 0.5)
        await _live(world, _said("謝謝你"))                 # the patient's next turn, before GPT-Live's call
        world.clients[-1].requested.append("stop_dance")   # GPT-Live delegates the stop, late
        late = await relay.tools.execute("stop_dance", {})
        assert late == {"ok": True, "tool": "stop_dance", "result": {"status": "cancelled"}} and link.stops == 1
        await _step(world, relay, live_session.INTENT_WAIT_SECONDS + 0.5)
        assert len(world.clients[-1].commentaries) == before   # GPT-Live says it itself
        await _end(world, running)

    asyncio.run(scenario())


def test_without_gpt_lives_call_it_is_told_the_dance_stopped_after_the_usual_grace(world):
    async def scenario():
        relay, running, link = await _with_tools(world, clock=world.clock)
        await relay.tools.execute("dance", {"dance": "simple_nod", "repeat": None})
        before = len(world.clients[-1].commentaries)
        await _live(world, _said("日希，停下來"))
        assert link.stops == 1
        await _live(world, _sound(), _reachy("好"))
        await _step(world, relay, live_session.INTENT_REPLY_GRACE_SECONDS - 0.5)
        assert len(world.clients[-1].commentaries) == before   # GPT-Live may still ask for the stop itself
        await _step(world, relay, 1.0)
        assert world.clients[-1].commentaries[before:] == [live_session.INTENT_LINES["stop_dance"]["zh-TW"]]
        assert link.stops == 1                             # the line says what the stop did: no second stop
        await _end(world, running)

    asyncio.run(scenario())


def test_a_stop_drops_a_dance_still_waiting_for_the_backstop(world):
    # 「跳舞給我看」, GPT-Live says 「好，我來跳」 but does not delegate (the 14 Oct miss), then 「停」 before the
    # backstop's grace ran out: the waiting dance makes the bare 停 a stop, and that dance never starts
    async def scenario():
        relay, running, link = await _with_tools(world, clock=world.clock)
        await _live(world, _said("跳舞給我看"))
        await _step(world, relay, 0.5)
        await _live(world, _sound(), _reachy("好，我來跳"))
        await _step(world, relay, live_session.TURN_GAP_SECONDS + 0.1)   # a new turn, inside the dance's grace
        assert relay._intent is not None and relay._intent["kind"] == "dance"
        await _live(world, _said("停"))
        assert link.stops == 1 and relay._intent is None
        for _ in range(20):
            await _step(world, relay, 0.5)
        assert link.dances == []
        await _end(world, running)

    asyncio.run(scenario())


def test_no_dance_starts_after_a_stop(world):
    # The backstop danced; the patient says 停; GPT-Live's delegated dance for the first turn arrives late (the probe
    # of 16 Oct started a second dance after the stop), and GPT-Live asks for another one on its own: both refused
    async def scenario():
        relay, running, link = await _with_tools(world, clock=world.clock)
        await _live(world, _said("跳舞給我看"), _sound(), _reachy("好，我來跳"))
        await _step(world, relay, live_session.INTENT_REPLY_GRACE_SECONDS + 0.5)
        assert link.dances == [(None, 2)]                  # the backstop danced
        await _live(world, _said("停"))
        assert link.stops == 1
        late = await relay.tools.execute("dance", {"dance": None, "repeat": None})
        assert late == {"ok": False, "tool": "dance", "error": "stopped_by_patient"}
        await _step(world, relay, tool_registry.STOP_HOLD_SECONDS - 1.0)
        again = await relay.tools.execute("dance", {"dance": "simple_nod", "repeat": 1})
        assert again == {"ok": False, "tool": "dance", "error": "stopped_by_patient"}
        assert link.dances == [(None, 2)]
        await _end(world, running)

    asyncio.run(scenario())


def test_a_stop_while_the_robot_still_loads_the_backstops_dance_stops_it_and_gpt_live_hears_it_stopped(world):
    # The real robot (medcare-reachy-bridge reachy_hardware.start_move) loads a dance's move off its event loop before
    # it starts it and answers: a stop meanwhile finds no move, answers idle, and the move starts after it (Task 5
    # fix round). The stop goes again when the dance answers, and GPT-Live is told it stopped, never that the dance
    # started or failed
    async def scenario():
        relay, running, link = await _with_tools(world, clock=world.clock)
        loaded, dance, moving = asyncio.Event(), link.dance, []

        async def loading(name, repeat):
            await loaded.wait()
            moving.append(name or "groovy_sway_and_roll")
            return await dance(name, repeat)

        async def stop():
            link.stops += 1
            stopped = moving.pop() if moving else None
            return {"status": "cancelled", "dance": stopped} if stopped else {"status": "idle"}

        link.dance, link.stop_dance = loading, stop
        before = len(world.clients[-1].commentaries)
        await _live(world, _said("跳舞給我看"), _sound(), _reachy("好，我來跳"))
        await _step(world, relay, live_session.INTENT_REPLY_GRACE_SECONDS + 0.5)   # the backstop's dance loads
        assert relay.tools.dancing() and link.dances == []
        await _live(world, _said("停"))
        assert link.stops == 1                             # at once: the robot has no move yet, so idle
        loaded.set()                                       # the move starts after the stop
        await _step(world, relay)
        assert link.dances == [(None, 2)] and link.stops == 2 and moving == []
        await _live(world, _sound(), _reachy("好"))
        await _step(world, relay, live_session.INTENT_REPLY_GRACE_SECONDS + 0.5)
        assert world.clients[-1].commentaries[before:] == [live_session.INTENT_LINES["stop_dance"]["zh-TW"]]
        await _end(world, running)

    asyncio.run(scenario())


@pytest.mark.parametrize("delegated", [True, False])
def test_a_new_dance_request_after_the_stop_dances_again(world, delegated):
    async def scenario():
        relay, running, link = await _with_tools(world, clock=world.clock)
        await relay.tools.execute("dance", {"dance": "simple_nod", "repeat": None})
        await _live(world, _said("不要再跳了"))
        assert link.stops == 1
        await _step(world, relay, live_session.TURN_GAP_SECONDS + 0.5)   # well inside the stop's hold
        await _live(world, _said("再跳一次"), _sound(), _reachy("好"))
        if delegated:                                      # GPT-Live asks for the dance itself
            world.clients[-1].requested.append("dance")
            again = await relay.tools.execute("dance", {"dance": None, "repeat": None})
            assert again["ok"] and again["result"]["status"] == "started"
        await _step(world, relay, live_session.INTENT_REPLY_GRACE_SECONDS + 0.5)
        assert link.dances == [("simple_nod", 2), (None, 2)]   # one dance for the request, either way
        await _end(world, running)

    asyncio.run(scenario())


def test_a_dance_gpt_live_asked_for_before_the_words_came_does_not_swallow_the_request(world):
    # GPT-Live can answer before its transcript of the patient is complete: its dance call, refused because the stop's
    # hold was still on, came before the relay read 「跳舞給我看」. The backstop then dances.
    async def scenario():
        relay, running, link = await _with_tools(world, clock=world.clock)
        await relay.tools.execute("dance", {"dance": "simple_nod", "repeat": None})
        await _live(world, _said("不要再跳了"))
        await _step(world, relay, live_session.TURN_GAP_SECONDS + 0.5)
        await _live(world, _said("日希"))                   # the next turn begins
        world.clients[-1].requested.append("dance")
        early = await relay.tools.execute("dance", {"dance": None, "repeat": None})
        assert early["error"] == "stopped_by_patient"
        await _live(world, _said("，跳舞給我看"), _sound(), _reachy("好"))
        await _step(world, relay, live_session.INTENT_REPLY_GRACE_SECONDS + 0.5)
        assert link.dances == [("simple_nod", 2), (None, 2)]
        await _end(world, running)

    asyncio.run(scenario())


# ── the patient's own word that the medicine is taken (took_medicine; agent tool fixes, Task 4) ──

@pytest.mark.parametrize("words", [
    "我吃完藥了", "我吃過藥了", "藥吃了", "我已經吃藥了", "I took my medicine", "I've taken my pills",
    "我吃完药了", "我剛剛吃藥了", "藥吃好了", "我已經吃過藥了", "日希，我吃完藥了！", "我把藥吃了", "吃完藥了", "我吃藥了",
    "Reachy, I already took my medication.", "yes, I have taken my pills",
    # a call name or a filler in front, without a comma (GPT-Live's transcripts leave some out)
    "日希我吃完藥了", "嗯我吃過藥了", "對我已經吃藥了", "我跟你說我吃完藥了", "瑞希，我跟你說，我吃完藥了",
    "我把藥吃完了", "藥我吃過了",
    # round 3: the closed whole-turn form, with the fillers dropped and one closing word after it at most
    "我吃完藥了，再見", "我吃完藥了，拜拜", "我吃完藥了，謝謝", "我吃完藥了啦", "嗯，好，我吃完藥了囉", "那個，我吃完藥了，拜拜",
    "I have had my meds", "I just took my medication", "I already took my pills", "Um, I took my medicine. Bye!",
    "Oh, Reachy, I took my medicine, thanks", "Well, I've had my medicine, thank you",
    "I’ve taken my pills",   # a curly apostrophe is made straight before the script conversion (it became 』)
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
    # a condition, a plan, a belief, a doubt or someone else's words before it (the claim starts its clause)
    "如果我吃完藥了，可以出去玩嗎", "等我吃完藥了，再跟你聊", "要是我吃完藥了，就去散步", "我以為我吃完藥了",
    "我不記得我吃過藥了", "我不確定我吃過藥了", "他說我吃完藥了", "他說我把藥吃了",
    "如果，我吃完藥了，可以出去玩嗎", "我以為，我吃完藥了", "他說，我吃完藥了", "I'm not sure, I took my medicine",
    # a "then" after it: the transcript's comma does not make it a claim
    "我吃完藥了，再跟你說", "我吃完藥了，就去睡覺",
    # ran out
    "藥吃完了，要再去拿", "藥吃完了", "藥已經吃完了", "我藥吃完了", "我把藥吃完了，要再去拿",
    # took extra: never praised, left to GPT-Live and the safety checks
    "我吃完藥了，又多吃了一次", "我吃完藥了，好像吃了兩次", "我吃完藥了，我吃太多了", "I took my medicine, I took it twice",
    "I took my medicine, and an extra pill",
    # every clause before it counts, not only the one right before it, and not only at its end: another person
    # (with a claim that has no subject of its own), another time, a condition, a belief, a question
    "我先生啊，已經吃過藥了", "我媽媽，吃完藥了", "他，已經吃藥了", "我老公，剛剛吃藥了", "她呢，吃完藥了",
    "早上，我吃過藥了", "今天早上，我吃過藥了", "昨天，我吃完藥了", "Yesterday, I took my medicine",
    "Last night, I took my medicine", "This morning, I took my pills",
    "如果明天，我吃完藥了，可以出去玩嗎", "我以為昨天，我吃完藥了", "是不是，我吃完藥了",
    # a clause after it that puts the intake at another time
    "我吃過藥了，早上吃的", "I took my medicine, last night",
    # taken back later in the turn
    "我吃完藥了，不對，我還沒吃", "我吃完藥了，喔不是，還沒吃", "我吃完藥了，騙你的", "I took my medicine, no wait, I didn't",
    "I took my medicine, actually no",
    # a whole box, everything, a count, a later dose taken too
    "我吃完藥了，一整盒都吃了", "我吃完藥了，整瓶都吃了", "我吃完藥了，全部都吃了", "我吃完藥了，吃了五顆",
    "我吃完藥了，連明天的也吃了", "我吃完藥了，晚上的也一起吃了", "I took my medicine, the whole bottle",
    "I took my pills, all of them", "I took all my pills",
    # round 3: anything else in the turn is no backstop claim (GPT-Live's own delegation answers it), on purpose:
    # another clause, even a harmless one, ...
    "我吃完藥了，你呢？", "我吃完藥了，今天好累", "我吃完藥了，就是有點累", "我一整天都很累，我吃完藥了",
    "我吃完藥了，還沒吃早餐", "我吃完藥了，吃了一顆", "I took my medicine, I feel fine",
    # ... a clock time (「我吃過藥了，八點吃的」 filed today's dose, and overdose protection then refused the real one) ...
    "我吃過藥了，八點吃的", "我八點吃過藥了", "我吃過藥了，八點", "八點，我吃過藥了", "我吃完今天的藥了",
    "I took my medicine at eight", "I took my medicine at 8 o'clock",
    # ... and words that name no medicine
    "我吃完了", "我吃過了", "吃完了", "I took it", "I'm done",
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
        # round 3: the backstop never praises (praise can land on a refused or doubled dose); GPT-Live's own path
        # praises with the applause
        assert gpt_live_config.CELEBRATION["zh-TW"] not in line and "拍拍手" not in line
        assert "家人確認" in line and "記錄" not in line
        await _end(world, running)

    asyncio.run(scenario())


REFUSED_MAX = {"status": "refused", "reason": "daily_max_reached", "reply": "今天已經吃夠了", "family_asked": False}


@pytest.mark.parametrize("status, key, record", [
    # round 3 (14 Oct rule: every medication event reaches family): a possible double dose goes to
    # record_medication_taken, which refuses it, sends the refused dose event and the double-dose alert, and its own
    # answer is said; nothing is recorded as taken
    ("refused_too_soon", "took_medicine_refused", {"status": "refused", "reason": "dose_too_soon",
                                                   "reply": "現在還不能吃", "family_asked": False}),
    ("refused_max_reached", "took_medicine_refused", REFUSED_MAX),
    # every other state: nothing to record, and GPT-Live says why
    ("taken", "took_medicine_taken", None), ("pending_confirmation", "took_medicine_waiting", None),
    ("refused_not_due", "took_medicine_refused", None), ("refused_expired", "took_medicine_refused", None),
    ("none", "took_medicine_none", None), ("skipped", "took_medicine_none", None),
])
def test_a_dose_the_due_check_does_not_allow_is_never_recorded_and_gpt_live_says_why(world, status, key, record):
    async def scenario():
        due = {**DUE_ALLOWED, "status": "none", "doses": []} if status == "none" else _due(status)
        backend = Backend(due=due, record=record)
        relay, running = await _claim(world, backend)
        if record is None:
            assert backend.calls == [("check_medication_due",)]   # no request to family for it
        else:
            assert backend.calls == [("check_medication_due",), ("record_medication_taken", "dose-12", True, None, None)]
        line = world.clients[-1].commentaries[-1]
        expected = live_session.INTENT_LINES[key]["zh-TW"]
        if key == "took_medicine_refused":
            expected = expected.format(why=live_session.CLAIM_REASONS[status]["zh-TW"])
            assert "沒有記錄" in line
        assert line == expected and gpt_live_config.CELEBRATION["zh-TW"] not in line
        await _end(world, running)

    asyncio.run(scenario())


def test_a_too_soon_dose_the_record_now_allows_is_asked_for(world):
    # The tool decides: a dose the due check found too soon that the record allows (time passed meanwhile) goes to
    # family like any other
    async def scenario():
        backend = Backend(due=_due("refused_too_soon"))
        relay, running = await _claim(world, backend)
        assert backend.calls == [("check_medication_due",), ("record_medication_taken", "dose-12", True, None, None)]
        assert world.clients[-1].commentaries[-1] == live_session.INTENT_LINES["took_medicine"]["zh-TW"]
        await _end(world, running)

    asyncio.run(scenario())


@pytest.mark.parametrize("answer, key", [
    ({"status": "refused", "reason": "dose_too_soon", "reply": "現在還不能吃", "family_asked": False},
     "took_medicine_refused"),
    ({"status": "already_taken", "family_asked": False}, "took_medicine_taken"),
    ({"status": "already_pending_confirmation", "family_asked": False}, "took_medicine_waiting"),
    ({"status": "already_skipped", "family_asked": False}, "took_medicine_none"),
    (ToolError("database_timeout"), "took_medicine_failed"),
])
def test_gpt_live_says_what_the_record_answered(world, answer, key):
    async def scenario():
        backend = Backend(record=answer)
        relay, running = await _claim(world, backend)
        assert [call[0] for call in backend.calls] == ["check_medication_due", "record_medication_taken"]
        line = world.clients[-1].commentaries[-1]
        expected = live_session.INTENT_LINES[key]["zh-TW"]
        if key == "took_medicine_refused":
            expected = expected.format(why=live_session.CLAIM_REASONS["refused_too_soon"]["zh-TW"])
        assert line == expected and gpt_live_config.CELEBRATION["zh-TW"] not in line
        await _end(world, running)

    asyncio.run(scenario())


def test_a_slot_nothing_of_which_is_allowed_names_each_medicine_with_its_state(world):
    # Round 3 (F2.3): with no dose allowed the line came from the slot's summary alone, so a refused medicine went
    # unnamed; every dose of the slot is said now
    async def scenario():
        backend = Backend(due=_due("taken", {**DOSE, "status": "taken"}, {**SECOND, "status": "refused_expired"}))
        relay, running = await _claim(world, backend)
        assert backend.calls == [("check_medication_due",)]
        assert world.clients[-1].commentaries[-1] == (
            live_session.CLAIM_PARTS["taken"]["zh-TW"].format(names="Allegra")
            + live_session.CLAIM_PARTS["refused"]["zh-TW"].format(
                names="Metformin", why=live_session.CLAIM_REASONS["refused_expired"]["zh-TW"]))
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


@pytest.mark.parametrize("tool", ["check_medication_due", "record_medication_taken"])
def test_gpt_lives_own_delegation_within_the_grace_means_no_second_run(world, tool):
    async def scenario():
        backend = Backend()
        relay, running, link = await _with_tools(world, backend)
        await _live(world, _said("我吃完藥了"), _sound(), _reachy("好，我幫你問問"))
        world.clients[-1].requested.append(tool)          # GPT-Live's backend took the claim
        await _step(world, relay, live_session.INTENT_WAIT_SECONDS + 1)
        assert backend.calls == [] and relay._claim is None
        await _end(world, running)

    asyncio.run(scenario())


@pytest.mark.parametrize("tool", ["get_medication_schedule", "get_pill_intake_result"])
def test_asking_the_schedule_is_no_delegation_of_the_claim(world, tool):
    # Round 3 (F2.7): reading the schedule records nothing, so it no longer drops a claim nobody recorded
    async def scenario():
        backend = Backend()
        relay, running, link = await _with_tools(world, backend)
        await _live(world, _said("我吃完藥了"), _sound(), _reachy("好，我看一下"))
        world.clients[-1].requested.append(tool)
        await _step(world, relay, live_session.INTENT_REPLY_GRACE_SECONDS + 0.5)
        for _ in range(5):
            await relay_chat._settle()
        assert backend.calls == [("check_medication_due",), ("record_medication_taken", "dose-12", True, None, None)]
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


def test_gpt_lives_late_record_shares_only_the_run_of_its_own_dose(world):
    # Round 3 (F2.6): a slot of two medicines records twice; GPT-Live's call for the first dose got the second dose's
    # answer (the runs were shared by the tool's name), and a call for a dose the backstop did not record ran nowhere
    async def scenario():
        backend = Backend(due=_due("pending_allowed", DOSE, SECOND),
                          records={"dose-12": ASKED, "dose-13": {**ASKED, "medication_id": "dose-13",
                                                                 "medication_name": "Metformin", "request_id": "r-2"}})
        relay, running = await _claim(world, backend)
        assert [call[1] for call in backend.calls[1:]] == ["dose-12", "dose-13"]
        first = await relay.tools.execute("record_medication_taken", {"medication_id": "dose-12",
                                                                      "patient_confirmed": True})
        second = await relay.tools.execute("record_medication_taken", {"medication_id": "dose-13",
                                                                       "patient_confirmed": True})
        assert first["result"]["request_id"] == "r-1" and second["result"]["request_id"] == "r-2"
        assert len(backend.calls) == 3                    # both shared: no second request to family
        other = await relay.tools.execute("record_medication_taken", {"medication_name": "Allegra",
                                                                      "patient_confirmed": True})
        assert other["ok"] and backend.calls[-1] == ("record_medication_taken", None, True, "Allegra", None)
        await _end(world, running)

    asyncio.run(scenario())


@pytest.mark.parametrize("words", ["我還沒吃藥", "他吃完藥了", "我等一下吃藥", "我吃藥了嗎", "Yesterday, I took my medicine",
                                   "我先生啊，已經吃過藥了", "早上，我吃過藥了"])
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


@pytest.mark.parametrize("fragments, claimed", [
    (["我吃完藥了", "再跟你說"], False), (["我吃完藥了", "，今天好累"], False), (["我吃完藥了", "，再跟你說"], False),
    (["我吃完藥了", "，好像吃了兩次"], False), (["I took my medicine", ", I took it twice"], False),
    (["我吃完藥了", "，不對，我還沒吃"], False), (["I took my medicine", ", no wait, I didn't"], False),
    (["我吃完藥了", "，一整盒都吃了"], False), (["我吃完藥了", "，晚上的也一起吃了"], False),
    (["I took my medicine", ", the whole bottle"], False), (["I took my pills", ", all of them"], False),
    # round 3: the claim is decided at dispatch on the whole turn, so a fragment that briefly reads as no claim never
    # drops it for good (「，再」 was read as 「再跟你說」), and one cut inside a word still makes one
    (["我吃完藥了", "，再", "見"], True), (["我吃完藥了", "，就", "是有點累"], False), (["我吃完", "藥了"], True),
    (["我吃完藥", "了"], True), (["I took my", " medicine"], True), (["I’ve taken", " my pills"], True),
    (["我吃過藥了", "，八點吃的"], False), (["我吃完藥了", "，再"], False),
])
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


def test_an_extra_dose_said_in_the_next_turn_drops_a_claim_still_waiting(world):
    # 「我吃完藥了」, a pause, then 「我好像吃了兩次」 before the backstop's grace ran out: no request to family and no
    # praise from the backstop; GPT-Live and the safety checks answer it (conversation.LIVE_CLAIM_PRAISE)
    async def scenario():
        backend = Backend()
        relay, running, link = await _with_tools(world, backend)
        await _live(world, _said("我吃完藥了"))
        await _step(world, relay, live_session.TURN_GAP_SECONDS + 0.5)    # the turn closes; the claim still waits
        assert relay._claim is not None and relay._claim["kind"] == "took_medicine"
        before = len(world.clients[-1].commentaries)
        await _live(world, _said("我好像吃了兩次"))
        await _step(world, relay, live_session.INTENT_WAIT_SECONDS + 0.5)
        for _ in range(5):
            await relay_chat._settle()
        assert backend.calls == [] and len(world.clients[-1].commentaries) == before
        await _end(world, running)

    asyncio.run(scenario())


@pytest.mark.parametrize("words", ["不對，我還沒吃", "騙你的", "No wait, I didn't take it",
                                   # round 3: any other words before the due check starts drop it, not only these
                                   "今天天氣很好", "我八點吃的", "我想一下"])
def test_a_claim_taken_back_in_the_next_turn_is_dropped(world, words):
    # 「我吃完藥了」, a pause, then 「不對，我還沒吃」 before the backstop's grace ran out: no request to family
    async def scenario():
        backend = Backend()
        relay, running, link = await _with_tools(world, backend)
        await _live(world, _said("我吃完藥了"))
        await _step(world, relay, live_session.TURN_GAP_SECONDS + 0.5)    # the turn closes; the claim still waits
        before = len(world.clients[-1].commentaries)
        await _live(world, _said(words))
        await _step(world, relay, live_session.INTENT_WAIT_SECONDS + 0.5)
        for _ in range(5):
            await relay_chat._settle()
        assert backend.calls == [] and len(world.clients[-1].commentaries) == before
        await _end(world, running)

    asyncio.run(scenario())


@pytest.mark.parametrize("words", ["謝謝", "拜拜", "嗯", "Thanks!", "我吃完藥了"])
def test_a_closing_word_or_the_same_claim_in_the_next_turn_keeps_the_claim(world, words):
    async def scenario():
        backend = Backend()
        relay, running, link = await _with_tools(world, backend)
        await _live(world, _said("我吃完藥了"))
        await _step(world, relay, live_session.TURN_GAP_SECONDS + 0.5)    # the turn closes; the claim still waits
        await _live(world, _said(words))
        await _step(world, relay, live_session.INTENT_WAIT_SECONDS + 0.5)
        for _ in range(5):
            await relay_chat._settle()
        assert backend.calls == [("check_medication_due",), ("record_medication_taken", "dose-12", True, None, None)]
        await _end(world, running)

    asyncio.run(scenario())


def test_a_request_in_the_next_turn_runs_and_drops_the_claim_still_waiting(world):
    # 「我吃完藥了」, a pause, then 「跳舞給我看」 before the claim's grace ran out: the robot dances; the patient said more
    # after the claim, so it is no backstop claim any more (round 3) and GPT-Live's own delegation answers it
    async def scenario():
        backend = Backend()
        relay, running, link = await _with_tools(world, backend)
        await _live(world, _said("我吃完藥了"))
        await _step(world, relay, live_session.TURN_GAP_SECONDS + 0.5)    # the turn closes; the claim still waits
        await _live(world, _said("跳舞給我看"))
        await _step(world, relay, live_session.INTENT_WAIT_SECONDS + 0.5)   # GPT-Live said nothing: both are due
        for _ in range(5):
            await relay_chat._settle()
        assert link.dances == [(None, 2)] and backend.calls == []
        assert live_session.INTENT_LINES["dance"]["zh-TW"] in world.clients[-1].commentaries
        await _end(world, running)

    asyncio.run(scenario())


def test_a_claim_with_a_request_in_the_same_turn_is_no_backstop_claim(world):
    async def scenario():
        backend = Backend()
        relay, running, link = await _with_tools(world, backend)
        await _live(world, _said("我吃完藥了，跳舞給我看"))
        await _step(world, relay, live_session.INTENT_WAIT_SECONDS + 0.5)
        for _ in range(5):
            await relay_chat._settle()
        assert link.dances == [(None, 2)] and backend.calls == []
        await _end(world, running)

    asyncio.run(scenario())


def test_every_allowed_dose_of_the_slot_is_asked_for(world):
    # A slot with two medicines, both allowed: 「我吃完藥了」 asks family about each, so neither is left pending for a
    # missed-dose reminder about a medicine the patient said they took
    async def scenario():
        backend = Backend(due=_due("pending_allowed", DOSE, SECOND))
        relay, running = await _claim(world, backend)
        assert backend.calls == [("check_medication_due",), ("record_medication_taken", "dose-12", True, None, None),
                                 ("record_medication_taken", "dose-13", True, None, None)]
        assert world.clients[-1].commentaries[-1] == live_session.INTENT_LINES["took_medicine"]["zh-TW"]
        await _end(world, running)

    asyncio.run(scenario())


REFUSED_TOO_SOON = {"status": "refused", "reason": "dose_too_soon", "reply": "現在還不能吃", "family_asked": False}


@pytest.mark.parametrize("due, records, recorded, why", [
    # the record refuses the second medicine (it became too soon meanwhile)
    (_due("pending_allowed", DOSE, SECOND), {"dose-13": REFUSED_TOO_SOON}, ["dose-12", "dose-13"], "refused_too_soon"),
    # the due check already says too soon: the record is asked too (round 3), refuses it and says why
    (_due("pending_allowed", DOSE, {**SECOND, "status": "refused_too_soon"}), {"dose-13": REFUSED_TOO_SOON},
     ["dose-12", "dose-13"], "refused_too_soon"),
    # the due check says missed: only the first is recorded
    (_due("pending_allowed", DOSE, {**SECOND, "status": "refused_expired"}), {}, ["dose-12"], "refused_expired"),
])
def test_a_slot_with_a_refused_medicine_says_each_by_name_and_is_never_praised(world, due, records, recorded, why):
    async def scenario():
        backend = Backend(due=due, records=records)
        relay, running = await _claim(world, backend)
        assert [call[1] for call in backend.calls[1:]] == recorded
        line = world.clients[-1].commentaries[-1]
        assert line == (live_session.CLAIM_PARTS["asked"]["zh-TW"].format(names="Allegra")
                        + live_session.CLAIM_PARTS["refused"]["zh-TW"].format(
                            names="Metformin", why=live_session.CLAIM_REASONS[why]["zh-TW"]))
        assert gpt_live_config.CELEBRATION["zh-TW"] not in line
        await _end(world, running)

    asyncio.run(scenario())


@pytest.mark.parametrize("words, recorded", [
    # round 3: words that arrive after the due check started cancel the records not yet started (an extra dose, a
    # doubt, anything else); a closing word does not
    ("我好像吃了兩次", []), ("等一下，我想一下", []), ("No wait", []), ("謝謝", ["dose-12", "dose-13"]),
])
def test_words_after_the_due_check_started_cancel_the_records_not_yet_started(world, words, recorded):
    async def scenario():
        backend = Backend(due=_due("pending_allowed", DOSE, SECOND))
        backend.gate = asyncio.Event()
        relay, running, link = await _with_tools(world, backend)
        await _live(world, _said("我吃完藥了"))
        await _step(world, relay, live_session.INTENT_WAIT_SECONDS + 0.5)   # GPT-Live said nothing: the backstop runs
        assert backend.calls == [("check_medication_due",)]
        before = len(world.clients[-1].commentaries)
        await _live(world, _said(words))                                     # while the due check runs
        backend.gate.set()
        for _ in range(5):
            await relay_chat._settle()
        assert [call[1] for call in backend.calls[1:]] == recorded
        said = world.clients[-1].commentaries[before:]
        # nothing was asked, so nothing is said: GPT-Live answers the new words
        assert said == ([live_session.INTENT_LINES["took_medicine"]["zh-TW"]] if recorded else [])
        await _end(world, running)

    asyncio.run(scenario())


def test_words_during_a_record_leave_the_records_already_made_and_say_only_those(world):
    # The first medicine's request went before the patient spoke again: family were asked, and the line says so;
    # the second medicine's record never starts
    async def scenario():
        backend = Backend(due=_due("pending_allowed", DOSE, SECOND))
        relay, running, link = await _with_tools(world, backend)
        recorded = backend.record_medication_taken

        async def record(*args):
            await _live(world, _said("不對"))                                 # the patient speaks during the record
            return await recorded(*args)

        backend.record_medication_taken = record
        await _live(world, _said("我吃完藥了"))
        await _step(world, relay, live_session.INTENT_WAIT_SECONDS + 0.5)
        for _ in range(5):
            await relay_chat._settle()
        assert [call[1] for call in backend.calls[1:]] == ["dose-12"]
        assert world.clients[-1].commentaries[-1] == live_session.CLAIM_PARTS["asked"]["zh-TW"].format(names="Allegra")
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
def test_every_claim_line_is_said_in_both_languages_and_never_praises(language):
    # Round 3 (F2.2): the backstop never runs the applause and never praises (praise could land on a refused or
    # doubled dose); it says what happened. Praise stays on GPT-Live's own path, with the applause emote
    keys = [key for key in live_session.INTENT_LINES if key.startswith("took_medicine")]
    assert len(keys) == 6
    for key in keys:
        line = live_session.INTENT_LINES[key][language].format(why="x")
        assert line and gpt_live_config.CELEBRATION[language] not in line and "拍拍手" not in line
    assert all(reason[language] for reason in live_session.CLAIM_REASONS.values())
    for part in live_session.CLAIM_PARTS.values():   # one medicine of several, by name
        line = part[language].format(names="Allegra", why="x")
        assert "Allegra" in line and gpt_live_config.CELEBRATION[language] not in line


def test_the_backends_completion_flow_records_before_it_applauds():
    # As the camera workflow does: the applause (and with it the praise line) only for a dose that stands
    text = gpt_live_config.BACKEND_INSTRUCTION
    flow = text[text.index("MEDICATION COMPLETION"):text.index("\nEMOTION\n")]
    assert flow.index("record_medication_taken(") < flow.index('trigger_antenna_emote(preset="applaud_cheer")')
    applause = flow[flow.index('trigger_antenna_emote(preset="applaud_cheer")'):]
    assert "pending_confirmation" in applause and "already_taken" in applause
    assert "already_pending_confirmation" in applause and "no applause" in applause
    # Round 3 (F2.5): every dose of the slot the due check allows is recorded, not only the summary's one
    # medication_id (a slot of two medicines left the second pending), and the applause needs every dose to stand
    record = flow[:flow.index('trigger_antenna_emote(preset="applaud_cheer")')]
    assert "once for every dose" in record and "doses" in record and "pending_allowed" in record
    assert "every dose stands" in applause and "name" in applause and "reason" in applause
