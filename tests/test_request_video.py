"""The family's confirmation request tells the truth about its video (plan 2026-10-16, Task 2).

On the 16 Oct robot test a request said 「影片已在前一則訊息送出」 while no message carried a clip: the GPT-Live agent
claimed `sent_earlier` whenever a watch's review message had been scheduled, even when that review went without a
clip, and a request with no clip at all said nothing about why. Now:
- record_medication_taken says `sent_earlier` only when the review task's outcome says its message went with the
  clip; a review still in its grace is dropped and one already being made is waited for (dose_event.sending);
- before a request is made, dose_video.clip_outlook knows whether a clip can come (the dose-video consent and at
  least MIN_FRAMES fresh frames), and the request's evidence carries `video`: with_request, sent_earlier,
  none:no_consent or none:no_frames, on the agent's path and on the robot's own /tasks/{id}/confirmation;
- dose_confirmation._request_messages renders each value in Chinese and English.
"""

import asyncio
import logging
import sys
import time
import types

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.services import dose_confirmation, dose_event, dose_video, tool_registry   # noqa: E402
from tests import test_device_api, test_tool_registry   # noqa: E402
from tests.test_device_api import TASK_ID   # noqa: E402
from tests.test_dose_confirmation import FakeDB   # noqa: E402
from tests.test_tool_registry import backend   # noqa: E402

env = test_device_api.env   # the harnesses' fixtures, registered here too
camera, now, vision_on = test_tool_registry.camera, test_tool_registry.now, test_tool_registry.vision_on
REAL_CREATE = dose_confirmation.create
DETAILS = {"patient": "阿嬤", "meds": "Allegra", "slot": "10/16 20:00", "notes_zh": [], "notes_en": []}
LINES = {
    "with_request": ("📹 影片會在下一則訊息送出。", "📹 The video follows in the next message."),
    "sent_earlier": ("📹 影片已在前一則訊息送出。", "📹 The video went with the previous message."),
    "none:no_consent": ("（沒有影片：這位病人沒有開啟「服藥影片傳給家人」）",
                        "(No video: \"Dose videos for family\" is off for this patient.)"),
    "none:no_frames": ("（沒有影片：鏡頭最近沒有畫面）", "(No video: the camera had no recent frames.)"),
}


@pytest.fixture(autouse=True)
def clean_buffers(monkeypatch):
    monkeypatch.setattr(dose_video, "_buffers", {})


def fill(u_id, count, *, age=0.0, step=0.2):
    """`count` frames of patient u_id, one every `step` seconds, the newest `age` seconds old (the buffer's clock)."""
    newest = time.monotonic() - age
    for index in range(count):
        dose_video.buffer_frame(u_id, b"jpeg", "reachy", now=newest - (count - 1 - index) * step)


def consent(monkeypatch, value):
    """dose_video.enabled answers `value` (an exception: the consent read fails)."""
    async def enabled(u_id):
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(dose_video, "enabled", enabled)


def request_text(evidence, *, reminder=False):
    return dose_confirmation._request_messages(DETAILS, "patient_claim", "conf-1", 34, reminder=reminder,
                                               evidence=evidence)[0]["text"]


# ── the request's words ──

@pytest.mark.parametrize("video", list(LINES))
def test_each_video_value_renders_its_own_line_in_chinese_and_english(video):
    text = request_text({"confidence": 0.5, "video": video})
    zh, en = LINES[video]
    assert zh in text and en in text
    assert text.index(zh) < text.index("請查看藥盒後回覆") and text.index(en) < text.index("Check the pill box")
    for other, (other_zh, other_en) in LINES.items():
        if other != video:
            assert other_zh not in text and other_en not in text


def test_a_request_from_before_the_video_value_says_nothing_about_a_video():
    text = request_text({"confidence": 0.5})
    assert "📹" not in text and "沒有影片" not in text and "No video" not in text


def test_a_reminder_points_back_to_the_video_and_never_promises_a_next_message():
    for video in ("with_request", "sent_earlier"):
        text = request_text({"video": video}, reminder=True)
        assert "📹 影片請看稍早的訊息。" in text and "📹 The video is in the earlier messages." in text
        assert "下一則" not in text and "next message" not in text and "前一則" not in text
    for video in ("none:no_consent", "none:no_frames"):
        zh, en = LINES[video]
        text = request_text({"video": video}, reminder=True)
        assert zh in text and en in text


# ── what can come: dose_video.clip_outlook ──

