"""The GPT-Live agent's tools on the server (services/tool_registry.py): the registry's envelope and validation, and
the care backend against a fake database and fakes of the services it reuses (dose_safety's facts answered by
tests/dose_facts, the monitor registry, the family confirmation request, memory, the outbox, the safety alert);
including the visual medication workflow (get_medication_schedule "now" -> verify_medication ->
start_pill_intake_detection -> get_pill_intake_result -> record_medication_taken) and the multimodal emotion."""

import asyncio
import datetime
import sys
import types
from zoneinfo import ZoneInfo

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.services import (conversation, dose_confirmation, dose_safety, dose_status, gpt_live_config, memory,   # noqa: E402
                          monitor_service, outbox, schedule, tool_registry)
from app.services.tool_registry import ServerCareBackend, ToolError, ToolRegistry, names_match   # noqa: E402
from tests import dose_facts   # noqa: E402

ZONE = ZoneInfo("Asia/Taipei")
MEDS = {10: {"med_name": "Allegra", "schedule_time": {"morning": True, "night": True}, "min_interval_minutes": None,
             "max_daily_doses": None}}


def at(hour, minute=0):
    return datetime.datetime(2026, 10, 12, hour, minute, tzinfo=ZONE)


def intakes():
    return {1: {"med_id": 10, "intake_stats": "taken", "intake_time_stamp": at(8), "actual_intake_time": at(8, 10)},
            2: {"med_id": 10, "intake_stats": "pending", "intake_time_stamp": at(20), "actual_intake_time": None}}


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Conn:
    def __init__(self, intakes, meds, task_ids=None):
        self.intakes, self.meds, self.task_ids = intakes, meds, task_ids or []
        self.executed = []

    def transaction(self):
        return Transaction()

    def _row(self, intk_id):
        row = self.intakes[intk_id]
        return {"intk_id": intk_id, "med_id": row["med_id"], "med_name": self.meds[row["med_id"]]["med_name"],
                "intake_stats": row["intake_stats"], "intake_time_stamp": row["intake_time_stamp"],
                "actual_intake_time": row.get("actual_intake_time")}

    async def fetchval(self, query, *args):
        if query == dose_status.TASK_SQL:
            return self.task_ids
        if query == tool_registry.NAME_SQL:
            return "阿嬤"
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        if query == tool_registry.DOSE_SQL:
            if args[1] not in self.intakes:
                return None
            return {**self._row(args[1]), "active": True}
        if query == tool_registry.MEDICINE_SQL:
            med = self.meds.get(args[1])
            return None if med is None else {"med_id": args[1], "med_name": med["med_name"], "active": True}
        raise AssertionError(query)

    async def fetch(self, query, *args):
        ordered = sorted(self.intakes, key=lambda i: (self.intakes[i]["intake_time_stamp"], i))
        if query == dose_status.TODAY_SQL:
            return [{"intk_id": i, "intake_time_stamp": self.intakes[i]["intake_time_stamp"]} for i in ordered]
        if query in (tool_registry.DOSES_SQL, tool_registry.SCHEDULE_SQL):
            wanted = args[1] if query == tool_registry.DOSES_SQL else ordered
            return [self._row(i) for i in ordered if i in wanted]
        if query == tool_registry.MEDICINES_SQL:
            return [{"med_id": med_id, "med_name": med["med_name"]} for med_id, med in self.meds.items()]
        if query in (tool_registry.EMOTION_HISTORY_SQL, tool_registry.MOOD_HISTORY_SQL):
            return []
        if dose_facts.is_facts(query):
            return dose_facts.rows(args, self.intakes, self.meds)
        raise AssertionError(query)


class Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        pool = self

        class Acquire:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *exc):
                return False
        return Acquire()


class Robot:
    def __init__(self, report=None):
        self.emotes, self.frames, self.report = [], 0, report

    async def capture_jpeg(self):
        self.frames += 1
        return b"\xff\xd8frame", {"width": 640, "height": 480, "captured_at": 1.0}

    async def emote(self, preset):
        self.emotes.append(preset)
        return {"status": "queued", "preset": preset, "pending_motion_jobs": 1}

    async def emotion(self):
        if isinstance(self.report, Exception):
            raise self.report
        return self.report

    async def dance(self, name, repeat):
        self.emotes.append(f"dance:{name}:{repeat}")
        return {"status": "started", "dance": name or "side_to_side_sway", "repeat": repeat, "replaced": False,
                "available": ["jackson_square", "side_to_side_sway"]}

    async def stop_dance(self):
        self.emotes.append("stop_dance")
        return {"status": "idle"}

    async def express(self, move, posture):
        self.emotes.append(f"express:{move}:{posture}")
        return {"status": "started", "library": "emotion", "move": move, "available": [move]}


class FakeOCR:
    def __init__(self, answer):
        self.answer, self.images = answer, []

    def process_image(self, image_bytes):
        self.images.append(image_bytes)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def backend(conn=None, **kw):
    conn = conn or Conn(intakes(), MEDS)
    return ServerCareBackend(7, "c-1", None, "zh-TW", pool=lambda: Pool(conn), **kw), conn


@pytest.fixture(autouse=True)
def quiet_events(monkeypatch):
    """Family's event messages (dose_event.schedule) are tests/test_medication_events.py's; here they are stubbed."""
    def schedule(u_id, *, delay=0.0, **fields):
        future = asyncio.get_running_loop().create_future()
        future.set_result({"event": fields.get("dedupe"), "video": False, "contacts": 0})
        return future
    monkeypatch.setattr(tool_registry.dose_event, "schedule", schedule)


@pytest.fixture(autouse=True)
def video_consent(monkeypatch):
    """The patient has dose videos on (dose_video.clip_outlook reads it before a request); no frames are buffered."""
    async def enabled(u_id):
        return True
    monkeypatch.setattr(tool_registry.dose_video, "enabled", enabled)
    monkeypatch.setattr(tool_registry.dose_video, "_buffers", {})


