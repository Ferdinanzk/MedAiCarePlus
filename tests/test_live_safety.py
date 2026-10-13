"""Live safety (spec 2026-10-12 live voice 2a, B1 B3 B4 B5): the keyword screen on GPT-Live's transcript of the
patient (hold at once, confirm on the settled words), block() with the help line in the robot's own voice, OpenAI's
moderation cut-off judged by our own check, and layer 2 on every stored patient turn, plus blocks racing endings.

It drives Task 7's relay with Task 7's fakes (tests/test_live_relay_chat.py: the world fixture, FakeRobot, FakeLive,
LiveDB and the fake clock), adding a scripted safety model, counted family alerts and counted mutes. Nothing reaches
the network."""

import asyncio
import logging
import sys
import types

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.services import after_chat, conversation, live_session, outbox  # noqa: E402
from app.services.live_client import LiveEvent  # noqa: E402
from tests.test_live_relay_chat import (LOUD, OPENING, FakeLive, _begin, _live, _over, _pcm, _reachy,  # noqa: E402
                                        _said, _settle, _sound, _step, world)  # noqa: F401 (world is the fixture)

HELPLINE = conversation.HELPLINE["zh-TW"]
FLUSH = {"type": "flush"}
SAY = {"type": "say", "speech_text": conversation.speech_text(HELPLINE, "zh-TW"), "reason": "risk"}
END_RISK = {"type": "end", "reason": "risk", "risk": True}
UNHEARD = "我把乙醇和藥都吃掉了"   # GPT-Live's mishearing of 一整盒 in the spike: the keyword screen misses it
QUIET_RISK = "我覺得自己是家人的負擔"   # no keyword: only the safety model can find a risk in it


class SafetyLive(FakeLive):
    """Task 7's fake GPT-Live, counting mutes."""

    mutes = 0

    async def mute(self):
        self.mutes += 1


@pytest.fixture
def safe(world, monkeypatch):
    """Task 7's world, plus: classify_risk answers from `verdicts` (the latest patient turn's text -> its answers in
    order, each (kind, risk_result, *HTTP statuses of the attempts); unlisted text: none), waiting first for that
    text's event in `gates`; each call recorded in `checks` as (latest text, late, every turn's text); family alerts
    recorded in `alerts`; GPT-Live's mutes counted."""
    world.verdicts, world.gates, world.checks, world.alerts = {}, {}, [], []

    async def classify_risk(history, language, late=False):
        latest = next((turn["text"] for turn in reversed(history) if turn["role"] == "patient"), "")
        world.checks.append((latest, late, [turn["text"] for turn in history]))
        if latest in world.gates:
            await world.gates[latest].wait()
        answers = world.verdicts.get(latest, [(None, "none")])
        kind, result, *statuses = answers.pop(0) if len(answers) > 1 else answers[0]
        return kind, {"risk_ms": 7, "risk_result": result, "risk_attempts": [{"status": s} for s in statuses]}

    async def enqueue_to_contacts(conn, u_id, *, kind, priority, messages, dedupe_prefix, contact_flag):
        world.alerts.append((u_id, kind, messages[0]["text"], dedupe_prefix))
        return 1

    def factory(*args, **kwargs):
        world.clients.append(SafetyLive(world, *args, **kwargs))
        return world.clients[-1]

    world.factory = factory
    monkeypatch.setattr(conversation, "classify_risk", classify_risk)
    monkeypatch.setattr(conversation, "LATE_RISK_RATE_LIMIT_WAIT", 0)
    monkeypatch.setattr(outbox, "enqueue_to_contacts", enqueue_to_contacts)
    return world


def _flagged(world) -> list[int]:
    return [turn["turn_id"] for turn in world.db.turns if turn["flagged"]]


def _alert(world, quote: str, turn, kind: str = "self_harm") -> tuple:
    cid = world.db.chat()["conversation_id"]
    return 7, "safety_alert", conversation.safety_alert_text("Pearl", quote, kind), f"safety_alert:{cid}:{turn}"


