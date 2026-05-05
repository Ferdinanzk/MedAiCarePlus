from datetime import date
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from app.database import get_pool
from app.routers.auth import current_user

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login")
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user})


@router.get("/today")
async def today_intake(request: Request):
    """
    Returns each active medication with today's intake status.
    If no intake row exists for today, status defaults to 'pending'.
    """
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)

    today = date.today()
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT
                 m.med_id,
                 m.med_name,
                 m.schedule_time,
                 m.pill_prescribed,
                 m.total_intake,
                 COALESCE(i.intake_stats, 'pending') AS intake_stats,
                 i.intk_id
               FROM medication m
               LEFT JOIN intake i
                 ON i.med_id = m.med_id AND DATE(i.intake_time_stamp) = $2
               WHERE m.u_id = $1 AND m.is_active = TRUE
               ORDER BY m.created_at""",
            user["u_id"], today
        )
    return [dict(r) for r in rows]


@router.get("/emotion-summary")
async def emotion_summary(request: Request, days: int = 7):
    """Returns last N emotion records for the chart on the dashboard."""
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT emotion_type, emotion_score, time_stamp "
            "FROM emotion WHERE u_id=$1 ORDER BY time_stamp DESC LIMIT $2",
            user["u_id"], days
        )
    return [dict(r) for r in rows]
