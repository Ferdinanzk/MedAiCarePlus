"""The live relay (live_session.LiveRelay) once GPT-Live has started: the opening, audio both ways, the 0.5 s hold,
turns from the transcripts, the endings and the fallback to today's text chat. A fake GPT-Live plays scripted events,
a fake robot records what it is sent, a fake clock drives every timer and a fake database keeps the rows. The start
itself (its checks, GPT-Live's start, the conversation row) is Task 6's and is tested there: _begin replaces
LiveRelay._open with what a successful start leaves behind."""

import asyncio
import json
import sys
import types
import uuid

import numpy as np
import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config  # noqa: E402
from app.services import after_chat, consent_service, conversation, live_session, memory  # noqa: E402
from app.services.live_client import LiveError, LiveEvent  # noqa: E402

DEVICE_ID = str(uuid.uuid4())
TASK_ID = str(uuid.uuid4())
DEVICE = {"device_id": DEVICE_ID, "u_id": 7, "auto_record": False, "face_label": "pearl", "name": "Pearl"}
ALLOWED = {scope: {"granted": True, "terms_version": config.TERMS_VERSION}   # robot and check-in consent current
           for scope in ("core", "robot_camera", "robot_microphone", "cloud_voice", "conversation_analysis",
                         "safety_alerts")}
LOUD, QUIET = 3000, 40   # PCM16 levels: GPT-Live speaking, and its near-silence (peak 45 measured)
OPENING = "王奶奶，今天感覺怎麼樣？想跟我聊聊嗎？"


def _pcm(level: int, ms: int = 100) -> bytes:
    return np.full(16 * ms, level, dtype="<i2").tobytes()


def _error(code: str) -> LiveError:
    error = LiveError(code)
    error.code = code
    return error


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeRobot:
    """The device WebSocket adapter: the test puts the robot's messages in `inbox` (None: the robot hung up)."""

    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list = []
        self.closed = None

    async def receive(self):
        return await self.inbox.get()

    async def send_json(self, message):
        self.sent.append(message)

    async def send_bytes(self, data):
        self.sent.append(bytes(data))

    async def close(self, code=1000):
        self.closed = code

    def json(self, kind=None):
        return [m for m in self.sent if isinstance(m, dict) and kind in (None, m["type"])]

    def audio(self):
        return [m for m in self.sent if isinstance(m, bytes)]


class FakeLive:
    """GPT-Live: the test puts LiveEvents in `events_in` (an exception is raised from events(), None ends it).
    world.gone makes every send raise LiveError("live_closed"), as once GPT-Live has closed the session."""

    def __init__(self, world, api_key, model, voice, instructions, *, rate=16000, connect=None):
        self.world, self.args = world, (api_key, model, voice, instructions, rate)
        self.events_in: asyncio.Queue = asyncio.Queue()
        self.audio, self.commentaries, self.closes = [], [], 0

    async def start(self, timeout=8.0):
        assert timeout == live_session.CONNECT_TIMEOUT

    async def send_audio(self, pcm):
        if self.world.gone:
            raise _error("live_closed")
        self.audio.append(pcm)

    async def mute(self):
        pass

    async def unmute(self):
        pass

    async def commentary(self, text):
        if self.world.gone:
            raise _error("live_closed")
        self.commentaries.append(text)

    async def instructions_append(self, text):
        pass

    async def events(self):
        while True:
            event = await self.events_in.get()
            if event is None:
                return
            if isinstance(event, Exception):
                raise event
            yield event

    async def close(self, wait=2.0):
        self.closes += 1
        return self.world.usage


