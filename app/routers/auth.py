from typing import List
from fastapi import APIRouter, Request, Form, UploadFile, File, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature
from app.config import SECRET_KEY
from app.database import get_pool
from app.routers.api_face import (FrameUploadError, log_login, login_answer, read_login_frames, run_login,
                                  save_enrollment)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
_signer = URLSafeTimedSerializer(SECRET_KEY)
# /auth/face-frame hands this to the page when a face is identified; /auth/confirm-login takes only it.
_ticket_signer = URLSafeTimedSerializer(SECRET_KEY, salt="face-login-ticket")

SESSION_COOKIE = "medai_session"
SESSION_MAX_AGE = 60 * 60 * 8  # 8 hours
TICKET_MAX_AGE = 60  # seconds


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
async def face_frame(files: List[UploadFile] = File(...)):
    """Up to FACE_LOGIN_FRAMES photos, return the face login decision; when identified, also a login_ticket
    (valid TICKET_MAX_AGE seconds) for /auth/confirm-login."""
    try:
        frames = await read_login_frames(files)
    except FrameUploadError as exc:
        return JSONResponse({"identified": False, "error": exc.error}, status_code=exc.status)
    decision = await run_login(frames)
    result = login_answer(decision)
    if decision.identified:
        result["login_ticket"] = _ticket_signer.dumps(decision.label)
    return JSONResponse(result)


@router.post("/confirm-login")
async def confirm_login(response: Response, ticket: str = Form("")):
    """Turn a login_ticket from /auth/face-frame into a session cookie. A missing, bad or expired ticket is 400."""
    try:
        name = _ticket_signer.loads(ticket, max_age=TICKET_MAX_AGE) if ticket else None
    except BadSignature:
        name = None
    if not isinstance(name, str) or not name:
        return JSONResponse({"error": "invalid_ticket"}, status_code=400)

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT u_id, name FROM "user" WHERE face_label=$1 AND user_active=TRUE', name
        )
    if not row:
        # Face recognised but no DB record — send straight to register with face_label pre-filled
        log_login(None, "match")
        return RedirectResponse(f"/auth/register?face_label={name}&hint=1", status_code=303)

    log_login(row["u_id"], "match")
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
    The legacy register page (static/js/photo_capture.js) posts them before /auth/register creates the
    account. They go through the same checks as POST /api/face/enroll (api_face.save_enrollment): a face
    login already takes for another account is refused with 409, nothing saved. This route has no login,
    so it only takes a label with no gallery photos and no account yet (409 face_label_taken): it can never
    replace anyone's photos.
    """
    label = face_label.strip().lower()
    if not label or "/" in label or "\\" in label:
        return JSONResponse({"error": "Invalid face_label"}, status_code=400)
    if len(photos) != 3:
        return JSONResponse({"error": "Exactly 3 photos required"}, status_code=400)

    saved = await save_enrollment(photos, label, caller="legacy /auth/register-photos", new_label_only=True)
    if isinstance(saved, JSONResponse):
        return saved
    return {"saved": [str(path) for path in saved], "face_label": label}
