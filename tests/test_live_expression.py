"""Reachy's body language in a live chat (services/live_session.py with services/affect.py): the relay reads the
patient's words and tells the robot what to show (`express`), without any tool call. The regression case is the
chat of 14 Oct 2026 at 00:00:49 (the patient was sad on every turn and the robot stayed neutral), against the weather
chat of 23:55 the same night (nothing to show). The harness is tests/test_live_relay_chat.py's."""

import asyncio

from app.services import live_session
from tests import test_live_relay_chat as relay_chat
from tests.test_live_relay_chat import _begin, _live, _over, _reachy, _said, _sound, _step

world = relay_chat.world   # the harness's fixture, registered in this module too

SAD_CHAT = ["日希我今天很難過", "我孩子都不跟我說話", "日希,你為什麼不愛我", "今天我不能有傷心的表情"]
WEATHER_CHAT = ["今天天氣什麼樣", "你要記得我是在花蓮", "所以花蓮的天氣什麼樣", "你可以跳舞嗎", "我今天情緒什麼樣"]


def _expressions(w):
    return [{k: v for k, v in m.items() if k != "type"} for m in w.robot.json("express")]


async def _turn(w, relay, words, reply="好的", gap=3.0):
    """The patient says `words` (one fragment), GPT-Live starts answering (its sound, then its words), and the
    turn closes after `gap` seconds."""
    await _live(w, _said(words))
    await _live(w, _sound(), _reachy(reply))
    await _step(w, relay, gap)


def test_the_sad_chat_of_14_october_now_moves_the_robot_at_the_first_sad_words_and_as_it_answers(world):
    async def scenario():
        relay, running = await _begin(world)
        await _live(world, _said(SAD_CHAT[0]))
        assert _expressions(world) == [{"affect": "concern", "reason": "patient_words"}]   # while it listens
        await _live(world, _sound(), _reachy("Aisha，我在這陪您。想跟我說說"))
        assert _expressions(world)[1] == {"move": "downcast1", "reason": "reply"}           # as it answers
        await _step(world, relay, 3.0)
        for words in SAD_CHAT[1:]:
            await _turn(world, relay, words, "被這樣對待真的很受傷。")
        # Still concern, no second move within AFFECT_MOVE_GAP_SECONDS, nothing repeated
        assert _expressions(world) == [{"affect": "concern", "reason": "patient_words"},
                                       {"move": "downcast1", "reason": "reply"}]
        for _ in range(3):   # the sad talk goes on past the move gap (the chat must not fall silent: 45 s ends it)
            await _turn(world, relay, "我真的好難過", gap=live_session.AFFECT_MOVE_GAP_SECONDS / 2 + 1)
        assert _expressions(world)[-1] == {"move": "yes_sad1", "reason": "reply"}           # a different move
        assert relay._affect == "sad"
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(world, running)

    asyncio.run(scenario())


def test_everyday_talk_shows_nothing_and_lifts_a_posture_once_the_feeling_is_some_time_ago(world):
    async def scenario():
        relay, running = await _begin(world)
        for words in WEATHER_CHAT:
            await _turn(world, relay, words, "今天台北晴天二十一度")
        assert _expressions(world) == []
        await _turn(world, relay, "我今天很難過")
        assert [m.get("affect") for m in _expressions(world)] == ["concern", None]
        await _turn(world, relay, "對了,今天天氣怎麼樣")                           # 3 s later: the feeling stays
        assert _expressions(world)[-1] == {"move": "downcast1", "reason": "reply"}
        await _step(world, relay, live_session.AFFECT_HOLD_SECONDS)
        await _turn(world, relay, "那明天呢")                                      # everyday talk again, later
        assert _expressions(world)[-1] == {"affect": None, "reason": "neutral_turn"} and relay._affect is None
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(world, running)

    asyncio.run(scenario())


def test_good_news_perks_reachy_up_and_the_replys_tone_catches_sorrow_the_words_did_not_name(world):
    async def scenario():
        relay, running = await _begin(world)
        await _live(world, _said("我得了第一名！"))
        assert _expressions(world) == [{"affect": "joy", "reason": "patient_words"}]
        await _live(world, _sound())
        assert _expressions(world)[-1] == {"move": "cheerful1", "reason": "reply"}
        await _step(world, relay, live_session.AFFECT_HOLD_SECONDS + 1)
        await _turn(world, relay, "他們都搬走了")                                  # no listed word: nothing yet
        assert relay._affect is None
        await _live(world, _said("還好吧"), _sound(), _reachy("被這樣對待真的很受傷。"))
        assert _expressions(world)[-1] == {"affect": "concern", "reason": "reply_tone"} and relay._affect == "sad"
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(world, running)

    asyncio.run(scenario())