def video_warnings(caplog):
    """The WARNING lines dose_video logged since the last call (then forgotten)."""
    lines = [record.getMessage() for record in caplog.records
             if record.name == dose_video.log.name and record.levelno == logging.WARNING]
    caplog.clear()
    return lines


def no_frames_line(u_id, count):
    return (f"dose video for patient {u_id}: no video with the confirmation request: "
            f"{count} recent frames buffered, {dose_video.MIN_FRAMES} needed")


def test_the_outlook_needs_the_consent_and_enough_fresh_frames(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=dose_video.log.name)
    consent(monkeypatch, False)
    fill(7, 8)
    assert asyncio.run(dose_video.clip_outlook(7)) == ("none:no_consent", [])
    assert video_warnings(caplog) == []                                                 # the patient's choice
    consent(monkeypatch, True)
    video, frames = asyncio.run(dose_video.clip_outlook(7))
    assert video == "with_request" and len(frames) == 8 and all(data == b"jpeg" for _, data in frames)
    assert video_warnings(caplog) == []
    # Each lost video logs one WARNING with its reason: the fresh frames it found and the number needed
    assert asyncio.run(dose_video.clip_outlook(8)) == ("none:no_frames", [])          # nothing buffered for 8
    assert video_warnings(caplog) == [no_frames_line(8, 0)]
    dose_video._buffers.clear()
    fill(7, dose_video.MIN_FRAMES - 1)
    assert asyncio.run(dose_video.clip_outlook(7)) == ("none:no_frames", [])          # too few
    assert video_warnings(caplog) == [no_frames_line(7, dose_video.MIN_FRAMES - 1)]
    dose_video._buffers.clear()
    fill(7, 8, age=dose_video.STALE_FRAME_SECONDS + 1)
    assert asyncio.run(dose_video.clip_outlook(7)) == ("none:no_frames", [])          # not this dose's frames
    assert video_warnings(caplog) == [no_frames_line(7, 0)]


def test_the_outlook_takes_the_frames_of_the_detected_moment(monkeypatch):
    consent(monkeypatch, True)
    fill(7, 50)                                                                         # the last 10 s
    _, everything = asyncio.run(dose_video.clip_outlook(7))
    _, moment = asyncio.run(dose_video.clip_outlook(7, event_started_at=time.monotonic() - 1.0))
    assert len(everything) == 50 and dose_video.MIN_FRAMES <= len(moment) < 50          # MIN_CLIP_SECONDS of them


def test_a_consent_that_cannot_be_read_never_fails_the_request(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=dose_video.log.name)
    consent(monkeypatch, RuntimeError("the pool is down"))
    assert asyncio.run(dose_video.clip_outlook(7)) == ("none:no_frames", [])
    assert no_frames_line(7, 0) in caplog.text                                         # the frames decided: lost
    fill(7, 8)
    video, frames = asyncio.run(dose_video.clip_outlook(7))
    assert video == "with_request" and len(frames) == 8   # capture's _store reads the consent again before writing
    assert "consent could not be read" in caplog.text


def test_a_consent_read_that_hangs_counts_as_unreadable_within_its_time_limit(monkeypatch, caplog):
    """Round 3 (F1.6): the consent read had no time limit inside record_medication_taken's TIMEOUTS entry, so a slow
    database could time the tool out and lose the request. Now it gets CONSENT_READ_SECONDS (about
    DB_TIMEOUT_SECONDS); a timeout is an unreadable consent, and the buffered frames decide."""
    caplog.set_level(logging.WARNING, logger=dose_video.log.name)
    assert getattr(dose_video, "CONSENT_READ_SECONDS", None) == 5.0
    monkeypatch.setattr(dose_video, "CONSENT_READ_SECONDS", 0.05, raising=False)

    async def enabled(u_id):
        await asyncio.Event().wait()                        # the database never answers
    monkeypatch.setattr(dose_video, "enabled", enabled)
    fill(7, 8)
    video, frames = asyncio.run(asyncio.wait_for(dose_video.clip_outlook(7), 1.0))
    assert video == "with_request" and len(frames) == 8
    assert "consent could not be read (TimeoutError)" in caplog.text


def test_a_record_has_time_for_a_review_being_made_beside_its_database_calls():
    """Round 3 (F1.6): record_medication_taken may wait REVIEW_WAIT_SECONDS for a review being made, beside its two
    database calls (DB_TIMEOUT_SECONDS each), inside its TIMEOUTS entry: the entry was raised by that wait."""
    assert tool_registry.TIMEOUTS["record_medication_taken"] == 10.0 + tool_registry.REVIEW_WAIT_SECONDS == 15.0


