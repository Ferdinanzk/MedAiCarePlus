"""Facial expression during each dose session: scoring a covered mouth, the per-dose result, and when it is written."""

import asyncio
import datetime
import json
import sys
import time
import types

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from tests.test_monitor_backend import _Faces, _hand, _jpeg, _packet as _backend_packet  # noqa: E402

from app import config  # noqa: E402
from app.routers import api_emotion, api_history, api_medications  # noqa: E402
from app.services import dose_emotion, intake_repository, monitor_service as service  # noqa: E402
from app.services.emotion_service import LABELS  # noqa: E402
from app.services.monitor_service import MonitorRegistry  # noqa: E402

UTC = datetime.timezone.utc


def _probs(winner: str, score: float = 0.9) -> dict:
    rest = (1.0 - score) / (len(LABELS) - 1)
    return {name: (score if name == winner else rest) for name in LABELS}


def _session(registry=None, intk_id=11, client_type="browser"):
    registry = registry or MonitorRegistry()
    return registry, registry.start(7, intk_id, "Pearl", "Pearl", client_type=client_type)


def _add(state, t, winner, occluded=False, source="server", score=0.9):
    dose_emotion.add_sample(state, t, _probs(winner, score), occluded, source)


# ── summarize (pure) ─────────────────────────────────────────────────────────

def _summary(samples, event, now=1000.0):
    state = types.SimpleNamespace(emotion_samples=[], emotion_totals=dose_emotion.new_totals())
    for t, winner, occluded in samples:
        _add(state, t, winner, occluded)
    return dose_emotion.summarize(state.emotion_samples, state.emotion_totals, event, now)


def test_the_result_prefers_uncovered_faces_just_before_and_after_the_intake():
    # Approach at 100 s, hand away at 102 s. Covered frames in between read 'angry' (the measured bias).
    result = _summary([(90.0, "sad", False),                                   # before the 5 s window
                       (96.0, "happy", False), (98.0, "happy", False),        # before
                       (100.5, "angry", True), (101.0, "angry", True), (101.5, "neutral", False),   # during
                       (103.0, "happy", False), (106.0, "neutral", False),    # after
                       (110.0, "sad", False)], event=(100.0, 102.0))          # after the window
    assert result["basis"] == "event" and result["basis_samples"] == 4
    assert result["dominant"] == "happy"
    assert result["samples"] == 9 and result["unoccluded_samples"] == 7
    assert result["occluded_share"] == pytest.approx(2 / 7, abs=1e-3)       # over the 7 samples of [95, 107]
    phases = result["phases"]
    assert phases["before"]["n"] == 2 and phases["before"]["dominant"] == "happy"
    assert phases["during"]["n"] == 3 and phases["during"]["n_unoccluded"] == 1
    assert phases["during"]["dominant"] == "neutral" and phases["during"]["occluded"] is False
    assert phases["after"]["n"] == 2 and phases["event_seconds"] == 2.0
    assert set(result["probabilities"]) == set(LABELS)
    assert result["timeline"]["origin"] == "event_start"
    assert [point[0] for point in result["timeline"]["points"]] == [-10.0, -4.0, -2.0, 0.5, 1.0, 1.5, 3.0, 6.0, 10.0]
    assert result["timeline"]["points"][3][1:] == [LABELS.index("angry"), 0.9, True]


def test_a_phase_with_only_covered_faces_is_reported_as_occluded():
    result = _summary([(99.0, "happy", False), (100.5, "angry", True), (101.0, "angry", True),
                       (103.0, "happy", False)], event=(100.0, 102.0))
    during = result["phases"]["during"]
    assert during["occluded"] is True and during["dominant"] == "angry" and during["occluded_share"] == 1.0
    assert result["basis"] == "event" and result["dominant"] == "happy"


def test_basis_falls_back_to_the_intake_itself_then_the_session_then_covered_faces():
    # One uncovered face around the event is not enough; with the one during the intake it is 'event_during'.
    during = _summary([(99.0, "sad", False), (101.0, "sad", False), (101.5, "angry", True)], event=(100.0, 102.0),
                      now=110.0)
    assert during["basis"] == "event_during" and during["basis_samples"] == 2 and during["dominant"] == "sad"
    # Nothing uncovered near the event: the session's uncovered faces (of its last KEEP_SECONDS).
    session = _summary([(20.0, "happy", False), (101.0, "angry", True)], event=(100.0, 102.0), now=110.0)
    assert session["basis"] == "session" and session["dominant"] == "happy" and session["basis_samples"] == 1
    # Nothing uncovered at all: the covered faces of the event window, flagged mostly covered.
    covered = _summary([(20.0, "neutral", True), (101.0, "angry", True)], event=(100.0, 102.0), now=110.0)
    assert covered["basis"] == "occluded_only" and covered["dominant"] == "angry" and covered["basis_samples"] == 1
    assert dose_emotion.mostly_occluded(covered["basis"], covered["occluded_share"])
    nothing = _summary([], event=(100.0, 102.0))
    assert nothing["basis"] == "none" and nothing["dominant"] is None and nothing["probabilities"] is None


