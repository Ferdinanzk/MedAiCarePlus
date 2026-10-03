"""Dose videos for family on LINE (opt-in: consent scope 'dose_video', notice kind 'video').

While a monitor session runs for a consenting patient, the camera frames it already receives (the browser's
identity JPEGs, Reachy's streamed frames) are also held in memory for BUFFER_SECONDS. When a dose is recorded, or
Reachy asks a caregiver to confirm one, capture() turns the frames before that moment into a short H.264 mp4 and a
JPEG preview on disk (DOSE_VIDEO_DIR).

taken_confirmation_job sends the clip with the "dose taken" message. LINE has no upload API: each recipient gets a
video message pointing at their own link (/api/media/line/<token>.mp4), and their LINE app downloads the clip from
this server through the LINE tunnel. cleanup() deletes the files once every recipient's app has the whole clip or
LINE reports it played to the end (after GRACE), MAX_AGE after sending in any case, at once when consent is
withdrawn, and when a clip is never sent. The dose_video rows stay, without media, as a record.
"""

import asyncio
import hashlib
import logging
import os
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlsplit

import cv2
import numpy as np
import requests

from app import config
from app.database import get_pool
from app.services import consent_service

log = logging.getLogger(__name__)

SCOPE = "dose_video"
SOURCES = ("browser", "reachy")
BUFFER_SECONDS = 20.0           # the notice promises at most the last 20 s are held in memory
BUFFER_MAX_FPS = 10.0           # frames closer together than this are not kept
BUFFER_MAX_BYTES = 24_000_000
PRE_ROLL_SECONDS = 2.0          # a detected event: the clip starts this long before its approach began
EVENT_MAX_SECONDS = 15.0
MIN_CLIP_SECONDS = 6.0
CLAIM_SECONDS = 20.0            # no detected event (the patient's claim, a caregiver check): the last 20 s
MIN_FRAMES = 5
STALE_FRAME_SECONDS = 30.0      # the newest buffered frame is older than this: the buffer is not this dose's
OUTPUT_FPS = 10
MAX_SIDE = 640
CRF = "28"
MAX_VIDEOS_PER_MESSAGE = 4      # one clip per dose; a LINE push carries at most 5 messages
GRACE = timedelta(minutes=10)   # after the last recipient's app has the clip: it may still re-read ranges
MAX_AGE = timedelta(hours=24)   # after sending, whatever happened
UNSENT_MAX_AGE = timedelta(hours=4)    # a caregiver confirmation can take 2 h; the message follows within minutes
ORPHAN_MIN_AGE_SECONDS = 600    # a file with no row younger than this may still be getting its row
BASE_URL_CACHE_SECONDS = 300
BASE_URL_RETRY_SECONDS = 60
TOKEN_BYTES = 32                # secrets.token_urlsafe(32): 43 characters
TOKEN_LENGTH = 43
TRACKING_PREFIX = "dv-"


# ── frames held in memory ──

@dataclass
class _Buffer:
    frames: deque = field(default_factory=deque)   # (time.monotonic(), JPEG bytes)
    size: int = 0
    source: str = "browser"


_buffers: dict[int, _Buffer] = {}
_tasks: set = set()


async def enabled(u_id: int) -> bool:
    return consent_service.is_current(await consent_service.get_state(u_id), SCOPE)


def buffer_frame(u_id: int, jpeg: bytes, source: str, now: float | None = None) -> None:
    """Keep one camera frame of a consenting patient's monitor session (the caller checked consent at start)."""
    now = time.monotonic() if now is None else now
    buffer = _buffers.setdefault(u_id, _Buffer())
    buffer.source = source
    if buffer.frames and now - buffer.frames[-1][0] < 1.0 / BUFFER_MAX_FPS:
        return
    buffer.frames.append((now, bytes(jpeg)))
    buffer.size += len(jpeg)
    while buffer.frames and (now - buffer.frames[0][0] > BUFFER_SECONDS or buffer.size > BUFFER_MAX_BYTES):
        buffer.size -= len(buffer.frames.popleft()[1])


def drop_buffer(u_id: int) -> None:
    _buffers.pop(u_id, None)