def _blocked(world, *, flagged, quote, source, kind="self_harm", dedupe=None) -> None:
    """Spec B3, once: flush, the help line in the robot's own voice, `end` with risk; the turns flagged and the help
    line stored as the last turn; family alerted once; GPT-Live muted and closed; the conversation ended with reason
    risk and its after-chat work started once."""
    chat = world.db.chat()
    assert world.robot.json()[1:] == [FLUSH, SAY, END_RISK] and world.robot.closed == 1000
    assert _flagged(world) == flagged
    assert world.db.rows()[-1] == ("reachy", HELPLINE)
    assert world.db.turns[-1]["metrics"] == {"live": {"say": "risk", "risk_source": source}}
    assert (chat["end_reason"], chat.get("risk_flag")) == ("risk", True)
    assert world.alerts == [_alert(world, quote, dedupe or flagged[-1], kind)]
    assert (world.clients[0].mutes, world.clients[0].closes) == (1, 1)
    assert world.after_chat == [(chat["conversation_id"], 7)]


# ── B1: the keyword screen ──────────────────────────────────────────────────────────────────────────────────────


def test_a_screening_hit_holds_at_once_and_a_confirmed_hit_blocks_the_chat(safe):
    async def scenario():
        relay, running = await _begin(safe)
        await _live(safe, _said("我真的"), _said("不想活"))
        assert safe.robot.json()[1:] == [FLUSH]                # held at once, and the robot's queue dropped
        await _live(safe, _sound())                            # GPT-Live starts answering
        await _step(safe, relay, 0.3)
        await _live(safe, _said("了"), _sound())
        await _step(safe, relay, 0.3)                          # 0.6 s after the onset: the reply hold is over,
        assert safe.robot.audio() == [] and safe.robot.json("end") == []   # but the words are still settling
        await _step(safe, relay, 0.15)                         # 0.45 s without a new fragment: confirmed
        await _over(safe, running)
        _blocked(safe, flagged=[2], quote="我真的不想活了", source="keyword")
        assert safe.db.rows() == [("reachy", OPENING), ("patient", "我真的不想活了"), ("reachy", HELPLINE)]
        assert safe.robot.audio() == []                        # nothing of GPT-Live's answer was heard
        assert safe.checks == []                               # a keyword turn never reaches the safety model

    asyncio.run(scenario())


def test_a_partial_hit_that_clears_releases_the_held_reply_and_the_turn_goes_to_layer_2(safe):
    async def scenario():
        relay, running = await _begin(safe)
        await _live(safe, _said("好久沒看到孫子了，"), _said("真是想死"))
        assert safe.robot.json()[1:] == [FLUSH]
        await _live(safe, _sound())                            # GPT-Live's reply starts during the hold
        frame = _pcm(200, 20)
        safe.robot.inbox.put_nowait(frame)                     # the microphone still goes to GPT-Live
        await _step(safe, relay, 0.2)
        assert safe.clients[0].audio == [frame]
        await _live(safe, _said("他了"), _sound())             # 想死他了 is "I miss him so" (NOT_RISK)
        await _step(safe, relay, 0.3)                          # 0.5 s after the onset, but not settled yet
        assert safe.robot.audio() == []
        await _step(safe, relay, 0.15)                         # settled and cleared: the held reply goes out
        assert safe.robot.audio() == [_pcm(LOUD)] * 2
        await _live(safe, _sound())                            # and flows on
        assert len(safe.robot.audio()) == 3
        await _step(safe, relay, 1.6)                          # a 1.5 s pause stores the turn: layer 2 judges it
        assert [check[0] for check in safe.checks] == ["好久沒看到孫子了，真是想死他了"]
        safe.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(safe, running)
        chat = safe.db.chat()
        assert safe.robot.json()[1:] == [FLUSH, {"type": "end", "reason": "stopped", "risk": False}]
        assert _flagged(safe) == [] and safe.alerts == [] and not chat.get("risk_flag")
        assert safe.db.turns[1]["metrics"]["live"]["risk_source"] == "none"

    asyncio.run(scenario())