class LiveDB:
    """The conversation row the start wrote and its turns. `slow` (an asyncio.Event): storing a patient turn waits
    for it. `live_switch` answers the access re-check's read of the robot's live switch (None: the robot is revoked
    or gone, False: switched off), counted in `access_reads`; `access_error`, when set, is raised by that read."""

    def __init__(self):
        self.conversations, self.turns = {}, []
        self.slow: asyncio.Event | None = None
        self.live_switch: bool | None = True
        self.access_reads = 0
        self.access_error: Exception | None = None

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchval(self, query, *args):
        if "INSERT INTO conversation_turn" in query:
            cid, u_id, role, text, flagged, metrics = args
            if self.slow is not None and role == "patient":
                await self.slow.wait()
            self.turns.append({"turn_id": len(self.turns) + 1, "conversation_id": cid, "role": role, "text": text,
                               "flagged": flagged, "metrics": None if metrics is None else json.loads(metrics)})
            return len(self.turns)
        if "SET risk_flag = TRUE" in query:   # conversation.alert_family: once per conversation
            row = self.conversations[args[0]]
            if row.get("risk_flag"):
                return None
            row["risk_flag"] = True
            return args[0]
        if "SET ended_at = NOW()" in query:
            row = self.conversations[args[0]]
            if row["ended_at"] is not None:
                return None
            row.update(ended_at="now", end_reason=args[1], after_chat_state="pending", live=json.loads(args[2]))
            return args[0]
        if "SELECT live_voice FROM reachy_device" in query:
            self.access_reads += 1
            if self.access_error is not None:
                raise self.access_error
            return self.live_switch
        raise AssertionError(query)

    async def execute(self, query, *args):
        if "UPDATE conversation_turn SET text" in query:
            turn_id, text, metrics = args
            self.turns[turn_id - 1].update(text=text, metrics=json.loads(metrics))
        elif "UPDATE conversation SET live" in query:
            row = self.conversations[args[0]]
            if row["ended_at"] is None:
                row["live"] = json.loads(args[1])
        elif "UPDATE conversation_turn SET flagged = TRUE WHERE turn_id = $1" in query:
            self.turns[args[0] - 1]["flagged"] = True
        elif "UPDATE conversation_turn SET metrics" in query and "jsonb_build_object('live'" in query:
            turn = self.turns[args[0] - 1]
            metrics = turn["metrics"] or {}
            turn["metrics"] = {**metrics, "live": {**metrics.get("live", {}), **json.loads(args[1])}}
        else:
            raise AssertionError(query)

    async def fetch(self, query, *args):
        if "SELECT turn_id, role, text FROM conversation_turn" in query:
            until = args[1] if "turn_id <= $2" in query else None
            return [{"turn_id": t["turn_id"], "role": t["role"], "text": t["text"]} for t in self.turns
                    if t["conversation_id"] == args[0] and (until is None or t["turn_id"] <= until)]
        raise AssertionError(query)

    def chat(self) -> dict:
        (row,) = self.conversations.values()
        return row

    def rows(self, role=None):
        return [(t["role"], t["text"]) for t in self.turns if role in (None, t["role"])]


@pytest.fixture
def world(monkeypatch):
    """Everything around the relay; the background ticker never fires (TICK_SECONDS is an hour), so each test moves
    the fake clock and runs relay._tick() itself (_step). The access re-check reads the patient's consent from
    `consent` (every scope current), first waiting for `consent_gate` when it is set."""
    w = types.SimpleNamespace(db=LiveDB(), robot=FakeRobot(), clock=Clock(), clients=[], after_chat=[], usage=42.0,
                              gone=False, consent={scope: dict(value) for scope, value in ALLOWED.items()},
                              consent_gate=None)

    async def classify_risk(history, language, late=False):   # what a later safety check may ask: no risk
        return None, {"risk_ms": 1, "risk_result": "none", "risk_attempts": []}

    async def get_state(u_id):
        if w.consent_gate is not None:
            await w.consent_gate.wait()
        return w.consent

    async def process(conversation_id, u_id):
        w.after_chat.append((conversation_id, u_id))
        return "done"

    def factory(*args, **kwargs):
        w.clients.append(FakeLive(w, *args, **kwargs))
        return w.clients[-1]

    w.factory = factory
    monkeypatch.setattr(live_session, "get_pool", lambda: w.db)
    monkeypatch.setattr(live_session, "TICK_SECONDS", 3600)
    monkeypatch.setattr(conversation, "classify_risk", classify_risk)
    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(after_chat, "process", process)
    return w


async def _settle():
    for _ in range(50):
        await asyncio.sleep(0)


async def _begin(w):
    """A relay whose start went through: GPT-Live started (a FakeLive from the factory), the conversation row and
    its opening turn stored, `ready` sent. Its chat then runs in the background."""
    relay = live_session.LiveRelay(w.robot, dict(DEVICE), client_factory=w.factory, clock=w.clock)

    async def started(start):
        relay.language = "zh-TW"
        relay.client = w.factory("sk-test", "gpt-live-1", "marin", "INSTRUCTIONS", rate=16000)
        await relay.client.start(timeout=live_session.CONNECT_TIMEOUT)
        relay.conversation_id = cid = str(uuid.uuid4())
        w.db.conversations[cid] = {"conversation_id": cid, "u_id": 7, "task_id": start["task_id"],
                                   "language": "zh-TW", "model": "gpt-live-1", "mode": "live",
                                   "followup_memory_id": None, "ended_at": None, "end_reason": None, "live": None}
        relay.opening = OPENING
        relay._memory_on = memory.consent_current(w.consent)   # as _open keeps it for the access re-checks
        relay.opening_turn_id = await w.db.fetchval(
            "INSERT INTO conversation_turn (conversation_id, u_id, role, text, flagged, metrics) "
            "VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb) RETURNING turn_id", cid, 7, "reachy", OPENING, False, None)
        await w.robot.send_json({"type": "ready", "conversation_id": cid})
        return True

    relay._open = started
    running = asyncio.create_task(relay.run())
    w.robot.inbox.put_nowait({"type": "start", "task_id": TASK_ID, "language": "zh-TW", "rate": 16000,
                              "bridge_version": "0.8.0"})
    await _settle()
    return relay, running