def clip_frames(u_id: int, now: float, event_started_at: float | None = None) -> list[tuple[float, bytes]]:
    """The buffered frames a clip made now would show: from PRE_ROLL_SECONDS before a detected event's approach
    (at least MIN_CLIP_SECONDS, at most EVENT_MAX_SECONDS), else the last CLAIM_SECONDS."""
    buffer = _buffers.get(u_id)
    if buffer is None or not buffer.frames or now - buffer.frames[-1][0] > STALE_FRAME_SECONDS:
        return []
    if event_started_at is not None and event_started_at <= now:
        start = min(max(event_started_at - PRE_ROLL_SECONDS, now - EVENT_MAX_SECONDS), now - MIN_CLIP_SECONDS)
    else:
        start = now - CLAIM_SECONDS
    return [frame for frame in buffer.frames if frame[0] >= start]


def capture(u_id: int, intk_ids: list[int], *, event_started_at: float | None = None) -> bool:
    """Make the clip for a dose just recorded (or sent to a caregiver) in the background.

    The frames are taken now, before anything else can arrive; consent is checked again before anything is
    written. False when there is nothing to make.
    """
    frames = clip_frames(u_id, time.monotonic(), event_started_at)
    if len(frames) < MIN_FRAMES:
        return False
    source = _buffers[u_id].source
    task = asyncio.get_running_loop().create_task(_store(u_id, [int(i) for i in intk_ids], source, frames))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


def _paths(video_id: str) -> tuple[Path, Path]:
    return config.DOSE_VIDEO_DIR / f"{video_id}.mp4", config.DOSE_VIDEO_DIR / f"{video_id}.jpg"


def _unlink(video_id: str) -> None:
    for path in (*_paths(video_id), config.DOSE_VIDEO_DIR / f"{video_id}.part.mp4"):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("could not delete %s", path)


async def _store(u_id: int, intk_ids: list[int], source: str, frames: list) -> None:
    video_id = str(uuid.uuid4())
    try:
        if not await enabled(u_id):
            drop_buffer(u_id)
            return
        mp4, preview = _paths(video_id)
        info = await asyncio.get_running_loop().run_in_executor(None, encode, frames, mp4, preview)
        base_url = await public_base_url()   # before the transaction: it may ask LINE and the tunnel
        async with get_pool().acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO dose_video (video_id, u_id, intk_ids, source, frames, duration_ms, size_bytes) "
                "VALUES ($1::uuid, $2, $3::int[], $4, $5, $6, $7)",
                video_id, u_id, intk_ids, source if source in SOURCES else "browser",
                info["frames"], info["duration_ms"], info["size_bytes"])
            if base_url is not None:
                await send_with_confirmation(conn, video_id, u_id, intk_ids, base_url)
    except Exception:
        log.exception("dose video for intakes %s failed", intk_ids)
        _unlink(video_id)


async def send_with_confirmation(conn, video_id: str, u_id: int, intk_ids: list[int], base_url: str) -> int:
    """A clip of a dose that is waiting for a caregiver's answer goes out at once, right after the request, so the
    caregiver can watch it before answering, whatever the camera's frame rate was. Otherwise it waits for the
    "dose taken" message. Returns how many contacts it was queued for."""
    from app.services import dose_confirmation, outbox   # dose_confirmation -> intake_repository -> this module

    request = await conn.fetchrow(
        "SELECT dc.created_at, u.name FROM dose_confirmation dc JOIN \"user\" u ON u.u_id = dc.u_id "
        "WHERE dc.u_id = $1 AND dc.intk_ids && $2::int[] AND dc.resolution IS NULL "
        "ORDER BY dc.created_at DESC LIMIT 1", u_id, list(intk_ids))
    if request is None:
        return 0
    contacts = await dose_confirmation._eligible_contacts(conn, u_id)
    if not contacts:
        return 0
    messages = await link_messages(conn, video_id, base_url, contacts)
    hours = int(MAX_AGE.total_seconds() // 3600)
    note = (f"📹 {request['name'] or 'Patient'} 服藥時鏡頭拍到的畫面，請看完再回覆上面的確認。您的 LINE 下載或播放完後，"
            f"影片就會從我們的系統刪除（最長 {hours} 小時）。\n"
            f"📹 What the camera saw. Please watch it before answering the request above. It is deleted from our "
            f"system once your LINE app has downloaded or played it ({hours} hours at most).")
    for contact in contacts:
        await outbox.enqueue(conn, u_id=u_id, recipient_line_id=contact["line_id"], kind="dose_video", priority=1,
                             messages=[{"type": "text", "text": note}, messages[contact["id"]]],
                             dedupe_key=outbox_key(video_id, contact["id"]), recipient_contact_id=contact["id"])
    return len(contacts)


# ── encoding ──

def _fit(image: np.ndarray, width: int, height: int) -> np.ndarray:
    if image.shape[1] == width and image.shape[0] == height:
        return image
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)