def test_the_session_basis_covers_only_the_last_two_minutes():
    # A browser session left open: an hour-old face never feeds the result ("at medication time").
    result = _summary([(0.0, "happy", False), (1.0, "happy", False), (3500.0, "sad", False), (3590.0, "sad", True)],
                      event=None, now=3600.0)
    assert result["basis"] == "session" and result["dominant"] == "sad" and result["basis_samples"] == 1
    assert result["samples"] == 4 and result["occluded_share"] == 0.5                # over the last 120 s
    assert [point[0] for point in result["timeline"]["points"]] == [-100.0, -10.0]
    # Only covered faces in that stretch: those, flagged.
    covered = _summary([(0.0, "happy", False), (3590.0, "angry", True)], event=None, now=3600.0)
    assert covered["basis"] == "occluded_only" and covered["dominant"] == "angry"


def test_without_an_event_the_session_counts_and_the_timeline_ends_at_resolution():
    result = _summary([(950.0, "happy", False), (960.0, "happy", False), (970.0, "angry", True),
                       (980.0, "neutral", True)], event=None, now=1000.0)
    assert result["basis"] == "session" and result["dominant"] == "happy" and result["phases"] is None
    assert result["occluded_share"] == 0.5
    assert result["timeline"]["origin"] == "resolved"
    assert [point[0] for point in result["timeline"]["points"]] == [-50.0, -40.0, -30.0, -20.0]


def test_timeline_is_capped_and_keeps_both_ends():
    samples = [(100.0 + i * 0.05, "happy", i % 2 == 0) for i in range(300)]
    result = _summary(samples, event=(105.0, 106.0))
    points = result["timeline"]["points"]
    assert len(points) == dose_emotion.TIMELINE_MAX_POINTS
    assert points[0][0] == -5.0 and points[-1][0] == pytest.approx(9.95)


def test_samples_keep_two_minutes_but_the_totals_cover_the_whole_session():
    _, state = _session()
    for i in range(300):
        _add(state, float(i), "happy", occluded=i % 3 == 0, source="robot")
    assert state.emotion_samples[0][0] >= 299 - dose_emotion.KEEP_SECONDS
    assert state.emotion_totals["n"] == 300 and state.emotion_totals["occluded"] == 100
    assert state.emotion_totals["robot"] == 300 and state.emotion_totals["server"] == 0


def test_probabilities_missing_a_class_count_as_zero():
    assert dose_emotion.unoccluded_mean([(1.0, {"happy": 1.0}, False, "server")], since=0.0)["sad"] == 0.0
    assert dose_emotion.unoccluded_mean([(1.0, {"happy": 1.0}, True, "server")], since=0.0) is None


@pytest.mark.parametrize("status,source,recorded_here,expected", [
    ("taken", None, True, "recorded"),
    ("taken", None, False, "taken_other"),
    ("pending_confirmation", "degraded", False, "sent_to_family"),
    ("pending_confirmation", "patient_claim", False, "patient_claim"),
    ("taken", "uncertain_detection", False, "sent_to_family"),     # family already answered 'taken'
    ("pending", "patient_claim", False, "patient_claim"),          # family answered 'not taken'
    ("pending_confirmation", None, False, "sent_to_family"),
    ("skipped", None, False, "skipped"),
    ("pending", None, False, "unresolved"),
    ("missed", None, False, "unresolved"),
])
def test_outcome_mapping(status, source, recorded_here, expected):
    assert dose_emotion.outcome_for(status, source, recorded_here) == expected
    assert expected in dose_emotion.OUTCOMES


