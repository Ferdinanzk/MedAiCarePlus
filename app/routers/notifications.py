from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from app.database import get_pool
from app.routers.auth import current_user

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def notifications_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login")
    return templates.TemplateResponse("notifications.html", {"request": request, "user": user})


@router.get("/pending")
async def pending_notifications(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT i.intk_id, m.med_name, i.intake_stats, i.notify_stats,
                      i.intake_time_stamp, i.notify_time
               FROM intake i
               JOIN medication m ON m.med_id = i.med_id
               WHERE i.u_id=$1 AND i.notify_stats='pending'
               ORDER BY i.intake_time_stamp DESC LIMIT 50""",
            user["u_id"]
        )
    return [dict(r) for r in rows]


@router.post("/dismiss/{intk_id}")
async def dismiss_notification(request: Request, intk_id: int):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE intake SET notify_stats='sent', notify_time=NOW() "
            "WHERE intk_id=$1 AND u_id=$2",
            intk_id, user["u_id"]
        )
    return {"dismissed": intk_id}


@router.post("/line-send/{intk_id}")
async def line_send(request: Request, intk_id: int):
    """Stub for LINE Messaging API integration."""
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    # TODO: integrate LINE Messaging API using user.line_id
    return {"status": "stub — LINE integration pending", "intk_id": intk_id}
