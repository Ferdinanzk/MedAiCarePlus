import secrets
import string
from pydantic import BaseModel
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from app.dependencies import get_current_user
from app.database import get_pool

router = APIRouter(prefix="/api/family", tags=["family-api"])


def _generate_code(length: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def _get_u_id(user: dict) -> int | None:
    # Face-auth tokens carry u_id directly
    if "u_id" in user:
        return user["u_id"]
    # Supabase tokens need lookup by supabase_id
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            'SELECT u_id FROM "user" WHERE supabase_id = $1 AND user_active = TRUE',
            user.get("sub"),
        )


class ContactPayload(BaseModel):
    name: str
    phone: str = ""
    relationship: str = "family"
    notify_missed: bool = True
    notify_emotion: bool = True
    notify_weekly: bool = False
    notify_skipped: bool = True


@router.post("/contacts")
async def create_contact(
    payload: ContactPayload,
    user: dict = Depends(get_current_user),
):
    u_id = await _get_u_id(user)
    if not u_id:
        return JSONResponse(
            {"detail": "Local user not found. Please complete face registration first."},
            status_code=404,
        )

    code = _generate_code()
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            contact_id = await conn.fetchval(
                """
                INSERT INTO family_contacts
                    (u_id, name, relationship, phone, verification_code,
                     notify_missed, notify_emotion, notify_weekly, notify_skipped)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                RETURNING id
                """,
                u_id,
                payload.name.strip(),
                payload.relationship,
                payload.phone or None,
                code,
                payload.notify_missed,
                payload.notify_emotion,
                payload.notify_weekly,
                payload.notify_skipped,
            )
        except Exception as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    return {"id": contact_id, "code": code, "name": payload.name.strip()}


@router.get("/contacts")
async def list_contacts(user: dict = Depends(get_current_user)):
    u_id = await _get_u_id(user)
    if not u_id:
        return []

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, relationship, phone, line_id,
                   verified, verified_at, notify_missed, notify_emotion, notify_weekly, notify_skipped
            FROM family_contacts
            WHERE u_id = $1
            ORDER BY created_at
            """,
            u_id,
        )
    return [dict(r) for r in rows]


@router.patch("/contacts/{contact_id}")
async def update_contact(
    contact_id: int,
    payload: ContactPayload,
    user: dict = Depends(get_current_user),
):
    u_id = await _get_u_id(user)
    if not u_id:
        return JSONResponse({"detail": "User not found"}, status_code=404)

    pool = get_pool()
    async with pool.acquire() as conn:
        updated = await conn.fetchval(
            """
            UPDATE family_contacts
            SET name=$1, relationship=$2, phone=$3,
                notify_missed=$4, notify_emotion=$5, notify_weekly=$6, notify_skipped=$7
            WHERE id=$8 AND u_id=$9
            RETURNING id
            """,
            payload.name.strip(),
            payload.relationship,
            payload.phone or None,
            payload.notify_missed,
            payload.notify_emotion,
            payload.notify_weekly,
            payload.notify_skipped,
            contact_id,
            u_id,
        )
    if not updated:
        return JSONResponse({"detail": "Contact not found"}, status_code=404)
    return {"id": updated}


@router.delete("/contacts/{contact_id}")
async def delete_contact(contact_id: int, user: dict = Depends(get_current_user)):
    u_id = await _get_u_id(user)
    if not u_id:
        return JSONResponse({"detail": "User not found"}, status_code=404)

    pool = get_pool()
    async with pool.acquire() as conn:
        deleted = await conn.fetchval(
            "DELETE FROM family_contacts WHERE id=$1 AND u_id=$2 RETURNING id",
            contact_id, u_id,
        )
    if not deleted:
        return JSONResponse({"detail": "Contact not found"}, status_code=404)
    return {"deleted": deleted}
