import json
from datetime import date
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from app.database import get_pool
from app.routers.auth import current_user

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


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
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    pool = get_pool()
    today = date.today()
    async with pool.acquire() as conn:
        # Upsert intake record for today
        existing = await conn.fetchrow(
            "SELECT intk_id FROM intake WHERE med_id=$1 AND DATE(intake_time_stamp)=$2",
            med_id, today
        )
        if existing:
            await conn.execute(
                "UPDATE intake SET intake_stats=$1 WHERE intk_id=$2",
                status, existing["intk_id"]
            )
        else:
            await conn.execute(
                "INSERT INTO intake (u_id, med_id, intake_stats) VALUES ($1,$2,$3)",
                user["u_id"], med_id, status
            )
        if status == "taken":
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
