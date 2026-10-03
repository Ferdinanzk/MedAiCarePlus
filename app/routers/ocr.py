import asyncio
import json
from decimal import Decimal, InvalidOperation
from fastapi import APIRouter, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from app.config import OCR_GEMINI_IMAGE_BUDGET, OLLAMA_TIMEOUT
from app.database import get_pool
from app.services.ocr_service import OCRService, OCRServiceError
from app.routers.api_medications import DOSE_FORMS
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
    try:
        timeout = OCR_GEMINI_IMAGE_BUDGET if svc.gemini_api_key else OLLAMA_TIMEOUT
        result = await asyncio.wait_for(asyncio.to_thread(svc.process_image, image_bytes), timeout=timeout)
    except asyncio.TimeoutError:
        return JSONResponse(
            {"error": "OCR exceeded its time limit. Please retry.", "code": "ocr_provider_timeout"},
            status_code=504,
        )
    except OCRServiceError as exc:
        return JSONResponse({"error": exc.message, "code": exc.code}, status_code=exc.status_code)
    if "error" in result:
        return JSONResponse({"error": result["error"], "code": "ocr_failed"}, status_code=503)
    return JSONResponse(result)


def _scanned_dose(body: dict) -> tuple[str, Decimal]:
    """The first medicine's dose form and amount per dose from the scan, as the medication table stores them.

    The scan names them per medicine (`medications[0]`); a body without a valid form is saved as 'other', so a
    medicine of unknown form is never recorded by the camera alone (only one solid_oral unit is).
    """
    first = body.get("medications")
    first = first[0] if isinstance(first, list) and first and isinstance(first[0], dict) else {}
    dose_form = body.get("dose_form") or first.get("dose_form")
    if dose_form not in DOSE_FORMS:
        dose_form = "other"
    units = body.get("units_per_dose")
    if units is None:
        units = first.get("units_per_dose")
    try:
        units = Decimal(str(units)) if units is not None and not isinstance(units, bool) else Decimal("1")
    except InvalidOperation:
        units = Decimal("1")
    if not units.is_finite() or not Decimal("0") < units < Decimal("100") or units != units.quantize(Decimal("0.01")):
        units = Decimal("1")
    return dose_form, units


@router.post("/save")
async def save_ocr(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)

    body = await request.json()
    dose_form, units_per_dose = _scanned_dose(body)
    pool = get_pool()
    async with pool.acquire() as conn:
        med_id = await conn.fetchval(
            """INSERT INTO medication
               (u_id, med_name, schedule_time, pill_prescribed, total_intake, is_active, dose_form, units_per_dose)
               VALUES ($1,$2,$3,$4,$5,TRUE,$6,$7) RETURNING med_id""",
            user["u_id"],
            body.get("med_name", "Unknown"),
            json.dumps(body.get("schedule_time") or {}),
            int(body.get("pill_prescribed") or 0),
            int(body.get("total_intake_num") or 0),
            dose_form,
            units_per_dose,
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
