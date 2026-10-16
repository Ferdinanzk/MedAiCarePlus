"""The live relay's agent layer switch (services/live_session.py, config.LIVE_TOOLS): with the switch off the relay
builds its GPT-Live client as before; with it on, the agent client gets the chat's tool registry, the tool note in
its instructions and the conversation id once the row exists, and the robot's frame and emote answers reach the
registry's robot link. No database: the relay is built directly and only _new_client and the uplink are exercised."""

import asyncio
import sys
import types

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config   # noqa: E402
from app.services import gpt_live_config, live_session   # noqa: E402
from app.services.ai_settings import LiveTarget   # noqa: E402
from app.services.live_session import RATE, LiveRelay   # noqa: E402

TARGET = LiveTarget("sk-live", "gpt-live-1", "marin")
DEVICE = {"device_id": "d-1", "u_id": 7, "auto_record": False, "face_label": "u7", "name": "A"}


class Robot:
    def __init__(self):
        self.sent = []

    async def receive(self):
        return None

    async def send_json(self, message):
        self.sent.append(message)

    async def send_bytes(self, data):
        pass

    async def close(self, code=1000):
        pass


class Plain:
    def __init__(self, api_key, model, voice, instructions, *, rate=16000, connect=None):
        self.args = (api_key, model, voice, instructions, rate)


class Agent(Plain):
    def __init__(self, api_key, model, voice, instructions, *, rate=16000, connect=None, registry, language):
        super().__init__(api_key, model, voice, instructions, rate=rate)
        self.registry, self.language = registry, language


def relay(agent=Agent):
    return LiveRelay(Robot(), DEVICE, client_factory=Plain, agent_factory=agent)


def test_with_the_switch_off_the_plain_client_is_built_as_before(monkeypatch):
    monkeypatch.setattr(config, "LIVE_TOOLS", False)
    r = relay()
    client = r._new_client(TARGET, "rules")
    assert isinstance(client, Plain) and not isinstance(client, Agent)
    assert client.args == ("sk-live", "gpt-live-1", "marin", "rules", RATE) and r.tools is None


def test_with_the_switch_on_the_agent_client_gets_the_registry_and_the_tool_note(monkeypatch):
    monkeypatch.setattr(config, "LIVE_TOOLS", True)

    async def run():
        r = relay()
        r.task_id = "task-1"
        first = r._new_client(TARGET, "rules")
        r.conversation_id = "c-1"
        r._sync_tools()
        second = r._new_client(TARGET, "rules\n\nnote")   # a companion stretch shares the registry
        return r, first, second

    r, first, second = asyncio.run(run())
    assert isinstance(first, Agent) and first.registry is r.tools and second.registry is r.tools
    assert first.args[3] == "rules\n\n" + gpt_live_config.live_tool_note("zh-TW") and first.language == "zh-TW"
    assert second.args[3].startswith("rules\n\nnote\n\n")
    backend = r.tools.backend
    assert (backend.u_id, backend.task_id, backend.conversation_id, backend.language) == (7, "task-1", "c-1", "zh-TW")


def test_the_robots_frame_and_emote_answers_reach_the_tool_link(monkeypatch):
    monkeypatch.setattr(config, "LIVE_TOOLS", True)

    async def run():
        r = relay()
        r._new_client(TARGET, "rules")
        link = r.tools.robot
        asked = asyncio.ensure_future(link.emote("curious_tilt"))
        await asyncio.sleep(0)
        request = r.robot.sent[-1]
        await r._on_robot_message({"type": "emote_result", "request_id": request["request_id"], "ok": True,
                                   "result": {"status": "queued", "preset": "curious_tilt", "pending_motion_jobs": 1}})
        answer = await asked
        await r._on_robot_message({"type": "frame", "request_id": "stale"})   # nobody waits: ignored
        return request, answer

    request, answer = asyncio.run(run())
    assert request["type"] == "emote" and request["preset"] == "curious_tilt"
    assert answer == {"status": "queued", "preset": "curious_tilt", "pending_motion_jobs": 1}


