"""Dose videos for family: what a clip shows, how LINE downloads it, what the message says, and when it is deleted."""

import asyncio
import base64
import datetime
import hashlib
import hmac
import json
import sys
import types
import uuid

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.jobs import taken_confirmation_job as job
from app.routers import api_dose_video, api_notify
from app.services import dose_video

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
TOKEN = "A" * dose_video.TOKEN_LENGTH


@pytest.fixture(autouse=True)
def clean_buffers():
    dose_video._buffers.clear()
    yield
    dose_video._buffers.clear()


class Pool:
    """A fake pool: every acquire() hands out the same connection."""

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


# ── what a clip shows ──

def _fill(u_id, start, end, step=0.1):
    at = start
    while at <= end + 1e-9:
        dose_video.buffer_frame(u_id, f"{at:.2f}".encode(), "reachy", now=at)
        at += step


def test_buffer_keeps_twenty_seconds_at_most_ten_fps():
    _fill(1, 0.0, 40.0, step=0.05)   # 20 fps offered
    frames = dose_video._buffers[1].frames
    assert frames[-1][0] - frames[0][0] <= dose_video.BUFFER_SECONDS
    assert len(frames) <= dose_video.BUFFER_SECONDS * dose_video.BUFFER_MAX_FPS + 1
    assert dose_video._buffers[1].size == sum(len(data) for _, data in frames)


def test_clip_starts_before_the_detected_approach():
    _fill(1, 0.0, 30.0)
    frames = dose_video.clip_frames(1, now=30.0, event_started_at=25.0)
    assert frames[0][0] >= 23.0 - 1e-6 and frames[0][0] < 23.2   # 2 s pre-roll ...
    frames = dose_video.clip_frames(1, now=30.0, event_started_at=29.0)
    assert frames[0][0] <= 24.0 + 0.1                            # ... but at least 6 s
    frames = dose_video.clip_frames(1, now=30.0, event_started_at=1.0)
    assert frames[0][0] >= 15.0 - 1e-6                           # ... and at most 15 s


def test_clip_without_an_event_is_the_last_twenty_seconds_and_stale_buffers_give_nothing():
    _fill(1, 0.0, 30.0)
    assert dose_video.clip_frames(1, now=30.0)[0][0] >= 10.0 - 1e-6
    assert dose_video.clip_frames(1, now=30.0 + dose_video.STALE_FRAME_SECONDS + 1) == []
    assert dose_video.clip_frames(2, now=30.0) == []


def test_capture_needs_enough_frames(monkeypatch):
    stored = []

    async def store(*args):
        stored.append(args)

    monkeypatch.setattr(dose_video, "_store", store)

    async def main():
        assert dose_video.capture(1, [5]) is False   # no frames at all
        import time
        now = time.monotonic()
        for index in range(12):
            dose_video.buffer_frame(1, b"jpeg", "browser", now=now - 2 + index * 0.15)
        assert dose_video.capture(1, [5]) is True
        await asyncio.sleep(0)

    asyncio.run(main())
    assert stored and stored[0][0] == 1 and stored[0][1] == [5] and stored[0][2] == "browser"


def test_store_writes_nothing_after_consent_is_withdrawn(monkeypatch, tmp_path):
    async def disabled(u_id):
        return False

    def encode(*args):
        raise AssertionError("must not encode without consent")

    monkeypatch.setattr(dose_video, "enabled", disabled)
    monkeypatch.setattr(dose_video, "encode", encode)
    dose_video.buffer_frame(1, b"x", "reachy", now=1.0)
    asyncio.run(dose_video._store(1, [5], "reachy", [(1.0, b"x")]))
    assert 1 not in dose_video._buffers


