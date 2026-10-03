"""Account export and deletion remain available without current consent."""

import io
import json
import logging
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response
from itsdangerous import BadSignature
from pydantic import BaseModel

from app.database import get_pool
from app.dependencies import get_current_user, _face_signer
from app.routers.api_auth import _verify_password
from app.services import deletion_ledger

router = APIRouter(prefix="/api/account", tags=["account"])
logger = logging.getLogger(__name__)
EXPORT_TABLES = (
    "user", "detail", "medication", "medication_supply", "intake", "emotion", "family_contacts",
    "notification_settings", "notification", "login_log", "monitor_event", "consent",
    "conversation", "conversation_turn", "patient_memory", "patient_memory_deleted",
    "dose_confirmation", "monitor_extra_event", "reachy_device", "reachy_task", "notification_outbox",
    "dose_video", "dose_video_link", "dose_emotion",
)
# Tables without a u_id column, read through the row that owns them.
EXPORT_QUERIES = {
    "dose_video_link": "SELECT l.* FROM dose_video_link l JOIN dose_video v USING (video_id) WHERE v.u_id=$1",
}


def _strip_secrets(account: dict) -> None:
    """Credential-derived values stay out of the export, even hashed (a dose-video link token opens the clip)."""
    for table, column in (("user", "password_hash"), ("reachy_device", "token_hash"),
                          ("dose_video_link", "token_sha256")):
        for row in account.get(table, []):
            row.pop(column, None)


class DeletePayload(BaseModel):
    password: str | None = None


@router.get("/export")
async def export_account(user: dict = Depends(get_current_user)):
    account = {}
    async with get_pool().acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            for table in EXPORT_TABLES:
                query = EXPORT_QUERIES.get(table, f'SELECT * FROM "{table}" WHERE u_id=$1')
                rows = await conn.fetch(query, user["u_id"])
                account[table] = [dict(row) for row in rows]
    if not account["user"]:
        raise HTTPException(404, "account_not_found")
    face_label = account["user"][0].get("face_label")
    _strip_secrets(account)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("account.json", json.dumps(jsonable_encoder(account), ensure_ascii=False))
        for path in deletion_ledger.gallery_files(face_label):
            output.write(path, f"face/{path.name}")
    return Response(archive.getvalue(), media_type="application/zip", headers={
        "Content-Disposition": 'attachment; filename="account.zip"',
        "Cache-Control": "no-store",
    })


@router.post("/delete")
async def delete_account(payload: DeletePayload, request: Request,
                         user: dict = Depends(get_current_user)):
    u_id = user["u_id"]
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                'SELECT password_hash, face_label FROM "user" WHERE u_id=$1 FOR UPDATE', u_id)
            if not row:
                raise HTTPException(404, "account_not_found")
            if row["password_hash"]:
                authenticated = bool(payload.password) and _verify_password(
                    payload.password, row["password_hash"])
            else:
                scheme, _, token = request.headers.get("Authorization", "").partition(" ")
                authenticated = False
                if scheme.lower() == "bearer" and token:
                    try:
                        data = _face_signer.loads(token, max_age=600)
                        authenticated = isinstance(data, dict) and data.get("u_id") == u_id
                    except BadSignature:
                        pass
            if not authenticated:
                raise HTTPException(401, "reauth_required")
            face_label = row["face_label"]
            await deletion_ledger.record(conn, "account", u_id, face_label)
            # Every user FK in sql/init.sql has ON DELETE CASCADE.
            await conn.execute('DELETE FROM "user" WHERE u_id=$1', u_id)

    try:
        deletion_ledger.delete_gallery_files(face_label)
    except Exception:
        logger.exception("Account %s deleted; gallery cleanup failed", u_id)
    deletion_ledger.append_host_file("account", u_id, face_label)
    try:
        deletion_ledger.refresh_gallery(face_label)
    except Exception:
        logger.exception("Account %s deleted; gallery refresh failed", u_id)
    return {"deleted": True}
