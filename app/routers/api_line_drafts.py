"""Medicine photos a patient sent on LINE, read and waiting as 24-hour drafts: the Scan page lists and opens them."""
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException

from app.database import get_pool
from app.dependencies import get_consented_user

router = APIRouter(prefix="/api/line-drafts")

# Only the caller's own drafts that are still waiting and not past their 24 hours.
_LIVE = "u_id = $1 AND status = 'pending' AND expires_at > NOW()"


def _result(row) -> dict:
    result = row["result"]
    # asyncpg gives JSONB back as text (no codec is registered)
    return json.loads(result) if isinstance(result, (str, bytes)) else (result or {})


def _count(result: dict) -> int:
    meds = result.get("medications")
    if isinstance(meds, list) and meds:
        return len(meds)
    return 1 if result.get("med_name") else 0


@router.get("")
async def list_drafts(user: dict = Depends(get_consented_user)):
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            f"SELECT draft_id, created_at, result FROM line_photo_draft WHERE {_LIVE} ORDER BY created_at DESC",
            user["u_id"],
        )
    return [{"id": str(r["draft_id"]), "created_at": r["created_at"].isoformat(), "count": _count(_result(r))}
            for r in rows]


@router.get("/{draft_id}")
async def get_draft(draft_id: uuid.UUID, user: dict = Depends(get_consented_user)):
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT draft_id, created_at, result FROM line_photo_draft WHERE draft_id = $2 AND {_LIVE}",
            user["u_id"], draft_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return {"id": str(row["draft_id"]), "created_at": row["created_at"].isoformat(), "result": _result(row)}


@router.post("/{draft_id}/saved")
async def mark_saved(draft_id: uuid.UUID, user: dict = Depends(get_consented_user)):
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE line_photo_draft SET status = 'saved', saved_at = NOW() WHERE draft_id = $2 AND {_LIVE} "
            "RETURNING draft_id",
            user["u_id"], draft_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return {"saved": True}