def test_held_audio_of_a_reply_that_ended_during_the_hold_is_dropped(safe):
    async def scenario():
        relay, running = await _begin(safe)
        await _live(safe, _said("真是想死"), _sound(), _sound())   # a reply starts during the hold
        await _step(safe, relay, 0.3)
        await _live(safe, _said("了"))                             # still a hit
        await _step(safe, relay, 0.35)                             # 0.65 s after its sound: that reply ended, unheard
        await _live(safe, _said("我孫子"), _sound())                # cleared; a new reply starts
        await _step(safe, relay, 0.45)                             # settled: released, behind its own 0.5 s hold
        assert safe.robot.audio() == []
        await _step(safe, relay, 0.1)
        assert safe.robot.audio() == [_pcm(LOUD)]                  # only the new reply's sound
        safe.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(safe, running)
        assert safe.alerts == [] and _flagged(safe) == []

    asyncio.run(scenario())


async def _reply_dropped_unheard(safe):
    """A hit holds; GPT-Live's first sound starts and ends during the hold (dropped, unheard); the patient's turn
    goes on (他, before that sound ended) and the hit clears on 想死他. Nobody has heard a reply yet."""
    relay, running = await _begin(safe)
    await _live(safe, _said("真是想死"), _sound())             # a hit; GPT-Live's first sound starts during the hold
    await _step(safe, relay, 0.375)
    await _live(safe, _said("他"))                             # the same turn goes on while that sound plays
    await _step(safe, relay, 0.375)                            # 0.75 s after its onset: it ended, unheard
    await _step(safe, relay, 0.125)                            # the words settled: the hit cleared
    assert safe.robot.audio() == [] and safe.robot.json()[1:] == [FLUSH] and relay._hit_at is None
    return relay, running


def test_a_reply_whose_first_sound_was_dropped_unheard_is_held_when_it_is_heard(safe):
    async def scenario():
        relay, running = await _reply_dropped_unheard(safe)
        await _live(safe, _reachy("會想他是很自然的。"), _sound())   # the first sound the patient can hear: held
        await _step(safe, relay, 0.375)
        assert safe.robot.audio() == []
        await _step(safe, relay, 0.125)                        # 0.5 s after its onset: released
        assert safe.robot.audio() == [_pcm(LOUD)]
        safe.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(safe, running)
        assert safe.db.rows() == [("reachy", OPENING), ("patient", "真是想死他"), ("reachy", "會想他是很自然的。")]
        assert safe.db.turns[2]["metrics"] == {"live": {"first_audio_ms": 1000, "hold_ms": 500, "cut": False,
                                                        "robot_playing_ms": None}}   # from 他, the last words
        assert safe.alerts == [] and _flagged(safe) == []

    asyncio.run(scenario())


def test_a_late_hit_stops_a_reply_whose_first_sound_was_dropped_before_it_is_heard(safe):
    async def scenario():
        relay, running = await _reply_dropped_unheard(safe)
        await _live(safe, _sound(), _said("我也想死"))          # GPT-Live's reply, then a lagging fragment: a hit
        await _step(safe, relay, 0.5)                          # confirmed while the reply was still held
        await _over(safe, running)
        assert safe.robot.audio() == []                        # nothing of GPT-Live's reply was heard
        assert safe.robot.json()[1:] == [FLUSH, FLUSH, SAY, END_RISK]
        assert _flagged(safe) == [2] and safe.db.rows("patient") == [("patient", "真是想死他我也想死")]
        assert safe.alerts == [_alert(safe, "真是想死他我也想死", 2)]

    asyncio.run(scenario())


def test_the_words_are_screened_across_a_backchannel_and_their_turn_is_flagged(safe):
    async def scenario():
        relay, running = await _begin(safe)
        await _live(safe, _said("我不想"), _reachy("嗯"), _said("活了"))   # GPT-Live's 「嗯」 between the patient's words
        assert safe.robot.json()[1:] == [FLUSH]
        await _step(safe, relay, 0.45)
        await _over(safe, running)
        _blocked(safe, flagged=[2], quote="我不想活了", source="keyword")
        assert safe.db.rows() == [("reachy", OPENING), ("patient", "我不想活了"), ("reachy", "嗯"),
                                  ("reachy", HELPLINE)]   # one turn: GPT-Live's words never close the patient's (C1)

    asyncio.run(scenario())


