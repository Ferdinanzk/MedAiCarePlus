"""Public legal notices (no login): the exact text and hash a consent refers to."""

from fastapi import APIRouter, HTTPException, Query

from app import config
from app.services import legal_service

router = APIRouter(prefix="/api/legal", tags=["legal"])


@router.get("/current")
async def current(kind: str = Query("core"), lang: str | None = Query(None)):
    try:
        doc = legal_service.get_document(kind, lang)
    except KeyError as exc:
        raise HTTPException(404, "unknown_document_kind") from exc
    if config.APP_ENV == "prod" and not doc.complete:
        raise HTTPException(503, "document_not_configured")
    return {"kind": doc.kind, "terms_version": doc.version, "language": doc.language,
            "sha256": doc.sha256, "complete": doc.complete, "document": doc.document}
