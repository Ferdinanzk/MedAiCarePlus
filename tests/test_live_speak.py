"""The live relay's own voice (LiveRelay.speak, services/live_session.py): a reminder said with the live voice as
binary audio on the live WebSocket, during a running chat (after GPT-Live's sound), while a companion chat is paused,
and as the reminder a check-in task was made for. The harnesses are tests/test_live_relay_chat.py's and
tests/test_live_companion.py's."""

import asyncio
import uuid

import pytest

from app.services import live_session, reminders
from tests import test_live_companion as companion_chat
from tests import test_live_relay_chat as relay_chat
from tests.test_live_relay_chat import _begin, _live, _over, _reachy, _said, _settle, _sound, _step

world = relay_chat.world
safe = companion_chat.safe
companion = companion_chat.companion
PCM = b"\x01\x00" * (16000 * 2)   # two seconds


def _chunks(w):
    return [m for m in w.robot.sent if isinstance(m, bytes)]


async def _fake_sleep(w):
    async def sleep(seconds):
        w.clock.now += seconds
        await asyncio.sleep(0)
    return sleep


def test_a_line_waits_for_gpt_lives_sound_then_plays_mutes_the_microphone_and_is_stored(world):
    async def scenario():
        relay, running = await _begin(world)
        relay.sleep = await _fake_sleep(world)
        notes = []
        world.clients[-1].instructions_append = lambda text: notes.append(text) or asyncio.sleep(0)
        await _live(world, _said("今天天氣怎麼樣"), _sound(), _reachy("今天晴天"))
        before = len(_chunks(world))
        speaking = asyncio.create_task(relay.speak("時間到了，請記得吃藥。", PCM))
        await _settle()
        assert len(_chunks(world)) == before                      # GPT-Live is still making sound
        await _step(world, relay, live_session.TAIL_SECONDS + 0.5)   # its sound ended
        assert await asyncio.wait_for(speaking, 2)
        sent = _chunks(world)[before:]                            # the released reply audio, then the line
        assert b"".join(sent).endswith(PCM) and all(len(c) <= live_session.SPEAK_CHUNK_BYTES for c in sent[-20:])
        assert world.db.rows()[-1] == ("reachy", "時間到了，請記得吃藥。")
        assert world.db.turns[-1]["metrics"] == {"live": {"say": "reminder"}}
        assert notes == [live_session.SPOKEN_NOTE["zh-TW"].format(text="時間到了，請記得吃藥。")]
        # the microphone reaches GPT-Live as silence while the line plays (its echo is no patient speech)
        heard = len(world.clients[-1].audio)
        world.robot.inbox.put_nowait(b"\x07\x07" * 320)
        await _settle()
        assert world.clients[-1].audio[heard:] == [bytes(640)]
        world.clock.now += 3.0
        world.robot.inbox.put_nowait(b"\x07\x07" * 320)
        await _settle()
        assert world.clients[-1].audio[-1] == b"\x07\x07" * 320
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(world, running)

    asyncio.run(scenario())


def test_no_line_without_audio_after_a_risk_or_once_the_chat_is_over(world):
    async def scenario():
        relay, running = await _begin(world)
        relay.sleep = await _fake_sleep(world)
        assert not await relay.speak("x", None)
        relay._risk = ["r"]
        assert not relay.can_speak() and not await relay.speak("x", PCM)
        relay._risk = None
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(world, running)
        assert not relay.can_speak() and not await relay.speak("x", PCM)

    asyncio.run(scenario())


def test_a_line_waits_while_the_robot_still_plays_and_gives_up_after_speak_wait_seconds(world):
    async def scenario():
        relay, running = await _begin(world)
        relay.sleep = await _fake_sleep(world)
        world.robot.inbox.put_nowait({"type": "playing"})
        await _settle()
        assert relay._robot_playing
        before = len(_chunks(world))
        assert not await relay.speak("x", PCM)                    # never idle: given up, nothing sent
        assert len(_chunks(world)) == before
        world.robot.inbox.put_nowait({"type": "idle"})
        await _settle()
        assert await relay.speak("x", PCM) and len(_chunks(world)) > before
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(world, running)

    asyncio.run(scenario())