@pytest.fixture(autouse=True)
def vision_on(monkeypatch):
    """The vision tools read frames with the OCR's model only with config.LIVE_VISION_CLOUD (one test turns it off)."""
    monkeypatch.setattr(tool_registry.config, "LIVE_VISION_CLOUD", True)


@pytest.fixture
def now(monkeypatch):
    monkeypatch.setattr(schedule, "current_time", lambda: at(20, 15))


@pytest.fixture
def consent_on(monkeypatch):
    async def get_state(u_id):
        return {}
    monkeypatch.setattr(tool_registry.consent_service, "get_state", get_state)
    monkeypatch.setattr(memory, "consent_current", lambda state: True)


class Session:
    def __init__(self):
        self.ended, self.extra_events, self.candidate, self.recorded = False, [], None, None
        self.landmark_fps, self.degraded, self.emotion, self.emotion_occluded = 2.0, True, None, False
        self.emotion_samples, self.identity_status, self.client_type = [], "verified", "reachy"
        self.latest_detector = {"stage": "WAITING_FOR_PEARL", "decision": "none"}

    def public(self):
        return {"identity_status": self.identity_status, "emotion": self.emotion,
                "emotion_occluded": self.emotion_occluded}


@pytest.fixture
def camera(monkeypatch):
    session = Session()
    registry = types.SimpleNamespace(sessions={"s1": session}, by_user={7: "s1"})
    monkeypatch.setattr(monitor_service, "registry", registry)
    return session


# ── the schemas ──

def test_the_seven_tools_are_registered_with_their_arguments():
    by_name = {tool["name"]: tool["parameters"] for tool in gpt_live_config.TOOLS_SCHEMA}
    assert len(gpt_live_config.TOOLS_SCHEMA) == 25 and len(set(by_name)) == 25
    assert by_name["play_emotion"]["properties"]["emotion"]["enum"] == list(gpt_live_config.EMOTION_MOVES)
    assert list(by_name["verify_medication"]["properties"]) == ["expected_medication"]
    assert list(by_name["get_weather"]["properties"]) == ["location"]
    assert by_name["dance"]["properties"]["dance"]["enum"] == [*gpt_live_config.DANCES, None]
    assert list(by_name["stop_dance"]["properties"]) == []
    assert list(by_name["describe_view"]["properties"]) == ["question"]
    assert by_name["get_medication_schedule"]["properties"]["time_scope"]["enum"] == ["now", "today", None]
    assert set(by_name["start_pill_intake_detection"]["properties"]) == {"medication_name", "timeout_seconds"}
    assert list(by_name["get_pill_intake_result"]["properties"]) == ["session_id"]
    assert list(by_name["get_current_emotion"]["properties"]) == []
    assert list(by_name["notify_family"]["properties"]) == ["reason", "severity", "message", "urgency"]
    assert set(by_name["record_medication_taken"]["properties"]) == {"medication_name", "session_id", "medication_id",
                                                                     "patient_confirmed"}
    for name, parameters in by_name.items():   # strict shape: every property listed as required, nulls allowed
        assert set(parameters["required"]) == set(parameters["properties"]), name
        assert parameters["additionalProperties"] is False
    assert set(tool_registry.TIMEOUTS) == set(gpt_live_config.TOOL_NAMES)
    assert ToolRegistry(Robot(), backend()[0])._handlers.keys() == set(gpt_live_config.TOOL_NAMES)


# ── the registry ──

def test_execute_always_answers_an_envelope(monkeypatch):
    async def run():
        registry = ToolRegistry(Robot(), backend()[0])
        unknown = await registry.execute("fly", {})
        not_object = await registry.execute("camera", "x")
        bad_args = await registry.execute("camera", {"zoom": 2})
        tool_error = await registry.execute("trigger_antenna_emote", {"preset": "backflip"})
        nulls = await registry.execute("get_pill_intake_result", {"session_id": None})
        monkeypatch.setitem(tool_registry.TIMEOUTS, "camera", 0.0)

        async def slow():
            await asyncio.sleep(0.2)
        monkeypatch.setattr(registry.robot, "capture_jpeg", slow)
        timeout = await registry.execute("camera", {})
        return unknown, not_object, bad_args, tool_error, nulls, timeout

    unknown, not_object, bad_args, tool_error, nulls, timeout = asyncio.run(run())
    assert unknown == {"ok": False, "tool": "fly", "error": "unknown_tool"}
    assert not_object["error"] == "arguments_must_be_object"
    assert bad_args["error"].startswith("invalid_arguments")
    assert tool_error == {"ok": False, "tool": "trigger_antenna_emote", "error": "invalid_emote_preset"}
    assert nulls["ok"] and nulls["result"]["state"] == "not_started"   # a strict schema's null is "not given"
    assert timeout == {"ok": False, "tool": "camera", "error": "tool_timeout"}


def test_argument_validation_of_the_new_tools():
    async def run():
        registry = ToolRegistry(Robot(), backend()[0])
        return [await registry.execute(name, args) for name, args in (
            ("verify_medication", {"expected_medication": "  "}),
            ("get_medication_schedule", {"time_scope": "tomorrow"}),
            ("start_pill_intake_detection", {"medication_name": "Allegra", "timeout_seconds": 2}),
            ("record_medication_taken", {"medication_name": None, "session_id": None, "medication_id": None,
                                         "patient_confirmed": True}),
            ("notify_family", {"reason": "mood", "severity": "urgent", "message": "x", "urgency": None}),
            ("notify_family", {"reason": None, "severity": None, "message": "", "urgency": None}))]

    errors = [answer["error"] for answer in asyncio.run(run())]
    assert errors == ["expected_medication_required", "invalid_time_scope", "timeout_seconds_out_of_range",
                      "medication_name_or_id_required", "invalid_notification_severity",
                      "notification_message_required"]