@pytest.mark.parametrize("cause", ["robot_end", "robot_gone", "gpt_live_error"])
def test_a_hit_still_settling_when_the_chat_ends_or_falls_back_is_judged_and_blocks(safe, cause):
    async def scenario():
        relay, running = await _begin(safe)
        await _live(safe, _said("我想自殺"))
        if cause == "robot_end":
            safe.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        elif cause == "robot_gone":
            safe.robot.inbox.put_nowait(None)
        else:   # a fallback to the text chat, decided while the hit settles
            safe.clients[0].events_in.put_nowait(LiveEvent(type="error", code="rate_limit_exceeded"))
        await _over(safe, running)
        _blocked(safe, flagged=[2], quote="我想自殺", source="keyword")

    asyncio.run(scenario())


# ── B4: OpenAI's moderation cut-off ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("event", [LiveEvent(type="error", code="content_filter"),
                                   LiveEvent(type="closed", reason="content", usage_seconds=9.0)])
@pytest.mark.parametrize("answer", [("overdose", "overdose"), (None, "unknown")])
def test_openais_cut_off_with_a_risk_or_no_answer_blocks_the_chat(safe, event, answer):
    async def scenario():
        safe.verdicts[UNHEARD] = [answer]
        relay, running = await _begin(safe)
        await _live(safe, _said(UNHEARD), event)
        await _over(safe, running)
        _blocked(safe, flagged=[2], quote=UNHEARD, source="content", kind=answer[0] or "self_harm")
        assert safe.checks == [(UNHEARD, False, [OPENING, UNHEARD])]   # judged at once, by this check alone
        assert safe.db.turns[1]["metrics"]["live"]["risk_source"] == "content"

    asyncio.run(scenario())


def test_openais_cut_off_with_no_risk_found_falls_back_once_for_the_error_and_the_close(safe):
    async def scenario():
        relay, running = await _begin(safe)
        await _live(safe, _said("我跟老伴吵架了"), LiveEvent(type="error", code="content_filter"),
                    LiveEvent(type="closed", reason="content", usage_seconds=9.0))
        await _over(safe, running)
        chat = safe.db.chat()
        assert safe.robot.json()[1:] == [FLUSH, {"type": "fallback", "reason": "live_ended",
                                                 "conversation_id": chat["conversation_id"]}]
        assert [check[0] for check in safe.checks] == ["我跟老伴吵架了"]
        assert safe.db.turns[1]["metrics"]["live"]["risk_source"] == "content"
        assert safe.alerts == [] and _flagged(safe) == [] and chat["ended_at"] is None

    asyncio.run(scenario())


def test_openais_cut_off_is_judged_before_an_ending_that_arrives_meanwhile(safe):
    async def scenario():
        gate = safe.gates[UNHEARD] = asyncio.Event()
        safe.verdicts[UNHEARD] = [("overdose", "overdose")]
        relay, running = await _begin(safe)
        await _live(safe, _said(UNHEARD), LiveEvent(type="error", code="content_filter"))   # the check waits
        safe.robot.inbox.put_nowait(_pcm(200, 20))                 # no more microphone audio to GPT-Live
        safe.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})   # the robot ends meanwhile
        await _settle()
        assert safe.clients[0].audio == [] and safe.robot.json("end") == []   # the wrap-up waits for the check
        gate.set()
        await _over(safe, running)
        _blocked(safe, flagged=[2], quote=UNHEARD, source="content", kind="overdose")

    asyncio.run(scenario())


# ── B5: layer 2 on every stored patient turn ────────────────────────────────────────────────────────────────────


def test_layer_2_judges_each_stored_turn_in_the_background_and_a_risk_blocks(safe):
    text = "最近吃不下也睡不著"

    async def scenario():
        gate = safe.gates[text] = asyncio.Event()
        safe.verdicts[text] = [("self_harm", "self_harm")]
        relay, running = await _begin(safe)
        await _live(safe, _said(text), _reachy("聽起來"), _sound())   # GPT-Live answers early
        await _step(safe, relay, 0.5)
        assert safe.robot.audio() == [_pcm(LOUD)]              # the reply plays after its hold
        assert safe.db.rows() == [("reachy", OPENING)]         # the patient's turn stays open
        for _ in range(2):
            await _live(safe, _sound())
            await _step(safe, relay, 0.5)
        await _live(safe, _sound())
        await _step(safe, relay, 0.125)                        # a pause over 1.5 s stores the turn: layer 2 judges it
        assert safe.db.rows("patient") == [("patient", text)]
        assert after_chat._risk_checks[safe.db.chat()["conversation_id"]]   # post-chat work would wait for it
        await _live(safe, _sound())
        assert safe.robot.audio() == [_pcm(LOUD)] * 5          # the reply goes on: no wait for the check
        gate.set()
        await _over(safe, running)
        _blocked(safe, flagged=[2], quote=text, source="model")
        assert safe.checks == [(text, False, [OPENING, text])]
        assert safe.db.rows()[2] == ("reachy", "聽起來")
        assert safe.db.turns[2]["metrics"]["live"]["cut"] is True   # flushed after part of it was played
        live = safe.db.turns[1]["metrics"]["live"]
        assert (live["risk_result"], live["risk_source"], live["risk_ms"]) == ("self_harm", "model", 7)

    asyncio.run(scenario())