def test_chip_parses_jsonb_text_and_flags_mostly_covered():
    value = json.dumps({"dominant": "sad", "score": 0.7, "occluded_share": 0.6, "basis": "event", "basis_samples": 9})
    chip = dose_emotion.chip(value)
    assert chip["mostly_occluded"] is True and chip["uncertain"] is False      # flagged once, as covered
    clear = dose_emotion.chip({"dominant": "sad", "score": 0.7, "occluded_share": 0.2, "basis": "event",
                               "basis_samples": 9})
    assert clear["mostly_occluded"] is False and clear["uncertain"] is False
    assert dose_emotion.chip(None) is None


@pytest.mark.parametrize("basis_samples,score,expected", [
    (1, 0.9, True),       # one clear frame
    (3, 0.9, True),
    (4, 0.9, False),
    (20, 0.3, True),      # a weak top class (of 7)
    (20, 0.4, False),
])
def test_thin_evidence_is_flagged_uncertain(basis_samples, score, expected):
    chip = dose_emotion.chip({"dominant": "sad", "score": score, "occluded_share": 0.1, "basis": "session",
                              "basis_samples": basis_samples})
    assert chip["uncertain"] is expected
    assert dose_emotion.uncertain(basis_samples, None) is False                  # no result: nothing to flag


def test_phases_get_their_own_uncertain_flag():
    phases = dose_emotion.phase_flags({"before": {"n_unoccluded": 8, "score": 0.7, "occluded": False},
                                       "during": {"n_unoccluded": 0, "score": 0.6, "occluded": True},
                                       "after": {"n_unoccluded": 2, "score": 0.8, "occluded": False},
                                       "event_seconds": 1.5})
    assert [phases[name]["uncertain"] for name in ("before", "during", "after")] == [False, False, True]
    assert dose_emotion.phase_flags(None) is None


# ── writing the result ───────────────────────────────────────────────────────

class _Conn:
    def __init__(self, facts=None, fail=False, consents=("core", "robot_camera")):
        self.facts = facts if facts is not None else {"intake_stats": "taken", "detection_method": "auto",
                                                       "confirmation_source": None, "recorded_here": True}
        self.fail = fail
        self.consents = consents
        self.inserted = []
        self.keys = set()
        self.facts_args = None

    async def fetch(self, query, *args):
        # consent_service.fetch_state: the latest row per scope; the scopes listed are granted at the current terms.
        assert "FROM consent" in query and args == (7,)
        return [{"scope": scope, "granted": True, "terms_version": config.TERMS_VERSION, "kind": "core",
                 "consent_id": index, "created_at": None} for index, scope in enumerate(self.consents)]

    async def fetchrow(self, query, *args):
        assert query == dose_emotion.FACTS_SQL
        self.facts_args = args
        if self.fail:
            raise RuntimeError("database is down")
        return self.facts

    async def fetchval(self, query, *args):
        assert query == dose_emotion.INSERT_SQL and "ON CONFLICT (session_id, intk_id) DO NOTHING" in query
        key = (args[2], args[1])
        if key in self.keys:
            return None
        self.keys.add(key)
        self.inserted.append(args)
        return len(self.inserted)


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn
        self.acquired = 0

    def acquire(self):
        self.acquired += 1
        return _Acquire(self.conn)


@pytest.fixture
def pool(monkeypatch):
    pool = _Pool(_Conn())
    monkeypatch.setattr(dose_emotion, "get_pool", lambda: pool)
    return pool


def _scored_session(intk_id=11, client_type="browser"):
    _, state = _session(intk_id=intk_id, client_type=client_type)
    now = time.monotonic()
    state.last_event = (now - 4.0, now - 2.0)
    for offset, winner, occluded in ((-5.0, "happy", False), (-4.5, "happy", False), (-3.0, "angry", True),
                                     (-1.0, "happy", False)):
        _add(state, now + offset, winner, occluded)
    return state


def test_finalize_writes_once_per_session_and_dose(pool):
    state = _scored_session()

    async def scenario():
        assert await dose_emotion.finalize(state, "resolved") is True
        assert await dose_emotion.finalize(state, "session_end") is False      # done: no second write
        state.dose_emotion_done = False                                        # even forced: ON CONFLICT
        assert await dose_emotion.finalize(state, "session_end") is False
    asyncio.run(scenario())
    conn = pool.conn
    assert len(conn.inserted) == 1
    args = conn.inserted[0]
    assert args[:6] == (7, 11, state.session_id, "browser", "server", "recorded")
    assert args[6:9] == ("taken", "auto", None)
    assert args[9] == state.started_at and isinstance(args[10], datetime.datetime)
    assert args[11:16] == (4, 3, 0.25, "event", 3)
    assert json.loads(args[16])["happy"] > 0.8 and args[17] == "happy"
    assert set(json.loads(args[19])) == {"before", "during", "after", "event_seconds"}
    assert json.loads(args[20])["origin"] == "event_start"
    assert conn.facts_args == (11, 7, state.started_at, state.session_id)