def test_camera_and_emote_go_to_the_robot_and_identify_reuses_a_fresh_frame(now):
    async def run():
        robot = Robot()
        b, _ = backend(ocr=FakeOCR({"medications": [{"med_name": "allegra"}]}))
        registry = ToolRegistry(robot, b, clock=lambda: 100.0)
        shot = await registry.execute("camera", {})
        emote = await registry.execute("trigger_antenna_emote", {"preset": "curious_tilt"})
        found = await registry.execute("identify_medicine", {"expected_name": "Allegra"})
        return robot, shot, emote, found

    robot, shot, emote, found = asyncio.run(run())
    assert shot["ok"] and shot["result"]["frame_id"] and shot["result"]["bytes"] == 7
    assert emote["result"] == {"status": "queued", "preset": "curious_tilt", "pending_motion_jobs": 1}
    assert robot.frames == 1   # the frame was fresh: identify_medicine took no new one
    result = found["result"]
    assert result["result"] == "match" and result["medication_id"] == "med-10"
    assert result["consistent_with_expected"] is True and result["prescription_verified"] is False
    assert result["frame_id"] == shot["result"]["frame_id"]


def test_identify_medicine_is_uncertain_without_a_readable_label(now):
    async def run():
        b, _ = backend(ocr=FakeOCR({"med_name": "N/A", "medications": []}))
        nothing = await b.identify_medicine(b"jpeg", None)
        b, _ = backend(ocr=FakeOCR({"medications": [{"med_name": "Metformin"}]}))
        other = await b.identify_medicine(b"jpeg", "Allegra")

        class Failure(Exception):
            code = "ocr_incomplete_schedule"
        b, _ = backend(ocr=FakeOCR(Failure()))
        failed = await b.identify_medicine(b"jpeg", None)
        return nothing, other, failed

    nothing, other, failed = asyncio.run(run())
    assert nothing["result"] == "no_label_read" and nothing["medication_id"] is None
    assert other["result"] == "uncertain" and other["consistent_with_expected"] is False
    assert other["names_read"] == ["Metformin"]
    assert failed == {"result": "uncertain", "reason": "ocr_incomplete_schedule", "names_read": [],
                      "medication_id": None, "consistent_with_expected": None}


# ── verify_medication ──

def test_names_match_by_label_text_only():
    assert names_match("Allegra", "ALLEGRA 60 mg") and names_match("allegra 60mg", "Allegra")
    assert names_match("普拿疼", "普拿疼 500mg") and not names_match("Allegra", "Metformin")
    assert not names_match("A", "Allegra")   # too short to be evidence


def test_verify_medication_matches_the_expected_medicine_from_label_text(now):
    async def run():
        robot = Robot()
        b, _ = backend(ocr=FakeOCR({"medications": [{"med_name": "Allegra 60mg"}]}))
        registry = ToolRegistry(robot, b, clock=lambda: 100.0)
        return await registry.execute("verify_medication", {"expected_medication": "Allegra"})

    answer = asyncio.run(run())["result"]
    assert answer["result"] == "match" and answer["reason"] == "label_names_match"
    assert answer["evidence"] == {"type": "label_text", "names_read": ["Allegra 60mg"], "colour_or_shape_used": False}
    assert answer["medication_id"] == "dose-2" and answer["dose_status"] == "pending_allowed"
    assert answer["prescribed"] is True and answer["medication_name"] == "Allegra" and answer["frame_id"]


def test_verify_medication_says_no_match_or_uncertain(now):
    async def run():
        b, _ = backend(ocr=FakeOCR({"medications": [{"med_name": "Metformin 500mg"}]}))
        no_match = await b.verify_medication(b"jpeg", "Allegra")
        b, _ = backend(ocr=FakeOCR({"medications": [], "med_name": "N/A"}))
        bare_pill = await b.verify_medication(b"jpeg", "Allegra")
        b, _ = backend(ocr=None)
        monkey = tool_registry.ServerCareBackend._read_labels

        async def unavailable(self, jpeg):
            return [], "ocr_unavailable"
        tool_registry.ServerCareBackend._read_labels = unavailable
        try:
            off = await b.verify_medication(b"jpeg", "Allegra")
        finally:
            tool_registry.ServerCareBackend._read_labels = monkey
        return no_match, bare_pill, off

    no_match, bare_pill, off = asyncio.run(run())
    assert no_match["result"] == "no_match" and no_match["evidence"]["names_read"] == ["Metformin 500mg"]
    assert bare_pill["result"] == "uncertain" and bare_pill["reason"] == "no_label_text_read"
    assert off["result"] == "uncertain" and off["reason"] == "ocr_unavailable"
    for answer in (no_match, bare_pill, off):
        assert answer["evidence"]["colour_or_shape_used"] is False and answer["medication_id"] is None


# ── medication ──

def test_check_medication_due_names_the_slots_dose_judged_by_overdose_protection(now):
    b, _ = backend()
    answer = asyncio.run(b.check_medication_due())
    assert answer["status"] == "pending_allowed" and answer["medication_id"] == "dose-2"
    assert answer["medication_name"] == "Allegra" and answer["last_taken_time"] is None
    assert [d["status"] for d in answer["doses"]] == ["pending_allowed"]   # 20:15: the 20:00 slot only


def test_get_medication_schedule_now_and_today(now):
    b, _ = backend()
    now_ = asyncio.run(b.get_medication_schedule("now"))
    assert now_["time_scope"] == "now" and now_["anything_due"] is True
    assert [(d["medication_id"], d["medication_name"]) for d in now_["due_now"]] == [("dose-2", "Allegra")]
    assert now_["status"] == "pending_allowed" and now_["medication_name"] == "Allegra"
    today = asyncio.run(b.get_medication_schedule("today"))
    assert today["time_scope"] == "today" and today["date"] == "2026-10-12"
    assert [(d["medication_id"], d["status"]) for d in today["doses"]] == [("dose-1", "taken"),
                                                                         ("dose-2", "pending_allowed")]
    assert today["doses"][0]["taken_time"] == "2026-10-12T08:10:00+08:00"
    default = asyncio.run(ToolRegistry(Robot(), b).execute("get_medication_schedule", {"time_scope": None}))
    assert default["result"]["time_scope"] == "today"


