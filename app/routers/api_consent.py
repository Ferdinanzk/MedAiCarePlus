"""Consent status and recording, available in limited mode."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, StrictBool

from app.database import get_pool
from app.dependencies import get_current_user
from app.services import consent_service, reachy_tasks

router = APIRouter(prefix="/api/consent", tags=["consent"])


class ConsentPayload(BaseModel):
    kind: str
    terms_version: str
    language: str
    document_sha256: str
    scopes: dict[str, StrictBool]
    source: Literal["register", "reconsent", "settings", "pairing"]


@router.get("/status")
async def status(user: dict = Depends(get_current_user)):
    return consent_service.status_payload(await consent_service.get_state(user["u_id"]))


@router.post("")
async def record_consent(payload: ConsentPayload, request: Request,
                         user: dict = Depends(get_current_user)):
    u_id = user["u_id"]
    try:
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                await consent_service.record(
                    conn, u_id, **payload.model_dump(), user_agent=request.headers.get("user-agent"))
                # Withdrawal must stop the robot in the same transaction (spec 04 §2.3);
                # the device API also re-checks consent on every request.
                if payload.scopes.get("robot_camera") is False:
                    await reachy_tasks.revoke_devices(conn, u_id, "consent_withdrawn")
                elif payload.scopes.get("core") is False:
                    await reachy_tasks.abort_open_tasks(conn, u_id, "consent_withdrawn")
    except consent_service.ConsentError as exc:
        status_code = 409 if exc.code in ("stale_terms_version", "document_hash_mismatch") else 422
        raise HTTPException(status_code, exc.code) from exc
    consent_service.invalidate(u_id)
    return consent_service.status_payload(await consent_service.get_state(u_id))