async def _step(w, relay, seconds=0.0):
    w.clock.now += seconds
    await relay._tick()
    await _settle()


async def _live(w, *events):
    for event in events:
        w.clients[-1].events_in.put_nowait(event)
    await _settle()


def _said(text):
    return LiveEvent(type="input_text", text=text)


def _reachy(text):
    return LiveEvent(type="output_text", text=text)


def _sound(level=LOUD):
    return LiveEvent(type="audio", data=_pcm(level))


async def _over(w, running):
    await asyncio.wait_for(running, 5)
    await asyncio.gather(*list(live_session._background))


def test_the_opening_goes_to_gpt_live_as_commentary_quoted_as_data(world):
    async def scenario():
        relay, running = await _begin(world)
        (client,) = world.clients
        assert client.commentaries == [live_session.OPENING_COMMENTARY["zh-TW"]
                                       + json.dumps(OPENING, ensure_ascii=False)]
        assert world.robot.json() == [{"type": "ready", "conversation_id": relay.conversation_id}]
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(world, running)

    asyncio.run(scenario())


def test_the_robots_microphone_goes_to_gpt_live(world):
    async def scenario():
        relay, running = await _begin(world)
        frames = [_pcm(200, 20), _pcm(0, 40)]
        for frame in frames + [b"\x01"]:   # an odd number of bytes is not PCM16: dropped
            world.robot.inbox.put_nowait(frame)
        await _settle()
        assert world.clients[0].audio == frames
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(world, running)

    asyncio.run(scenario())


def test_no_reply_audio_reaches_the_robot_before_the_hold(world):
    async def scenario():
        relay, running = await _begin(world)
        await _live(world, _said("今天天氣很好"))         # the patient spoke: GPT-Live's next sound is a reply
        await _live(world, _sound(QUIET), _sound(QUIET))   # GPT-Live's stream starts with silence
        assert world.robot.audio() == []
        for index in range(4):                         # the onset, then a chunk every 0.125 s
            if index:
                await _step(world, relay, 0.125)
            await _live(world, _sound())
        assert world.robot.audio() == []               # 0.375 s after the onset
        await _step(world, relay, 0.125)               # 0.5 s: the tick lets the reply out, in order
        chunks = [_pcm(LOUD)] * 4
        assert world.robot.audio() == chunks
        await _live(world, _sound(QUIET))   # after the onset, everything flows as it comes
        assert world.robot.audio() == chunks + [_pcm(QUIET)]
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(world, running)

    asyncio.run(scenario())


def test_after_a_pause_the_same_reply_flows_and_a_reply_to_new_words_is_held_again(world):
    async def scenario():
        relay, running = await _begin(world)
        await _live(world, _sound())                  # nobody spoke yet (the opening): not held
        assert len(world.robot.audio()) == 1
        await _step(world, relay, 0.7)                # 0.6 s without sound: GPT-Live stopped
        await _live(world, _sound(QUIET))             # silence between sentences is dropped
        await _live(world, _sound())                  # it goes on without the patient speaking: no new hold
        assert len(world.robot.audio()) == 2
        await _step(world, relay, 1.0)
        await _live(world, _said("嗯"), _sound())     # the patient spoke: the next sound is held
        assert len(world.robot.audio()) == 2
        await _step(world, relay, 0.5)
        assert len(world.robot.audio()) == 3
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(world, running)

    asyncio.run(scenario())


