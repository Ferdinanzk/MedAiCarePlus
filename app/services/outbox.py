"""Durable LINE notification queue: rows are written with the event, sent later.

Writers call enqueue inside their own transaction; app/services/outbox_dispatcher.py
delivers queued rows with retries so a crash between commit and send loses nothing.
"""

import asyncio
import json

_wake: asyncio.Event | None = None


def _event() -> asyncio.Event:
    global _wake
    if _wake is None:
        _wake = asyncio.Event()
    return _wake


def wake() -> None:
    _event().set()


async def wait_for_wake(timeout: float) -> None:
    event = _event()
    try:
        await asyncio.wait_for(event.wait(), timeout)
    except asyncio.TimeoutError:
        pass
    event.clear()


async def enqueue(conn, *, u_id: int, recipient_line_id: str, kind: str, priority: int,
                  messages: list[dict], dedupe_key: str, recipient_contact_id: int | None = None) -> bool:
    inserted = await conn.fetchval(
        "INSERT INTO notification_outbox (dedupe_key, u_id, recipient_contact_id, recipient_line_id, "
        "kind, priority, payload) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb) "
        "ON CONFLICT (dedupe_key) DO NOTHING RETURNING outbox_id",
        dedupe_key, u_id, recipient_contact_id, recipient_line_id, kind, priority,
        json.dumps({"messages": messages}, ensure_ascii=False))
    if inserted is not None and priority == 0:
        wake()
    return inserted is not None


async def enqueue_to_contacts(conn, u_id: int, *, kind: str, priority: int, messages: list[dict],
                              dedupe_prefix: str, contact_flag: str | None) -> int:
    if contact_flag not in (None, "notify_missed", "notify_emotion", "notify_weekly", "notify_skipped"):
        raise ValueError(f"Unknown contact flag {contact_flag}")
    flag_sql = f" AND {contact_flag} = TRUE" if contact_flag else ""
    contacts = await conn.fetch(
        "SELECT id, line_id FROM family_contacts WHERE u_id = $1 AND verified = TRUE "
        "AND line_id IS NOT NULL AND relationship IS DISTINCT FROM 'user'" + flag_sql + " ORDER BY id",
        u_id)
    count = 0
    for contact in contacts:
        count += await enqueue(conn, u_id=u_id, recipient_line_id=contact["line_id"], kind=kind,
                               priority=priority, messages=messages,
                               dedupe_key=f"{dedupe_prefix}:{contact['id']}",
                               recipient_contact_id=contact["id"])
    return count