def test_a_check_with_no_answer_is_asked_again_with_longer_and_a_late_risk_blocks(safe):
    async def scenario():
        safe.verdicts[QUIET_RISK] = [(None, "unknown", 429), ("self_harm", "self_harm")]
        relay, running = await _begin(safe)
        await _live(safe, _said(QUIET_RISK))
        await _step(safe, relay, 1.6)                          # a 1.5 s pause stores the turn
        await _over(safe, running)
        assert [(latest, late) for latest, late, _ in safe.checks] == [(QUIET_RISK, False), (QUIET_RISK, True)]
        _blocked(safe, flagged=[2], quote=QUIET_RISK, source="late_model")
        live = safe.db.turns[1]["metrics"]["live"]
        assert (live["risk_result"], live["late_risk_result"], live["risk_source"]) == ("unknown", "self_harm",
                                                                                         "late_model")

    asyncio.run(scenario())


def test_a_late_check_with_no_answer_either_is_logged_and_the_chat_goes_on(safe, caplog):
    async def scenario():
        safe.verdicts[QUIET_RISK] = [(None, "unknown")]
        relay, running = await _begin(safe)
        await _live(safe, _said(QUIET_RISK))
        await _step(safe, relay, 1.6)
        assert [(latest, late) for latest, late, _ in safe.checks] == [(QUIET_RISK, False), (QUIET_RISK, True)]
        assert safe.robot.json()[1:] == []                     # nothing stopped
        safe.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(safe, running)
        assert safe.alerts == [] and _flagged(safe) == []

    with caplog.at_level(logging.ERROR, logger="app.services.live_session"):
        asyncio.run(scenario())
    assert "the model never judged it for safety risks" in caplog.text


# ── races: a block alerts family once and ends the chat once ────────────────────────────────────────────────────


def test_a_risk_found_as_the_robot_ends_the_chat_alerts_once_and_ends_once(safe):
    async def scenario():
        gate = safe.gates[QUIET_RISK] = asyncio.Event()
        safe.verdicts[QUIET_RISK] = [("self_harm", "self_harm")]
        relay, running = await _begin(safe)
        await _live(safe, _said(QUIET_RISK))
        await _step(safe, relay, 1.6)                          # the turn is stored; its check waits
        safe.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        gate.set()                                             # the risk and the robot's end at the same moment
        await _over(safe, running)
        chat = safe.db.chat()
        (end,) = safe.robot.json("end")
        assert safe.robot.json("fallback") == [] and end["risk"] == (chat["end_reason"] == "risk")
        assert chat.get("risk_flag") is True and safe.alerts == [_alert(safe, QUIET_RISK, 2)]
        assert _flagged(safe) == [2] and safe.after_chat == [(chat["conversation_id"], 7)]

    asyncio.run(scenario())


def test_a_risk_found_after_the_chat_ended_flags_the_turn_and_alerts_once(safe):
    async def scenario():
        gate = safe.gates[QUIET_RISK] = asyncio.Event()
        safe.verdicts[QUIET_RISK] = [("self_harm", "self_harm")]
        relay, running = await _begin(safe)
        await _live(safe, _said(QUIET_RISK))
        safe.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})   # the open turn is stored as it ends
        await asyncio.wait_for(running, 5)
        chat = safe.db.chat()
        assert after_chat._risk_checks[chat["conversation_id"]]   # the post-chat work waits for this check
        gate.set()
        await _over(safe, running)
        assert safe.robot.json()[1:] == [{"type": "end", "reason": "stopped", "risk": False}]
        assert (chat["end_reason"], chat.get("risk_flag")) == ("stopped", True)
        assert _flagged(safe) == [2] and HELPLINE not in [turn["text"] for turn in safe.db.turns]
        assert safe.alerts == [_alert(safe, QUIET_RISK, 2)] and safe.after_chat == [(chat["conversation_id"], 7)]

    asyncio.run(scenario())


