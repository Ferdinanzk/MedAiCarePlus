"""The GPT-Live agent's weather and dance tools on the server (13 Oct 2026): the registry's validation and dispatch
(services/tool_registry.py), the robot link's dance requests and answers over the live WebSocket
(services/agent_bridge.py) and the relay's routing of `dance_result` (services/live_session.py)."""

import asyncio
import sys
import types

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config   # noqa: E402
from app.services import tool_registry, weather_lookup   # noqa: E402
from app.services.agent_bridge import RelayRobotLink   # noqa: E402
from app.services.tool_registry import ToolError, ToolRegistry   # noqa: E402
from tests.test_live_tools import TARGET, relay   # noqa: E402
from tests.test_tool_registry import Robot, backend   # noqa: E402


def registry(robot=None):
    return ToolRegistry(robot or Robot(), backend()[0])


# ── the registry ──
def test_dance_and_stop_dance_go_to_the_robot_with_a_checked_move_and_repeat():
    async def run():
        robot = Robot()
        r = registry(robot)
        started = await r.execute("dance", {"dance": "jackson_square", "repeat": 2})
        any_move = await r.execute("dance", {"dance": None, "repeat": None})
        stopped = await r.execute("stop_dance", {})
        unknown = await r.execute("dance", {"dance": "moonwalk", "repeat": None})
        too_many = await r.execute("dance", {"dance": None, "repeat": 9})
        return robot, started, any_move, stopped, unknown, too_many

    robot, started, any_move, stopped, unknown, too_many = asyncio.run(run())
    assert started["ok"] and started["result"]["dance"] == "jackson_square" and started["result"]["repeat"] == 2
    assert any_move["ok"] and any_move["result"]["dance"] == "side_to_side_sway" and any_move["result"]["repeat"] == 2
    assert stopped == {"ok": True, "tool": "stop_dance", "result": {"status": "idle"}}
    assert unknown == {"ok": False, "tool": "dance", "error": "unknown_dance"}
    assert too_many == {"ok": False, "tool": "dance", "error": "repeat_out_of_range"}
    assert robot.emotes == ["dance:jackson_square:2", "dance:None:2", "stop_dance"]


def test_get_weather_runs_the_lookup_off_the_loop_with_the_chats_language(monkeypatch):
    asked = []

    def lookup(place, language):
        asked.append((place, language))
        return {"status": "ok", "location": "花蓮市, 花蓮縣, 台灣", "temperature_c": 27}

    monkeypatch.setattr(weather_lookup, "lookup", lookup)

    async def run():
        r = registry()
        named = await r.execute("get_weather", {"location": " 花蓮市 "})
        home = await r.execute("get_weather", {"location": None})
        blank = await r.execute("get_weather", {"location": "   "})
        return named, home, blank

    named, home, blank = asyncio.run(run())
    assert named["ok"] and named["result"]["location"] == "花蓮市, 花蓮縣, 台灣"
    assert home["ok"] and blank["ok"]
    assert asked == [("花蓮市", "zh-TW"), (None, "zh-TW"), (None, "zh-TW")]
    assert tool_registry.TIMEOUTS["get_weather"] >= weather_lookup.NOMINATIM_TIMEOUT + weather_lookup.WEATHER_TIMEOUT


# ── the robot link ──
async def _sent(robot_messages, answers):
    sent = []

    async def send(message):
        sent.append(message)
        request_id = message["request_id"]
        answer = answers.pop(0)
        if answer is not None:
            link.answer({**answer, "request_id": request_id})

    link = RelayRobotLink(send)
    return link, sent