def test_transcripts_become_turns_with_their_live_timings(world):
    async def scenario():
        relay, running = await _begin(world)
        await _live(world, _reachy("王奶奶，"), _reachy("今天好嗎？"))
        await _step(world, relay, 1.0)
        await _live(world, _sound())                   # the spoken opening, 1.0 s after the commentary: not held
        await _step(world, relay, 4.0)
        await _live(world, _said("我"))
        await _step(world, relay, 0.3)
        await _live(world, _said("很好"))
        assert world.db.rows() == [("reachy", "王奶奶，今天好嗎？")]   # GPT-Live's words replace the opening
        assert world.db.turns[0]["metrics"] == {"live": {"first_audio_ms": 1000, "hold_ms": 0, "cut": False,
                                                         "robot_playing_ms": None}}
        await _step(world, relay, 1.4)
        assert len(world.db.turns) == 1                # 1.4 s is no pause yet
        await _step(world, relay, 0.2)                 # 1.6 s: the patient's turn is closed
        assert world.db.rows()[1:] == [("patient", "我很好")]
        assert world.db.turns[1]["metrics"] == {"live": {"transcript_ms": 300, "fragments": 2, "risk_ms": 1,
                                                         "risk_result": "none", "risk_attempts": [],
                                                         "risk_source": "none"}}
        await _live(world, _reachy("太好了。"))
        await _step(world, relay, 0.1)
        await _live(world, _sound())                   # 1.7 s after the patient's last words
        await _step(world, relay, 0.5)
        world.robot.inbox.put_nowait({"type": "playing"})   # the robot reports its first sound
        await _settle()
        await _step(world, relay, 0.3)
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(world, running)
        assert world.db.rows()[2:] == [("reachy", "太好了。")]
        assert world.db.turns[2]["metrics"] == {"live": {"first_audio_ms": 2200, "hold_ms": 500, "cut": False,
                                                         "robot_playing_ms": 2200}}

    asyncio.run(scenario())


def test_reachys_first_words_after_the_patient_spoke_are_a_turn_of_their_own(world):
    async def scenario():
        relay, running = await _begin(world)
        await _live(world, _said("哈囉"))
        await _step(world, relay, 0.3)
        await _live(world, _reachy("哈囉，王奶奶"))      # not the spoken opening: the patient's turn had begun
        assert world.db.rows() == [("reachy", OPENING)]   # and GPT-Live's words leave that turn open
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(world, running)
        assert world.db.rows() == [("reachy", OPENING), ("patient", "哈囉"), ("reachy", "哈囉，王奶奶")]

    asyncio.run(scenario())


def test_only_a_pause_over_1_5_s_closes_the_patients_turn_never_gpt_lives_words(world):
    async def scenario():
        relay, running = await _begin(world)
        await _live(world, _said("我今天"))
        await _step(world, relay, 1.4)
        await _live(world, _said("去公園"))             # 1.4 s apart: the same turn
        await _step(world, relay, 1.6)                  # 1.6 s without words: the turn is closed
        await _live(world, _said("散步"))
        await _step(world, relay, 0.3)
        await _live(world, _reachy("真好"))             # GPT-Live answers early: the patient's turn stays open
        await _step(world, relay, 0.3)
        await _live(world, _said("對啊"))               # 0.6 s after 散步: the same turn
        assert world.db.rows() == [("reachy", OPENING), ("patient", "我今天去公園")]
        await _step(world, relay, 1.6)                  # the pause closes it; Reachy's reply stays open
        assert world.db.rows()[2:] == [("patient", "散步對啊")]
        await _live(world, _said("你呢"))               # a new turn closes Reachy's reply
        assert world.db.rows()[3:] == [("reachy", "真好")]
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(world, running)
        assert world.db.rows() == [("reachy", OPENING), ("patient", "我今天去公園"), ("patient", "散步對啊"),
                                   ("reachy", "真好"), ("patient", "你呢")]

    asyncio.run(scenario())


def test_a_pause_the_timers_have_not_closed_yet_still_starts_a_new_turn(world):
    async def scenario():
        relay, running = await _begin(world)
        await _live(world, _said("我今天"))
        world.clock.now += 2.0                          # no tick ran in between
        await _live(world, _said("想出門"))
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(world, running)
        assert world.db.rows("patient") == [("patient", "我今天"), ("patient", "想出門")]

    asyncio.run(scenario())


# GPT-Live is full duplex: its answer can begin before its transcript of the patient is complete (the spike's 3b:
# audible 0.00-0.31 s after the last input fragment). These replay the spike's orders.


def test_gpt_lives_early_answer_to_an_idiom_stores_one_patient_turn_and_one_reply(world):
    async def scenario():
        relay, running = await _begin(world)
        await _live(world, _said("好久沒看到孫子了。"))
        await _step(world, relay, 0.2)
        await _live(world, _said("真是想死"))            # a screening hit holds GPT-Live's voice
        await _step(world, relay, 0.2)
        await _live(world, _said("他"), _reachy("會想"))   # 他 clears it; GPT-Live's answer has begun
        await _step(world, relay, 0.2)
        await _live(world, _said("了"), _sound())        # the last fragment, as GPT-Live becomes audible
        await _step(world, relay, 0.2)
        await _live(world, _reachy("他是很自然的。"))
        await _step(world, relay, 0.25)                  # the words settled: the hit cleared
        await _step(world, relay, 1.15)                  # 1.6 s after 了: the patient's turn is closed
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(world, running)
        assert world.db.rows() == [("reachy", OPENING), ("patient", "好久沒看到孫子了。真是想死他了"),
                                   ("reachy", "會想他是很自然的。")]

    asyncio.run(scenario())