def test_without_an_agent_factory_the_switch_changes_nothing(monkeypatch):
    monkeypatch.setattr(config, "LIVE_TOOLS", True)
    r = relay(agent=None)
    assert isinstance(r._new_client(TARGET, "rules"), Plain) and r.tools is None


def test_the_default_agent_factory_is_the_bridges_client():
    from app.services.agent_bridge import AgentLiveClient
    assert LiveRelay(Robot(), DEVICE).agent_factory is AgentLiveClient


@pytest.mark.parametrize("language", ["zh-TW", "en"])
def test_with_tools_the_live_rules_send_lookups_to_the_backend(monkeypatch, language):
    # 14 Oct 2026: told to answer everything itself and never look anything up, GPT-Live said 「我現在看不到你」 to
    # 「你可以看到我嗎」 (3 of 3 probe sessions) instead of delegating the camera
    monkeypatch.setattr(live_session.conversation, "reply_prompt",
                        lambda language, tools=False: f"REPLY PROMPT {language} tools={tools}")
    plain = live_session.instructions(language, None, "")
    tools = live_session.instructions(language, None, "", tools=True)
    assert plain == f"REPLY PROMPT {language} tools=False\n\n" + live_session.LIVE_RULES[language]
    assert tools == f"REPLY PROMPT {language} tools=True\n\n" + live_session.LIVE_RULES_TOOLS[language]
    assert "不能上網或查資料" not in tools and "cannot look anything up" not in tools


def test_the_relay_gives_tools_only_with_the_switch_and_an_agent_client(monkeypatch):
    monkeypatch.setattr(config, "LIVE_TOOLS", True)
    assert relay()._with_tools() and not relay(agent=None)._with_tools()
    monkeypatch.setattr(config, "LIVE_TOOLS", False)
    assert not relay()._with_tools()


def test_the_tool_note_names_the_camera_and_dance_questions_that_must_be_delegated():
    zh, en = gpt_live_config.live_tool_note("zh-TW"), gpt_live_config.live_tool_note("en")
    assert "你可以看到我嗎" in zh and "你可以跳舞嗎" in zh and "絕對不要說你看不到" in zh and "不要先問" in zh
    assert "can you see me?" in en and "can you dance?" in en and "never say you cannot see" in en
    assert '"Can you see me?"' in gpt_live_config.BACKEND_INSTRUCTION
    assert '"Can you dance?" is a request' in gpt_live_config.BACKEND_INSTRUCTION
    # the time, web search and reminders (14 Oct 2026)
    assert "現在幾點了" in zh and "東京現在幾點" in zh and "上網搜尋" in zh and "設定提醒" in zh and "不要自己編" in zh
    assert "what time is it in Tokyo?" in en and "search the web" in en and "set the reminder" in en
    for section in ("TIME", "WEB SEARCH", "REMINDERS"):
        assert f"\n{section}\n" in gpt_live_config.BACKEND_INSTRUCTION
    for name in ("get_current_time", "search_web", "set_reminder", "cancel_reminder"):
        assert name in gpt_live_config.TOOL_NAMES and name in gpt_live_config.BACKEND_INSTRUCTION
    assert "沒有時鐘" in live_session.LIVE_RULES_TOOLS["zh-TW"] and "no clock" in live_session.LIVE_RULES_TOOLS["en"]
    assert "不是現在的時間" in live_session.COMPANION_NOTE["zh-TW"] and "not now" in live_session.COMPANION_NOTE["en"]


@pytest.mark.parametrize("language", ["zh-TW", "en"])
def test_the_tool_note_and_celebration_exist_in_both_languages(language):
    assert gpt_live_config.live_tool_note(language) and gpt_live_config.CELEBRATION[language]
    assert "拍拍手" in gpt_live_config.live_tool_note("zh-TW")