def test_finalize_skips_a_session_that_saw_nothing_and_resolved_nothing(pool):
    _, state = _session()
    assert asyncio.run(dose_emotion.finalize(state, "session_end")) is False
    assert pool.acquired == 0
    # Resolved with no face scored: a 'none' row says the camera had no expression data for this dose.
    _, resolved = _session()
    resolved.dose_emotion_resolved_at = datetime.datetime(2026, 10, 3, tzinfo=UTC)
    assert asyncio.run(dose_emotion.finalize(resolved, "resolved")) is True
    assert pool.conn.inserted[0][14] == "none" and pool.conn.inserted[0][10] == resolved.dose_emotion_resolved_at


def test_finalize_skips_observe_sessions(pool):
    registry = MonitorRegistry()
    state = registry.start(7, None, "Pearl", "Pearl", mode="observe")
    _add(state, time.monotonic(), "happy")
    assert asyncio.run(dose_emotion.finalize(state, "session_end")) is False and pool.acquired == 0


def test_a_database_error_is_logged_never_raised(monkeypatch, caplog):
    pool = _Pool(_Conn(fail=True))
    monkeypatch.setattr(dose_emotion, "get_pool", lambda: pool)
    state = _scored_session()
    with caplog.at_level("ERROR", logger=dose_emotion.log.name):
        assert asyncio.run(dose_emotion.finalize(state, "resolved")) is False
    assert "was not written" in caplog.text


@pytest.mark.parametrize("client_type,consents", [
    ("browser", ()),                          # core withdrawn while the session was open
    ("reachy", ("core",)),                    # robot_camera withdrawn
])
def test_nothing_is_written_once_consent_was_withdrawn(monkeypatch, caplog, client_type, consents):
    pool = _Pool(_Conn(consents=consents))
    monkeypatch.setattr(dose_emotion, "get_pool", lambda: pool)
    state = _scored_session(client_type=client_type)
    with caplog.at_level("INFO", logger=dose_emotion.log.name):
        assert asyncio.run(dose_emotion.finalize(state, "session_end")) is False
    assert not pool.conn.inserted and pool.conn.facts_args is None and state.dose_emotion_done
    assert "consent is no longer current" in caplog.text


def test_a_robot_session_with_both_consents_is_written(pool):
    state = _scored_session(client_type="reachy")
    assert asyncio.run(dose_emotion.finalize(state, "session_end")) is True
    assert pool.conn.inserted[0][3] == "reachy"


def test_a_swept_session_is_described_at_its_last_activity(pool):
    # A closed tab: no frame for 10 minutes, then the idle sweep ends the session.
    _, state = _session()
    idle = 600.0
    last = time.monotonic() - idle
    state.last_activity_at = last
    for offset, winner, occluded in ((-20.0, "happy", False), (-10.0, "happy", False), (-5.0, "sad", True)):
        _add(state, last + offset, winner, occluded)
    before = datetime.datetime.now(UTC)
    assert asyncio.run(dose_emotion.finalize(state, "session_end")) is True
    args = pool.conn.inserted[0]
    assert args[5] == "recorded" and args[14] == "session" and args[17] == "happy"
    resolved_at = args[10]
    assert before - datetime.timedelta(seconds=idle + 5) < resolved_at < before - datetime.timedelta(seconds=idle - 5)
    timeline = json.loads(args[20])
    assert timeline["origin"] == "resolved"
    assert [point[0] for point in timeline["points"]] == pytest.approx([-20.0, -10.0, -5.0], abs=0.05)


def test_the_log_line_names_no_expression_class(pool, caplog):
    state = _scored_session()
    with caplog.at_level("INFO", logger=dose_emotion.log.name):
        assert asyncio.run(dose_emotion.finalize(state, "resolved")) is True
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("dose emotion for dose"))
    assert "outcome recorded" in line and not any(label in line for label in LABELS)


