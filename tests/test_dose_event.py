"""Medication events to family on LINE, whatever the AI's confidence (services/dose_event.py): the message's words
(the expected medicine, the label check, the intake check and its estimate, the time, a clear negative mood, the
plain "needs your review"), the clip of the moment in the same push when the dose-video consent is on, one outbox
row per contact deduplicated by the event, and the words added to a confirmation request (dose_confirmation)."""

import asyncio
import sys
import types

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.services import dose_confirmation, dose_event, dose_report, dose_video, outbox   # noqa: E402


class Conn:
    def __init__(self, contacts=({"id": 34, "name": "Mei", "line_id": "U34"}, {"id": 35, "name": "Bo", "line_id": "U35"})):
        self.contacts, self.executed, self.inserted = list(contacts), [], []

    def transaction(self):
        conn = self

        class Tx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return Tx()

    async def fetchval(self, query, *args):
        if 'SELECT name FROM "user"' in query:
            return "阿嬤"
        raise AssertionError(query)

    async def fetch(self, query, *args):
        if "FROM family_contacts" in query:
            return self.contacts
        raise AssertionError(query)

    async def execute(self, query, *args):
        self.executed.append((query, args))


class Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return Ctx()


@pytest.fixture
def world(monkeypatch):
    conn = Conn()
    sent = []
    w = types.SimpleNamespace(conn=conn, sent=sent, consent=True, base_url="https://x.trycloudflare.com",
                              clips=[], links=[])

    async def enqueue(c, *, u_id, recipient_line_id, kind, priority, messages, dedupe_key, recipient_contact_id=None):
        sent.append({"u_id": u_id, "to": recipient_line_id, "kind": kind, "priority": priority,
                     "messages": messages, "dedupe_key": dedupe_key, "contact": recipient_contact_id})
        return True

    async def enabled(u_id):
        return w.consent

    async def base_url():
        return w.base_url

    async def make_clip(frames):
        w.clips.append(len(frames))
        return "vid-1", {"frames": len(frames), "duration_ms": 1000, "size_bytes": 10}

    async def insert_clip(c, video_id, u_id, intk_ids, source, info):
        w.links.append(("insert", video_id, intk_ids, source))

    async def link_messages(c, video_id, base, contacts):
        w.links.append(("link", video_id))
        return {contact["id"]: {"type": "video", "originalContentUrl": f"{base}/{contact['id']}.mp4"}
                for contact in contacts}

    monkeypatch.setattr(dose_event, "get_pool", lambda: Pool(conn))
    monkeypatch.setattr(outbox, "enqueue", enqueue)
    monkeypatch.setattr(dose_video, "enabled", enabled)
    monkeypatch.setattr(dose_video, "public_base_url", base_url)
    monkeypatch.setattr(dose_video, "make_clip", make_clip)
    monkeypatch.setattr(dose_video, "insert_clip", insert_clip)
    monkeypatch.setattr(dose_video, "link_messages", link_messages)
    monkeypatch.setattr(dose_video, "_unlink", lambda video_id: w.links.append(("unlink", video_id)))
    dose_video._buffers.clear()
    return w


def _buffer(u_id=7, count=12):
    for i in range(count):
        dose_video.buffer_frame(u_id, b"\xff\xd8" + bytes([i]), "reachy", now=1000.0 + i * 0.2)


def test_an_uncertain_event_goes_to_every_contact_with_the_clip_in_the_same_push(world, monkeypatch):
    monkeypatch.setattr(dose_event, "_clock", lambda: 1000.0 + 11 * 0.2)
    _buffer()
    result = asyncio.run(dose_event.report(
        7, kind="intake", dedupe="watch:abc", intk_ids=[2], medication_name="Allegra",
        verification={"result": "no_match", "expected_medication": "Allegra", "names_read": ["Metformin"]},
        intake="uncertain", confidence=0.48, event_time="10/14 08:05", needs_review=True,
        emotion={"top": "sad", "confidence": 0.62}))
    assert result == {"event": "watch:abc", "video": True, "contacts": 2}
    assert world.clips == [12] and world.links == [("insert", "vid-1", [2], "reachy"), ("link", "vid-1")]
    assert [m["dedupe_key"] for m in world.sent] == ["dose_event:watch:abc:34", "dose_event:watch:abc:35"]
    assert all(m["kind"] == "dose_event" and m["priority"] == 1 and m["u_id"] == 7 for m in world.sent)
    text, video = world.sent[0]["messages"]
    assert video == {"type": "video", "originalContentUrl": "https://x.trycloudflare.com/34.mp4"}
    for words in ("服藥情況需要您確認", "阿嬤", "10/14 08:05", "預期藥物：Allegra", "標籤與預期藥物不符", "讀到：Metformin",
                  "有手到嘴邊的動作，但 AI 不確定", "48%（不確定）", "看起來難過（可能性 62%）", "請您觀看影片後判斷",
                  "尚未記錄為已服藥", "📹 下方影片", "Expected medicine: Allegra", "wrong medicine", "needs your review",
                  dose_report.FOOTNOTE_ZH):
        assert words in text["text"], words