def test_verify_prescription_takes_dose_and_medicine_ids(now):
    b, _ = backend()
    dose = asyncio.run(b.verify_prescription("dose-2"))
    med = asyncio.run(b.verify_prescription("med-10"))
    missing = asyncio.run(b.verify_prescription("dose-99"))
    assert dose["verified"] and dose["med_name"] == "Allegra" and dose["status"] == "pending"
    assert med == {"medication_id": "med-10", "verified": True, "med_name": "Allegra", "reason": None}
    assert missing == {"medication_id": "dose-99", "verified": False, "reason": "not_this_patients"}
    with pytest.raises(ToolError, match="invalid_medication_id"):
        asyncio.run(b.verify_prescription("pill"))


# ── pill intake ──

def test_intake_detection_watches_the_patients_camera_session_for_a_medicine(now, camera):
    clock = [100.0]
    b, _ = backend(monotonic=lambda: clock[0])
    started = asyncio.run(b.start_pill_intake_detection(30, "Allegra"))
    session_id = started["session_id"]
    assert started["state"] == "watching" and started["result"] == "still_monitoring"
    assert started["medication_name"] == "Allegra" and started["medication_id"] == "dose-2"
    assert started["medication_known"] is True and started["camera_fps"] == 2.0 and started["degraded"] is True

    watching = asyncio.run(b.get_pill_intake_result(session_id))
    assert watching["result"] == "still_monitoring" and watching["state"] == "watching"
    assert watching["medication_id"] == "dose-2" and watching["evidence"] == {}
    with pytest.raises(ToolError, match="unknown_session"):
        asyncio.run(b.get_pill_intake_result("other"))

    camera.latest_detector = {"stage": "AT_MOUTH", "decision": "none", "hand_near_mouth": True, "mouth_open": False,
                              "evidence": {"approach": {"avg_approach_norm": 0.05}}}
    camera.candidate = {"confidence": 0.42, "decision": "uncertain",
                        "evidence": {"approach": {"avg_approach_norm": 0.06}, "contact": {"peak_mouth_contact": 0.9},
                                     "dwell": {"dwell": 4}, "withdrawal": {"withdrew_enough": True},
                                     "trajectory": {"trajectory_contribution": 0.12}, "event_style": "pinch"}}
    uncertain = asyncio.run(b.get_pill_intake_result(session_id))
    assert uncertain["result"] == "uncertain" and uncertain["state"] == "candidate"
    assert uncertain["confidence"] == 0.42 and uncertain["recorded"] is False
    assert set(uncertain["evidence"]) == {"approach", "contact", "dwell", "withdrawal", "trajectory", "event_style"}
    assert uncertain["detector"] == {"stage": "AT_MOUTH", "decision": "none", "hand_near_mouth": True,
                                     "mouth_open": False}

    camera.extra_events.append({"decision": "confirmed", "confidence": 0.81, "evidence": {"dwell": {"dwell": 6}}})
    taken = asyncio.run(b.get_pill_intake_result(session_id))
    assert taken["result"] == "taken" and taken["state"] == "hand_to_mouth_seen" and taken["confidence"] == 0.81
    assert taken["evidence"] == {"dwell": {"dwell": 6}} and taken["recorded"] is False

    camera.recorded = {"detector_score": 0.9}
    recorded = asyncio.run(b.get_pill_intake_result(session_id))
    assert recorded["result"] == "taken" and recorded["state"] == "recorded" and recorded["recorded"] is True

    camera.recorded, camera.extra_events, camera.candidate = None, [], None
    clock[0] = 140.0
    not_taken = asyncio.run(b.get_pill_intake_result(session_id))
    assert not_taken["result"] == "not_taken" and not_taken["state"] == "timeout"
    assert not_taken["confidence"] == 0.0 and not_taken["seconds_watched"] == 40.0


def test_intake_detection_without_a_known_medicine_or_a_camera_session(now, monkeypatch, camera):
    b, _ = backend()
    started = asyncio.run(b.start_pill_intake_detection(30, "Vitamin C"))
    assert started["medication_known"] is False and started["medication_id"] is None
    monkeypatch.setattr(monitor_service, "registry", types.SimpleNamespace(sessions={}, by_user={}))
    assert asyncio.run(b.get_pill_intake_result(started["session_id"]))["state"] == "no_camera_session"
    b, _ = backend()
    assert asyncio.run(b.get_pill_intake_result()) == {"result": "not_taken", "state": "not_started",
                                                        "confidence": None, "recorded": False, "evidence": {}}
    with pytest.raises(ToolError, match="no_camera_session"):
        asyncio.run(b.start_pill_intake_detection(30))


def test_detector_evidence_is_the_json_safe_subset_of_a_detector_result():
    result = {"avg_approach_norm": 0.0512345, "approach_std_norm": 0.02, "peak_mouth_contact": 0.95,
              "in_mouth_zone_occurred": True, "dwell": 5, "withdrew_enough": True, "partial_withdrawal_seen": False,
              "trajectory_contribution": 0.12, "dwell_contribution": 0.1, "event_style": "pinch",
              "mouth_open_allowed": None, "unrelated": object()}
    evidence = monitor_service.detector_evidence(result)
    assert evidence == {"approach": {"avg_approach_norm": 0.0512, "approach_std_norm": 0.02},
                        "contact": {"peak_mouth_contact": 0.95, "in_mouth_zone_occurred": True},
                        "dwell": {"dwell": 5.0}, "withdrawal": {"withdrew_enough": True, "partial_withdrawal_seen": False},
                        "trajectory": {"trajectory_contribution": 0.12, "dwell_contribution": 0.1},
                        "event_style": "pinch"}
    assert monitor_service.detector_evidence(None) == {} and monitor_service.detector_evidence({}) == {}