def test_capture_makes_the_clip_from_the_frames_it_is_given(monkeypatch):
    stored = []

    async def store(*args):
        stored.append(args)
    monkeypatch.setattr(dose_video, "_store", store)
    frames = [(1.0 + index * 0.2, b"jpeg") for index in range(6)]

    async def main():
        assert dose_video.capture(7, [5], frames=frames) is True   # nothing buffered now: the frames given count
        await asyncio.sleep(0)
    asyncio.run(main())
    assert stored == [(7, [5], "reachy", frames)]


# ── the review message's outcome: dose_event.sending ──

def test_a_scheduled_report_is_sending_only_after_its_delay_and_until_it_is_done(monkeypatch):
    async def main():
        gate = asyncio.Event()

        async def report(u_id, **fields):
            await gate.wait()
            return {"event": fields["dedupe"], "video": True, "contacts": 1}
        monkeypatch.setattr(dose_event, "report", report)
        task = dose_event.schedule(7, delay=0.05, kind="intake", dedupe="watch:w", intk_ids=[], frames=[])
        await asyncio.sleep(0)
        waiting = dose_event.sending(task)
        await asyncio.sleep(0.1)
        running = dose_event.sending(task)
        gate.set()
        result = await task
        return waiting, running, dose_event.sending(task), result

    waiting, running, done, result = asyncio.run(main())
    assert (waiting, running, done) == (False, True, False) and result["video"] is True


# ── record_medication_taken ──

@pytest.fixture
def reports(monkeypatch):
    """dose_event.schedule recorded, never run; a future stands in for the review task."""
    scheduled = []

    def schedule(u_id, *, delay=0.0, **fields):
        task = asyncio.get_running_loop().create_future()
        scheduled.append({"delay": delay, **fields, "task": task})
        return task
    monkeypatch.setattr(dose_event, "schedule", schedule)
    return scheduled


@pytest.fixture
def captures(monkeypatch):
    """capture() records the patient, the doses and how many frames it was given."""
    taken = []
    monkeypatch.setattr(dose_video, "capture", lambda u_id, intk_ids, *, event_started_at=None, frames=None:
                        taken.append((u_id, intk_ids, len(frames or ()))) or True)
    return taken


@pytest.fixture
def requests_db(monkeypatch):
    """The real dose_confirmation.create on test_dose_confirmation's FakeDB: the family's LINE text is the one
    production writes to the outbox."""
    fake = FakeDB()
    fake.intakes[2] = fake._intake(7, 40, "pending")   # the backend's fake calls this dose "dose-2"
    fake.protection = False                            # the two fakes' dates differ; overdose rules not under test

    async def create(conn, **kwargs):
        return await REAL_CREATE(fake, **kwargs)
    monkeypatch.setattr(dose_confirmation, "create", create)
    return fake


@pytest.fixture
def asked(monkeypatch):
    """dose_confirmation.create records the evidence it was given: what the request says is decided before it."""
    seen = []

    async def create(conn, *, u_id, task_id, intk_ids, source, evidence, started_at=None):
        seen.append(dict(evidence))
        return "req-1"
    monkeypatch.setattr(dose_confirmation, "create", create)
    return seen


def watched_uncertain(b, camera):
    """A watch whose result was uncertain, so its review message was scheduled; returns the watch's session id."""
    started = asyncio.run(b.start_pill_intake_detection(30, "Allegra"))
    camera.extra_events.append({"event_id": "e1", "decision": "uncertain", "confidence": 0.48, "evidence": {}})
    assert asyncio.run(b.get_pill_intake_result(started["session_id"]))["result"] == "uncertain"
    return started["session_id"]


def settle(task, outcome):
    if outcome == "cancelled":
        task.cancel()
    elif isinstance(outcome, Exception):
        task.set_exception(outcome)
    else:
        task.set_result(outcome)


