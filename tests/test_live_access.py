"""A running live chat re-checks its access (spec 2026-10-12 live voice 2a, A2 and E1). Every ACCESS_RECHECK_SECONDS
the relay re-reads what the text chat checks at every call: the robot not unpaired, the patient's live switch on,
robot consent and check-in consent current. When one is gone the chat ends with reason access_withdrawn, as any other
ending, so the patient's voice stops going to OpenAI. A failed read is logged and tried again, a slow one never holds
up the timers or runs twice at once, and a risk found at the same moment is still carried out as a block.

It drives Task 7's relay with Task 7's fakes (tests/test_live_relay_chat.py: the world fixture with its consent state
and live switch, FakeRobot, FakeLive, LiveDB and the fake clock) and, for the blocks, Task 8's safe fixture
(tests/test_live_safety.py). Nothing reaches the network."""

import asyncio
import logging
import sys
import types

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.services import consent_service  # noqa: E402
from tests.test_live_relay_chat import (_begin, _ended, _live, _over, _pcm, _said, _settle, _step,  # noqa: E402
                                        world)  # noqa: F401 (world is a fixture)
from tests.test_live_safety import QUIET_RISK, _blocked, safe  # noqa: E402,F401 (safe is a fixture)

LOGGER = "app.services.live_session"
END_WITHDRAWN = {"type": "end", "reason": "access_withdrawn", "risk": False}


def _revoked(w):
    w.db.live_switch = None   # the robot was unpaired: LIVE_SWITCH_SQL finds no row


def _switched_off(w):
    w.db.live_switch = False   # the patient's live switch on the Reachy card


def _robot_consent_withdrawn(w):
    w.consent = {**w.consent, "robot_camera": {**w.consent["robot_camera"], "granted": False}}


def _checkin_consent_withdrawn(w):
    w.consent = {scope: value for scope, value in w.consent.items() if scope != "safety_alerts"}


WITHDRAWALS = {"revoked": _revoked, "live_switch_off": _switched_off, "robot_consent": _robot_consent_withdrawn,
               "checkin_consent": _checkin_consent_withdrawn}