# ── record_medication_taken ──

def test_record_medication_taken_stores_the_detectors_result_and_asks_family(now, camera, monkeypatch):
    asked = []

    async def create(conn, *, u_id, task_id, intk_ids, source, evidence, started_at=None):
        asked.append((u_id, task_id, intk_ids, source, evidence, started_at))
        return "req-1"
    monkeypatch.setattr(dose_confirmation, "create", create)
    b, _ = backend(monotonic=lambda: 100.0)
    started = asyncio.run(b.start_pill_intake_detection(30, "Allegra"))
    camera.extra_events.append({"decision": "confirmed", "confidence": 0.81, "evidence": {"dwell": {"dwell": 6}}})
    answer = asyncio.run(b.record_medication_taken(medication_name="Allegra", session_id=started["session_id"]))
    assert answer == {"status": "pending_confirmation", "medication_id": "dose-2", "medication_name": "Allegra",
                      "request_id": "req-1", "family_asked": True, "recorded_at": "2026-10-12T20:15:00+08:00",
                      "session_id": started["session_id"], "detector_result": "taken", "detector_confidence": 0.81,
                      "source": "degraded", "video": "none:no_frames"}   # no buffered frames: no clip, and why
    u_id, task_id, ids, source, evidence, started_at = asked[0]
    assert (u_id, task_id, ids, source, started_at) == (7, None, [2], "degraded", at(20, 15))
    assert evidence["session_id"] == started["session_id"] and evidence["detector_result"] == "taken"
    assert evidence["confidence"] == 0.81 and evidence["detector_evidence"] == {"dwell": {"dwell": 6}}
    assert evidence["recorded_at"] == "2026-10-12T20:15:00+08:00" and evidence["patient_confirmed"] is False
    assert evidence["landmark_fps"] == 2.0 and evidence["degraded"] is True
    assert evidence["video"] == "none:no_frames"   # the request itself says it comes without a video


def test_record_medication_taken_with_the_patients_word_and_a_dose_id(now, monkeypatch):
    asked = []

    async def create(conn, *, u_id, task_id, intk_ids, source, evidence, started_at=None):
        asked.append((intk_ids, source, evidence["patient_confirmed"], evidence["session_id"]))
        return "req-2"
    monkeypatch.setattr(dose_confirmation, "create", create)
    b, _ = backend()
    answer = asyncio.run(b.record_medication_taken("dose-2", True))
    assert answer["status"] == "pending_confirmation" and answer["source"] == "patient_claim"
    assert asked == [([2], "patient_claim", True, None)]
    by_medicine = asyncio.run(b.record_medication_taken("med-10", True))
    assert by_medicine["medication_id"] == "dose-2"
    with pytest.raises(ToolError, match="medicine_not_found_today"):
        asyncio.run(b.record_medication_taken(medication_name="Vitamin C", patient_confirmed=True))
    with pytest.raises(ToolError, match="not_this_patients_dose"):
        asyncio.run(b.record_medication_taken("dose-99", True))


def test_without_the_patients_word_the_detector_must_be_sure(now, camera):
    async def run():
        b, _ = backend(monotonic=lambda: 100.0)
        registry = ToolRegistry(Robot(), b)
        nothing = await registry.execute("record_medication_taken", {"medication_id": "dose-2",
                                                                     "patient_confirmed": False})
        started = await registry.execute("start_pill_intake_detection", {"medication_name": "Allegra",
                                                                         "timeout_seconds": None})
        camera.candidate = {"confidence": 0.42, "decision": "uncertain"}
        weak = await registry.execute("record_medication_taken", {"medication_name": "Allegra",
                                                                  "session_id": started["result"]["session_id"]})
        return nothing, weak

    nothing, weak = asyncio.run(run())
    assert nothing == {"ok": False, "tool": "record_medication_taken",
                       "error": "dose_not_confirmed_by_patient_or_detector"}
    assert weak["error"] == "dose_not_confirmed_by_patient_or_detector"


def test_a_refused_or_already_taken_dose_is_reported_never_recorded(now, monkeypatch):
    class Refused(schedule.DoseRefused):
        def __init__(self):
            Exception.__init__(self, "dose_too_soon")

    alerts = []

    async def create(conn, **kw):
        raise Refused()

    async def alert_after(refused):
        alerts.append(refused)
    monkeypatch.setattr(dose_confirmation, "create", create)
    monkeypatch.setattr(dose_safety, "alert_after", alert_after)
    monkeypatch.setattr(dose_safety, "refusal_body", lambda r: {"detail": "dose_too_soon", "reply": "太快了"})
    b, _ = backend()
    answer = asyncio.run(b.record_medication_taken("dose-2", True))
    assert answer == {"status": "refused", "medication_id": "dose-2", "medication_name": "Allegra",
                      "reason": "dose_too_soon", "reply": "太快了", "family_asked": False, "family_notified": "queued"}
    assert len(alerts) == 1

    async def nothing_open(conn, **kw):
        raise ValueError("No pending or missed dose to confirm")
    monkeypatch.setattr(dose_confirmation, "create", nothing_open)
    answer = asyncio.run(b.record_medication_taken("dose-1", True))
    assert answer["status"] == "already_taken" and answer["family_asked"] is False


# ── emotion ──

ROBOT_REPORT = {"available": True, "active": False,
                "fused": {"top": "sad", "confidence": 0.61, "probabilities": {"sad": 0.61, "neutral": 0.39},
                          "used": {"face": 0.5, "speech": 0.3, "text": 0.2}, "age_seconds": 12.0},
                "branches": {"face": True, "speech": True, "text": True},
                "fer": {"status": "scored"}, "ser": {"status": "ok", "top": "sad", "confidence": 0.7},
                "ter": {"status": "stale"}}


