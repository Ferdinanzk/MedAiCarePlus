"""The admin's switch for LINE medication reminders, its state for every user's banner, and the month's LINE usage."""
import asyncio
import datetime
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, StrictBool

from app.database import get_pool
from app.dependencies import get_consented_user, require_admin
from app.services import line_pause
from app.services.line_service import LineService

log = logging.getLogger(__name__)

router = APIRouter()


class PauseBody(BaseModel):
    paused: StrictBool


def _since(state: dict) -> str | None:
    since = state["since"]
    return since.isoformat() if since else None


@router.get("/api/line/reminders")
async def banner_state(user: dict = Depends(get_consented_user)):
    state = line_pause.state()
    return {"paused": state["paused"], "since": _since(state)}


async def _admin_view() -> dict:
    state = line_pause.state()
    changed_by = None
    if state["paused"] and state["by"] is not None:
        async with get_pool().acquire() as conn:
            changed_by = await conn.fetchval('SELECT name FROM "user" WHERE u_id = $1', state["by"])
    # quota() makes blocking HTTP calls (cached for 5 minutes), so keep it off the event loop
    quota = await asyncio.to_thread(LineService.get_instance().quota)
    return {"paused": state["paused"], "since": _since(state), "changed_by": changed_by, "quota": quota}


@router.get("/api/admin/line")
async def admin_state(admin: dict = Depends(require_admin)):
    return await _admin_view()


@router.put("/api/admin/line/reminders")
async def set_reminders(body: PauseBody, admin: dict = Depends(require_admin)):
    now = datetime.datetime.now(datetime.timezone.utc)
    async with get_pool().acquire() as conn:
        await line_pause.set_paused(conn, body.paused, admin["u_id"], now)
    log.info("LINE medication reminders %s by user %s", "paused" if body.paused else "resumed", admin["u_id"])
    return await _admin_view()