def encode(frames: list[tuple[float, bytes]], mp4_path: Path, preview_path: Path) -> dict:
    """JPEG frames with their times -> a constant-rate H.264 mp4 (faststart, so LINE can stream it) and a JPEG
    preview. Each output frame shows the newest input frame at or before its time."""
    import av   # PyAV (requirements-video.txt); imported here so the rest of the app never needs it

    images = []
    for at, data in frames:
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if image is not None:
            images.append((at, image))
    if len(images) < MIN_FRAMES:
        raise ValueError("too few frames for a clip")
    height, width = images[-1][1].shape[:2]
    scale = min(1.0, MAX_SIDE / max(height, width))
    width, height = max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)
    start, end = images[0][0], images[-1][0]
    ticks = int(round((end - start) * OUTPUT_FPS)) + 1
    mp4_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = mp4_path.with_name(mp4_path.stem + ".part.mp4")
    container = av.open(str(partial), mode="w", format="mp4", container_options={"movflags": "+faststart"})
    try:
        stream = container.add_stream("libx264", rate=OUTPUT_FPS,
                                      options={"crf": CRF, "preset": "veryfast", "profile": "main"})
        stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
        index = 0
        for tick in range(ticks):
            at = start + tick / OUTPUT_FPS
            while index + 1 < len(images) and images[index + 1][0] <= at:
                index += 1
            frame = av.VideoFrame.from_ndarray(_fit(images[index][1], width, height), format="bgr24")
            frame.pts, frame.time_base = tick, Fraction(1, OUTPUT_FPS)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()
    os.replace(partial, mp4_path)
    preview = _fit(images[min(len(images) - 1, int(len(images) * 0.6))][1], width, height)
    ok, jpeg = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        raise ValueError("preview encoding failed")
    preview_path.write_bytes(jpeg.tobytes())
    return {"frames": len(images), "duration_ms": int(ticks * 1000 / OUTPUT_FPS),
            "size_bytes": mp4_path.stat().st_size}


# ── the public address LINE downloads from ──

_base_url: tuple[float, str | None] | None = None


def _webhook_base_url() -> str | None:
    """https://<host> of the LINE webhook (the tunnel scripts/line-tunnel.ps1 set up), if it answers."""
    if not config.LINE_CHANNEL_ACCESS_TOKEN:
        return None
    response = requests.get(config.LINE_WEBHOOK_ENDPOINT_URL, timeout=3,
                            headers={"Authorization": f"Bearer {config.LINE_CHANNEL_ACCESS_TOKEN}"})
    if response.status_code != 200:
        return None
    parts = urlsplit(str(response.json().get("endpoint") or ""))
    if parts.scheme != "https" or not parts.netloc:
        return None
    base = f"https://{parts.netloc}"
    # A tunnel that restarted has a new address; the old one answers 5xx (Cloudflare 530) or not at all.
    probe = requests.get(f"{base}/api/media/line/probe", timeout=3, allow_redirects=False)
    return base if probe.status_code < 500 else None


async def public_base_url() -> str | None:
    global _base_url
    if config.PUBLIC_BASE_URL:
        return config.PUBLIC_BASE_URL if config.PUBLIC_BASE_URL.startswith("https://") else None
    now = time.monotonic()
    if _base_url is not None:
        checked, base = _base_url
        if now - checked < (BASE_URL_CACHE_SECONDS if base else BASE_URL_RETRY_SECONDS):
            return base
    try:
        base = await asyncio.get_running_loop().run_in_executor(None, _webhook_base_url)
    except (requests.RequestException, ValueError):
        base = None
    _base_url = (now, base)
    return base


# ── sending ──

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


async def has_unsent(conn, u_id: int, intk_ids: list[int]) -> bool:
    return bool(await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM dose_video WHERE u_id = $1 AND intk_ids && $2::int[] "
        "AND deleted_at IS NULL AND sent_at IS NULL)", u_id, list(intk_ids)))