def test_gpt_lives_early_answer_stores_one_patient_turn_before_one_reply(world):
    async def scenario():
        relay, running = await _begin(world)
        await _live(world, _said("今天早上去公園"))
        await _step(world, relay, 0.2)
        await _live(world, _said("走了"), _reachy("真好"))
        await _step(world, relay, 0.2)
        await _live(world, _said("一圈"), _sound())
        await _step(world, relay, 0.2)
        await _live(world, _reachy("，走路對身體很好。"))
        await _step(world, relay, 1.4)                   # 1.6 s after 一圈: the patient's turn is closed
        assert world.db.rows()[1:] == [("patient", "今天早上去公園走了一圈")]
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(world, running)
        assert world.db.rows() == [("reachy", OPENING), ("patient", "今天早上去公園走了一圈"),
                                   ("reachy", "真好，走路對身體很好。")]

    asyncio.run(scenario())


def test_a_goodbye_split_by_gpt_lives_early_answer_is_one_turn_and_ends_the_chat(world):
    async def scenario():
        relay, running = await _begin(world)
        await _live(world, _said("好，我先去睡了，拜"))
        await _step(world, relay, 0.2)
        await _live(world, _reachy("好"))                # GPT-Live answers before the last fragment
        await _step(world, relay, 0.1)
        await _live(world, _said("拜"), _sound())        # the last fragment, as GPT-Live becomes audible
        for _ in range(3):                               # its answer sounds on for 1.5 s
            await _step(world, relay, 0.5)
            await _live(world, _sound())
        await _step(world, relay, 0.1)                   # 1.6 s after 拜: the goodbye turn is closed
        assert world.db.rows("patient") == [("patient", "好，我先去睡了，拜拜")]
        await _step(world, relay, 0.7)                   # GPT-Live's sound stopped
        await _step(world, relay, 2.9)
        assert world.robot.json("end") == []
        await _step(world, relay, 0.2)                   # 3 s after the end of its sound was seen
        await _over(world, running)
        _ended(world, "goodbye")
        assert world.db.rows() == [("reachy", OPENING), ("patient", "好，我先去睡了，拜拜"), ("reachy", "好")]

    asyncio.run(scenario())


def test_a_late_fragment_after_the_replys_onset_holds_nothing_more_and_keeps_the_replys_timings(world):
    async def scenario():
        relay, running = await _begin(world)
        await _live(world, _said("今天天氣"))
        await _step(world, relay, 0.25)
        await _live(world, _reachy("對啊，"), _sound())   # GPT-Live answers: its first sound is held
        await _step(world, relay, 0.125)
        await _live(world, _said("很好"))                # the patient's last fragment comes after the reply's onset
        await _step(world, relay, 0.25)
        assert world.robot.audio() == []                 # 0.375 s after the onset
        await _step(world, relay, 0.125)                 # 0.5 s: released
        assert world.robot.audio() == [_pcm(LOUD)]
        world.robot.inbox.put_nowait({"type": "playing"})
        await _settle()
        await _step(world, relay, 0.75)                  # GPT-Live paused: its sound segment ended
        await _live(world, _reachy("出去走走吧。"), _sound())   # the same reply's next sentence
        assert world.robot.audio() == [_pcm(LOUD)] * 2   # forwarded at once: no second hold
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(world, running)
        assert world.db.rows() == [("reachy", OPENING), ("patient", "今天天氣很好"), ("reachy", "對啊，出去走走吧。")]
        assert world.db.turns[2]["metrics"] == {"live": {"first_audio_ms": 750, "hold_ms": 500, "cut": False,
                                                         "robot_playing_ms": 750}}   # from the patient's 1st words

    asyncio.run(scenario())


def test_a_new_turn_after_a_pause_closes_reachys_reply_and_its_answer_is_held_again(world):
    async def scenario():
        relay, running = await _begin(world)
        await _live(world, _said("今天天氣很好"))
        await _step(world, relay, 0.25)
        await _live(world, _reachy("出去走走吧。"), _sound())
        await _step(world, relay, 0.5)                   # released after its hold
        assert len(world.robot.audio()) == 1
        await _step(world, relay, 1.0)                   # the pause closes the patient's turn; the reply stays open
        assert world.db.rows()[1:] == [("patient", "今天天氣很好")]
        await _live(world, _said("好啊"))                # new words after the pause: a new turn closes the reply
        assert world.db.rows()[2:] == [("reachy", "出去走走吧。")]
        await _live(world, _sound())                     # GPT-Live answers them: held again
        await _step(world, relay, 0.375)
        assert len(world.robot.audio()) == 1
        await _step(world, relay, 0.125)
        assert len(world.robot.audio()) == 2
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(world, running)
        assert world.db.rows() == [("reachy", OPENING), ("patient", "今天天氣很好"), ("reachy", "出去走走吧。"),
                                   ("patient", "好啊")]

    asyncio.run(scenario())