def test_resolution_waits_for_the_after_window_unless_the_session_ends_first(pool, monkeypatch):
    monkeypatch.setattr(dose_emotion, "RESOLVE_FLOOR_SECONDS", 0.2)

    async def scenario():
        state = _scored_session()
        state.last_event = (time.monotonic() - 1.0, time.monotonic())     # hand just came away: wait ~5.5 s
        dose_emotion.note_resolution(state)
        timer = state.dose_emotion_timer
        assert timer is not None and state.dose_emotion_resolved_at is not None
        await asyncio.sleep(0.05)
        assert not pool.conn.inserted
        dose_emotion.finalize_soon(state, "session_end")                  # the session ends: written now
        await asyncio.sleep(0.05)
        assert timer.cancelled() and len(pool.conn.inserted) == 1
        dose_emotion.note_resolution(state)                                # nothing more to schedule
        dose_emotion.finalize_soon(state)
        await asyncio.sleep(0.05)
        assert len(pool.conn.inserted) == 1

        late = _scored_session(intk_id=12)
        late.last_event = (time.monotonic() - 20.0, time.monotonic() - 10.0)   # after-window already over
        dose_emotion.note_resolution(late)
        # Still not at once: a confirmation request notes it inside its transaction, which may not have committed.
        await asyncio.sleep(0.05)
        assert len(pool.conn.inserted) == 1
        await asyncio.sleep(0.3)
        assert len(pool.conn.inserted) == 2 and pool.conn.inserted[1][1] == 12
    asyncio.run(scenario())


def test_the_resolve_floor_outlasts_a_request_commit():
    assert dose_emotion.RESOLVE_FLOOR_SECONDS >= 1.0


def test_note_resolution_without_an_event_loop_does_nothing():
    state = _scored_session()
    dose_emotion.note_resolution(state)
    dose_emotion.finalize_soon(state)
    assert state.dose_emotion_timer is None and not state.dose_emotion_done


# ── where the dose is resolved ───────────────────────────────────────────────

def test_a_camera_commit_schedules_the_result(monkeypatch):
    noted = []
    results = iter([{"event_id": "e", "status": "taken", "already_recorded": False},
                    {"event_id": "e", "status": "taken", "already_recorded": True}])

    async def commit(state, candidate, method):
        return next(results)

    monkeypatch.setattr(intake_repository, "_commit_monitored", commit)
    monkeypatch.setattr(dose_emotion, "note_resolution", noted.append)
    state = types.SimpleNamespace(u_id=7, intk_id=11, clip_enabled=False)
    asyncio.run(intake_repository.commit_monitored(state, {"event_id": "e"}, "auto"))
    asyncio.run(intake_repository.commit_monitored(state, {"event_id": "e"}, "auto"))   # a retry: nothing new
    assert noted == [state]


def test_a_confirmation_request_notes_the_live_session_of_its_dose(monkeypatch):
    from tests.test_dose_confirmation import FakeDB, create

    noted = []
    monkeypatch.setattr(dose_emotion, "note_resolution_for", lambda u_id, ids: noted.append((u_id, list(ids))))
    create(FakeDB(), intk_ids=(100,), source="patient_claim")
    assert noted == [(7, [100])]


def test_note_resolution_for_finds_the_session_and_never_raises(monkeypatch):
    registry = MonitorRegistry()
    monkeypatch.setattr(service, "registry", registry)
    noted = []
    monkeypatch.setattr(dose_emotion, "note_resolution", noted.append)
    state = registry.start(7, 100, "Pearl", "Pearl", client_type="reachy")
    dose_emotion.note_resolution_for(7, [101])
    dose_emotion.note_resolution_for(8, [100])
    dose_emotion.note_resolution_for(7, [100, 101])
    assert noted == [state]

    class _Broken:
        def get(self, *args):
            raise RuntimeError("registry broke")

    monkeypatch.setattr(registry, "sessions", _Broken())
    dose_emotion.note_resolution_for(7, [100])      # logged, not raised


class _Detector:
    """Scripted detector stages per frame: {seq: (stage, decision, event_id)}."""

    def __init__(self, script=None):
        self.script = script or {}

    async def process_frame(self, u_id, session_id, payload, result_transform=None):
        stage, decision, event_id = self.script.get(payload["frame_seq"], ("READY", "none", None))
        return {"stage": stage, "decision": decision, "event_confidence": .9, "frame_seq": payload["frame_seq"],
                "policy": {"event_id": event_id}}

    async def end_session(self, *args, **kwargs):
        return None


@pytest.fixture
def finalized(monkeypatch):
    calls = []
    monkeypatch.setattr(dose_emotion, "finalize_soon", lambda state, reason="session_end": calls.append(
        (state.session_id, reason)))
    monkeypatch.setattr(service.IntakeDetectionService, "get_instance", classmethod(lambda cls: _Detector()))
    return calls


