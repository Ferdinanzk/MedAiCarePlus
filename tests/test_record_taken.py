"""record_medication_taken never loses a legitimate request (plan 2026-10-16, Task 3).

The read-only review of 16 Oct 2026 (probes in the session scratchpad) found five ways the GPT-Live agent's record of
a dose went wrong:
1. A record without session_id (the MEDICATION COMPLETION flow, or the patient's word) left the open watch of the same
   dose unsettled: its review message ("not recorded, please judge") and a second clip went to family beside the
   request, or the chat's end reported the watch "unresolved". Now a record settles the open watch of the same dose
   (the same dose, or names_match on the medicine's name) whether or not it names it: its pending review is
   cancelled, the watch is spent and the clip goes with the request. The detector's verdict still authorises a record
   only for the watch the call names.
2. patient_confirmed true with an unknown or spent session_id failed the record; now it counts as "no detector
   evidence" (logged at INFO). Without the patient's word it stays an error.
3. A dose already waiting for family answered already_recorded; now already_pending_confirmation.
4. Every ValueError from dose_confirmation.create read as "already recorded"; now only its NothingToConfirm does, so a
   database or encoding error is an error.
5. Closing GPT-Live (a goodbye, a companion pause) cancelled a record still running; now a record, a note to family or
   an emergency alert runs to its end (each within its TIMEOUTS entry), the chat's end waits for it before the tools
   close, and the companion's quiet pause waits while one runs. Other tools are cancelled as before.
"""

import asyncio
import logging
import sys
import types

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.services import dose_confirmation, dose_event, dose_video, live_session, tool_registry   # noqa: E402
from app.services.tool_registry import ToolError, ToolRegistry   # noqa: E402
from tests import test_tool_registry   # noqa: E402
from tests.test_agent_bridge import call_event, client   # noqa: E402
from tests.test_dose_confirmation import FakeDB   # noqa: E402
from tests.test_live_client import FakeSocket, starts_only   # noqa: E402
from tests.test_live_companion import PAUSED, _speak, _start, companion   # noqa: E402,F401 (companion: a fixture)
from tests.test_live_relay_chat import _begin, _live, _over, _said, _step, world   # noqa: E402,F401 (world: a fixture)
from tests.test_live_safety import safe   # noqa: E402,F401 (a fixture)
from tests.test_live_tool_backstop import Backend, Link   # noqa: E402
from tests.test_tool_registry import MEDS, Conn, Robot, at, backend, intakes   # noqa: E402

camera, now, vision_on = test_tool_registry.camera, test_tool_registry.now, test_tool_registry.vision_on
REAL_CREATE = dose_confirmation.create
RECORD_ARGS = {"medication_name": None, "session_id": None, "medication_id": "dose-2", "patient_confirmed": True}


@pytest.fixture
def reports(monkeypatch):
    """dose_event.schedule recorded, never run; a future stands in for each review task."""
    scheduled = []

    def schedule(u_id, *, delay=0.0, **fields):
        task = asyncio.get_running_loop().create_future()
        scheduled.append({"delay": delay, **fields, "task": task})
        return task
    monkeypatch.setattr(dose_event, "schedule", schedule)
    return scheduled


@pytest.fixture
def captures(monkeypatch):
    """A clip can come (dose_video.clip_outlook: consent on, frames buffered); capture() records the patient and the
    doses whose request the clip rides with."""
    taken = []

    async def clip_outlook(u_id, *, event_started_at=None):
        return "with_request", [(0.0, b"jpeg")] * dose_video.MIN_FRAMES
    monkeypatch.setattr(dose_video, "clip_outlook", clip_outlook)
    monkeypatch.setattr(dose_video, "capture", lambda u_id, intk_ids, *, event_started_at=None, frames=None:
                        taken.append((u_id, intk_ids)) or True)
    return taken


