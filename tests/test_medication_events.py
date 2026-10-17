"""The GPT-Live agent's medication workflow tells family about every event (services/tool_registry.py with
services/dose_event.py): a label that did not match or could not be read, a watch that ended uncertain or empty
(after a grace, unless the record comes first and sends the clip with its request), a refusal, a chat that ended
mid-watch; the confirmation request's evidence carries the label check, the mood and where the clip went. The
device route the robot files its own watch outcomes to, and the spoken prompt clip route."""

import asyncio
import sys
import types

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.services import dose_confirmation, dose_event, dose_video, med_clips, tool_registry   # noqa: E402
from tests import test_device_api   # noqa: E402
from tests.test_device_api import TASK_ID   # noqa: E402
from tests import test_tool_registry   # noqa: E402
from tests.test_tool_registry import FakeOCR, backend   # noqa: E402

env = test_device_api.env   # the harnesses' fixtures, registered here too
camera, now, vision_on = test_tool_registry.camera, test_tool_registry.now, test_tool_registry.vision_on


@pytest.fixture
def reports(monkeypatch):
    """dose_event.schedule recorded, never run; a task object stands in for the background report."""
    scheduled = []

    def schedule(u_id, *, delay=0.0, **fields):
        task = asyncio.get_running_loop().create_future()
        scheduled.append({"u_id": u_id, "delay": delay, **fields, "task": task})
        return task

    monkeypatch.setattr(dose_event, "schedule", schedule)
    monkeypatch.setattr(tool_registry.dose_event, "schedule", schedule)
    return scheduled


@pytest.fixture
def captures(monkeypatch):
    """A clip can come (dose_video.clip_outlook, before the request: consent on, frames buffered); capture() is
    recorded with the patient, the doses and the detected moment its frames were chosen for."""
    taken, moments = [], []

    async def clip_outlook(u_id, *, event_started_at=None):
        moments.append(event_started_at)
        return "with_request", [(0.0, b"jpeg")] * dose_video.MIN_FRAMES
    monkeypatch.setattr(dose_video, "clip_outlook", clip_outlook)
    monkeypatch.setattr(dose_video, "capture", lambda u_id, intk_ids, *, event_started_at=None, frames=None:
                        taken.append((u_id, intk_ids, moments[-1])) or True)
    return taken


def test_a_label_that_does_not_match_or_cannot_be_read_goes_to_family_once_per_result(now, reports, camera,
                                                                                       monkeypatch):
    b, _ = backend(monotonic=lambda: 100.0, ocr=FakeOCR({"medications": [{"med_name": "Metformin"}]}))
    first = asyncio.run(b.verify_medication(b"\xff\xd8", "Allegra"))
    second = asyncio.run(b.verify_medication(b"\xff\xd8", "Allegra"))   # looked again: no second message
    assert first["result"] == "no_match" and first["family_notified"] == "queued" and "family_notified" not in second
    assert len(reports) == 1 and reports[0]["kind"] == "verification" and reports[0]["needs_review"] is True
    assert reports[0]["intk_ids"] == [2] and reports[0]["medication_name"] == "Allegra"
    assert reports[0]["verification"] == {"result": "no_match", "expected_medication": "Allegra", "names_read": ["Metformin"]}
    assert reports[0]["dedupe"].startswith("verify:c-1:allegra:no_match:") and reports[0]["intake"] == "not_checked"
    b._ocr = FakeOCR({"medications": []})
    unread = asyncio.run(b.verify_medication(b"\xff\xd8", "Allegra"))
    assert unread["result"] == "uncertain" and len(reports) == 2 and reports[1]["verification"]["result"] == "uncertain"
    b._ocr = FakeOCR({"medications": [{"med_name": "Allegra"}]})
    matched = asyncio.run(b.verify_medication(b"\xff\xd8", "Metformin"))   # another medicine: a different label
    assert matched["result"] == "no_match" and len(reports) == 3 and reports[2]["intk_ids"] == []
    assert asyncio.run(b.verify_medication(b"\xff\xd8", "Allegra"))["result"] == "match" and len(reports) == 3