def test_the_patient_talking_over_the_spoken_opening_leaves_it_whole_as_turn_1(world):
    async def scenario():
        relay, running = await _begin(world)
        await _live(world, _reachy("王奶奶，"), _sound())   # GPT-Live says the opening: nobody spoke, no hold
        await _step(world, relay, 0.5)
        await _live(world, _said("你好"), _sound())         # the patient talks over it
        await _step(world, relay, 0.5)
        await _live(world, _reachy("今天感覺怎麼樣？"), _sound())
        await _step(world, relay, 0.5)
        assert world.db.rows() == [("reachy", OPENING)]   # the opening is still being said
        assert len(world.robot.audio()) == 3
        await _step(world, relay, 0.25)                    # 0.75 s without sound: the opening is over
        assert world.db.rows() == [("reachy", "王奶奶，今天感覺怎麼樣？")]
        assert world.db.turns[0]["metrics"] == {"live": {"first_audio_ms": 0, "hold_ms": 0, "cut": False,
                                                         "robot_playing_ms": None}}
        await _live(world, _reachy("你好呀！"), _sound())   # GPT-Live answers the patient's words: held
        assert len(world.robot.audio()) == 3
        await _step(world, relay, 0.5)
        assert len(world.robot.audio()) == 4
        await _step(world, relay, 0.5)                     # 2.25 s after 你好: the patient's turn is closed
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(world, running)
        assert world.db.rows() == [("reachy", "王奶奶，今天感覺怎麼樣？"), ("patient", "你好"), ("reachy", "你好呀！")]
        assert world.db.turns[2]["metrics"]["live"]["hold_ms"] == 500

    asyncio.run(scenario())


def _ended(world, reason):
    chat = world.db.chat()
    assert (chat["ended_at"], chat["end_reason"], chat["after_chat_state"]) == ("now", reason, "pending")
    assert chat["live"] == {"usage_seconds": 42.0, "close_reason": "close_requested", "fallback_reason": None}
    assert world.after_chat == [(chat["conversation_id"], 7)]   # the post-chat work, once
    assert world.robot.json("end") == [{"type": "end", "reason": reason, "risk": False}]
    assert world.robot.json("fallback") == [] and world.robot.closed == 1000
    assert world.clients[0].closes == 1


def test_a_goodbye_ends_the_chat_3_s_after_gpt_lives_answer(world):
    async def scenario():
        relay, running = await _begin(world)
        await _live(world, _said("好，拜拜"))
        await _step(world, relay, 1.6)                 # the goodbye turn is closed
        await _step(world, relay, 1.0)
        await _live(world, _sound())                   # GPT-Live answers
        await _step(world, relay, 0.5)
        await _live(world, _sound())
        await _step(world, relay, 0.7)                 # its sound stopped 0.6 s ago
        await _step(world, relay, 2.9)
        assert world.robot.json("end") == []
        await _step(world, relay, 0.4)                 # 3 s after the end of its sound was seen
        await _over(world, running)
        _ended(world, "goodbye")
        assert world.db.rows("patient") == [("patient", "好，拜拜")]

    asyncio.run(scenario())


def test_a_goodbye_gpt_live_does_not_answer_ends_the_chat_3_s_later(world):
    async def scenario():
        relay, running = await _begin(world)
        await _live(world, _said("我不聊了"))
        await _step(world, relay, 1.6)                 # the goodbye turn is closed
        await _step(world, relay, 2.9)
        assert world.robot.json("end") == []
        await _step(world, relay, 0.2)
        await _over(world, running)
        _ended(world, "goodbye")

    asyncio.run(scenario())


def test_45_s_without_patient_speech_ends_the_chat(world):
    async def scenario():
        relay, running = await _begin(world)
        await _step(world, relay, 30)
        await _live(world, _said("嗯"))
        await _step(world, relay, 44.5)
        assert world.robot.json("end") == []
        await _step(world, relay, 0.5)                 # 45 s after the patient's last words
        await _over(world, running)
        _ended(world, "silence")

    asyncio.run(scenario())


