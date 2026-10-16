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
   close, and the companion's quiet pause waits while one runs, then INTENT_WAIT_SECONDS for GPT-Live's answer to
   start (it waits for that answer's sound to end, as for any). Other tools are cancelled as before.
"""

import asyncio
import logging
import sys
import types

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.services import (agent_bridge, dose_confirmation, dose_event, dose_video, live_session,  # noqa: E402
                          tool_registry)
from app.services.tool_registry import ToolError, ToolRegistry   # noqa: E402
from tests import test_tool_registry   # noqa: E402
from tests.test_agent_bridge import call_event, client   # noqa: E402
from tests.test_dose_confirmation import FakeDB   # noqa: E402
from tests.test_live_client import FakeSocket, starts_only   # noqa: E402
from tests.test_live_companion import PAUSED, _speak, _start, companion   # noqa: E402,F401 (companion: a fixture)
from tests.test_live_relay_chat import (_begin, _live, _over, _said, _settle, _sound, _step,  # noqa: E402
                                        world)  # noqa: F401 (world is a fixture)
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


def test_a_record_of_another_dose_of_the_watched_medicine_leaves_the_watch_alone(now, camera, reports, captures,
                                                                                asked):
    """Round 3 (F1.1): the same medicine is not the same dose. Before, names_match on the medicine settled the watch of
    tonight's dose with a record of this morning's, and the watch's camera reading was lost."""
    doses = intakes()
    doses[1].update(intake_stats="missed", actual_intake_time=None)    # this morning's Allegra, never taken
    b, _ = backend(Conn(doses, MEDS), monotonic=lambda: 100.0)
    session_id = watched_uncertain(b, camera)                           # the watch is on tonight's dose, dose-2
    answer = asyncio.run(b.record_medication_taken("dose-1", True))
    assert answer["status"] == "pending_confirmation" and answer["medication_id"] == "dose-1"
    assert not reports[0]["task"].cancelled() and b._watch["id"] == session_id   # tonight's watch goes on
    assert len(captures) == 1 and "camera_watch" not in asked[0]["evidence"]     # this morning's request has its clip


@pytest.mark.parametrize("seen, reading", [
    ("not_taken", {"result": "not_taken", "confidence": 0.0}),           # the watch timed out and saw nothing
    ("uncertain", {"result": "uncertain", "confidence": 0.48}),          # a hand-to-mouth moment it was unsure of
    ("never_read", {"result": "not_read_yet", "confidence": None}),      # GPT-Live never asked for the result
])
def test_a_record_that_settles_a_watch_tells_family_what_the_camera_read(now, camera, reports, captures, asked,
                                                                          seen, reading):
    """Round 3 (F1.1): the patient's word settles the watch of the same dose without naming it, and the watch's own
    reading goes into the request's evidence, so family see when the camera disagrees with the patient."""
    clock = [100.0]
    b, _ = backend(monotonic=lambda: clock[0])
    started = asyncio.run(b.start_pill_intake_detection(30, "Allegra"))
    if seen == "not_taken":
        clock[0] += 31                                                   # past the watch's 30 s
        assert asyncio.run(b.get_pill_intake_result(started["session_id"]))["result"] == "not_taken"
    elif seen == "uncertain":
        camera.extra_events.append({"event_id": "e1", "decision": "uncertain", "confidence": 0.48, "evidence": {}})
        assert asyncio.run(b.get_pill_intake_result(started["session_id"]))["result"] == "uncertain"
    answer = asyncio.run(b.record_medication_taken("dose-2", True))
    assert answer["status"] == "pending_confirmation" and b._watch is None
    evidence = asked[0]["evidence"]
    assert evidence["camera_watch"] == reading
    assert evidence["detector_result"] is None and evidence["confidence"] is None   # its verdict authorised nothing
    text = dose_confirmation._request_messages(
        {"patient": "阿嬤", "meds": "Allegra", "slot": "10/12 20:00"}, "patient_claim", "conf-1", 34,
        evidence=evidence)[0]["text"]
    zh, en = {"not_taken": ("⚠️ 鏡頭觀察：沒有看到服藥動作，和本人說的不同"
                            "（AI 判斷已服藥的可能性：0%（低））",
                            "⚠️ Camera watch: no intake was seen, unlike what the patient said "
                            "(AI estimate that it was taken: 0% (low))"),
              "uncertain": ("鏡頭觀察：有手到嘴邊的動作，但 AI 不確定"
                            "（AI 判斷已服藥的可能性：48%（不確定））",
                            "Camera watch: a hand-to-mouth moment the AI was not sure of "
                            "(AI estimate that it was taken: 48% (uncertain))"),
              "never_read": ("鏡頭觀察：還沒有讀取結果", "Camera watch: its result was not read yet")}[seen]
    assert zh in text and en in text


def test_a_watch_started_without_a_medicine_is_never_settled_by_a_record(now, camera, reports, captures, asked):
    """Round 3 (F1.2): a watch started with medication_name null has no dose and no name, so no record settles it."""
    b, _ = backend(monotonic=lambda: 100.0)
    session_id = watched_uncertain(b, camera, None)
    answer = asyncio.run(b.record_medication_taken("dose-2", True))
    assert answer["status"] == "pending_confirmation" and "camera_watch" not in asked[0]["evidence"]
    assert not reports[0]["task"].cancelled() and b._watch["id"] == session_id


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

    def hold(self, run, *names):
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
        registry.hold(run, "record_medication_taken")
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


class SlowRecords:
    """A care backend whose records take `seconds` each; it notes each record and when the chat's end reached it
    (close: a watch left open would be reported to family then)."""

    def __init__(self, order, seconds):
        self.order, self.seconds = order, seconds

    async def check_medication_due(self):
        return {"status": "pending_allowed", "doses": []}

    async def record_medication_taken(self, medication_id, patient_confirmed, medication_name, session_id):
        await asyncio.sleep(self.seconds)
        self.order.append(f"recorded {medication_id}")
        return {"status": "pending_confirmation", "medication_id": medication_id}

    async def close(self):
        self.order.append("watch reported")


def test_the_chats_end_waits_for_every_held_call_not_one_of_each_kind(monkeypatch):
    """Round 3 (F1.3): FINISH_WAIT_SECONDS (10 + 8 + 12 s) assumed one held call of each kind. GPT-Live's client runs
    its calls one at a time, so two records held together take two records' time, more than that fixed bound, and
    the chat's end reported the watch unresolved while the second still ran. Now the wait is bounded from the calls
    actually held: each one's TIMEOUTS entry, counted from when it was held, one after the other."""
    for name, seconds in (("record_medication_taken", 1.0), ("notify_family", 0.1), ("emergency_alert", 0.1)):
        monkeypatch.setitem(tool_registry.TIMEOUTS, name, seconds)
    monkeypatch.setattr(tool_registry, "FINISH_WAIT_SECONDS", 1.2, raising=False)   # the old bound for these entries
    order = []

    async def run():
        socket = FakeSocket(starts_only)
        registry = ToolRegistry(Robot(), SlowRecords(order, 0.7))
        c = client(socket, registry)
        await c.start()
        for number in (1, 2):                       # two records, together 1.4 s: past the old 1.2 s bound
            socket.push(call_event("record_medication_taken",
                                   f'{{"medication_id": "dose-{number}", "patient_confirmed": true}}',
                                   call_id=f"c{number}"))
        await _until(lambda: c.requested.count("record_medication_taken") == 2)
        bound = registry.finish_wait()
        await c.close(wait=0.05)                    # the chat ends while the first record runs
        await asyncio.wait_for(registry.close(), 3)
        return bound

    bound = asyncio.run(run())
    assert order == ["recorded dose-1", "recorded dose-2", "watch reported"]
    assert bound == pytest.approx(2.0, abs=0.05)    # 1 s for each record held, one after the other


def test_a_claim_of_a_dose_holds_each_record_as_it_starts(monkeypatch):
    """Round 3 (F1.3): the backstop's claim of a dose is held for its due check and its first record; a slot of two
    medicines records twice, and run_intent holds each record as it starts, so the chat's end waits for both."""
    for name, seconds in (("check_medication_due", 0.1), ("record_medication_taken", 0.6)):
        monkeypatch.setitem(tool_registry.TIMEOUTS, name, seconds)
    order = []

    async def run():
        registry = ToolRegistry(Robot(), SlowRecords(order, 0.4))

        async def claim():                          # live_session.LiveRelay._run_intent with _record_claim
            await registry.run_intent("check_medication_due", {})
            for number in (1, 2):
                await registry.run_intent("record_medication_taken",
                                          {"medication_id": f"dose-{number}", "patient_confirmed": True})
        registry.hold(asyncio.ensure_future(claim()), "check_medication_due", "record_medication_taken")
        await asyncio.sleep(0)
        await asyncio.wait_for(registry.close(), 3)   # the chat ends at once: 0.7 s of claim, 0.8 s of records

    asyncio.run(run())
    assert order == ["recorded dose-1", "recorded dose-2", "watch reported"]


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
        relay.tools.hold(asyncio.ensure_future(record()), "record_medication_taken")
        world.robot.inbox.put_nowait({"type": "end", "reason": "goodbye"})
        await asyncio.wait_for(running, 5)

    asyncio.run(scenario())
    assert order == ["record finished", "tools closed"]


def test_the_patients_live_chat_slot_is_freed_before_the_held_calls_are_waited_for(world):
    """Round 3 (F1.5): after the robot's side ended, the relay's finally waited for held calls (up to 35 s) while the
    patient's live-chat slot (api_device._live_chats) stayed taken, so the patient's next live chat was refused with
    4409. Now the relay says its chat is over (on_over) once GPT-Live is closed, before that wait."""
    order = []

    class Ending(Backend):
        async def close(self):
            order.append("tools closed")

    async def scenario():
        relay, running = await _begin(world)
        relay.on_over = lambda: order.append(f"slot freed, GPT-Live closes {world.clients[-1].closes}")
        relay.tools = ToolRegistry(Link(), Ending())

        async def record():
            await asyncio.sleep(0.2)
            order.append("record finished")
        relay.tools.hold(asyncio.ensure_future(record()), "record_medication_taken")
        world.robot.inbox.put_nowait({"type": "end", "reason": "goodbye"})
        await asyncio.wait_for(running, 5)

    asyncio.run(scenario())
    assert order == ["slot freed, GPT-Live closes 1", "record finished", "tools closed"]


def test_a_held_call_counts_as_finishing_until_its_end_is_stamped_on_the_relays_clock():
    clock = [1000.0]

    async def main():
        registry = ToolRegistry(Robot(), types.SimpleNamespace(), monotonic=lambda: clock[0])
        run = asyncio.get_running_loop().create_future()
        registry.hold(run, "record_medication_taken")
        before = registry.finished_at
        run.set_result(None)
        just_done = registry.finishing()          # done, but its done callback (which stamps the time) has not run
        clock[0] = 1003.0
        await asyncio.sleep(0)
        return before, just_done, registry.finishing(), registry.finished_at

    before, just_done, still, finished_at = asyncio.run(main())
    assert before == float("-inf") and just_done is True
    assert still is False and finished_at == 1003.0


def test_the_relays_registry_stamps_held_calls_on_the_relays_clock():
    relay = types.SimpleNamespace(u_id=7, conversation_id="c-1", task_id=None, language="zh-TW",
                                  _to_robot=None, clock=lambda: 42.0)
    assert agent_bridge.make_registry(relay).monotonic is relay.clock


async def _record_ran_through_a_quiet_spell(companion):
    """A companion stretch whose patient said they took their medicine, then 40 s of quiet while a record GPT-Live
    asked for still runs (the camera workflow's 30 s watch): no pause meanwhile. The record then finishes, as
    agent_bridge's _run_call does once it has sent GPT-Live the function_call_output and response.create."""
    relay, running = await _start(companion)
    await _speak(companion)
    relay.tools = ToolRegistry(Link(), Backend(), monotonic=companion.clock)
    record = asyncio.get_running_loop().create_future()
    relay.tools.hold(record, "record_medication_taken")
    await _live(companion, _said("我吃完藥了"))
    await _step(companion, relay, 1.75)                       # the patient's turn is stored
    await _step(companion, relay, 40)                         # 40 s of quiet, but the record still runs
    assert companion.clients[0].closes == 0 and companion.robot.json("paused") == []
    record.set_result({"ok": True})
    await _settle()
    await _step(companion, relay, 0.1)                        # GPT-Live's answer cannot have started yet
    assert companion.clients[0].closes == 0 and companion.robot.json("paused") == []
    return relay, running


def test_the_companions_quiet_pause_waits_for_a_record_and_then_for_gpt_lives_answer_to_be_heard(companion):
    async def scenario():
        relay, running = await _record_ran_through_a_quiet_spell(companion)
        await _step(companion, relay, 5.4)                    # 5.5 s after the record: GPT-Live answers
        assert companion.clients[0].closes == 0
        for _ in range(3):                                    # it speaks from 5.5 s to 6.5 s, past the window
            await _live(companion, _sound())
            await _step(companion, relay, 0.5)
        assert companion.clients[0].closes == 0 and companion.robot.json("paused") == []
        assert len(companion.robot.audio()) == 3              # its answer reached the robot after its hold
        await _step(companion, relay, 0.25)                   # its sound ended: the pause comes now
        assert companion.clients[0].closes == 1 and companion.robot.json("paused") == [PAUSED]
        companion.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(companion, running)

    asyncio.run(scenario())


def test_the_companions_quiet_pause_comes_once_gpt_live_leaves_a_finished_record_unanswered(companion):
    async def scenario():
        relay, running = await _record_ran_through_a_quiet_spell(companion)
        await _step(companion, relay, 5.8)                    # 5.9 s after the record: still GPT-Live's to answer
        assert companion.clients[0].closes == 0 and companion.robot.json("paused") == []
        await _step(companion, relay, 0.2)                    # INTENT_WAIT_SECONDS passed with no answer: paused
        assert live_session.INTENT_WAIT_SECONDS == 6.0
        assert companion.clients[0].closes == 1 and companion.robot.json("paused") == [PAUSED]
        companion.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(companion, running)

    asyncio.run(scenario())


class LabelCheck(Backend):
    """A care backend whose label check (verify_medication: the OCR, up to 36 s) answers once the test opens it."""

    def __init__(self):
        super().__init__()
        self.ocr = asyncio.Event()

    async def verify_medication(self, jpeg, expected_medication):
        await self.ocr.wait()
        return {"result": "match", "expected_medication": expected_medication}


async def _label_check_through_a_quiet_spell(companion):
    """A companion stretch whose patient asked Reachy to check the medicine, then fell quiet for 33 s while GPT-Live's
    verify_medication still ran: no pause meanwhile (round 3, F1.4: before, the pause closed GPT-Live at 30 s, before
    it could ask for record_medication_taken)."""
    relay, running = await _start(companion)
    await _speak(companion)
    backend = LabelCheck()
    relay.tools = ToolRegistry(Link(), backend, monotonic=companion.clock)
    call = asyncio.ensure_future(relay.tools.execute("verify_medication", {"expected_medication": "Allegra"}))
    await _live(companion, _said("你幫我看這是不是我的藥"))
    await _step(companion, relay, 1.75)                       # the patient's turn is stored
    await _step(companion, relay, 33)                         # 33 s of quiet, but the label check still runs
    assert companion.clients[0].closes == 0 and companion.robot.json("paused") == []
    return relay, running, backend, call


def test_the_companions_quiet_pause_waits_for_a_medication_step_and_gpt_lives_next_move(companion):
    async def scenario():
        relay, running, backend, call = await _label_check_through_a_quiet_spell(companion)
        backend.ocr.set()
        assert (await call)["ok"] is True                     # the label check answered GPT-Live
        await _step(companion, relay, 5.8)                    # GPT-Live may still ask for the next step
        assert companion.clients[0].closes == 0 and companion.robot.json("paused") == []
        await _step(companion, relay, 0.3)                    # INTENT_WAIT_SECONDS with nothing: paused
        assert companion.clients[0].closes == 1 and companion.robot.json("paused") == [PAUSED]
        companion.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(companion, running)

    asyncio.run(scenario())


def test_the_quiet_pause_waits_for_a_medication_step_no_longer_than_its_own_time_limit(companion):
    async def scenario():
        relay, running, backend, call = await _label_check_through_a_quiet_spell(companion)
        assert tool_registry.TIMEOUTS["verify_medication"] == 36.0
        await _step(companion, relay, 1.5)                    # 36 s after it started: the bound has passed
        assert companion.clients[0].closes == 1 and companion.robot.json("paused") == [PAUSED]
        backend.ocr.set()
        await call
        companion.robot.inbox.put_nowait({"type": "end", "reason": "stopped"})
        await _over(companion, running)

    asyncio.run(scenario())