def test_every_way_a_session_ends_writes_its_result(finalized):
    async def scenario():
        registry = MonitorRegistry()
        first = registry.start(7, 11, "Pearl", "Pearl")
        await registry.end(first)
        second = registry.start(7, 11, "Pearl", "Pearl")
        third = await registry.replace(7, 12, "Pearl", "Pearl")
        fourth = registry.start(8, 13, "Jane", "Jane")
        fourth.last_activity_at = time.monotonic() - service.IDLE_SESSION_SECONDS - 1
        assert await registry.sweep_idle() == 1
        assert fourth.ended and fourth.session_id not in registry.sessions and 8 not in registry.by_user
        assert registry.by_user[7] == third.session_id and not third.ended
        return first, second, fourth
    first, second, fourth = asyncio.run(scenario())
    assert finalized == [(first.session_id, "session_end"), (second.session_id, "replaced"),
                         (fourth.session_id, "session_end")]


def test_vision_interval_is_four_hertz_for_a_verified_patient_until_the_dose_result_is_written():
    registry = MonitorRegistry()
    dose = registry.start(7, 11, "Pearl", "Pearl", client_type="reachy")
    # Not verified: every vision call would run identity, which stays at 2 Hz.
    assert service.vision_interval(dose) == service.VISION_INTERVAL == 0.5
    _verify(dose)
    assert service.vision_interval(dose) == service.DOSE_VISION_INTERVAL == 0.25
    dose.dose_emotion_done = True
    assert service.vision_interval(dose) == 0.5
    observe = registry.start(8, None, "Jane", "Jane", mode="observe", client_type="reachy")
    _verify(observe)
    assert service.vision_interval(observe) == 0.5


# ── scoring in the session ───────────────────────────────────────────────────

class _Emotion:
    def __init__(self, winner="happy"):
        self.winner = winner
        self.calls = 0

    def predict_crop(self, crop):
        self.calls += 1
        probabilities = _probs(self.winner)
        return {"detected": True, "emotion_type": self.winner.capitalize(), "emotion_score": 0.9,
                "probabilities": probabilities, "error": None}


def _verify(state):
    state.identity_hits, state.verified_at, state.target_box = 2, time.monotonic(), [0.1, 0.1, 0.4, 0.4]


def _covered(seq):
    packet = _backend_packet(seq)
    packet["hands"] = [_hand(0.3, 0.3)]
    packet["poses"][0]["wrists"] = [[0.3, 0.32, 0.9]]
    return packet


def test_a_covered_mouth_is_scored_as_occluded_and_never_shown_live(monkeypatch, finalized):
    emotion = _Emotion("angry")
    monkeypatch.setattr(service.EmotionService, "get_instance", classmethod(lambda cls: emotion))
    monkeypatch.setattr(service.FaceRecognitionService, "get_instance", classmethod(lambda cls: _Faces()))

    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl")
        _verify(state)
        await registry.landmarks(state, _covered(1))
        arrived = state.packet_times[1]
        result = await registry.vision(state, 1, _jpeg(), lambda *_: None)
        assert emotion.calls == 1 and result["emotion"] is None and result["emotion_occluded"] is True
        assert state.emotion_samples == [(arrived, _probs("angry"), True, "server")]

        emotion.winner = "happy"
        _verify(state)
        await registry.landmarks(state, _backend_packet(2))
        result = await registry.vision(state, 2, _jpeg(), lambda *_: None)
        assert result["emotion"]["emotion_type"] == "Happy" and result["emotion_occluded"] is False
        assert [sample[2] for sample in state.emotion_samples] == [True, False]
        assert state.emotion_totals["n"] == 2 and state.emotion_totals["occluded"] == 1
    asyncio.run(scenario())


def test_a_candidate_still_becomes_ready_only_on_an_uncovered_frame(monkeypatch, finalized):
    monkeypatch.setattr(service.EmotionService, "get_instance", classmethod(lambda cls: _Emotion()))
    monkeypatch.setattr(service.FaceRecognitionService, "get_instance", classmethod(lambda cls: _Faces()))

    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl")
        _verify(state)
        await registry.landmarks(state, _covered(1))
        state.candidate = {"event_id": "evt", "decision": "uncertain", "confidence": .5,
                           "created_at": time.monotonic(), "frame_seq": 1, "emotion_probabilities": None,
                           "identity_distance": .2, "ready": False}
        await registry.vision(state, 1, _jpeg(), lambda *_: None)
        assert state.candidate["ready"] is False and state.emotion_samples[-1][2] is True
        await registry.landmarks(state, _backend_packet(2))
        await registry.vision(state, 2, _jpeg(), lambda *_: None)
        assert state.candidate["ready"] is True
    asyncio.run(scenario())