def test_a_watch_that_ends_uncertain_or_empty_is_reported_once_after_a_grace(now, reports, camera):
    clock = [100.0]
    b, _ = backend(monotonic=lambda: clock[0], ocr=FakeOCR({"medications": [{"med_name": "Nothing"}]}))
    asyncio.run(b.verify_medication(b"\xff\xd8", "Allegra"))          # no_match, remembered for the dose's events
    started = asyncio.run(b.start_pill_intake_detection(30, "Allegra"))
    camera.candidate = {"confidence": 0.4}
    assert asyncio.run(b.get_pill_intake_result(started["session_id"]))["result"] == "uncertain"
    assert len(reports) == 1                                              # a candidate is still being judged
    camera.candidate = None
    camera.extra_events.append({"event_id": "e1", "decision": "uncertain", "confidence": 0.48, "evidence": {"dwell": 1}})
    uncertain = asyncio.run(b.get_pill_intake_result(started["session_id"]))
    again = asyncio.run(b.get_pill_intake_result(started["session_id"]))
    assert uncertain["result"] == "uncertain" and uncertain["family_notified"] == "queued" and "family_notified" not in again
    assert len(reports) == 2
    report = reports[1]
    assert report["kind"] == "intake" and report["delay"] == tool_registry.WATCH_REPORT_GRACE_SECONDS
    assert report["dedupe"] == f"watch:{started['session_id']}" and report["intk_ids"] == [2]
    assert report["intake"] == "uncertain" and report["confidence"] == 0.48 and report["needs_review"] is True
    assert report["verification"]["result"] == "no_match" and report["medication_name"] == "Allegra"
    b2, _ = backend(monotonic=lambda: clock[0])
    timed = asyncio.run(b2.start_pill_intake_detection(5, "Allegra"))
    clock[0] = 110.0
    empty = asyncio.run(b2.get_pill_intake_result(timed["session_id"]))
    assert empty["result"] == "not_taken" and reports[-1]["intake"] == "not_taken" and reports[-1]["event_started_at"] is None


def test_a_record_right_after_an_uncertain_watch_sends_the_clip_with_the_request_instead(now, reports, camera,
                                                                                         captures, monkeypatch):
    asked = []

    async def create(conn, *, u_id, task_id, intk_ids, source, evidence, started_at=None):
        asked.append(evidence)
        return "req-1"
    monkeypatch.setattr(dose_confirmation, "create", create)
    b, _ = backend(monotonic=lambda: 100.0)
    started = asyncio.run(b.start_pill_intake_detection(30, "Allegra"))
    camera.extra_events.append({"event_id": "e1", "decision": "uncertain", "confidence": 0.5, "evidence": {}})
    asyncio.run(b.get_pill_intake_result(started["session_id"]))
    assert len(reports) == 1 and not reports[0]["task"].cancelled()
    answer = asyncio.run(b.record_medication_taken(medication_name="Allegra", session_id=started["session_id"],
                                                   patient_confirmed=True))
    assert reports[0]["task"].cancelled()                                  # the review message never goes
    assert answer["status"] == "pending_confirmation" and answer["video"] == "with_request"
    assert captures == [(7, [2], None)] and asked[0]["video"] == "with_request" and asked[0]["emotion"] is None


def test_a_record_after_the_review_message_went_says_the_video_was_sent_earlier(now, reports, camera, captures,
                                                                                monkeypatch):
    asked = []

    async def create(conn, *, u_id, task_id, intk_ids, source, evidence, started_at=None):
        asked.append(evidence)
        return "req-1"
    monkeypatch.setattr(dose_confirmation, "create", create)
    b, _ = backend(monotonic=lambda: 100.0)
    started = asyncio.run(b.start_pill_intake_detection(30, "Allegra"))
    camera.extra_events.append({"event_id": "e1", "decision": "uncertain", "confidence": 0.5, "evidence": {}})
    asyncio.run(b.get_pill_intake_result(started["session_id"]))
    reports[0]["task"].set_result({"video": True})                       # the grace passed: it went
    answer = asyncio.run(b.record_medication_taken(medication_name="Allegra", session_id=started["session_id"],
                                                   patient_confirmed=True))
    assert answer["video"] == "sent_earlier" and captures == [] and asked[0]["video"] == "sent_earlier"


def test_a_confirmed_intake_recorded_at_once_captures_the_clip_for_the_request(now, reports, camera, captures,
                                                                              monkeypatch):
    async def create(conn, **kwargs):
        return "req-1"
    monkeypatch.setattr(dose_confirmation, "create", create)
    b, _ = backend(monotonic=lambda: 100.0)
    camera.event_started_at = 95.0
    started = asyncio.run(b.start_pill_intake_detection(30, "Allegra"))
    camera.extra_events.append({"event_id": "e1", "decision": "confirmed", "confidence": 0.82, "evidence": {}})
    assert asyncio.run(b.get_pill_intake_result(started["session_id"]))["result"] == "taken"
    assert reports == []                                                  # a confirmed intake is the record's
    answer = asyncio.run(b.record_medication_taken(medication_name="Allegra", session_id=started["session_id"]))
    assert answer["video"] == "with_request" and captures == [(7, [2], 95.0)]
    asyncio.run(b.close())
    assert reports == []                                                  # nothing left open