def test_encode_makes_a_streamable_h264_mp4_and_a_small_preview(tmp_path):
    pytest.importorskip("av")
    frames = []
    for index in range(30):
        image = np.full((480, 640, 3), (index * 8) % 255, np.uint8)
        cv2.putText(image, str(index), (100, 240), cv2.FONT_HERSHEY_SIMPLEX, 4, (255, 255, 255), 8)
        frames.append((index * 0.1, cv2.imencode(".jpg", image)[1].tobytes()))
    mp4, preview = tmp_path / "clip.mp4", tmp_path / "clip.jpg"
    info = dose_video.encode(frames, mp4, preview)
    data = mp4.read_bytes()
    assert data[4:8] == b"ftyp" and data.index(b"moov") < data.index(b"mdat")   # faststart: LINE can stream it
    assert info["frames"] == 30 and info["size_bytes"] == len(data) and 2800 <= info["duration_ms"] <= 3100
    assert preview.read_bytes()[:2] == b"\xff\xd8" and preview.stat().st_size < 1_000_000
    import av
    with av.open(str(mp4)) as container:
        stream = container.streams.video[0]
        assert stream.codec_context.name == "h264" and (stream.width, stream.height) == (640, 480)


# ── how LINE downloads it ──

@pytest.mark.parametrize("header, expected", [
    (None, (0, 999)), ("bytes=0-99", (0, 99)), ("bytes=100-", (100, 999)), ("bytes=-100", (900, 999)),
    ("bytes=900-5000", (900, 999)), ("bytes=0-1,5-9", (0, 999)), ("items=0-1", (0, 999)),
    ("bytes=1000-", None), ("bytes=5-1", None), ("bytes=-0", None),
])
def test_range_header(header, expected):
    assert api_dose_video.parse_range(header, 1000) == expected


@pytest.fixture
def media(monkeypatch, tmp_path):
    from app.main import app

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(bytes(range(256)) * 4)   # 1024 bytes
    opened, fetched = [], []

    async def open_link(token, kind):
        opened.append((token, kind))
        return (clip, "link-1") if token == TOKEN else None

    async def mark_fetched(link_id):
        fetched.append(link_id)

    monkeypatch.setattr(dose_video, "open_link", open_link)
    monkeypatch.setattr(dose_video, "mark_fetched", mark_fetched)
    return types.SimpleNamespace(client=TestClient(app), opened=opened, fetched=fetched)


def test_media_serves_the_whole_clip_and_notes_the_download(media):
    response = media.client.get(f"/api/media/line/{TOKEN}.mp4")
    assert response.status_code == 200 and len(response.content) == 1024
    assert response.headers["content-type"] == "video/mp4" and response.headers["accept-ranges"] == "bytes"
    assert "no-store" in response.headers["cache-control"]
    assert media.fetched == ["link-1"]


def test_media_serves_ranges_and_counts_only_the_end(media):
    response = media.client.get(f"/api/media/line/{TOKEN}.mp4", headers={"Range": "bytes=0-99"})
    assert response.status_code == 206 and response.content == (bytes(range(256)) * 4)[:100]
    assert response.headers["content-range"] == "bytes 0-99/1024" and media.fetched == []
    response = media.client.get(f"/api/media/line/{TOKEN}.mp4", headers={"Range": "bytes=1000-"})
    assert response.status_code == 206 and len(response.content) == 24 and media.fetched == ["link-1"]
    response = media.client.get(f"/api/media/line/{TOKEN}.mp4", headers={"Range": "bytes=2000-"})
    assert response.status_code == 416 and response.headers["content-range"] == "bytes */1024"


def test_media_head_and_preview(media):
    response = media.client.head(f"/api/media/line/{TOKEN}.mp4")
    assert response.status_code == 200 and response.headers["content-length"] == "1024" and not response.content
    assert media.fetched == []
    response = media.client.get(f"/api/media/line/{TOKEN}.jpg")
    assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg" and media.fetched == []


@pytest.mark.parametrize("name", [f"{'B' * 43}.mp4", f"{TOKEN}.png", f"{TOKEN[:-1]}.mp4", "x.mp4"])
def test_media_unknown_or_malformed_links_are_404(media, name):
    assert media.client.get(f"/api/media/line/{name}").status_code == 404
    assert all(token == "B" * 43 for token, _ in media.opened)   # a malformed name never reaches the database


def test_media_link_is_not_served_on_the_robot_port(media):
    from app.main import app

    client = TestClient(app, base_url=f"http://testserver:{config.DEVICE_PORT}")
    assert client.get(f"/api/media/line/{TOKEN}.mp4").status_code == 404 and media.opened == []