def test_the_event_window_restarts_after_an_abandoned_approach(monkeypatch, finalized):
    detector = _Detector({1: ("APPROACHING", "none", None), 2: ("READY", "none", None),
                          3: ("APPROACHING", "none", None), 4: ("AT_MOUTH", "none", None),
                          5: ("WITHDRAWING", "uncertain", "evt-1")})
    monkeypatch.setattr(service.IntakeDetectionService, "get_instance", classmethod(lambda cls: detector))

    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl")
        _verify(state)
        await registry.landmarks(state, _backend_packet(1))
        first = state.emotion_event_start
        await registry.landmarks(state, _backend_packet(2))
        assert first is not None and state.emotion_event_start is None        # the approach came to nothing
        await registry.landmarks(state, _backend_packet(3))
        start = state.emotion_event_start
        assert start is not None and start >= first
        assert state.event_started_at == first                                 # dose_video's start is unchanged
        _add(state, start - 3.0, "sad")                                        # before this event: not the commit's
        _add(state, start + 0.001, "happy")                                    # the face during the approach
        _add(state, start + 0.002, "angry", occluded=True)                     # hand over the mouth
        await registry.landmarks(state, _backend_packet(4))
        await registry.landmarks(state, _backend_packet(5))
        candidate = state.candidate
        assert candidate["event_id"] == "evt-1"
        # The commit's emotion row, as before: the faces since this event's start, the uncovered ones only.
        assert candidate["emotion_probabilities"]["happy"] == pytest.approx(0.9)
        assert state.last_event[0] == start and state.last_event[1] >= start
        assert state.emotion_event_start is None
    asyncio.run(scenario())


def test_the_event_window_starts_when_the_detector_skips_approaching(monkeypatch, finalized):
    # READY -> AT_MOUTH in one detector frame (a hand first seen near the face, or a fast approach at 10 fps):
    # the window still starts at the event's first frame, not at the candidate.
    detector = _Detector({1: ("READY", "none", None), 2: ("AT_MOUTH", "none", None),
                          3: ("OCCLUDED", "none", None), 4: ("AT_MOUTH", "none", None),
                          5: ("COOLDOWN", "uncertain", "evt-2")})
    monkeypatch.setattr(service.IntakeDetectionService, "get_instance", classmethod(lambda cls: detector))

    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl")
        _verify(state)
        await registry.landmarks(state, _backend_packet(1))
        assert state.emotion_event_start is None
        await registry.landmarks(state, _backend_packet(2))
        start = state.emotion_event_start
        assert start == state.packet_times[2]
        _add(state, start + 0.001, "angry", occluded=True)
        await asyncio.sleep(0.01)
        await registry.landmarks(state, _backend_packet(3))
        await registry.landmarks(state, _backend_packet(4))
        await registry.landmarks(state, _backend_packet(5))
        assert state.candidate["event_id"] == "evt-2"
        assert state.last_event[0] == start and state.last_event[1] - start >= 0.01
        assert state.candidate["emotion_probabilities"] is None               # only a covered face since the start
    asyncio.run(scenario())


# ── API ──────────────────────────────────────────────────────────────────────

class _RowsConn:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        return self.rows


class _RowsPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def _intake_row(**extra):
    return {"intake_id": 9, "id": 9, "med_id": 4, "name": "Metformin", "medication_name": "Metformin",
            "status": "taken", "scheduled_time": datetime.datetime(2026, 10, 3, tzinfo=UTC), "previous_time": None,
            "schedule_time": None, "use_before": None, "total": 1, **extra}