async def unsent_videos(conn, u_id: int, intk_ids: list[int]) -> list[dict]:
    """The newest unsent clip of each of these doses, newest first (at most MAX_VIDEOS_PER_MESSAGE); older clips
    of the same doses are deleted as superseded."""
    rows = await conn.fetch(
        "SELECT video_id, intk_ids FROM dose_video WHERE u_id = $1 AND intk_ids && $2::int[] "
        "AND deleted_at IS NULL AND sent_at IS NULL ORDER BY created_at DESC",
        u_id, list(intk_ids))
    chosen, covered = [], set()
    for row in rows:
        doses = set(row["intk_ids"]) & set(intk_ids)
        if doses - covered and len(chosen) < MAX_VIDEOS_PER_MESSAGE:
            chosen.append(dict(row))
            covered |= doses
        else:
            await delete_video(conn, str(row["video_id"]), "superseded")
    return chosen


async def link_messages(conn, video_id: str, base_url: str, contacts) -> dict[int, dict]:
    """A private link per recipient and the LINE video message that points at it; marks the clip sent."""
    messages = {}
    for contact in contacts:
        link_id, token = uuid.uuid4(), secrets.token_urlsafe(TOKEN_BYTES)
        await conn.execute(
            "INSERT INTO dose_video_link (link_id, video_id, contact_id, line_id, token_sha256) "
            "VALUES ($1::uuid, $2::uuid, $3, $4, $5)",
            str(link_id), video_id, contact["id"], contact["line_id"], _token_hash(token))
        messages[contact["id"]] = {
            "type": "video",
            "originalContentUrl": f"{base_url}/api/media/line/{token}.mp4",
            "previewImageUrl": f"{base_url}/api/media/line/{token}.jpg",
            "trackingId": f"{TRACKING_PREFIX}{link_id.hex}",
        }
    await conn.execute(
        "UPDATE dose_video SET sent_at = NOW(), expires_at = NOW() + make_interval(secs => $2) "
        "WHERE video_id = $1::uuid", video_id, MAX_AGE.total_seconds())
    return messages


def outbox_key(video_id: str, contact_id: int) -> str:
    return f"dose_video:{video_id}:{contact_id}"


async def discard(conn, u_id: int, intk_ids: list[int], reason: str) -> None:
    """Delete the unsent clips of these doses (nobody will get them)."""
    for row in await conn.fetch(
            "SELECT video_id FROM dose_video WHERE u_id = $1 AND intk_ids && $2::int[] "
            "AND deleted_at IS NULL AND sent_at IS NULL", u_id, list(intk_ids)):
        await delete_video(conn, str(row["video_id"]), reason)


# ── serving and delivery reports ──

async def open_link(token: str, kind: str) -> tuple[Path, str] | None:
    """(file, link_id) for a live link's clip ('mp4') or preview ('jpg'); None when it is unknown or gone."""
    if len(token) != TOKEN_LENGTH or kind not in ("mp4", "jpg"):
        return None
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT l.link_id, v.video_id, v.u_id FROM dose_video_link l JOIN dose_video v USING (video_id) "
            "WHERE l.token_sha256 = $1 AND v.deleted_at IS NULL AND (v.expires_at IS NULL OR v.expires_at > NOW())",
            _token_hash(token))
    if row is None or not await enabled(row["u_id"]):
        return None
    mp4, preview = _paths(str(row["video_id"]))
    path = mp4 if kind == "mp4" else preview
    return (path, str(row["link_id"])) if path.is_file() else None


async def mark_fetched(link_id: str) -> None:
    async with get_pool().acquire() as conn:
        await conn.execute("UPDATE dose_video_link SET fetched_at = COALESCE(fetched_at, NOW()) "
                           "WHERE link_id = $1::uuid", link_id)


async def video_viewed(tracking_id: str, line_user_id: str) -> bool:
    """LINE's videoPlayComplete for one of our links, from the person it was sent to."""
    if not isinstance(tracking_id, str) or not tracking_id.startswith(TRACKING_PREFIX) or not line_user_id:
        return False
    try:
        link_id = str(uuid.UUID(hex=tracking_id[len(TRACKING_PREFIX):]))
    except ValueError:
        return False
    async with get_pool().acquire() as conn:
        updated = await conn.fetchval(
            "UPDATE dose_video_link SET viewed_at = COALESCE(viewed_at, NOW()) "
            "WHERE link_id = $1::uuid AND line_id = $2 RETURNING link_id", link_id, line_user_id)
    return updated is not None