@pytest.fixture
def asked(monkeypatch):
    """dose_confirmation.create records what it was asked and answers a new request id."""
    seen = []

    async def create(conn, *, u_id, task_id, intk_ids, source, evidence, started_at=None):
        seen.append({"intk_ids": intk_ids, "source": source, "evidence": dict(evidence), "started_at": started_at})
        return f"req-{len(seen)}"
    monkeypatch.setattr(dose_confirmation, "create", create)
    return seen


def watched_uncertain(b, camera, name="Allegra"):
    """A watch whose result was read as uncertain, so its review message waits its grace; returns its session id."""
    started = asyncio.run(b.start_pill_intake_detection(30, name))
    camera.extra_events.append({"event_id": "e1", "decision": "uncertain", "confidence": 0.48, "evidence": {}})
    assert asyncio.run(b.get_pill_intake_result(started["session_id"]))["result"] == "uncertain"
    return started["session_id"]


# ── 1. a record settles the open watch of the same dose, named or not ──

@pytest.mark.parametrize("call", [
    {"medication_id": "dose-2"},       # MEDICATION COMPLETION: check_medication_due's id
    {"medication_id": "med-10"},       # the medicine's id, resolved to today's dose
    {"medication_name": "allegra"},    # the name as the patient said it
], ids=["dose_id", "medicine_id", "name"])
def test_a_record_without_session_id_settles_the_open_watch_of_the_same_dose(now, camera, reports, captures, asked,
                                                                            call):
    b, _ = backend(monotonic=lambda: 100.0)
    watched_uncertain(b, camera)
    review = reports[0]["task"]
    answer = asyncio.run(b.record_medication_taken(patient_confirmed=True, **call))
    assert answer["status"] == "pending_confirmation" and answer["video"] == "with_request"
    assert review.cancelled()                  # no "not recorded, please judge" message beside the request
    assert len(captures) == 1                  # one clip of the moment, with the request
    assert b._watch is None                    # the watch is spent
    # Its verdict authorised nothing: the patient's word, judged now
    request = asked[0]
    assert request["source"] == "patient_claim" and request["started_at"] is None
    assert request["evidence"]["session_id"] is None and request["evidence"]["detector_result"] is None
    asyncio.run(b.close())
    assert [report["kind"] for report in reports] == ["intake"]   # the chat's end reports no unresolved watch


def test_a_record_of_another_dose_of_the_watched_medicine_settles_the_watch(now, camera, reports, captures, asked):
    doses = intakes()
    doses[1].update(intake_stats="missed", actual_intake_time=None)    # this morning's Allegra, never taken
    b, _ = backend(Conn(doses, MEDS), monotonic=lambda: 100.0)
    watched_uncertain(b, camera)                                        # the watch is on tonight's dose, dose-2
    answer = asyncio.run(b.record_medication_taken("dose-1", True))    # names_match on the medicine's name
    assert answer["status"] == "pending_confirmation" and answer["medication_id"] == "dose-1"
    assert reports[0]["task"].cancelled() and b._watch is None and len(captures) == 1


def test_a_watch_never_read_is_settled_too_and_the_chats_end_reports_nothing(now, camera, reports, captures, asked):
    b, _ = backend(monotonic=lambda: 100.0)
    asyncio.run(b.start_pill_intake_detection(30, "Allegra"))
    answer = asyncio.run(b.record_medication_taken("dose-2", True))
    assert answer["status"] == "pending_confirmation" and b._watch is None
    asyncio.run(b.close())
    assert reports == []                       # before: an "unresolved" report beside the request


def test_a_watch_of_another_medicine_is_left_as_it_is(now, camera, reports, captures, asked):
    meds = {**MEDS, 11: {**MEDS[10], "med_name": "Metformin"}}
    doses = {**intakes(), 3: {"med_id": 11, "intake_stats": "pending", "intake_time_stamp": at(20),
                              "actual_intake_time": None}}
    b, _ = backend(Conn(doses, meds), monotonic=lambda: 100.0)
    session_id = watched_uncertain(b, camera, "Metformin")
    answer = asyncio.run(b.record_medication_taken("dose-2", True))   # Allegra
    assert answer["status"] == "pending_confirmation"
    assert not reports[0]["task"].cancelled() and b._watch["id"] == session_id   # Metformin's watch goes on


