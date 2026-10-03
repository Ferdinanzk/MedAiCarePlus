"""The patient's own memory notes (memory notice §3-§5).

Viewing and deleting work in limited mode (core notice §9: export and deletion stay available even before
updated terms are accepted); adding and correcting need current consent including conversation_memory."""

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.database import get_pool
from app.dependencies import get_consented_user, get_current_user
from app.services import consent_service, deletion_ledger, memory

router = APIRouter(prefix="/api/memory", tags=["memory"])
Kind = Literal["name", "person", "like", "routine", "event"]


class FactPayload(BaseModel):
    kind: Kind
    text: str = Field(min_length=1, max_length=memory.TEXT_MAX)
    subject: str | None = Field(default=None, max_length=memory.SUBJECT_MAX)
    event_date: date | None = None


class FactUpdate(BaseModel):
    text: str = Field(min_length=1, max_length=memory.TEXT_MAX)
    event_date: date | None = None


def _item(fact: dict, chats: dict) -> dict:
    return {"kind": fact["kind"], "subject": fact["subject"], "text": fact["text"],
            "event_date": fact["event_date"].isoformat() if fact["event_date"] else None,
            "source": fact["source"], "learned_at": fact["created_at"].isoformat(),
            "conversation_ids": [str(cid) for cid in chats.get((fact["kind"], fact["subject"]), [])]}


@router.get("")
async def list_memory(user: dict = Depends(get_current_user)):
    u_id = user["u_id"]
    async with get_pool().acquire() as conn:
        facts = await memory.current_facts(conn, u_id)
        rows = await conn.fetch(
            "SELECT kind, subject, array_agg(conversation_id ORDER BY created_at DESC) AS chats "
            "FROM patient_memory WHERE u_id = $1 AND conversation_id IS NOT NULL GROUP BY kind, subject", u_id)
    chats = {(row["kind"], row["subject"]): row["chats"] for row in rows}
    enabled = memory.consent_current(await consent_service.get_state(u_id))
    return {"enabled": enabled, "items": [_item(fact, chats) for fact in facts]}


async def _save(u_id: int, raw: dict) -> dict:
    if not memory.consent_current(await consent_service.get_state(u_id)):
        raise HTTPException(403, "memory_consent_required")
    fact = memory.validate_fact(raw, today=memory.local_today(), source="patient")
    if fact is None:
        raise HTTPException(422, "invalid_fact")
    async with get_pool().acquire() as conn, conn.transaction():
        saved = await memory.save_patient_fact(conn, u_id, fact)
    if saved is None:
        raise HTTPException(403, "memory_consent_required")
    return _item(saved, {})


@router.post("")
async def add_fact(payload: FactPayload, user: dict = Depends(get_consented_user)):
    raw = payload.model_dump()
    raw["event_date"] = payload.event_date.isoformat() if payload.event_date else None
    return await _save(user["u_id"], raw)


@router.patch("/fact")
async def update_fact(payload: FactUpdate, kind: Kind = Query(...), subject: str = Query(..., max_length=memory.SUBJECT_MAX),
                      user: dict = Depends(get_consented_user)):
    async with get_pool().acquire() as conn:
        existing = {(f["kind"], f["subject"]) for f in await memory.current_facts(conn, user["u_id"])}
    if (kind, subject) not in existing:
        raise HTTPException(404, "Fact not found")
    return await _save(user["u_id"], {"kind": kind, "subject": subject, "text": payload.text,
                                      "event_date": payload.event_date.isoformat() if payload.event_date else None})


async def _delete(u_id: int, kind: str | None, subject: str | None) -> list[str]:
    async with get_pool().acquire() as conn, conn.transaction():
        ids = await memory.delete_facts(conn, u_id, kind, subject)
    for memory_id in ids:
        deletion_ledger.append_host_file("memory", u_id, memory_id)
    return ids


@router.delete("/fact")
async def delete_fact(kind: Kind = Query(...), subject: str = Query(..., max_length=memory.SUBJECT_MAX),
                      user: dict = Depends(get_current_user)):
    ids = await _delete(user["u_id"], kind, subject)
    if not ids:
        raise HTTPException(404, "Fact not found")
    return {"deleted": len(ids)}


@router.delete("")
async def delete_all(confirm: str = Query(""), user: dict = Depends(get_current_user)):
    if confirm != "all":
        raise HTTPException(400, "confirm=all required")
    return {"deleted": len(await _delete(user["u_id"], None, None))}
