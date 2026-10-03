import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from app.config import MEDCARE_TIMEZONE
from app.database import get_pool
from app.routers.auth import current_user
from app.services import dose_safety, schedule
from app.services.intake_repository import transition_intake

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
_TZ = ZoneInfo(MEDCARE_TIMEZONE)


@router.get("/", response_class=HTMLResponse)
async def medicines_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login")
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT med_id, med_name, schedule_time, pill_prescribed, total_intake, created_at "
            "FROM medication WHERE u_id=$1 AND is_active=TRUE ORDER BY created_at DESC",
            user["u_id"]
        )
    meds = [dict(r) for r in rows]
    return templates.TemplateResponse("medicines.html", {"request": request, "user": user, "meds": meds})


@router.post("/")
async def create_medicine(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    body = await request.json()
    pool = get_pool()
    async with pool.acquire() as conn:
        med_id = await conn.fetchval(
            "INSERT INTO medication (u_id, med_name, schedule_time, pill_prescribed) "
            "VALUES ($1,$2,$3,$4) RETURNING med_id",
            user["u_id"],
            body["med_name"],
            json.dumps(body.get("schedule_time") or {}),
            int(body.get("pill_prescribed", 0)),
        )
    return {"med_id": med_id}


@router.get("/{med_id}")
async def get_medicine(request: Request, med_id: int):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM medication WHERE med_id=$1 AND u_id=$2", med_id, user["u_id"]
        )
    if not row:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return dict(row)


@router.patch("/{med_id}/taken")
async def mark_taken(request: Request, med_id: int):
    return await _update_intake(request, med_id, "taken")


@router.patch("/{med_id}/skipped")
async def mark_skipped(request: Request, med_id: int):
    return await _update_intake(request, med_id, "skipped")


async def _update_intake(request: Request, med_id: int, status: str):
    """The legacy page records through the app's rules (transition_intake: stock, the caregiver-confirmation lock,
    overdose protection). 'taken' applies to today's open dose of this medicine a pill taken now may count for
    (dose_safety.open_sql: due, and not expired), nearest first and the earlier one on a tie, as Take Now picks it;
    without one, to today's nearest open dose, whose refusal (409 with the reason and the patient's sentence) says
    why, and which protection switched off records as before. 'skipped' applies to today's nearest open dose, which
    may be later."""
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    now = schedule.current_time()
    local_start = datetime.combine(now.astimezone(_TZ).date(), time.min, tzinfo=_TZ)
    first = f"({dose_safety.open_sql('intake', '$4::timestamptz')}) DESC, " if status == "taken" else ""
    pool = get_pool()
    async with pool.acquire() as conn:
        dose = await conn.fetchrow(
            "SELECT intk_id FROM intake WHERE med_id=$1 AND u_id=$2 AND intake_stats IN ('pending','missed') "
            "AND intake_time_stamp >= $3 AND intake_time_stamp < $5 "
            f"ORDER BY {first}ABS(EXTRACT(EPOCH FROM (intake_time_stamp - $4::timestamptz))), intake_time_stamp "
            "LIMIT 1",
            med_id, user["u_id"], local_start, now, local_start + timedelta(days=1))
    if dose is None:
        return JSONResponse({"error": "No dose of this medicine is waiting today"}, status_code=409)
    try:
        result = await transition_intake(user["u_id"], dose["intk_id"], status)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    if status == "taken" and result["changed"]:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE medication SET total_intake = total_intake + 1, actual_intake_time = NOW() "
                "WHERE med_id=$1 AND u_id=$2",
                med_id, user["u_id"]
            )
    return {"status": status, "med_id": med_id}


@router.delete("/{med_id}")
async def deactivate_medicine(request: Request, med_id: int):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE medication SET is_active=FALSE WHERE med_id=$1 AND u_id=$2",
            med_id, user["u_id"]
        )
    return {"deleted": med_id}