def test_the_detectors_verdict_still_authorises_only_the_watch_the_call_names(now, camera, reports, captures, asked):
    b, _ = backend(monotonic=lambda: 100.0)
    started = asyncio.run(b.start_pill_intake_detection(30, "Allegra"))
    camera.extra_events.append({"event_id": "e1", "decision": "confirmed", "confidence": 0.9, "evidence": {}})
    with pytest.raises(ToolError, match="dose_not_confirmed_by_patient_or_detector"):
        asyncio.run(b.record_medication_taken("dose-2", False))          # the detector is sure, but not named
    assert asked == [] and b._watch["id"] == started["session_id"]      # nothing recorded, nothing settled


# ── 2. the patient's word with an unknown or spent session ──

def test_the_patients_word_with_a_spent_or_unknown_session_counts_as_no_detector_evidence(now, camera, captures,
                                                                                         asked, caplog):
    caplog.set_level(logging.INFO, logger=tool_registry.log.name)
    b, _ = backend(monotonic=lambda: 100.0)
    started = asyncio.run(b.start_pill_intake_detection(30, "Allegra"))
    camera.extra_events.append({"event_id": "e1", "decision": "confirmed", "confidence": 0.82, "evidence": {}})
    first = asyncio.run(b.record_medication_taken(medication_name="Allegra", session_id=started["session_id"]))
    assert first["detector_result"] == "taken" and b._watch is None                  # that watch is spent
    caplog.clear()
    for session_id in (started["session_id"], "no-such-watch"):
        answer = asyncio.run(b.record_medication_taken(medication_name="Allegra", session_id=session_id,
                                                       patient_confirmed=True))
        assert answer["status"] == "pending_confirmation" and answer["source"] == "patient_claim"
        assert answer["session_id"] is None and answer["detector_result"] is None
        assert answer["detector_confidence"] is None
    assert [request["started_at"] for request in asked] == [at(20, 15), None, None]
    assert [request["evidence"]["session_id"] for request in asked] == [started["session_id"], None, None]
    mine = [record for record in caplog.records if record.name == tool_registry.log.name]
    assert [record.levelno for record in mine] == [logging.INFO, logging.INFO]
    assert all("no detector evidence" in record.getMessage() for record in mine)
    # Without the patient's word the unknown watch stays an error
    for session_id in (started["session_id"], "no-such-watch"):
        with pytest.raises(ToolError, match="unknown_session"):
            asyncio.run(b.record_medication_taken(medication_name="Allegra", session_id=session_id))
    assert len(asked) == 3


# ── 3. a dose no longer open says what it is ──

@pytest.mark.parametrize("stats, status", [
    ("pending_confirmation", "already_pending_confirmation"),   # 16 Oct: answered already_recorded
    ("taken", "already_taken"),
    ("skipped", "already_skipped"),
])
def test_a_dose_no_longer_open_answers_what_it_is_never_already_recorded(now, captures, monkeypatch, stats, status):
    fake = FakeDB()                                    # the real create, on test_dose_confirmation's database
    fake.intakes[2] = fake._intake(7, 40, stats)
    fake.protection = False

    async def create(conn, **kwargs):
        return await REAL_CREATE(fake, **kwargs)
    monkeypatch.setattr(dose_confirmation, "create", create)
    doses = intakes()
    doses[2]["intake_stats"] = stats
    b, _ = backend(Conn(doses, MEDS))
    answer = asyncio.run(b.record_medication_taken("dose-2", True))
    assert answer == {"status": status, "medication_id": "dose-2", "medication_name": "Allegra",
                      "family_asked": False}
    assert fake.confirmations == {} and fake.outbox == []