def test_a_live_chat_ends_after_live_max_seconds(world):
    async def scenario():
        relay, running = await _begin(world)
        for _ in range(14):                            # the patient keeps talking: 14 x 40 s = 560 s
            await _step(world, relay, 40)
            await _live(world, _said("還有"))
        await _step(world, relay, 39.5)
        assert world.robot.json("end") == []
        await _step(world, relay, 0.5)                 # 600 s
        await _over(world, running)
        _ended(world, "limit")

    asyncio.run(scenario())


@pytest.mark.parametrize("message, reason", [({"type": "end", "reason": "stopped"}, "stopped"),
                                             ({"type": "end", "reason": "patient_left"}, "patient_left"),
                                             ({"type": "end", "reason": "risk"}, "stopped"),
                                             (None, "disconnected")])
def test_the_robots_end_or_a_dropped_robot_ends_the_chat_with_the_open_turn_stored(world, message, reason):
    async def scenario():
        relay, running = await _begin(world)
        await _live(world, _said("我想去睡了"))
        world.robot.inbox.put_nowait(message)
        await _over(world, running)
        _ended(world, reason)
        assert world.db.rows("patient") == [("patient", "我想去睡了")]   # stored before the post-chat work

    asyncio.run(scenario())


def test_a_turn_being_stored_when_the_chat_ends_is_never_lost(world):
    async def scenario():
        relay, running = await _begin(world)
        world.db.slow = asyncio.Event()
        await _live(world, _said("我想去睡了"), _reachy("好"))
        world.clock.now += 2.0                          # no tick ran in between
        await _live(world, _said("晚安"))               # a new turn after the pause stores the last one: it waits
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})   # the robot ends meanwhile
        await _settle()
        assert world.robot.json("end") == []           # the ending waits for the turn being stored
        world.db.slow.set()
        await _over(world, running)
        _ended(world, "stopped")
        assert world.db.rows() == [("reachy", OPENING), ("patient", "我想去睡了"), ("reachy", "好"),
                                   ("patient", "晚安")]

    asyncio.run(scenario())


@pytest.mark.parametrize("event, reason, close_reason", [
    (LiveEvent(type="error", code="rate_limit_exceeded"), "rate_limit_exceeded", "close_requested"),
    (LiveEvent(type="closed", reason="connection_lost", usage_seconds=12.0), "live_closed", "connection_lost"),
    (_error("live_closed"), "live_closed", "close_requested"),
    (None, "live_closed", "close_requested"),
])
def test_gpt_live_failing_mid_chat_falls_back_with_the_conversation_id(world, event, reason, close_reason):
    async def scenario():
        relay, running = await _begin(world)
        await _live(world, _said("今天天氣很好"))
        await _step(world, relay, 1.6)
        await _live(world, _reachy("出去走走吧"))
        await _live(world, event)
        await _over(world, running)
        chat = world.db.chat()
        assert world.robot.json("fallback") == [{"type": "fallback", "reason": reason,
                                                 "conversation_id": chat["conversation_id"]}]
        assert world.robot.json("end") == [] and world.robot.closed == 1000
        assert chat["ended_at"] is None and chat["mode"] == "live" and world.after_chat == []
        usage = 12.0 if close_reason == "connection_lost" else 42.0
        assert chat["live"] == {"usage_seconds": usage, "close_reason": close_reason, "fallback_reason": reason,
                                "live_patient_turns": 1}
        assert world.db.rows() == [("reachy", OPENING), ("patient", "今天天氣很好"), ("reachy", "出去走走吧")]

    asyncio.run(scenario())


def test_gpt_live_closing_before_the_opening_went_falls_back_with_the_conversation_id(world):
    async def scenario():
        world.gone = True                                # GPT-Live closed the session as the chat began
        relay, running = await _begin(world)
        assert world.clients[0].commentaries == []       # the opening's commentary was refused
        assert not running.done()                        # GPT-Live's own closed event decides what happens
        await _live(world, LiveEvent(type="closed", reason="connection_lost", usage_seconds=1.0))
        await _over(world, running)
        chat = world.db.chat()
        cid = chat["conversation_id"]
        assert world.robot.json() == [{"type": "ready", "conversation_id": cid},
                                      {"type": "fallback", "reason": "live_closed", "conversation_id": cid}]
        assert world.robot.closed == 1000 and world.clients[0].closes == 1
        assert chat["ended_at"] is None and chat["mode"] == "live" and world.after_chat == []
        assert chat["live"] == {"usage_seconds": 1.0, "close_reason": "connection_lost",
                                "fallback_reason": "live_closed", "live_patient_turns": 0}
        assert world.db.rows() == [("reachy", OPENING)]

    asyncio.run(scenario())