def test_without_the_video_consent_or_frames_the_text_still_goes_and_says_why(world, monkeypatch):
    monkeypatch.setattr(dose_event, "_clock", lambda: 1000.0 + 11 * 0.2)
    _buffer()
    world.consent = False
    result = asyncio.run(dose_event.report(7, kind="watch", dedupe="robot:t:5:not_seen", intk_ids=[5],
                                           medication_name="NUCAN", intake="not_seen", event_time="10/14 20:10"))
    assert result == {"event": "robot:t:5:not_seen", "video": False, "contacts": 2} and world.clips == []
    text = world.sent[0]["messages"]
    assert len(text) == 1 and "沒有影片：病人未開啟用藥影片" in text[0]["text"]
    assert "沒有看到服藥動作" in text[0]["text"] and "AI 判斷" not in text[0]["text"]   # no estimate: no percentage line
    world.sent.clear()
    world.consent = True
    dose_video._buffers.clear()                                     # no frames of the moment
    asyncio.run(dose_event.report(7, kind="watch", dedupe="robot:t:5:patient_left", intk_ids=[5],
                                  medication_name="NUCAN", intake="patient_left", event_time="10/14 20:10"))
    assert "沒有影片：當時沒有鏡頭畫面" in world.sent[0]["messages"][0]["text"] and world.clips == []


def test_without_a_public_link_the_clip_is_dropped_and_the_text_goes_alone(world, monkeypatch, caplog):
    monkeypatch.setattr(dose_event, "_clock", lambda: 1000.0 + 11 * 0.2)
    monkeypatch.setattr(dose_video, "no_link_reason", lambda: "tunnel reaches another server")
    _buffer()
    world.base_url = None
    result = asyncio.run(dose_event.report(7, kind="unresolved", dedupe="watch:x", intk_ids=[2],
                                           intake="still_monitoring", event_time="10/14 08:05"))
    assert result["video"] is False and ("unlink", "vid-1") in world.links
    assert len(world.sent[0]["messages"]) == 1 and "對話結束時還在觀察" in world.sent[0]["messages"][0]["text"]
    # The lost clip's WARNING says why.
    assert "no public link for the clip (tunnel reaches another server)" in caplog.text


def test_nobody_to_tell_sends_nothing_and_a_failure_never_raises(world, monkeypatch):
    monkeypatch.setattr(dose_event, "_clock", lambda: 1000.0 + 11 * 0.2)
    _buffer()
    world.conn.contacts = []
    assert asyncio.run(dose_event.report(7, kind="intake", dedupe="w", intk_ids=[2])) == {
        "event": "w", "video": False, "contacts": 0}
    assert ("unlink", "vid-1") in world.links and world.sent == []

    async def broken(frames):
        raise RuntimeError("ffmpeg gone")
    monkeypatch.setattr(dose_video, "make_clip", broken)
    world.conn.contacts = [{"id": 34, "name": "Mei", "line_id": "U34"}]
    _buffer()
    result = asyncio.run(dose_event.report(7, kind="intake", dedupe="w2", intk_ids=[2]))
    assert result["error"] == "failed" and result["contacts"] == 0


def test_schedule_takes_the_frames_now_and_a_cancelled_report_sends_nothing(world, monkeypatch):
    monkeypatch.setattr(dose_event, "_clock", lambda: 1000.0 + 11 * 0.2)

    async def run():
        _buffer()
        task = dose_event.schedule(7, delay=0.05, kind="intake", dedupe="w3", intk_ids=[2], intake="uncertain")
        dose_video._buffers.clear()                                  # later frames are not this event's
        await task
        cancelled = dose_event.schedule(7, delay=0.2, kind="intake", dedupe="w4", intk_ids=[2])
        cancelled.cancel()
        await asyncio.sleep(0.01)
        return task.result()

    result = asyncio.run(run())
    assert result["video"] is True and world.clips == [12]
    assert [m["dedupe_key"] for m in world.sent] == ["dose_event:w3:34", "dose_event:w3:35"]


def test_the_words_for_each_outcome_and_a_calm_mood_adds_nothing():
    text = dose_event.message_text(patient="A", kind="refused", medication_name="X", verification=None,
                                   intake="taken", confidence=0.82, event_time="10/14 08:00", needs_review=True,
                                   emotion={"top": "neutral", "confidence": 0.9}, video="attached",
                                   notes=[("未記錄的原因：太早", "Not recorded because: too soon")])
    assert "用藥未記錄" in text and "偵測到服藥動作" in text and "82%（高）" in text and "未記錄的原因：太早" in text
    assert "情緒" not in text
    assert dose_report.emotion_note({"top": "sad", "confidence": 0.3}) is None       # not clear enough
    assert dose_report.emotion_note({"top": "happy", "confidence": 0.9}) is None     # not negative
    assert dose_report.verification_words({"result": "match"})[0] == "標籤符合預期藥物"
    assert dose_report.verification_words({"result": "weird"}) is None
    assert dose_event._when("10/14 08:00") == "10/14 08:00"


def test_a_confirmation_request_names_the_label_check_the_mood_and_an_earlier_video():
    details = {"patient": "阿嬤", "meds": "Allegra", "slot": "10/14 08:00", "notes_zh": [], "notes_en": []}
    evidence = {"confidence": 0.56, "verification": {"result": "uncertain"}, "emotion": {"top": "fear", "confidence": 0.7},
                "video": "sent_earlier"}
    text, buttons = dose_confirmation._request_messages(details, "degraded", "conf-1", 34, evidence=evidence)
    assert "藥物辨識：無法辨識（沒有讀到標籤文字）" in text["text"] and "看起來害怕（可能性 70%）" in text["text"]
    assert "影片已在前一則訊息送出" in text["text"] and "The video went with the previous message" in text["text"]
    plain, _ = dose_confirmation._request_messages(details, "degraded", "conf-1", 34, evidence={"confidence": 0.56})
    assert "藥物辨識" not in plain["text"] and "情緒" not in plain["text"]