def test_the_link_sends_dance_requests_and_reads_the_robots_answers():
    async def run():
        answers = [{"type": "dance_result", "ok": True, "result": {"status": "started", "dance": "yeah_nod",
                                                                    "repeat": 1, "available": ["yeah_nod"]}},
                   {"type": "dance_result", "ok": False, "error": "unknown_dance:moonwalk; available: yeah_nod"},
                   {"type": "dance_result", "ok": True, "result": {"status": "cancelled", "dance": "yeah_nod"}},
                   {"type": "dance_result", "ok": False, "error": "no_motion"}]
        link, sent = await _sent([], answers)
        started = await link.dance("yeah_nod", 1)
        try:
            await link.dance("moonwalk", 1)
        except ToolError as error:
            refused = str(error)
        stopped = await link.stop_dance()
        try:
            await link.stop_dance()
        except ToolError as error:
            no_motion = str(error)
        return sent, started, refused, stopped, no_motion

    sent, started, refused, stopped, no_motion = asyncio.run(run())
    assert [m["type"] for m in sent] == ["dance", "dance", "stop_dance", "stop_dance"]
    assert sent[0]["dance"] == "yeah_nod" and sent[0]["repeat"] == 1 and sent[0]["request_id"]
    assert started["status"] == "started" and started["dance"] == "yeah_nod"
    assert refused == "dance_failed: unknown_dance:moonwalk; available: yeah_nod"
    assert stopped == {"status": "cancelled", "dance": "yeah_nod"}
    assert no_motion == "stop_dance_failed: no_motion"


# ── the relay ──
def test_the_robots_dance_answer_reaches_the_tool_link(monkeypatch):
    monkeypatch.setattr(config, "LIVE_TOOLS", True)

    async def run():
        r = relay()
        r._new_client(TARGET, "rules")
        link = r.tools.robot
        asked = asyncio.ensure_future(link.dance(None, 2))
        await asyncio.sleep(0)
        request = r.robot.sent[-1]
        await r._on_robot_message({"type": "dance_result", "request_id": request["request_id"], "ok": True,
                                   "result": {"status": "started", "dance": "simple_nod", "repeat": 2,
                                              "available": ["simple_nod"]}})
        return request, await asked

    request, answer = asyncio.run(run())
    assert request["type"] == "dance" and request["dance"] is None and request["repeat"] == 2
    assert answer["dance"] == "simple_nod" and answer["repeat"] == 2


def test_play_emotion_goes_to_the_robot_with_a_checked_move_and_posture():
    async def run():
        robot = Robot()
        r = registry(robot)
        played = await r.execute("play_emotion", {"emotion": "downcast1", "posture": "concern"})
        plain = await r.execute("play_emotion", {"emotion": "cheerful1", "posture": None})
        unknown = await r.execute("play_emotion", {"emotion": "rage1", "posture": None})
        bad = await r.execute("play_emotion", {"emotion": "cheerful1", "posture": "fury"})
        return robot, played, plain, unknown, bad

    robot, played, plain, unknown, bad = asyncio.run(run())
    assert played["ok"] and played["result"]["move"] == "downcast1"
    assert plain["ok"] and unknown == {"ok": False, "tool": "play_emotion", "error": "unknown_emotion"}
    assert bad == {"ok": False, "tool": "play_emotion", "error": "invalid_posture"}
    assert robot.emotes == ["express:downcast1:concern", "express:cheerful1:None"]


def test_the_link_sends_express_requests_for_the_tool():
    async def run():
        answers = [{"type": "express_result", "ok": True, "result": {"status": "started", "move": "downcast1"}},
                   {"type": "express_result", "ok": False, "error": "gestures_off"}]
        link, sent = await _sent([], answers)
        started = await link.express("downcast1", "concern")
        try:
            await link.express("cheerful1", None)
        except ToolError as error:
            refused = str(error)
        return sent, started, refused

    sent, started, refused = asyncio.run(run())
    assert sent[0]["type"] == "express" and sent[0]["move"] == "downcast1" and sent[0]["affect"] == "concern"
    assert "affect" not in sent[1] and sent[1]["reason"] == "tool"
    assert started["move"] == "downcast1" and refused == "play_emotion_failed: gestures_off"