def test_a_paused_companion_chat_plays_the_line_and_keeps_no_echo_as_pre_roll(companion):
    async def scenario():
        relay, running = await companion_chat._start(companion)
        assert relay._phase == "paused"
        before = len(_chunks(companion))
        assert await relay.speak("時間到了，請吃第一種藥，Adapalene。", PCM)
        assert b"".join(_chunks(companion)[before:]) == PCM
        companion.robot.inbox.put_nowait(b"\x07\x07" * 320)      # the robot's microphone during the line
        await _settle()
        assert relay._pre_roll_bytes == 0
        assert companion.db.rows()[-1] == ("reachy", "時間到了，請吃第一種藥，Adapalene。")
        companion.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(companion, running)

    asyncio.run(scenario())


def test_a_checkin_task_made_for_a_reminder_opens_with_a_lead_in_then_says_it_after_the_opening(companion, monkeypatch):
    rid = str(uuid.uuid4())
    taken, said = [], []
    base = companion.db.fetchrow

    async def fetchrow(query, *args):
        row = await base(query, *args)
        if row is not None and "FROM reachy_task" in query:
            row = {**row, "detail": {"reminder": {"reminder_id": rid, "text": "時間到了，請吃第一種藥，Adapalene。"}}}
        return row

    async def take_for_robot(reminder_id):
        taken.append(reminder_id)
        return {"reminder_id": reminder_id, "text": "時間到了，請吃第一種藥，Adapalene。", "language": "zh-TW", "status": "queued_robot"}

    async def audio(text, language):
        return PCM

    async def said_by_robot(reminder_id, via):
        said.append((reminder_id, via))

    monkeypatch.setattr(companion.db, "fetchrow", fetchrow)
    monkeypatch.setattr(reminders, "take_for_robot", take_for_robot)
    monkeypatch.setattr(reminders, "audio", audio)
    monkeypatch.setattr(reminders, "said_by_robot", said_by_robot)

    async def scenario():
        relay, running = await companion_chat._start(companion)
        ready = companion.robot.json("ready")[0]
        assert ready["speech_text"] == "王奶奶，我来提醒您一件事。"       # the lead-in, in the robot's voice
        await _settle()
        assert _chunks(companion) == [] and taken == [rid]            # waits for the robot to say the opening
        companion.robot.inbox.put_nowait({"type": "playing"})
        await _settle()
        assert _chunks(companion) == []
        companion.robot.inbox.put_nowait({"type": "idle"})
        for _ in range(20):
            await _settle()
        assert b"".join(_chunks(companion)) == PCM and said == [(rid, "robot_live")]
        assert companion.db.rows() == [("reachy", "王奶奶，我來提醒您一件事。"), ("reachy", "時間到了，請吃第一種藥，Adapalene。")]
        companion.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(companion, running)

    asyncio.run(scenario())


def test_a_task_without_a_reminder_opens_as_before(companion):
    async def scenario():
        relay, running = await companion_chat._start(companion)
        assert companion.robot.json("ready")[0]["speech_text"] == "王奶奶，今天感觉怎么样？想跟我聊聊吗？"
        assert relay._reminder is None
        companion.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(companion, running)

    asyncio.run(scenario())


@pytest.mark.parametrize("task, expected", [
    ({"task_id": "t"}, None),
    ({"task_id": "t", "detail": None}, None),
    ({"task_id": "t", "detail": '{"reminder": {"reminder_id": "r-1", "text": "x"}}'}, {"reminder_id": "r-1", "text": "x"}),
    ({"task_id": "t", "detail": {"retry_due": {}}}, None),
    ({"task_id": "t", "detail": "not json"}, None),
])
def test_the_reminder_of_a_task_is_read_from_its_detail(task, expected):
    assert live_session._reminder_of(task) == expected