def test_open_link_needs_a_live_clip_and_consent(monkeypatch, tmp_path):
    video_id = uuid.uuid4()
    monkeypatch.setattr(config, "DOSE_VIDEO_DIR", tmp_path)
    (tmp_path / f"{video_id}.mp4").write_bytes(b"mp4")
    queries = []

    class Conn:
        async def fetchrow(self, query, *args):
            queries.append((query, args))
            return {"link_id": "link-1", "video_id": video_id, "u_id": 7}

    consent = {"on": True}

    async def enabled(u_id):
        return consent["on"]

    monkeypatch.setattr(dose_video, "get_pool", lambda: Pool(Conn()))
    monkeypatch.setattr(dose_video, "enabled", enabled)
    assert asyncio.run(dose_video.open_link(TOKEN, "mp4")) == (tmp_path / f"{video_id}.mp4", "link-1")
    query, args = queries[0]
    assert "deleted_at IS NULL" in query and "expires_at" in query
    assert args == (hashlib.sha256(TOKEN.encode()).hexdigest(),)   # only the token's hash is stored
    assert asyncio.run(dose_video.open_link(TOKEN, "jpg")) is None   # no preview file
    consent["on"] = False
    assert asyncio.run(dose_video.open_link(TOKEN, "mp4")) is None


def test_link_messages_give_each_recipient_a_private_link(monkeypatch):
    executed = []

    class Conn:
        async def execute(self, query, *args):
            executed.append((query, args))

    contacts = [{"id": 3, "line_id": "U3"}, {"id": 4, "line_id": "U4"}]
    messages = asyncio.run(dose_video.link_messages(Conn(), "vid", "https://x.trycloudflare.com", contacts))
    assert set(messages) == {3, 4}
    first, second = messages[3], messages[4]
    assert first["type"] == "video" and first["originalContentUrl"] != second["originalContentUrl"]
    token = first["originalContentUrl"].rsplit("/", 1)[1][:-4]
    assert first["originalContentUrl"] == f"https://x.trycloudflare.com/api/media/line/{token}.mp4"
    assert first["previewImageUrl"] == f"https://x.trycloudflare.com/api/media/line/{token}.jpg"
    assert api_dose_video._NAME.fullmatch(f"{token}.mp4")
    inserts = [args for query, args in executed if "INSERT INTO dose_video_link" in query]
    assert [args[2:4] for args in inserts] == [(3, "U3"), (4, "U4")]
    assert hashlib.sha256(token.encode()).hexdigest() in inserts[0] and token not in inserts[0]
    assert first["trackingId"] == "dv-" + uuid.UUID(inserts[0][0]).hex and len(first["trackingId"]) <= 100
    assert "SET sent_at = NOW()" in executed[-1][0]


# ── LINE reports a video watched ──

def test_video_viewed_needs_our_tracking_id_and_the_recipient(monkeypatch):
    calls = []

    class Conn:
        async def fetchval(self, query, *args):
            calls.append(args)
            return args[0] if args[1] == "U3" else None

    monkeypatch.setattr(dose_video, "get_pool", lambda: Pool(Conn()))
    link = uuid.uuid4()
    assert asyncio.run(dose_video.video_viewed(f"dv-{link.hex}", "U3")) is True
    assert asyncio.run(dose_video.video_viewed(f"dv-{link.hex}", "U9")) is False
    assert asyncio.run(dose_video.video_viewed("other-tracking", "U3")) is False
    assert asyncio.run(dose_video.video_viewed("dv-not-a-uuid", "U3")) is False
    assert calls == [(str(link), "U3"), (str(link), "U9")]


def _signed(body: bytes, secret: str) -> Request:
    signature = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "headers": [(b"x-line-signature", signature.encode())]}, receive)