def test_current_emotion_fuses_the_robots_report_with_the_servers_face(camera):
    camera.emotion = {"detected": True, "emotion_type": "Sad", "emotion_score": 0.7,
                      "probabilities": {"sad": 0.7, "happy": 0.3}, "error": None, "source": "server"}

    async def run():
        with_robot = await ToolRegistry(Robot(ROBOT_REPORT), backend(monotonic=lambda: 50.0)[0]).execute(
            "get_current_emotion", {})
        without = await ToolRegistry(Robot(ToolError("robot_did_not_answer")),
                                     backend(monotonic=lambda: 50.0)[0]).execute("get_current_emotion", {})
        return with_robot["result"], without["result"]

    fused, face_only = asyncio.run(run())
    assert fused["available"] and fused["source"] == "robot_multimodal"
    assert fused["fused"]["top"] == "sad" and fused["fused"]["confidence"] == 0.61
    assert fused["modalities"] == ["face", "speech", "text"]
    assert fused["ser"] == {"status": "ok", "top": "sad", "confidence": 0.7} and fused["ter"] == {"status": "stale"}
    assert fused["fer"]["server"]["top"] == "sad" and fused["fer"]["robot"] == {"status": "scored"}
    assert face_only["source"] == "server_face" and face_only["modalities"] == ["face"]
    assert face_only["fused"]["used"] == {"face": 1.0} and face_only["ser"] == {"status": "robot_unavailable"}


def test_current_emotion_without_a_face_or_a_robot_is_unavailable(camera):
    b, _ = backend(monotonic=lambda: 50.0)
    answer = asyncio.run(b.get_current_emotion(None))
    assert answer["available"] is False and answer["fused"] is None and answer["modalities"] == []
    assert answer["fer"]["server"]["reason"] == "no_uncovered_face_lately"
    camera.emotion_samples = [(40.0, {"happy": 0.8, "sad": 0.2}, False, "server")]
    mean = asyncio.run(b.get_current_emotion({"available": False, "error": "no_emotion_engine"}))
    assert mean["source"] == "server_face" and mean["fused"]["top"] == "happy"
    assert mean["fer"]["server"]["basis"] == "mean_last_30s"


# ── memory, family, emergency ──

def test_remember_refuses_care_notes_and_needs_memory_consent(monkeypatch):
    b, _ = backend()
    assert asyncio.run(b.remember("血壓藥吃完了", "care_note")) == {"stored": False,
                                                                "reason": "category_not_stored"}

    async def get_state(u_id):
        return {}
    monkeypatch.setattr(tool_registry.consent_service, "get_state", get_state)
    monkeypatch.setattr(memory, "consent_current", lambda state: False)
    assert asyncio.run(b.remember("喜歡喝茶", "preference")) == {"stored": False, "reason": "memory_consent_off"}


def test_remember_stores_a_valid_fact_from_the_chat(consent_on, monkeypatch):
    stored = []

    async def store_facts(conn, u_id, conversation_id, facts):
        stored.append((u_id, conversation_id, facts))
        return 1
    monkeypatch.setattr(memory, "store_facts", store_facts)
    monkeypatch.setattr(memory, "validate_fact",
                        lambda raw, *, today, source: {"kind": raw["kind"], "subject": "茶", "text": raw["text"],
                                                       "event_date": None} if source == "chat" else None)
    b, _ = backend()
    assert asyncio.run(b.remember("喜歡喝茶", "preference")) == {"stored": True, "kind": "like", "subject": "茶",
                                                                "reason": None}
    assert stored == [(7, "c-1", [{"kind": "like", "subject": "茶", "text": "喜歡喝茶", "event_date": None}])]
    monkeypatch.setattr(memory, "validate_fact", lambda raw, *, today, source: None)
    assert asyncio.run(b.remember("x", "routine")) == {"stored": False, "reason": "invalid_fact"}


def test_get_patient_memory_searches_the_current_facts(consent_on, monkeypatch):
    async def current_facts(conn, u_id):
        return [{"kind": "like", "subject": "茶", "text": "喜歡喝烏龍茶", "event_date": None},
                {"kind": "person", "subject": "小明", "text": "孫子小明住台中", "event_date": None},
                {"kind": "event", "subject": "生日", "text": "孫子生日", "event_date": datetime.date(2026, 11, 2)}]
    monkeypatch.setattr(memory, "current_facts", current_facts)
    b, _ = backend()
    answer = asyncio.run(b.get_patient_memory("孫子"))
    assert answer["total_facts"] == 3 and [f["subject"] for f in answer["facts"]] == ["小明", "生日"]
    assert answer["facts"][1]["event_date"] == "2026-11-02"


def test_notify_family_goes_through_the_outbox_to_the_databases_contacts(monkeypatch):
    queued = []

    async def enqueue_to_contacts(conn, u_id, *, kind, priority, messages, dedupe_prefix, contact_flag):
        queued.append((u_id, kind, priority, messages, contact_flag))
        return 2
    monkeypatch.setattr(outbox, "enqueue_to_contacts", enqueue_to_contacts)

    async def run():
        registry = ToolRegistry(Robot(), backend()[0])
        new = await registry.execute("notify_family", {"reason": "mood", "severity": "high",
                                                       "message": "阿嬤今天心情低落", "urgency": None})
        old = await registry.execute("notify_family", {"message": "阿嬤今天心情不錯", "urgency": "low"})
        return new["result"], old["result"]

    new, old = asyncio.run(run())
    assert new == {"queued": True, "contacts": 2, "severity": "high", "urgency": "high", "reason": "mood",
                   "recipients": "verified_family_contacts", "failure": None}
    assert old["severity"] == "low" and old["reason"] is None
    assert queued == [(7, "live_agent_note", 0, [{"type": "text", "text": "🤖 Reachy（mood）：阿嬤今天心情低落"}], None),
                      (7, "live_agent_note", 1, [{"type": "text", "text": "🤖 Reachy：阿嬤今天心情不錯"}], None)]