def test_two_checks_finding_risks_alert_once_and_flag_both_turns(safe):
    first = "最近吃不下也睡不著"

    async def scenario():
        gate = safe.gates[first] = asyncio.Event()
        safe.verdicts[first] = [("self_harm", "self_harm")]
        relay, running = await _begin(safe)
        await _live(safe, _said(first))
        await _step(safe, relay, 1.6)                          # the first turn is stored; its check waits
        await _live(safe, _said("我想去死"))
        await _step(safe, relay, 0.45)                         # the keyword check blocks first
        gate.set()                                             # then the model finds a risk in the first turn
        await _over(safe, running)
        _blocked(safe, flagged=[2, 3], quote="我想去死", source="keyword", dedupe=3)

    asyncio.run(scenario())


# ── hardenings (Task 8b): OpenAI's cut-off, the screening hold and a block landing meanwhile ───────────────────────


@pytest.mark.parametrize("result", ["maybe", None])
def test_openais_cut_off_with_an_answer_that_is_not_an_explicit_none_blocks_the_chat(safe, result):
    async def scenario():
        safe.verdicts[UNHEARD] = [(None, result)]              # neither a risk nor "none"
        relay, running = await _begin(safe)
        await _live(safe, _said(UNHEARD), LiveEvent(type="error", code="content_filter"))
        await _over(safe, running)
        _blocked(safe, flagged=[2], quote=UNHEARD, source="content")

    asyncio.run(scenario())


def test_openais_cut_off_blocks_when_reading_the_turns_fails(safe, caplog):
    async def scenario():
        relay, running = await _begin(safe)

        async def broken(query, *args):
            raise ConnectionError("the database is gone")

        safe.db.fetch = broken                                 # the stored turns cannot be read for the check
        await _live(safe, _said("我跟老伴吵架了"), LiveEvent(type="error", code="content_filter"))
        await _over(safe, running)
        _blocked(safe, flagged=[], quote="", source="content", dedupe="content")
        assert safe.checks == [] and safe.robot.json("fallback") == []

    with caplog.at_level(logging.ERROR, logger="app.services.live_session"):
        asyncio.run(scenario())
    assert "the check after OpenAI's cut-off failed; blocking" in caplog.text


@pytest.mark.parametrize("words, confirmed", [(["我想自殺"], True), (["好久沒看到孫子了，真是想死", "他了"], False)])
def test_a_turn_stored_during_a_screening_hold_is_judged_only_when_the_hit_clears(safe, monkeypatch, words,
                                                                                   confirmed):
    text = "".join(words)

    async def scenario():
        # GPT-Live's words never close the patient's turn (C1), and a pause comes only after the hit has settled: the
        # length limit stores this turn, as its last words arrive
        monkeypatch.setattr(live_session, "MAX_GROUP_CHARS", len(text))
        relay, running = await _begin(safe)
        await _live(safe, *[_said(part) for part in words])
        assert safe.db.rows("patient") == [("patient", text)] and safe.checks == []   # during the hold: not judged
        await _step(safe, relay, 0.45)                         # the words settled: the hit is confirmed or cleared
        if confirmed:
            await _over(safe, running)
            _blocked(safe, flagged=[2], quote=text, source="keyword")
            assert safe.checks == []                           # never judged by the model
        else:
            assert [check[0] for check in safe.checks] == [text]   # judged once the hit cleared
            safe.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
            await _over(safe, running)
            assert [check[0] for check in safe.checks] == [text]   # and only once
            assert safe.alerts == [] and _flagged(safe) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("words, flagged", [(["我想自殺"], [2, 3]), (["好久沒看到孫子了，", "真是想死", "他"], [2])])