def test_webhook_passes_video_play_complete_on(monkeypatch):
    seen = []

    async def video_viewed(tracking_id, user_id):
        seen.append((tracking_id, user_id))
        return True

    monkeypatch.setattr(api_notify, "LINE_CHANNEL_SECRET", "secret")
    monkeypatch.setattr(api_notify.dose_video, "video_viewed", video_viewed)
    monkeypatch.setattr(api_notify, "get_pool", lambda: None)
    body = json.dumps({"events": [{"type": "videoPlayComplete", "source": {"type": "user", "userId": "U3"},
                                   "videoPlayComplete": {"trackingId": "dv-abc"}}]}).encode()
    response = asyncio.run(api_notify.line_webhook(_signed(body, "secret")))
    assert response == {"status": "ok"} or getattr(response, "status_code", 200) == 200
    assert seen == [("dv-abc", "U3")]


# ── when a clip is deleted ──

def _row(**changes):
    row = {"sent_at": NOW - datetime.timedelta(hours=1), "created_at": NOW - datetime.timedelta(hours=1),
           "expires_at": NOW + datetime.timedelta(hours=23), "links": 2, "waiting": 1, "last_done": None}
    row.update(changes)
    return row


@pytest.mark.parametrize("row, consented, reason", [
    (_row(), True, None),                                                             # someone still waiting
    (_row(), False, "consent_withdrawn"),
    (_row(waiting=0, last_done=NOW - datetime.timedelta(minutes=11)), True, "delivered"),
    (_row(waiting=0, last_done=NOW - datetime.timedelta(minutes=5)), True, None),     # grace: may re-read ranges
    (_row(expires_at=NOW - datetime.timedelta(seconds=1)), True, "expired"),
    (_row(links=0, waiting=0), True, "no_recipients"),
    (_row(sent_at=None, expires_at=None, links=0, waiting=0), True, None),            # waiting for its message
    (_row(sent_at=None, expires_at=None, links=0, waiting=0,
          created_at=NOW - dose_video.UNSENT_MAX_AGE), True, "not_sent"),
])
def test_delete_reason(row, consented, reason):
    assert dose_video.delete_reason(row, consented, NOW) == reason