@pytest.mark.parametrize("outcome", [
    {"event": "watch:x", "video": False, "contacts": 2},   # it went, without a clip (no frames, no link, consent off)
    RuntimeError("the review failed"),
    "cancelled",
], ids=["without_a_clip", "failed", "cancelled"])
def test_a_review_that_did_not_carry_the_clip_never_says_sent_earlier(now, camera, reports, captures, requests_db,
                                                                      monkeypatch, outcome):
    consent(monkeypatch, True)
    fill(7, 8)
    camera.intk_id = None                    # the chat's observe session holds no dose (dose_emotion reads it)
    b, _ = backend(monotonic=lambda: 100.0)
    session_id = watched_uncertain(b, camera)
    settle(reports[0]["task"], outcome)
    answer = asyncio.run(b.record_medication_taken(medication_name="Allegra", session_id=session_id,
                                                   patient_confirmed=True))
    assert answer["status"] == "pending_confirmation" and answer["video"] == "with_request"
    assert captures == [(7, [2], 8)]                                       # the request's own clip of the moment
    (request, *_) = [row for row in requests_db.outbox if row["kind"] == "dose_confirm"]
    text = request["payload"]["messages"][0]["text"]
    assert "📹 影片會在下一則訊息送出。" in text and "📹 The video follows in the next message." in text
    assert "前一則" not in text and "previous message" not in text
    assert requests_db.confirmations[answer["request_id"]]["evidence"]["video"] == "with_request"


def test_a_review_that_went_with_its_clip_is_sent_earlier(now, camera, reports, captures, asked, monkeypatch):
    consent(monkeypatch, True)
    fill(7, 8)
    b, _ = backend(monotonic=lambda: 100.0)
    session_id = watched_uncertain(b, camera)
    reports[0]["task"].set_result({"event": f"watch:{session_id}", "video": True, "contacts": 2})
    answer = asyncio.run(b.record_medication_taken(medication_name="Allegra", session_id=session_id,
                                                   patient_confirmed=True))
    assert answer["video"] == "sent_earlier" and asked[0]["video"] == "sent_earlier" and captures == []


def test_a_review_still_in_its_grace_is_dropped_and_the_clip_goes_with_the_request(now, camera, reports, captures,
                                                                                   asked, monkeypatch):
    consent(monkeypatch, True)
    fill(7, 8)
    b, _ = backend(monotonic=lambda: 100.0)
    session_id = watched_uncertain(b, camera)
    answer = asyncio.run(b.record_medication_taken(medication_name="Allegra", session_id=session_id,
                                                   patient_confirmed=True))
    assert reports[0]["task"].cancelled()
    assert answer["video"] == "with_request" and asked[0]["video"] == "with_request" and captures == [(7, [2], 8)]


@pytest.mark.parametrize("carried, video", [(True, "sent_earlier"), (False, "with_request")])
def test_a_review_already_being_made_is_waited_for_never_cut(now, camera, captures, asked, monkeypatch, carried,
                                                             video):
    consent(monkeypatch, True)
    fill(7, 8)
    monkeypatch.setattr(tool_registry, "WATCH_REPORT_GRACE_SECONDS", 0.0)

    async def main():
        release = asyncio.Event()

        async def report(u_id, **fields):
            await release.wait()        # making the clip, sending the message
            return {"event": fields["dedupe"], "video": carried, "contacts": 2}
        monkeypatch.setattr(dose_event, "report", report)
        b, _ = backend(monotonic=lambda: 100.0)
        started = await b.start_pill_intake_detection(30, "Allegra")
        camera.extra_events.append({"event_id": "e1", "decision": "uncertain", "confidence": 0.48, "evidence": {}})
        await b.get_pill_intake_result(started["session_id"])
        review = b._watch["report_task"]
        await asyncio.sleep(0)          # its grace is over: it is making its message
        assert dose_event.sending(review)
        asyncio.get_running_loop().call_later(0.05, release.set)
        answer = await b.record_medication_taken(medication_name="Allegra", session_id=started["session_id"],
                                                 patient_confirmed=True)
        return review, answer

    review, answer = asyncio.run(main())
    assert not review.cancelled() and review.result()["video"] is carried
    assert answer["video"] == video and asked[0]["video"] == video
    assert captures == ([] if carried else [(7, [2], 8)])


