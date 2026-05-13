import asyncio
from fastapi import APIRouter, UploadFile, File, Depends
from fastapi.responses import JSONResponse
from app.dependencies import get_current_user
from app.services.ocr_service import OCRService

router = APIRouter(prefix="/api/ocr", tags=["ocr-api"])


@router.post("/parse")
async def parse_prescription(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Upload a prescription image, return structured JSON via YOLO + Ollama."""
    image_bytes = await file.read()
    svc = OCRService.get_instance()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, svc.process_image, image_bytes)
    return result