def test_emergency_alert_raises_the_safety_alert_and_the_attention_gesture(monkeypatch):
    alerts = []

    async def alert_family(conn, u_id, conversation_id, text, dedupe):
        alerts.append((u_id, conversation_id, text, dedupe))
        return len(alerts) == 1
    monkeypatch.setattr(conversation, "alert_family", alert_family)

    async def run():
        robot = Robot()
        registry = ToolRegistry(robot, backend()[0])
        first = await registry.execute("emergency_alert", {"severity": "critical", "reason": "跌倒了"})
        second = await registry.execute("emergency_alert", {"severity": "high", "reason": "又跌倒"})
        return robot, first, second

    robot, first, second = asyncio.run(run())
    assert robot.emotes == ["alert_attention", "alert_attention"]
    assert first["result"]["alert"] == {"accepted": True, "severity": "critical", "alerted": True, "reason": None}
    assert first["result"]["gesture"]["status"] == "queued"
    assert second["result"]["alert"]["alerted"] is False
    assert alerts[0][:2] == (7, "c-1") and "跌倒了" in alerts[0][2] and "阿嬤" in alerts[0][2]
    assert alerts[0][3].startswith("tool:")


# ── the review's fixes ──

def test_the_watch_sees_a_new_event_even_when_the_sessions_list_is_at_its_cap(now, camera, monkeypatch):
    """monitor_service trims extra_events to EXTRA_EVENT_CAP from the front: events are known by id, not position."""
    camera.extra_events = [{"event_id": f"old-{i}", "decision": "uncertain", "confidence": 0.3} for i in range(20)]
    b, _ = backend(monotonic=lambda: 100.0)
    started = asyncio.run(b.start_pill_intake_detection(30, "Allegra"))
    assert asyncio.run(b.get_pill_intake_result(started["session_id"]))["result"] == "still_monitoring"
    camera.extra_events.append({"event_id": "new-1", "decision": "confirmed", "confidence": 0.86})
    del camera.extra_events[:-20]   # as monitor_service does
    assert len(camera.extra_events) == 20
    taken = asyncio.run(b.get_pill_intake_result(started["session_id"]))
    assert taken["result"] == "taken" and taken["confidence"] == 0.86

    asked = []

    async def create(conn, *, u_id, task_id, intk_ids, source, evidence, started_at=None):
        asked.append((intk_ids, source, started_at))
        return "req-9"
    monkeypatch.setattr(dose_confirmation, "create", create)
    answer = asyncio.run(b.record_medication_taken(medication_name="Allegra", session_id=started["session_id"]))
    assert answer["status"] == "pending_confirmation" and asked == [([2], "degraded", at(20, 15))]
    # the watch is spent: a second record needs the patient's word, and its started_at is no longer the watch's
    with pytest.raises(ToolError, match="unknown_session"):
        asyncio.run(b.record_medication_taken(medication_name="Allegra", session_id=started["session_id"]))


def test_a_stale_watch_never_authorises_a_record_without_its_session_id(now, camera, monkeypatch):
    asked = []

    async def create(conn, *, u_id, task_id, intk_ids, source, evidence, started_at=None):
        asked.append((source, started_at, evidence["session_id"], evidence["detector_result"]))
        return "req-3"
    monkeypatch.setattr(dose_confirmation, "create", create)
    b, _ = backend(monotonic=lambda: 100.0)
    asyncio.run(b.start_pill_intake_detection(30, "Allegra"))
    camera.extra_events.append({"event_id": "e1", "decision": "confirmed", "confidence": 0.9})
    with pytest.raises(ToolError, match="dose_not_confirmed"):
        asyncio.run(b.record_medication_taken("dose-2", False))         # no session named: the watch does not count
    answer = asyncio.run(b.record_medication_taken("dose-2", True))     # the patient's word, judged now
    assert answer["source"] == "patient_claim" and asked == [("patient_claim", None, None, None)]
    with pytest.raises(ToolError, match="unknown_session"):
        asyncio.run(b.record_medication_taken("dose-2", True, session_id="nope"))


def test_family_notes_are_capped_and_never_repeated_in_a_conversation(monkeypatch):
    queued = []

    async def enqueue_to_contacts(conn, u_id, *, kind, priority, messages, dedupe_prefix, contact_flag):
        queued.append(dedupe_prefix)
        return 1
    monkeypatch.setattr(outbox, "enqueue_to_contacts", enqueue_to_contacts)
    b, _ = backend()
    first = asyncio.run(b.notify_family("今天心情不錯", "normal"))
    again = asyncio.run(b.notify_family("今天心情不錯", "high"))
    assert first["queued"] and again == {**again, "queued": False, "failure": "already_sent_this_conversation"}
    asyncio.run(b.notify_family("第二則", "normal"))
    asyncio.run(b.notify_family("第三則", "normal"))
    fourth = asyncio.run(b.notify_family("第四則", "emergency"))
    assert fourth["queued"] is False and fourth["failure"] == "limit_reached_this_conversation"
    assert queued == ["live_agent_note:c-1:0", "live_agent_note:c-1:1", "live_agent_note:c-1:2"]


def test_the_servers_face_result_is_read_from_the_emotion_report(camera):
    """MonitorSession.emotion is the EmotionService report, with the label map under "probabilities"."""
    camera.emotion = {"detected": True, "emotion_type": "Sad", "emotion_score": 0.7,
                      "probabilities": {"sad": 0.7, "happy": 0.3}, "error": None, "source": "server"}
    b, _ = backend(monotonic=lambda: 50.0)
    answer = asyncio.run(b.get_current_emotion(None))
    assert answer["available"] and answer["fused"]["top"] == "sad" and answer["fer"]["server"]["basis"] == "latest_frame"