# ── deletion ──

async def delete_video(conn, video_id: str, reason: str) -> None:
    """Delete the clip's files and stop any of its messages not yet sent; the row stays as a record."""
    await conn.execute("UPDATE dose_video SET deleted_at = NOW(), delete_reason = $2 "
                       "WHERE video_id = $1::uuid AND deleted_at IS NULL", video_id, reason)
    await conn.execute("UPDATE notification_outbox SET status = 'cancelled' "
                       "WHERE dedupe_key LIKE $1 AND status IN ('queued','failed')", f"dose_video:{video_id}:%")
    _unlink(video_id)


async def delete_all(u_id: int, reason: str) -> int:
    drop_buffer(u_id)
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT video_id FROM dose_video WHERE u_id = $1 AND deleted_at IS NULL", u_id)
        for row in rows:
            await delete_video(conn, str(row["video_id"]), reason)
    return len(rows)


def delete_reason(row, consented: bool, now: datetime) -> str | None:
    """Why a live clip (a cleanup() row) must go now, or None to keep it."""
    if not consented:
        return "consent_withdrawn"
    if row["sent_at"] is None:
        return "not_sent" if row["created_at"] <= now - UNSENT_MAX_AGE else None
    if row["links"] == 0:
        return "no_recipients"
    if row["waiting"] == 0 and row["last_done"] is not None and row["last_done"] <= now - GRACE:
        return "delivered"
    if row["expires_at"] is not None and row["expires_at"] <= now:
        return "expired"
    return None


async def cleanup(now: datetime | None = None) -> int:
    """Every few minutes: delete clips whose time is up (delete_reason), files without a live row, and buffers
    no session has fed for a while. Returns how many clips were deleted."""
    now = now or datetime.now(timezone.utc)
    deleted = 0
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT v.video_id, v.u_id, v.created_at, v.sent_at, v.expires_at, COUNT(l.link_id) AS links, "
            "COUNT(l.link_id) FILTER (WHERE l.fetched_at IS NULL AND l.viewed_at IS NULL) AS waiting, "
            "MAX(GREATEST(l.fetched_at, l.viewed_at)) AS last_done "
            "FROM dose_video v LEFT JOIN dose_video_link l ON l.video_id = v.video_id "
            "WHERE v.deleted_at IS NULL GROUP BY v.video_id")
        consents: dict[int, bool] = {}
        kept = set()
        for row in rows:
            if row["u_id"] not in consents:
                consents[row["u_id"]] = await enabled(row["u_id"])
            reason = delete_reason(row, consents[row["u_id"]], now)
            if reason:
                await delete_video(conn, str(row["video_id"]), reason)
                deleted += 1
            else:
                kept.add(str(row["video_id"]))
    _remove_orphans(kept)
    for u_id, buffer in tuple(_buffers.items()):
        if not buffer.frames or time.monotonic() - buffer.frames[-1][0] > BUFFER_SECONDS:
            _buffers.pop(u_id, None)
    return deleted


def _remove_orphans(live: set[str]) -> None:
    directory = config.DOSE_VIDEO_DIR
    if not directory.is_dir():
        return
    cutoff = time.time() - ORPHAN_MIN_AGE_SECONDS
    for path in directory.iterdir():
        video_id = path.name.split(".", 1)[0]
        try:
            if video_id not in live and path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            log.warning("could not delete %s", path)


async def status(u_id: int) -> dict:
    """For the patient's settings: whether dose videos are on, whether LINE can reach the clips now, and how many
    clips are stored on this server at the moment."""
    async with get_pool().acquire() as conn:
        stored = await conn.fetchval(
            "SELECT COUNT(*) FROM dose_video WHERE u_id = $1 AND deleted_at IS NULL", u_id)
    return {"enabled": await enabled(u_id), "public_link": await public_base_url() is not None,
            "stored": int(stored or 0), "max_age_hours": int(MAX_AGE.total_seconds() // 3600)}
