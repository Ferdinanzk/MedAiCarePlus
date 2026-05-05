import asyncio
import cv2
import numpy as np
from typing import List
from fastapi import APIRouter, Request, Form, UploadFile, File, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature
from app.config import SECRET_KEY, FACE_GALLERY_DIR
from app.database import get_pool
from app.services.face_recognition_service import FaceRecognitionService

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
_signer = URLSafeTimedSerializer(SECRET_KEY)

SESSION_COOKIE = "medai_session"
SESSION_MAX_AGE = 60 * 60 * 8  # 8 hours


def _sign(data: dict) -> str:
    return _signer.dumps(data)


def _unsign(token: str) -> dict | None:
    try:
        return _signer.loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None


def current_user(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    return _unsign(token) if token else None


# ── Pages ─────────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/display/")
    return templates.TemplateResponse("login.html", {"request": request})


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


# ── API ───────────────────────────────────────────────────────────────────────

@router.post("/face-frame")
async def face_frame(request: Request):
    """Accept raw JPEG bytes, return face recognition result."""
    body = await request.body()
    nparr = np.frombuffer(body, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if frame is None:
        return JSONResponse({"identified": False, "error": "Invalid image"}, status_code=400)

    svc = FaceRecognitionService.get_instance()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, svc.identify_frame, frame)
    return JSONResponse(result)


@router.post("/confirm-login")
async def confirm_login(response: Response, name: str = Form(...)):
    """Look up user by face_label, create session cookie."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT u_id, name FROM "user" WHERE face_label=$1 AND user_active=TRUE', name
        )
    if not row:
        # Face recognised but no DB record — send straight to register with face_label pre-filled
        return RedirectResponse(f"/auth/register?face_label={name}&hint=1", status_code=303)

    token = _sign({"u_id": row["u_id"], "name": row["name"]})
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO login_log (u_id, login_session) VALUES ($1, $2)",
                row["u_id"], token[:100]
            )
    except Exception:
        pass  # login_log is audit-only; never block login if table is missing

    redir = RedirectResponse("/display/", status_code=303)
    redir.set_cookie(SESSION_COOKIE, token, max_age=SESSION_MAX_AGE, httponly=True)
    return redir


@router.post("/register")
async def register(
    name: str = Form(...),
    face_label: str = Form(...),
    line_id: str = Form(""),
    age: str = Form(""),
    gender: str = Form(""),
    addres: str = Form(""),
):
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            u_id = await conn.fetchval(
                'INSERT INTO "user" (name, face_label, line_id) VALUES ($1,$2,$3) RETURNING u_id',
                name, face_label.strip().lower(), line_id or None
            )
            age_val = int(age) if age.isdigit() else None
            await conn.execute(
                "INSERT INTO detail (u_id, age, gender, addres) VALUES ($1,$2,$3,$4)",
                u_id, age_val, gender or None, addres or None
            )
        except Exception as exc:
            return RedirectResponse(f"/auth/register?error={exc}", status_code=303)

    return RedirectResponse("/auth/login?registered=1", status_code=303)


@router.get("/logout")
async def logout(response: Response):
    redir = RedirectResponse("/auth/login", status_code=303)
    redir.delete_cookie(SESSION_COOKIE)
    return redir


@router.post("/register-photos")
async def register_photos(
    face_label: str = Form(...),
    photos: List[UploadFile] = File(...),
):
    """
    Save 3 enrollment photos into face_gallery as:
      {face_label}-0.jpg, {face_label}-1.jpg, {face_label}-2.jpg
    Mirrors the save logic from photo_capture.py.
    """
    label = face_label.strip().lower()
    if not label or "/" in label or "\\" in label:
        return JSONResponse({"error": "Invalid face_label"}, status_code=400)
    if len(photos) != 3:
        return JSONResponse({"error": "Exactly 3 photos required"}, status_code=400)

    FACE_GALLERY_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    for i, upload in enumerate(photos):
        data = await upload.read()
        nparr = np.frombuffer(data, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return JSONResponse({"error": f"Cannot decode photo {i}"}, status_code=400)
        path = FACE_GALLERY_DIR / f"{label}-{i}.jpg"
        cv2.imwrite(str(path), img)
        saved.append(str(path))

    return {"saved": saved, "face_label": label}