class VisionOCR(FakeOCR):
    """An OCR service with the Gemini call describe_view uses (OCRService._call_gemini)."""

    def __init__(self, answer):
        super().__init__(answer)
        self.prompts = []

    def _call_gemini(self, prompt, img):
        self.prompts.append((prompt, img.shape))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _jpeg():
    import cv2
    import numpy as np
    ok, buf = cv2.imencode(".jpg", np.full((48, 64, 3), 120, np.uint8))
    assert ok
    return buf.tobytes()


def test_describe_view_answers_from_the_vision_model(now):
    ocr = VisionOCR({"description": "一位穿藍色上衣的人坐在桌前。", "answer": "藍色", "people_visible": 1})
    b, _ = backend(ocr=ocr)

    class Robot2(Robot):
        async def capture_jpeg(self):
            self.frames += 1
            return _jpeg(), {"width": 64, "height": 48, "captured_at": 1.0}

    async def run():
        return await ToolRegistry(Robot2(), b, clock=lambda: 100.0).execute("describe_view",
                                                                            {"question": "我穿什麼顏色的衣服？"})
    answer = asyncio.run(run())["result"]
    assert answer == {"available": True, "question": "我穿什麼顏色的衣服？", "description": "一位穿藍色上衣的人坐在桌前。",
                      "answer": "藍色", "people_visible": 1, "frame_id": answer["frame_id"]}
    assert "我穿什麼顏色的衣服？" in ocr.prompts[0][0] and ocr.prompts[0][1] == (48, 64, 3)


def test_describe_view_and_the_label_readers_refuse_when_cloud_vision_is_off(now, monkeypatch):
    monkeypatch.setattr(tool_registry.config, "LIVE_VISION_CLOUD", False)
    ocr = VisionOCR({"description": "x", "answer": "y", "people_visible": 0})
    b, _ = backend(ocr=ocr)
    assert asyncio.run(b.describe_view(b"jpeg", "誰在這裡？")) == {"available": False, "reason": "vision_off",
                                                               "description": None, "answer": None}
    assert asyncio.run(b.verify_medication(b"jpeg", "Allegra"))["reason"] == "vision_off"
    assert asyncio.run(b.identify_medicine(b"jpeg", None))["reason"] == "vision_off"
    assert ocr.prompts == [] and ocr.images == []


# ── the time, web search and reminders (14 Oct 2026) ──

def test_get_current_time_reads_the_clock_at_the_call_here_or_in_a_place(monkeypatch):
    registry = ToolRegistry(Robot(), backend()[0])
    home = asyncio.run(registry.execute("get_current_time", {"place": None}))
    assert home["ok"] and home["result"]["status"] == "home" and home["result"]["timezone"] == "Asia/Taipei"
    assert home["result"]["spoken"] and home["result"]["weekday"].startswith("星期")
    tokyo = asyncio.run(registry.execute("get_current_time", {"place": "東京現在幾點"}))
    assert tokyo["result"]["status"] == "ok" and tokyo["result"]["hours_from_home"] == 1.0
    assert asyncio.run(registry.execute("get_current_time", {"place": "火星"}))["result"]["status"] == "not_found"


def test_search_web_runs_the_search_with_the_live_connections_key(monkeypatch):
    from app.services import ai_settings, web_search
    calls = []
    monkeypatch.setattr(ai_settings, "live_target", lambda snapshot=None: ai_settings.LiveTarget("sk-live", "gpt-live-1", "marin"))
    monkeypatch.setattr(web_search, "search", lambda query, language, api_key: calls.append((query, language, api_key)) or {
        "status": "ok", "query": query, "answer": "找到了。", "sources": [{"title": str(i), "url": f"u{i}"} for i in range(6)],
        "queries": [query], "searched": True})
    registry = ToolRegistry(Robot(), backend()[0])
    answer = asyncio.run(registry.execute("search_web", {"query": " Reachy Mini 最新消息 ", "max_results": 2}))
    assert calls == [("Reachy Mini 最新消息", "zh-TW", "sk-live")]
    assert answer["ok"] and answer["result"]["answer"] == "找到了。" and len(answer["result"]["sources"]) == 2
    assert asyncio.run(registry.execute("search_web", {"query": "", "max_results": None})) == {
        "ok": False, "tool": "search_web", "error": "query_required"}
    monkeypatch.setattr(ai_settings, "live_target", lambda snapshot=None: None)
    off = asyncio.run(registry.execute("search_web", {"query": "x", "max_results": None}))
    assert off["ok"] and off["result"] == {"status": "unavailable", "query": "x", "reason": "no_live_connection"}


def test_set_and_cancel_reminder_go_to_the_reminder_service_with_the_patient_and_language(monkeypatch):
    from app.services import reminders
    created, cancelled = [], []

    async def create(u_id, **kw):
        created.append((u_id, kw))
        return {"status": "scheduled", "reminder_id": "r-1", "text": "時間到了，請吃第一種藥，Adapalene。"}

    async def cancel(u_id, reminder_id=None):
        cancelled.append((u_id, reminder_id))
        return {"status": "cancelled", "count": 1}

    monkeypatch.setattr(reminders, "create", create)
    monkeypatch.setattr(reminders, "cancel", cancel)
    registry = ToolRegistry(Robot(), backend()[0])
    answer = asyncio.run(registry.execute("set_reminder", {"what": None, "medication_name": None, "position": 1,
                                                           "in_minutes": 1, "at_time": None, "day": None}))
    assert answer["ok"] and answer["result"]["text"] == "時間到了，請吃第一種藥，Adapalene。"
    assert created == [(7, {"what": None, "medication_name": None, "position": 1, "in_minutes": 1, "at_time": None,
                            "day": None, "language": "zh-TW", "conversation_id": "c-1"})]
    assert asyncio.run(registry.execute("cancel_reminder", {"reminder_id": None}))["result"]["count"] == 1
    assert cancelled == [(7, None)]
