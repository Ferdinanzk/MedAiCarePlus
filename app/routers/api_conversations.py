"""The patient's own Reachy check-in conversations (robot notice §4: the patient sees everything)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query

from app.database import get_pool
from app.dependencies import get_consented_user

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


def _conversation_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise HTTPException(404, "Conversation not found") from exc


@router.get("")
async def list_conversations(user: dict = Depends(get_consented_user),
                             limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.conversation_id AS id, c.started_at, c.ended_at, c.end_reason, c.summary, c.mood,
                   c.risk_flag, c.language,
                   COUNT(t.turn_id) FILTER (WHERE t.role = 'patient') AS patient_turns,
                   (SELECT text FROM conversation_turn p WHERE p.conversation_id = c.conversation_id
                      AND p.role = 'patient' ORDER BY p.turn_id LIMIT 1) AS first_words,
                   COUNT(*) OVER () AS total
            FROM conversation c
            LEFT JOIN conversation_turn t ON t.conversation_id = c.conversation_id
            WHERE c.u_id = $1
            GROUP BY c.conversation_id
            ORDER BY c.started_at DESC
            LIMIT $2 OFFSET $3
            """,
            user["u_id"], limit, offset)
    items = [{**{key: value for key, value in dict(row).items() if key != "total"},
              "id": str(row["id"]), "patient_turns": int(row["patient_turns"])} for row in rows]
    total = int(rows[0]["total"]) if rows else 0
    return {"items": items, "total": total, "has_more": offset + len(items) < total}


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: str, user: dict = Depends(get_consented_user)):
    conversation_id = _conversation_id(conversation_id)
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT conversation_id AS id, started_at, ended_at, end_reason, summary, mood, risk_flag, language, model "
            "FROM conversation WHERE conversation_id = $1::uuid AND u_id = $2", conversation_id, user["u_id"])
        if not row:
            raise HTTPException(404, "Conversation not found")
        turns = await conn.fetch(
            "SELECT role, text, flagged, created_at FROM conversation_turn "
            "WHERE conversation_id = $1::uuid ORDER BY turn_id", conversation_id)
    return {**dict(row), "id": str(row["id"]), "turns": [dict(turn) for turn in turns]}


@router.delete("/{conversation_id}")
async def delete_conversation(conversation_id: str, user: dict = Depends(get_consented_user)):
    conversation_id = _conversation_id(conversation_id)
    async with get_pool().acquire() as conn:
        deleted = await conn.fetchval(
            "DELETE FROM conversation WHERE conversation_id = $1::uuid AND u_id = $2 RETURNING conversation_id",
            conversation_id, user["u_id"])
    if not deleted:
        raise HTTPException(404, "Conversation not found")
    return {"deleted": str(deleted)}