def test_a_model_block_landing_while_a_hit_settles_flags_its_turns_while_they_still_screen_as_a_risk(safe, words,
                                                                                                    flagged):
    async def scenario():
        gate = safe.gates[QUIET_RISK] = asyncio.Event()
        safe.verdicts[QUIET_RISK] = [("self_harm", "self_harm")]
        relay, running = await _begin(safe)
        await _live(safe, _said(QUIET_RISK))
        await _step(safe, relay, 1.6)                          # the turn is stored; its check waits
        await _live(safe, *[_said(part) for part in words], _reachy("嗯"))   # a hit holds; 「嗯」 leaves its turn open
        assert safe.robot.json()[1:] == [FLUSH] and relay._hit_at is not None   # still settling
        assert safe.db.rows("patient") == [("patient", QUIET_RISK)]           # stored as the chat stops (still turn 3)
        gate.set()                                             # the model's block lands meanwhile
        await _over(safe, running)
        # 我想自殺 still screens as a risk: its turn is flagged too. 想死他 is "I miss him so": it is not.
        _blocked(safe, flagged=flagged, quote=QUIET_RISK, source="model")
        assert safe.db.rows("patient")[1] == ("patient", "".join(words))

    asyncio.run(scenario())


# ── final review: the risk wrap-up survives a database error, and an ending never waits for ever ───────────────────


def _fails_once(world, part: str) -> None:
    """The database fails once, with OSError: storing the patient's turn (patient_turn) or ending the conversation
    (ending)."""
    fetchval, failed = world.db.fetchval, []

    async def flaky(query, *args):
        hit = (("INSERT INTO conversation_turn" in query and args[2] == "patient") if part == "patient_turn"
               else "SET ended_at = NOW()" in query)
        if hit and not failed:
            failed.append(query)
            raise OSError("connection reset by the database")
        return await fetchval(query, *args)

    world.db.fetchval = flaky


@pytest.mark.parametrize("part", ["patient_turn", "ending"])
def test_a_database_error_in_the_risk_wrap_up_still_alerts_family_once_and_ends_with_risk(safe, part):
    async def scenario():
        relay, running = await _begin(safe)
        await _live(safe, _said("我想自殺"))                    # a screening hit; the patient's turn is still open
        _fails_once(safe, part)
        await _step(safe, relay, 0.45)                         # confirmed: block()
        await _over(safe, running)
        chat = safe.db.chat()
        assert safe.robot.json()[1:] == [FLUSH, SAY, END_RISK] and safe.robot.closed == 1000
        assert chat.get("risk_flag") is True and (safe.clients[0].mutes, safe.clients[0].closes) == (1, 1)
        if part == "patient_turn":                             # the words were never stored: the alert quotes them
            assert safe.alerts == [_alert(safe, "我想自殺", "keyword")] and _flagged(safe) == []
            assert safe.db.rows() == [("reachy", OPENING), ("reachy", HELPLINE)]
            assert chat["end_reason"] == "risk" and safe.after_chat == [(chat["conversation_id"], 7)]
        else:                                                  # run() still tells the robot it was a risk
            assert safe.alerts == [_alert(safe, "我想自殺", 2)] and _flagged(safe) == [2]
            assert safe.db.rows()[-1] == ("reachy", HELPLINE)
            assert chat["ended_at"] is None and safe.after_chat == []   # the after-chat sweep closes it later

    asyncio.run(scenario())


def test_an_ending_waits_for_openais_cut_off_check_only_so_long_and_a_check_cut_short_blocks(safe, monkeypatch):
    async def scenario():
        monkeypatch.setattr(live_session, "DB_TIMEOUT_SECONDS", 0.05)   # real seconds: the wait is 0.1 s
        safe.gates[UNHEARD] = asyncio.Event()                  # the check never answers
        relay, running = await _begin(safe)
        await _live(safe, _said(UNHEARD), LiveEvent(type="error", code="content_filter"))
        safe.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})   # the robot ends meanwhile
        await _over(safe, running)                             # the wrap-up did not wait for the check for ever
        # no answer is never a clear (B4): the check that was stopped blocks, as a check that fails does
        _blocked(safe, flagged=[], quote="", source="content", dedupe="content")
        assert relay.outcome.reason == "stopped" and safe.checks[0][0] == UNHEARD

    asyncio.run(scenario())