def test_delete_video_removes_files_and_cancels_unsent_messages(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOSE_VIDEO_DIR", tmp_path)
    for suffix in (".mp4", ".jpg"):
        (tmp_path / f"vid{suffix}").write_bytes(b"x")
    executed = []

    class Conn:
        async def execute(self, query, *args):
            executed.append((query, args))

    asyncio.run(dose_video.delete_video(Conn(), "vid", "delivered"))
    assert list(tmp_path.iterdir()) == []
    assert executed[0][1] == ("vid", "delivered")
    assert "cancelled" in executed[1][0] and executed[1][1] == ("dose_video:vid:%",)


def test_cleanup_deletes_due_clips_and_orphan_files(monkeypatch, tmp_path):
    import os
    import time

    monkeypatch.setattr(config, "DOSE_VIDEO_DIR", tmp_path)
    keep, due = uuid.uuid4(), uuid.uuid4()
    for name in (f"{keep}.mp4", f"{due}.mp4", "orphan.mp4", "fresh.mp4"):
        (tmp_path / name).write_bytes(b"x")
    old = time.time() - dose_video.ORPHAN_MIN_AGE_SECONDS - 1
    for name in (f"{keep}.mp4", f"{due}.mp4", "orphan.mp4"):
        os.utime(tmp_path / name, (old, old))
    deleted = []

    class Conn:
        async def fetch(self, query, *args):
            return [{"video_id": keep, "u_id": 1, **_row()},
                    {"video_id": due, "u_id": 1, **_row(expires_at=NOW - datetime.timedelta(minutes=1))}]

    async def delete_video(conn, video_id, reason):
        deleted.append((video_id, reason))
        (tmp_path / f"{video_id}.mp4").unlink()

    async def enabled(u_id):
        return True

    monkeypatch.setattr(dose_video, "get_pool", lambda: Pool(Conn()))
    monkeypatch.setattr(dose_video, "delete_video", delete_video)
    monkeypatch.setattr(dose_video, "enabled", enabled)
    assert asyncio.run(dose_video.cleanup(NOW)) == 1
    assert deleted == [(str(due), "expired")]
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted([f"{keep}.mp4", "fresh.mp4"])


def test_withdrawing_consent_deletes_clips_at_once(monkeypatch, tmp_path):
    from app.dependencies import get_current_user
    from app.main import app
    from app.routers import api_consent
    from app.services import consent_service

    withdrawn, ledger = [], []
    monkeypatch.setattr(config, "DELETION_LEDGER_FILE", tmp_path / "ledger.jsonl")

    async def record(conn, u_id, **kwargs):
        assert kwargs["scopes"] == {"dose_video": False}

    async def delete_all(u_id, reason):
        withdrawn.append((u_id, reason))
        return 2

    async def get_state(u_id):
        return {}

    class Conn:
        def transaction(self):
            class Tx:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False
            return Tx()

        async def execute(self, query, *args):
            assert "INSERT INTO deletion_ledger" in query
            ledger.append(args)

    monkeypatch.setattr(consent_service, "record", record)
    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(api_consent, "get_pool", lambda: Pool(Conn()))
    monkeypatch.setattr(api_consent.dose_video, "delete_all", delete_all)
    monkeypatch.setattr(app, "dependency_overrides", {get_current_user: lambda: {"u_id": 7}})
    response = TestClient(app).post("/api/consent", json={
        "kind": "video", "terms_version": config.TERMS_VERSION, "language": "en", "document_sha256": "x",
        "scopes": {"dose_video": False}, "source": "settings"})
    assert response.status_code == 200 and withdrawn == [(7, "consent_withdrawn")]
    # Like every withdrawn scope, it is in the deletion ledger, so a restore re-applies it.
    assert ledger == [("consent", 7, "dose_video")]
    assert json.loads((tmp_path / "ledger.jsonl").read_text(encoding="utf-8"))["object_id"] == "dose_video"


# ── what the message says ──

def _dose(**changes):
    row = {"id": 5, "u_id": 1, "med_name": "Aspirin", "detection_method": "reachy_prompted",
           "detection_confidence": 0.82, "detector_score": 0.82, "confirm_evidence": None, "confirmed_by": None,
           "patient_name": "Aisha", "notify_family_on_taken": True,
           "intake_time_stamp": datetime.datetime(2026, 10, 2, 12, 0, tzinfo=UTC),
           "actual_intake_time": NOW - datetime.timedelta(minutes=6)}
    row.update(changes)
    return row


def test_message_gives_the_ai_estimate_and_its_limits():
    text = job.taken_message("Aisha", "20:00", [_dose()], videos=1)
    assert "Aisha 已服用 20:00 的藥物" in text and "• Aspirin" in text
    assert "記錄方式：Reachy 鏡頭偵測" in text and "AI 判斷已服藥的可能性：82%（高）" in text
    assert "AI estimate that it was taken: 82% (high)" in text and "cannot see the pill itself" in text
    assert "📹" in text and "24" in text


def test_message_for_a_caregiver_confirmed_claim_without_video():
    evidence = json.dumps({"said_done": True, "camera": "no_event", "degraded": True, "landmark_fps": 4.2})
    text = job.taken_message("Aisha", "20:00", [_dose(detection_method="caregiver_confirmed",
                                                      detection_confidence=None, detector_score=None,
                                                      confirm_evidence=evidence, confirmed_by="aisha")], videos=0)
    assert "aisha 在 LINE 確認" in text and "無法判斷（鏡頭沒有看到服藥動作）" in text
    assert "每秒只有 4.2 張（需要 12 張）" in text and "本人有說「吃完了」" in text
    assert "only 4.2 frames per second (it needs 12)" in text
    assert "the patient said they had finished" in text
    assert "📹" not in text and "cannot see the pill" not in text


@pytest.mark.parametrize("dose, zh", [
    (_dose(detection_method="manual", detection_confidence=None, detector_score=None), "無（沒有使用鏡頭）"),
    (_dose(detection_method="confirmed_by_user", detection_confidence=0.56), "56%（不確定）"),
    (_dose(detection_method="caregiver_confirmed", detection_confidence=None, detector_score=None,
           confirm_evidence={"confidence": 0.3, "decision": "uncertain"}), "30%（低）"),
])
def test_message_levels(dose, zh):
    assert zh in job.taken_message("Aisha", "08:00", [dose], videos=0)


def test_slot_label_is_local_time():
    assert job.slot_label(datetime.datetime(2026, 10, 2, 12, 0, tzinfo=UTC)) == "20:00"


# ── the job: text and clips through the outbox ──

class JobConn:
    def __init__(self, doses, contacts, videos):
        self.doses, self.contacts, self.videos = doses, contacts, videos
        self.executed = []

    async def fetch(self, query, *args):
        if "FROM intake i" in query:
            return self.doses
        if "FROM family_contacts" in query:
            return self.contacts
        raise AssertionError(query)

    async def execute(self, query, *args):
        self.executed.append((query, args))

    def transaction(self):
        class Tx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False
        return Tx()


def _run_job(monkeypatch, conn, base_url="https://x.trycloudflare.com"):
    enqueued, deleted = [], []

    async def enqueue(conn, **kwargs):
        enqueued.append(kwargs)
        return True

    async def has_unsent(conn, u_id, intk_ids):
        return bool(conn.videos)

    async def unsent_videos(conn, u_id, intk_ids):
        return conn.videos

    async def public_base_url():
        return base_url

    async def link_messages(conn, video_id, base, contacts):
        return {contact["id"]: {"type": "video", "originalContentUrl": f"{base}/{video_id}/{contact['id']}"}
                for contact in contacts}

    async def delete_video(conn, video_id, reason):
        deleted.append((video_id, reason))

    async def discard(conn, u_id, intk_ids, reason):
        deleted.append(("discard", reason))

    monkeypatch.setattr(job, "get_pool", lambda: Pool(conn))
    monkeypatch.setattr(job.outbox, "enqueue", enqueue)
    for name, fake in (("has_unsent", has_unsent), ("unsent_videos", unsent_videos),
                       ("public_base_url", public_base_url), ("link_messages", link_messages),
                       ("delete_video", delete_video), ("discard", discard)):
        monkeypatch.setattr(job.dose_video, name, fake)
    asyncio.run(job.check_taken_confirmations())
    return enqueued, deleted


def test_job_sends_the_text_then_each_clip(monkeypatch):
    conn = JobConn([_dose()], [{"id": 3, "line_id": "U3", "name": "aisha"}], [{"video_id": "vid", "intk_ids": [5]}])
    enqueued, deleted = _run_job(monkeypatch, conn)
    assert [item["kind"] for item in enqueued] == ["taken_confirmation", "dose_video"]
    text, video = enqueued
    assert "82%（高）" in text["messages"][0]["text"] and "📹" in text["messages"][0]["text"]
    assert video["messages"] == [{"type": "video", "originalContentUrl": "https://x.trycloudflare.com/vid/3"}]
    assert video["dedupe_key"] == "dose_video:vid:3" and video["recipient_line_id"] == "U3"
    assert any("taken_notified = TRUE" in query for query, _ in conn.executed) and deleted == []


def test_job_without_a_public_link_sends_text_only_and_deletes_the_clip(monkeypatch):
    conn = JobConn([_dose()], [{"id": 3, "line_id": "U3", "name": "aisha"}], [{"video_id": "vid", "intk_ids": [5]}])
    enqueued, deleted = _run_job(monkeypatch, conn, base_url=None)
    assert [item["kind"] for item in enqueued] == ["taken_confirmation"]
    assert "📹" not in enqueued[0]["messages"][0]["text"] and deleted == [("vid", "no_public_link")]


def test_job_waits_for_the_batch_window_and_respects_the_switch(monkeypatch):
    fresh = _dose(actual_intake_time=datetime.datetime.now(UTC))
    conn = JobConn([fresh], [{"id": 3, "line_id": "U3", "name": "aisha"}], [])
    assert _run_job(monkeypatch, conn) == ([], [])
    conn = JobConn([_dose(notify_family_on_taken=False)], [], [{"video_id": "vid", "intk_ids": [5]}])
    enqueued, deleted = _run_job(monkeypatch, conn)
    assert enqueued == [] and deleted == [("discard", "notifications_off")]


def test_the_uncertain_22_00_dose_reads_as_uncertain_with_its_frame_rate():
    evidence = {"decision": "uncertain", "confidence": 0.48, "degraded": True, "landmark_fps": 10.1}
    text = job.taken_message("Aisha", "22:00", [_dose(detection_method="caregiver_confirmed", detection_confidence=None,
                                                      detector_score=None, confirm_evidence=evidence,
                                                      confirmed_by="aisha")], videos=0)
    assert "AI 判斷已服藥的可能性：48%（不確定）" in text and "48% (uncertain)" in text
    assert "每秒只有 10.1 張（需要 12 張）" in text and "cannot see the pill itself" in text


# ── a clip of a dose waiting for a caregiver goes out with the request ──

class ConfirmConn:
    def __init__(self, request=True, contacts=({"id": 34, "name": "aisha", "line_id": "U34"},)):
        self.request, self.contacts = request, list(contacts)

    async def fetchrow(self, query, *args):
        assert "resolution IS NULL" in query
        return {"created_at": NOW, "name": "Aisha"} if self.request else None

    async def fetch(self, query, *args):
        assert "notify_missed = TRUE" in query
        return self.contacts


def _run_send(monkeypatch, conn):
    enqueued, linked = [], []

    async def link_messages(conn, video_id, base, contacts):
        linked.append((video_id, [contact["id"] for contact in contacts]))
        return {contact["id"]: {"type": "video", "originalContentUrl": f"{base}/{contact['id']}.mp4"}
                for contact in contacts}

    async def enqueue(conn, **kwargs):
        enqueued.append(kwargs)
        return True

    from app.services import outbox
    monkeypatch.setattr(dose_video, "link_messages", link_messages)
    monkeypatch.setattr(outbox, "enqueue", enqueue)
    count = asyncio.run(dose_video.send_with_confirmation(conn, "vid", 1, [4], "https://x.trycloudflare.com"))
    return count, enqueued, linked


def test_clip_goes_with_an_open_confirmation_request(monkeypatch):
    count, enqueued, linked = _run_send(monkeypatch, ConfirmConn())
    assert count == 1 and linked == [("vid", [34])]
    item = enqueued[0]
    assert item["kind"] == "dose_video" and item["dedupe_key"] == "dose_video:vid:34" and item["priority"] == 1
    note, video = item["messages"]
    assert note["type"] == "text" and "請看完再回覆上面的確認" in note["text"] and "24" in note["text"]
    assert video == {"type": "video", "originalContentUrl": "https://x.trycloudflare.com/34.mp4"}


def test_clip_without_an_open_request_or_contacts_waits_for_the_taken_message(monkeypatch):
    assert _run_send(monkeypatch, ConfirmConn(request=False)) == (0, [], [])
    assert _run_send(monkeypatch, ConfirmConn(contacts=())) == (0, [], [])


def test_store_sends_a_new_clip_with_the_request(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOSE_VIDEO_DIR", tmp_path)
    calls = []

    class Conn:
        async def execute(self, query, *args):
            calls.append(("execute", query))

        def transaction(self):
            class Tx:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False
            return Tx()

    async def enabled(u_id):
        return True

    async def base_url():
        return "https://x.trycloudflare.com"

    async def send(conn, video_id, u_id, intk_ids, base):
        calls.append(("send", u_id, intk_ids, base))
        return 1

    def encode(frames, mp4, preview):
        return {"frames": len(frames), "duration_ms": 1000, "size_bytes": 10}

    monkeypatch.setattr(dose_video, "enabled", enabled)
    monkeypatch.setattr(dose_video, "public_base_url", base_url)
    monkeypatch.setattr(dose_video, "send_with_confirmation", send)
    monkeypatch.setattr(dose_video, "encode", encode)
    monkeypatch.setattr(dose_video, "get_pool", lambda: Pool(Conn()))
    asyncio.run(dose_video._store(1, [4], "reachy", [(0.0, b"x")] * 6))
    assert "INSERT INTO dose_video" in calls[0][1] and calls[1] == ("send", 1, [4], "https://x.trycloudflare.com")