def test_a_dose_open_again_when_read_after_the_refusal_is_an_error(now, captures, monkeypatch):
    async def create(conn, **kwargs):
        raise dose_confirmation.NothingToConfirm("No pending or missed dose to confirm")
    monkeypatch.setattr(dose_confirmation, "create", create)
    b, _ = backend()                                   # dose-2 still reads pending: it changed in between
    with pytest.raises(ToolError, match="dose_changed_meanwhile"):
        asyncio.run(b.record_medication_taken("dose-2", True))


# ── 4. only "nothing to confirm" is read as already done ──

def test_create_raises_nothing_to_confirm_of_its_own_only_when_no_dose_is_open():
    db = FakeDB()

    def create(**kwargs):
        return asyncio.run(dose_confirmation.create(db, u_id=7, task_id=None, evidence={}, **kwargs))

    with pytest.raises(dose_confirmation.NothingToConfirm) as nothing:
        create(intk_ids=[102, 103], source="patient_claim")          # taken, and another patient's
    assert isinstance(nothing.value, ValueError)                       # the robot's route still answers 409
    with pytest.raises(ValueError) as unknown:
        create(intk_ids=[100], source="guess")
    assert not isinstance(unknown.value, dose_confirmation.NothingToConfirm)
    assert db.confirmations == {} and db.outbox == []


@pytest.mark.parametrize("error", [
    ValueError("invalid input for query argument $7"),
    UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed"),
    RuntimeError("the pool is closed"),
], ids=["value_error", "encoding_error", "runtime_error"])
def test_any_other_failure_of_the_request_is_an_error_never_already_recorded(now, captures, monkeypatch, error):
    async def create(conn, **kwargs):
        raise error
    monkeypatch.setattr(dose_confirmation, "create", create)
    b, _ = backend()
    result = asyncio.run(ToolRegistry(Robot(), b).execute("record_medication_taken", dict(RECORD_ARGS)))
    assert result == {"ok": False, "tool": "record_medication_taken",
                      "error": f"internal_tool_error: {type(error).__name__}"}


# ── 5. closing GPT-Live lets a record, a note or an alert finish ──

class GatedRegistry:
    """A registry whose calls wait for their tool's gate (when the test set one); it records what started, what
    finished and the runs it was asked to hold (ToolRegistry.hold)."""

    def __init__(self, gated):
        self.gates = {name: asyncio.Event() for name in gated}
        self.started, self.finished, self.held = [], [], []

    def hold(self, run):
        self.held.append(run)

    async def execute(self, name, args):
        self.started.append(name)
        if name in self.gates:
            await self.gates[name].wait()
        self.finished.append(name)
        return {"ok": True, "tool": name, "result": {"status": "pending_confirmation"}}


async def _until(condition, limit=1.0):
    async with asyncio.timeout(limit):
        while not condition():
            await asyncio.sleep(0)


@pytest.mark.parametrize("name", ["record_medication_taken", "notify_family", "emergency_alert"])
def test_closing_gpt_live_lets_a_running_record_note_or_alert_finish(name):
    async def run():
        socket = FakeSocket(starts_only)
        registry = GatedRegistry([name])
        c = client(socket, registry)
        await c.start()
        socket.push(call_event(name, "{}", call_id="c1"))
        await _until(lambda: name in registry.started)
        await c.close(wait=0.05)                    # a goodbye right after the praise, or the stretch paused
        cut = [task.cancelled() for task in registry.held]
        registry.gates[name].set()
        await asyncio.wait_for(asyncio.gather(*registry.held), 1)
        return registry, cut, socket.sent_types()

    registry, cut, kinds = asyncio.run(run())
    assert tool_registry.FINISH_ON_CLOSE == ("record_medication_taken", "notify_family", "emergency_alert")
    assert registry.finished == [name] and cut == [False] and len(registry.held) == 1
    assert "response.item.create" not in kinds     # its result has no session to go to


