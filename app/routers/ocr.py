import asyncio
import json
from fastapi import APIRouter, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from app.database import get_pool
from app.services.ocr_service import OCRService
from app.routers.auth import current_user

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def ocr_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/auth/login")
    return templates.TemplateResponse("ocr.html", {"request": request, "user": user})


@router.post("/upload")
async def upload_prescription(request: Request, file: UploadFile = File(...)):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)

    image_bytes = await file.read()
    svc = OCRService.get_instance()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, svc.process_image, image_bytes)
    return JSONResponse(result)


@router.post("/save")
async def save_ocr(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)

    body = await request.json()
    pool = get_pool()
    async with pool.acquire() as conn:
        med_id = await conn.fetchval(
            """INSERT INTO medication
               (u_id, med_name, schedule_time, pill_prescribed, total_intake, is_active)
               VALUES ($1,$2,$3,$4,$5,TRUE) RETURNING med_id""",
            user["u_id"],
            body.get("med_name", "Unknown"),
            json.dumps(body.get("schedule_time") or {}),
            int(body.get("pill_prescribed") or 0),
            int(body.get("total_intake_num") or 0),
        )
    return {"med_id": med_id}


@router.get("/results")
async def ocr_results(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT med_id, med_name, schedule_time, pill_prescribed, total_intake, created_at "
            "FROM medication WHERE u_id=$1 AND is_active=TRUE ORDER BY created_at DESC",
            user["u_id"]
        )
    return [dict(r) for r in rows]