def test_today_and_history_rows_carry_the_dose_emotion(monkeypatch):
    chip = json.dumps({"dominant": "happy", "score": 0.81, "occluded_share": 0.62, "basis": "event", "samples": 30,
                       "unoccluded_samples": 21, "outcome": "sent_to_family"})
    conn = _RowsConn([_intake_row(dose_emotion=chip), _intake_row(id=10, intake_id=10, dose_emotion=None)])
    monkeypatch.setattr(api_medications, "get_pool", lambda: _RowsPool(conn))
    monkeypatch.setattr(api_history, "get_pool", lambda: _RowsPool(conn))
    today = asyncio.run(api_medications.today_medications({"u_id": 7}, "2026-10-03"))
    assert today[0]["emotion"]["dominant"] == "happy" and today[0]["emotion"]["mostly_occluded"] is True
    assert today[0]["emotion"]["uncertain"] is False
    assert today[1]["emotion"] is None and "dose_emotion" not in today[0]
    query = conn.queries[0][0]
    assert "FROM dose_emotion de" in query and "dose_emotion_best.dose_emotion" in query
    assert "de.intk_id = i.intk_id AND de.u_id = i.u_id AND i.intake_stats IN ('taken', 'pending_confirmation')" in query
    history = asyncio.run(api_history.get_intake_history({"u_id": 7}, None, None, 50, 0))
    assert history["items"][0]["emotion"]["outcome"] == "sent_to_family" and history["items"][1]["emotion"] is None
    query = conn.queries[1][0]
    assert "FROM dose_emotion de" in query
    # The page is cut before the per-dose probe, so it runs only for the rows returned.
    assert query.index("LIMIT $5 OFFSET $6") < query.index("FROM dose_emotion de")
    assert "de.intk_id = page.id AND de.u_id = $1 AND page.status IN ('taken', 'pending_confirmation')" in query


def test_the_chip_is_only_for_doses_taken_or_sent_on():
    # "Expression while taking": never on a dose the session left pending or skipped, nor once the dose is no
    # longer taken or waiting (an undo, family answering 'not taken'). The Emotion page still lists those results.
    sql = dose_emotion.chip_join("i.intk_id", "i.u_id", "i.intake_stats")
    assert "de.outcome IN ('recorded', 'taken_other', 'sent_to_family', 'patient_claim')" in sql
    assert "i.intake_stats IN ('taken', 'pending_confirmation')" in sql
    assert "'unresolved'" not in sql and "'skipped'" not in sql
    assert "'basis_samples', de.basis_samples" in sql


def test_rows_without_the_column_are_left_as_they_were(monkeypatch):
    conn = _RowsConn([_intake_row()])
    monkeypatch.setattr(api_history, "get_pool", lambda: _RowsPool(conn))
    history = asyncio.run(api_history.get_intake_history({"u_id": 7}, None, None, 50, 0))
    assert "emotion" not in history["items"][0]


def test_medication_emotion_list_and_detail_are_the_patients_own(monkeypatch):
    row = {"id": 1, "intk_id": 9, "med_name": "Metformin", "status": "pending_confirmation",
           "outcome": "sent_to_family", "basis": "event", "basis_samples": 2, "occluded_share": 0.3,
           "dominant": "sad", "score": 0.6, "probabilities": json.dumps(_probs("sad", 0.6)),
           "phases": json.dumps({"before": {"dominant": "sad", "n_unoccluded": 6, "score": 0.6, "occluded": False}}),
           "timeline": json.dumps({"points": []})}
    conn = _RowsConn([row])
    monkeypatch.setattr(api_emotion, "get_pool", lambda: _RowsPool(conn))
    listed = asyncio.run(api_emotion.medication_emotions({"u_id": 7}, 20))
    assert listed[0]["phases"]["before"]["dominant"] == "sad" and listed[0]["mostly_occluded"] is False
    assert listed[0]["uncertain"] is True and listed[0]["phases"]["before"]["uncertain"] is False   # 2 clear frames
    assert listed[0]["status"] == "pending_confirmation"                     # the dose's status now, beside outcome
    assert "timeline" not in listed[0]
    query, args = conn.queries[-1]
    assert "WHERE de.u_id = $1" in query and "DISTINCT ON (de.intk_id)" in query and args == (7, 20)
    detail = asyncio.run(api_emotion.medication_emotion_detail(9, {"u_id": 7}))
    assert detail["sessions"][0]["timeline"] == {"points": []}
    query, args = conn.queries[-1]
    assert "WHERE de.u_id = $1 AND de.intk_id = $2" in query and args == (7, 9)
    conn.rows = []
    missing = asyncio.run(api_emotion.medication_emotion_detail(9, {"u_id": 8}))
    assert missing.status_code == 404


def test_medication_emotion_routes_require_consent():
    from app.dependencies import get_consented_user

    paths = {route.path: route for route in api_emotion.router.routes}
    for path in ("/api/emotion/medication", "/api/emotion/medication/{intk_id}"):
        assert get_consented_user in [dep.call for dep in paths[path].dependant.dependencies]