def _warnings(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_the_chat_re_checks_its_access_every_10_s_and_goes_on_while_it_is_allowed(world):
    async def scenario():
        relay, running = await _begin(world)
        await _step(world, relay, 9.5)
        assert world.db.access_reads == 0                      # no re-check before 10 s
        await _step(world, relay, 0.5)
        assert world.db.access_reads == 1                      # one at 10 s
        await _step(world, relay, 9.5)
        assert world.db.access_reads == 1
        await _step(world, relay, 0.5)
        assert world.db.access_reads == 2                      # the next at 20 s
        assert relay.outcome is None and world.clients[0].closes == 0   # everything allowed: the chat goes on
        assert world.robot.json() == [{"type": "ready", "conversation_id": relay.conversation_id}]
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(world, running)
        _ended(world, "stopped")

    asyncio.run(scenario())


@pytest.mark.parametrize("withdrawal", list(WITHDRAWALS))
def test_access_withdrawn_during_the_chat_ends_it_at_the_next_re_check(world, caplog, withdrawal):
    async def scenario():
        relay, running = await _begin(world)
        heard = _pcm(200, 20)
        world.robot.inbox.put_nowait(heard)
        await _step(world, relay, 9.5)
        await _live(world, _said("我等一下要去散步"))
        WITHDRAWALS[withdrawal](world)
        assert world.db.rows("patient") == []                  # the patient's turn is still open
        await _step(world, relay, 0.5)                         # 10 s: the re-check finds access gone
        world.robot.inbox.put_nowait(_pcm(200, 20))            # the microphone after the outcome
        await _settle()
        await _over(world, running)
        _ended(world, "access_withdrawn")
        assert world.robot.json()[-1] == END_WITHDRAWN
        assert world.db.rows("patient") == [("patient", "我等一下要去散步")]   # stored by the ending
        assert world.clients[0].audio == [heard]               # nothing reached GPT-Live after the outcome
        (warning,) = _warnings(caplog)
        assert warning.args == (relay.conversation_id, 7)      # the conversation and the patient, nothing else

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        asyncio.run(scenario())


@pytest.mark.parametrize("failing", ["live_switch", "consent"])
def test_a_failed_access_read_is_logged_and_tried_again_10_s_later(world, caplog, monkeypatch, failing):
    async def scenario():
        relay, running = await _begin(world)
        get_state = consent_service.get_state                  # the world's consent state

        async def broken(u_id):
            raise ConnectionError("lost the connection to 10.0.0.5")

        if failing == "live_switch":
            world.db.access_error = ConnectionError("lost the connection to 10.0.0.5")
        else:
            monkeypatch.setattr(consent_service, "get_state", broken)
        await _step(world, relay, 10)
        assert relay.outcome is None and world.clients[0].closes == 0   # a failed read never ends the chat
        (warning,) = _warnings(caplog)
        assert warning.args == (relay.conversation_id, "ConnectionError")   # the class name, not the message
        assert "10.0.0.5" not in warning.getMessage()
        world.db.access_error = None
        monkeypatch.setattr(consent_service, "get_state", get_state)
        world.db.live_switch = False                           # meanwhile the patient switched live voice off
        await _step(world, relay, 9.5)
        assert relay.outcome is None
        await _step(world, relay, 0.5)                         # the next re-check, 10 s after the failed one
        await _over(world, running)
        _ended(world, "access_withdrawn")

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        asyncio.run(scenario())


def test_a_slow_access_check_never_holds_up_the_timers_and_never_runs_twice_at_once(world):
    async def scenario():
        world.consent_gate = asyncio.Event()                   # the consent read never answers
        relay, running = await _begin(world)
        await _step(world, relay, 10)
        assert world.db.access_reads == 1                      # the check started and waits
        await _step(world, relay, 10)
        await _step(world, relay, 10.5)                        # past 20 s and 30 s: no second check
        assert world.db.access_reads == 1
        await _step(world, relay, 14)                          # 44.5 s
        assert world.robot.json("end") == []
        await _step(world, relay, 0.5)                         # 45 s without patient speech: the ending is on time
        await _over(world, running)
        _ended(world, "silence")
        assert world.db.access_reads == 1 and relay._access_check.cancelled()   # it did not outlive the chat

    asyncio.run(scenario())


@pytest.mark.parametrize("found_by", ["keyword", "model"])
def test_a_risk_found_as_access_is_withdrawn_is_still_carried_out_as_a_block(safe, found_by):
    async def scenario():
        relay, running = await _begin(safe)
        if found_by == "keyword":
            await _step(safe, relay, 9.75)
            await _live(safe, _said("我想自殺"))                # a screening hit, still settling at the re-check
            _checkin_consent_withdrawn(safe)
            await _step(safe, relay, 0.25)                     # 10 s: access_withdrawn is decided first
            quote = "我想自殺"
        else:
            gate = safe.gates[QUIET_RISK] = asyncio.Event()
            safe.verdicts[QUIET_RISK] = [("self_harm", "self_harm")]
            await _live(safe, _said(QUIET_RISK))
            await _step(safe, relay, 1.75)                     # the turn is stored; its check waits
            await relay._handler_lock.acquire()                # a timer or a GPT-Live event is being handled
            gate.set()
            await _settle()                                    # the model found a risk: its block waits its turn
            _checkin_consent_withdrawn(safe)
            await _step(safe, relay, 8.25)                     # 10 s: access_withdrawn is decided first
            relay._handler_lock.release()
            quote = QUIET_RISK
        await _over(safe, running)
        assert relay.outcome.reason == "access_withdrawn" and safe.db.access_reads == 1
        _blocked(safe, flagged=[2], quote=quote, source=found_by)   # the help line, then `end` with risk true

    asyncio.run(scenario())