def test_closing_gpt_live_cancels_the_other_tools_and_a_record_queued_behind_one_still_runs():
    async def run():
        socket = FakeSocket(starts_only)
        registry = GatedRegistry(["describe_view"])   # a camera look that never answers
        c = client(socket, registry)
        await c.start()
        socket.push(call_event("describe_view", '{"question": "我穿什麼"}', call_id="c1"))
        socket.push(call_event("record_medication_taken", '{"medication_id": "dose-2", "patient_confirmed": true}',
                               call_id="c2"))
        await _until(lambda: "describe_view" in registry.started and len(registry.held) == 1)
        await c.close(wait=0.05)
        await asyncio.wait_for(asyncio.gather(*registry.held), 1)
        return registry

    registry = asyncio.run(run())
    assert registry.started == ["describe_view", "record_medication_taken"]
    assert registry.finished == ["record_medication_taken"]   # the look was cancelled; the record ran to its end


def test_the_chats_end_waits_for_a_held_record_before_reporting_an_open_watch(now, camera, reports, captures, asked):
    async def main():
        b, _ = backend(monotonic=lambda: 100.0)
        registry = ToolRegistry(Robot(), b)
        await b.start_pill_intake_detection(30, "Allegra")          # a watch with no verdict read yet
        gate = asyncio.Event()

        async def record():
            await gate.wait()
            return await registry.execute("record_medication_taken", dict(RECORD_ARGS))
        run = asyncio.ensure_future(record())
        registry.hold(run)
        busy = registry.finishing()
        closing = asyncio.ensure_future(registry.close())
        await asyncio.sleep(0.01)
        waited = not closing.done() and reports == []
        gate.set()
        await asyncio.wait_for(closing, 1)
        return busy, waited, registry.finishing(), run.result()

    busy, waited, still, result = asyncio.run(main())
    assert busy is True and waited is True and still is False
    assert result["ok"] is True and result["result"]["status"] == "pending_confirmation"
    assert reports == []                                            # the record settled the watch first
    assert tool_registry.FINISH_WAIT_SECONDS == sum(tool_registry.TIMEOUTS[name]
                                                    for name in tool_registry.FINISH_ON_CLOSE)


def test_the_relays_end_waits_for_a_held_call_longer_than_a_database_call(world, monkeypatch):
    monkeypatch.setattr(live_session, "DB_TIMEOUT_SECONDS", 0.05)   # the record takes longer than one database call
    order = []

    class Ending(Backend):
        async def close(self):
            order.append("tools closed")

    async def scenario():
        relay, running = await _begin(world)
        relay.tools = ToolRegistry(Link(), Ending())

        async def record():
            await asyncio.sleep(0.2)
            order.append("record finished")
        relay.tools.hold(asyncio.ensure_future(record()))
        world.robot.inbox.put_nowait({"type": "end", "reason": "goodbye"})
        await asyncio.wait_for(running, 5)

    asyncio.run(scenario())
    assert order == ["record finished", "tools closed"]


def test_the_companions_quiet_pause_waits_while_a_record_runs(companion):
    async def scenario():
        relay, running = await _start(companion)
        await _speak(companion)
        relay.tools = ToolRegistry(Link(), Backend())
        record = asyncio.get_running_loop().create_future()
        relay.tools.hold(record)
        await _live(companion, _said("我吃完藥了"))
        await _step(companion, relay, 1.75)                   # the patient's turn is stored
        await _step(companion, relay, 40)                     # 40 s of quiet, but the record still runs
        assert companion.clients[0].closes == 0 and companion.robot.json("paused") == []
        record.set_result({"ok": True})
        await _step(companion, relay, 0.1)                    # it finished: the pause comes now
        assert companion.clients[0].closes == 1 and companion.robot.json("paused") == [PAUSED]
        companion.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(companion, running)

    asyncio.run(scenario())