def test_a_moderation_stop_with_no_risk_found_falls_back_as_live_ended(world):
    async def scenario():
        relay, running = await _begin(world)
        await _live(world, _said("我跟老伴吵架了"))
        await _live(world, LiveEvent(type="error", code="content_filter"))
        await _over(world, running)
        chat = world.db.chat()
        assert world.robot.json("fallback") == [{"type": "fallback", "reason": "live_ended",
                                                 "conversation_id": chat["conversation_id"]}]
        assert chat["ended_at"] is None and chat["live"]["fallback_reason"] == "live_ended"
        assert chat["live"]["close_reason"] == "content"

    asyncio.run(scenario())


def test_audio_gpt_live_refuses_waits_for_gpt_lives_own_close(world):
    async def scenario():
        relay, running = await _begin(world)
        world.gone = True                              # GPT-Live closed; its events are still on their way
        world.robot.inbox.put_nowait(_pcm(200, 20))
        await _settle()
        assert world.robot.json("fallback") == [] and not running.done()
        await _live(world, LiveEvent(type="error", code="content_filter"),
                    LiveEvent(type="closed", reason="content", usage_seconds=9.0))
        await _over(world, running)
        assert world.robot.json("fallback")[0]["reason"] == "live_ended"   # not a plain live_closed

    asyncio.run(scenario())


def test_an_ending_and_a_fallback_at_the_same_moment_end_the_chat_once(world):
    async def scenario():
        relay, running = await _begin(world)
        world.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        world.clients[0].events_in.put_nowait(LiveEvent(type="error", code="live_unavailable"))
        await _over(world, running)
        sent = world.robot.json("end") + world.robot.json("fallback")
        assert len(sent) == 1 and world.clients[0].closes == 1
        assert len(world.after_chat) == (1 if sent[0]["type"] == "end" else 0)

    asyncio.run(scenario())


# ── final review: an unexpected failure, and a database that stops answering ──────────────────────────────────────


@pytest.mark.parametrize("failing", ["ending", "fallback"])
def test_an_unexpected_failure_mid_chat_falls_back_with_the_conversation_id(world, failing):
    """The ending's or the fallback's own write fails: the robot goes on with today's text chat in the same
    conversation (left open, as any fallback leaves it) instead of a bare 1011 and a new chat."""
    async def scenario():
        relay, running = await _begin(world)

        async def broken(query, *args):
            raise OSError("connection reset by the database")

        if failing == "ending":
            world.db.fetchval = broken                     # the 45 s silence ending cannot end the conversation
            await _step(world, relay, 45)
        else:
            world.db.execute = broken                      # GPT-Live fails; the fallback cannot store why
            await _live(world, LiveEvent(type="error", code="rate_limit_exceeded"))
        await _over(world, running)
        chat = world.db.chat()
        assert world.robot.json() == [{"type": "ready", "conversation_id": chat["conversation_id"]},
                                      {"type": "fallback", "reason": "error",
                                       "conversation_id": chat["conversation_id"]}]
        assert world.robot.closed == 1000 and world.clients[0].closes == 1
        assert chat["ended_at"] is None and world.after_chat == []

    asyncio.run(scenario())


def test_a_turn_insert_that_never_returns_still_ends_the_chat_and_closes_gpt_live(world, monkeypatch):
    async def scenario():
        monkeypatch.setattr(live_session, "DB_TIMEOUT_SECONDS", 0.05)   # real seconds, so the test is quick
        world.db.slow = asyncio.Event()                    # storing a patient turn never returns
        relay, running = await _begin(world)
        heard = _pcm(200, 20)
        world.robot.inbox.put_nowait(heard)
        await _live(world, _said("我今天"))
        world.clock.now += 2.0
        await _live(world, _said("想出門"))                 # a new turn stores the last one: its INSERT hangs
        await asyncio.wait_for(relay._done.wait(), 1)      # given up on after DB_TIMEOUT_SECONDS: an error fallback
        world.robot.inbox.put_nowait(_pcm(200, 20))        # the microphone after the outcome
        await _over(world, running)                        # the fallback then goes as usual, the lost turn aside
        chat = world.db.chat()
        assert relay.outcome.kind == "fallback" and relay.outcome.reason == "error"
        assert world.robot.json()[1:] == [{"type": "fallback", "reason": "error",
                                           "conversation_id": chat["conversation_id"]}]
        assert world.robot.closed == 1000 and world.clients[0].closes == 1   # GPT-Live closed, once
        assert world.clients[0].audio == [heard]           # nothing reached GPT-Live after the outcome
        assert world.db.rows() == [("reachy", OPENING)] and chat["ended_at"] is None

    asyncio.run(scenario())