def test_a_review_that_takes_too_long_is_left_to_finish_and_the_request_gets_its_own_clip(now, camera, captures,
                                                                                          asked, monkeypatch):
    consent(monkeypatch, True)
    fill(7, 8)
    monkeypatch.setattr(tool_registry, "WATCH_REPORT_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tool_registry, "REVIEW_WAIT_SECONDS", 0.05)

    async def main():
        release = asyncio.Event()

        async def report(u_id, **fields):
            await release.wait()
            return {"event": fields["dedupe"], "video": True, "contacts": 2}
        monkeypatch.setattr(dose_event, "report", report)
        b, _ = backend(monotonic=lambda: 100.0)
        started = await b.start_pill_intake_detection(30, "Allegra")
        camera.extra_events.append({"event_id": "e1", "decision": "uncertain", "confidence": 0.48, "evidence": {}})
        await b.get_pill_intake_result(started["session_id"])
        review = b._watch["report_task"]
        await asyncio.sleep(0)
        answer = await b.record_medication_taken(medication_name="Allegra", session_id=started["session_id"],
                                                 patient_confirmed=True)
        cut = review.cancelled()
        release.set()
        await review
        return cut, answer

    cut, answer = asyncio.run(main())
    assert cut is False                                                   # never cut half made
    assert answer["video"] == "with_request" and asked[0]["video"] == "with_request" and captures == [(7, [2], 8)]


@pytest.mark.parametrize("consented, frames, video", [
    (False, 8, "none:no_consent"),
    (True, 0, "none:no_frames"),
    (True, dose_video.MIN_FRAMES - 1, "none:no_frames"),
    (True, 8, "with_request"),
])
def test_the_request_knows_before_it_is_made_whether_a_clip_can_come(now, captures, asked, monkeypatch, caplog,
                                                                     consented, frames, video):
    caplog.set_level(logging.WARNING, logger=dose_video.log.name)
    consent(monkeypatch, consented)
    fill(7, frames)
    b, _ = backend()
    answer = asyncio.run(b.record_medication_taken("dose-2", True))   # the patient's word, no watch
    assert answer["status"] == "pending_confirmation" and answer["video"] == video
    assert asked[0]["video"] == video                                    # in the evidence the request was made with
    assert captures == ([(7, [2], 8)] if video == "with_request" else [])
    assert video_warnings(caplog) == ([no_frames_line(7, frames)] if video == "none:no_frames" else [])


# ── the robot's own request: /api/device/tasks/{id}/confirmation ──

@pytest.mark.parametrize("consented, frames, video", [
    (False, 8, "none:no_consent"),
    (True, 0, "none:no_frames"),
    (True, 8, "with_request"),
])
def test_the_robots_request_carries_the_same_video_values(env, captures, monkeypatch, caplog, consented, frames,
                                                          video):
    caplog.set_level(logging.WARNING, logger=dose_video.log.name)
    consent(monkeypatch, consented)
    fill(7, frames)
    response = env.robot.post(f"/api/device/tasks/{TASK_ID}/confirmation",
                              json={"intk_id": 101, "source": "patient_claim", "evidence": {"said_done": True}})
    assert response.status_code == 200
    assert env.confirmations[-1]["evidence"] == {"said_done": True, "video": video}   # the robot's own keys kept
    assert captures == ([(7, [101], 8)] if video == "with_request" else [])
    assert video_warnings(caplog) == ([no_frames_line(7, frames)] if video == "none:no_frames" else [])


def test_the_robots_request_without_evidence_still_says_where_the_video_is(env, captures, monkeypatch):
    consent(monkeypatch, False)
    response = env.robot.post(f"/api/device/tasks/{TASK_ID}/confirmation",
                              json={"intk_id": 101, "source": "degraded"})
    assert response.status_code == 200 and env.confirmations[-1]["evidence"] == {"video": "none:no_consent"}


def test_the_robots_detected_moment_chooses_the_frames_only_for_an_uncertain_detection(env, monkeypatch):
    asked_at = []

    async def clip_outlook(u_id, *, event_started_at=None):
        asked_at.append(event_started_at)
        return "none:no_frames", []
    monkeypatch.setattr(dose_video, "clip_outlook", clip_outlook)
    assert test_device_api._start(env, mode="dose", intk_id=101, task_id=TASK_ID).status_code == 200
    (session,) = [s for s in env.registry.sessions.values() if not s.ended]
    session.event_started_at = 42.0
    for source in ("uncertain_detection", "patient_claim"):
        assert env.robot.post(f"/api/device/tasks/{TASK_ID}/confirmation",
                              json={"intk_id": 101, "source": source}).status_code == 200
    assert asked_at == [42.0, None]


def test_a_refused_robot_request_makes_no_clip(env, captures, monkeypatch):
    consent(monkeypatch, True)
    fill(7, 8)
    assert env.robot.post(f"/api/device/tasks/{TASK_ID}/confirmation",
                          json={"intk_id": 102, "source": "patient_claim"}).status_code == 409
    assert captures == []
