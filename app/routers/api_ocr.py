import asyncio
from fastapi import APIRouter, UploadFile, File, Depends
from fastapi.responses import JSONResponse
from app.dependencies import get_consented_user
from app.config import OCR_GEMINI_IMAGE_BUDGET, OLLAMA_TIMEOUT
from app.services.ocr_service import OCRService, OCRServiceError

router = APIRouter(prefix="/api/ocr", tags=["ocr-api"])


@router.post("/parse")
async def parse_prescription(
    file: UploadFile = File(...),
    user: dict = Depends(get_consented_user),
):
    """Upload a prescription image and return structured OCR results."""
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
    return result
