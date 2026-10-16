"""The live relay's tool backstop (services/live_session.py with services/tool_intents.py): a clear request to dance,
to stop or to look runs its tool when GPT-Live has not asked for it in time, and GPT-Live is told what happened.
The regression case is the chat of 14 Oct 2026 at 10:04 (「你可以看到我嗎」 got 「我現在看不到你」, 「你可以跳舞嗎」 got
「可以啊，我現在就跳，你想看嗎？」, no tool either time). The harness is tests/test_live_relay_chat.py's; the registry is
the real ToolRegistry over a fake robot link."""

import asyncio
import types

import pytest

from app import config
from app.services import live_session, tool_intents
from app.services.tool_registry import ToolRegistry
from tests import test_live_relay_chat as relay_chat
from tests.test_live_relay_chat import _begin, _live, _over, _reachy, _said, _sound, _step

world = relay_chat.world   # the harness's fixture, registered in this module too


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

    def __init__(self, available=True):
        self.available, self.questions = available, []

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
