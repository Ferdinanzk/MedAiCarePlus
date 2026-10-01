"""Delivers notification_outbox rows to LINE.

One in-process worker (started from the shared lifespan) claims due rows, commits
them as 'sending', pushes them outside the transaction, then records the result.
Every row is pushed with X-Line-Retry-Key = uuid5(outbox_id), so a resend after a
crash between commit and send is de-duplicated by LINE (409 → accepted).
"""

import asyncio
import datetime
import json
import uuid

from app.database import get_pool
from app.services import outbox
from app.services.line_service import LineService

BATCH_SIZE = 20
POLL_SECONDS = 5
STOP_TIMEOUT_SECONDS = 5
_BACKOFF_SECONDS = (5, 15, 30)

_task: asyncio.Task | None = None
_stopping = False


def retry_key(outbox_id: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"outbox:{outbox_id}"))


def backoff_seconds(attempts: int, priority: int) -> int:
    """Delay before the next try after the attempts-th failed send (1-based)."""
    if attempts <= len(_BACKOFF_SECONDS):
        return _BACKOFF_SECONDS[max(attempts, 1) - 1]
    return 60 if priority == 0 else 600


async def recover_stuck() -> int:
    """Rows left in 'sending' by a crash go back to the queue (single dispatcher per deployment)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        status = await conn.execute("UPDATE notification_outbox SET status='queued' WHERE status='sending'")
    try:
        return int(str(status).split()[-1])
    except (ValueError, IndexError):
        return 0


async def _send(row) -> dict:
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    service = LineService.get_instance()
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, service.push_messages, row["recipient_line_id"], payload["messages"], retry_key(row["outbox_id"]))
    except Exception as exc:
        return {"status": "failed", "http_status": None, "request_id": None, "error": str(exc)}


async def dispatch_once(now: datetime.datetime | None = None) -> int:
    """Claim and send one batch of due rows; returns how many rows were claimed."""
    fixed_now = now
    now = now or datetime.datetime.now(datetime.timezone.utc)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                "SELECT outbox_id, recipient_line_id, priority, payload, attempts FROM notification_outbox "
                "WHERE status IN ('queued','failed') AND next_attempt_at <= $1 "
                "ORDER BY priority, next_attempt_at LIMIT $2 FOR UPDATE SKIP LOCKED",
                now, BATCH_SIZE)
            if not rows:
                return 0
            await conn.execute(
                "UPDATE notification_outbox SET status='sending' WHERE outbox_id = ANY($1::bigint[])",
                [row["outbox_id"] for row in rows])

    for index, row in enumerate(rows):
        if _stopping:
            unsent = [r["outbox_id"] for r in rows[index:]]
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE notification_outbox SET status='queued' "
                    "WHERE outbox_id = ANY($1::bigint[]) AND status='sending'", unsent)
            break
        result = await _send(row)
        finished = fixed_now or datetime.datetime.now(datetime.timezone.utc)
        async with pool.acquire() as conn:
            if result.get("status") in ("accepted", "duplicate"):
                await conn.execute(
                    "UPDATE notification_outbox SET status='accepted', line_request_id=$2, accepted_at=$3, "
                    "attempts=attempts+1, last_error=NULL WHERE outbox_id=$1",
                    row["outbox_id"], result.get("request_id"), finished)
            else:
                attempts = row["attempts"] + 1
                delay = backoff_seconds(attempts, row["priority"])
                error = result.get("error") or f"LINE status {result.get('status')}"
                await conn.execute(
                    "UPDATE notification_outbox SET status='failed', attempts=$2, next_attempt_at=$3, "
                    "last_error=$4 WHERE outbox_id=$1",
                    row["outbox_id"], attempts, finished + datetime.timedelta(seconds=delay), error[:1000])
    return len(rows)


async def _run() -> None:
    try:
        recovered = await recover_stuck()
        if recovered:
            print(f"[Outbox] Re-queued {recovered} row(s) left in 'sending'")
    except Exception as exc:
        print(f"[Outbox] Recovery failed: {exc}")
    while not _stopping:
        try:
            claimed = await dispatch_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[Outbox] Dispatch failed: {exc}")
            claimed = 0
        if _stopping:
            break
        if claimed < BATCH_SIZE:
            await outbox.wait_for_wake(timeout=POLL_SECONDS)


async def start_dispatcher() -> None:
    global _task, _stopping
    if _task is not None and not _task.done():
        return
    _stopping = False
    _task = asyncio.create_task(_run(), name="outbox-dispatcher")


async def stop_dispatcher() -> None:
    """Let an in-flight send finish (bounded), then stop; unsent claimed rows return to the queue."""
    global _task, _stopping
    task = _task
    if task is None:
        return
    _stopping = True
    outbox.wake()
    try:
        await asyncio.wait_for(asyncio.shield(task), STOP_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    except Exception as exc:
        print(f"[Outbox] Dispatcher stopped with error: {exc}")
    _task = None