def test_a_refusal_and_a_chat_that_ends_mid_watch_are_reported(now, reports, camera, monkeypatch):
    from app.services import schedule as schedule_service

    async def refuse(conn, **kwargs):
        raise schedule_service.DoseRefused("dose_too_soon", intk_id=2)

    async def no_alert(refused):
        pass
    monkeypatch.setattr(dose_confirmation, "create", refuse)
    monkeypatch.setattr(tool_registry.dose_safety, "alert_after", no_alert)
    monkeypatch.setattr(tool_registry.dose_safety, "refusal_body",
                        lambda refused: {"detail": "dose_too_soon", "reply": "太早了"})
    b, _ = backend(monotonic=lambda: 100.0)
    answer = asyncio.run(b.record_medication_taken("dose-2", True))
    assert answer["status"] == "refused" and answer["family_notified"] == "queued"
    assert reports[-1]["kind"] == "refused" and reports[-1]["notes"][0][0] == "未記錄的原因：太早了"
    assert reports[-1]["dedupe"] == "refused:2:0" and reports[-1]["intk_ids"] == [2]
    b2, _ = backend(monotonic=lambda: 100.0)
    started = asyncio.run(b2.start_pill_intake_detection(30, "Allegra"))
    asyncio.run(b2.close())
    assert reports[-1]["kind"] == "unresolved" and reports[-1]["dedupe"] == f"watch:{started['session_id']}"
    assert reports[-1]["intake"] == "still_monitoring" and b2._watch is None
    asyncio.run(b2.close())                                               # nothing twice
    assert reports[-1]["kind"] == "unresolved" and len([r for r in reports if r["kind"] == "unresolved"]) == 1


# ── the device routes ──

def test_the_robot_files_a_watch_outcome_and_family_get_the_clip(env, reports):
    response = env.robot.post(f"/api/device/tasks/{TASK_ID}/event",
                              json={"intk_id": 101, "outcome": "not_seen",
                                    "evidence": {"said_done": True, "emotion": {"top": "sad", "confidence": 0.7}}})
    assert response.status_code == 200
    assert response.json() == {"event": f"robot:{TASK_ID}:101:not_seen", "queued": True}
    assert len(reports) == 1 and reports[0]["u_id"] == 7 and reports[0]["kind"] == "watch"
    assert reports[0]["intake"] == "not_seen" and reports[0]["emotion"] == {"top": "sad", "confidence": 0.7}
    assert reports[0]["notes"] == [("本人有說「吃完了」", "The patient said they had finished")]
    assert env.robot.post(f"/api/device/tasks/{TASK_ID}/event",
                          json={"intk_id": 999, "outcome": "not_seen"}).status_code == 409
    assert env.robot.post(f"/api/device/tasks/{TASK_ID}/event",
                          json={"intk_id": 101, "outcome": "dance"}).status_code == 422


def test_the_robot_fetches_a_spoken_prompt_or_a_404(env, monkeypatch):
    async def clip(u_id, med_id, position, language):
        return (b"RIFFwav", f"k{med_id}{position}{language}") if med_id == 10 else None
    monkeypatch.setattr(med_clips, "clip", clip)
    response = env.robot.get("/api/device/clips/med/10?position=2&language=zh-TW")
    assert response.status_code == 200 and response.content == b"RIFFwav"
    assert response.headers["etag"] == '"k102zh-TW"' and response.headers["content-type"].startswith("audio/wav")
    assert env.robot.get("/api/device/clips/med/11").status_code == 404
    assert env.robot.get("/api/device/clips/med/10?position=9").status_code == 422


def test_a_robot_whose_medicine_clip_is_current_gets_a_304(env, monkeypatch):
    asked = []

    async def clip(u_id, med_id, position, language):
        asked.append(med_id)
        return b"RIFFwav", "k10"
    monkeypatch.setattr(med_clips, "clip", clip)
    response = env.robot.get("/api/device/clips/med/10", headers={"If-None-Match": '"k10"'})
    assert response.status_code == 304 and response.content == b"" and response.headers["etag"] == '"k10"'
    assert response.headers["cache-control"] == "private"
    # another key (the medicine renamed, the voice changed): the clip itself
    response = env.robot.get("/api/device/clips/med/10", headers={"If-None-Match": '"k9"'})
    assert response.status_code == 200 and response.content == b"RIFFwav"
    # a weak tag, or several tags: the key among them is enough (RFC 9110)
    assert env.robot.get("/api/device/clips/med/10", headers={"If-None-Match": 'W/"k10"'}).status_code == 304
    assert env.robot.get("/api/device/clips/med/10", headers={"If-None-Match": '"k9", "k10"'}).status_code == 304
    assert asked == [10, 10, 10, 10]   # the kept clip is read each time (disk, not the API); the body is skipped
