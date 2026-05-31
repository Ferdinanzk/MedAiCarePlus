import cv2
import numpy as np
from fastapi import APIRouter, UploadFile, File, Request, Response, Depends
from fastapi.responses import JSONResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature
import bcrypt as _bcrypt
from app.config import SECRET_KEY
from app.database import get_pool
from app.dependencies import get_current_user
from app.services.face_recognition_service import FaceRecognitionService

router = APIRouter(prefix="/api/auth", tags=["auth-api"])
_signer = URLSafeTimedSerializer(SECRET_KEY)


def _hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()


def _verify_password(password: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False

SESSION_COOKIE = "medai_session"
SESSION_MAX_AGE = 60 * 60 * 8  # 8 hours


def _sign(data: dict) -> str:
    return _signer.dumps(data)


def _unsign(token: str) -> dict | None:
    try:
        return _signer.loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None


@router.post("/face-login")
async def face_login(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
):
    """
    Upload a face photo; if recognized and the user exists in the local DB,
    create a session and return user info.
    """
    image_bytes = await file.read()
    nparr = np.frombuffer(image_bytes, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if frame is None:
        return JSONResponse(
            {"success": False, "error": "Invalid image"},
            status_code=400,
        )

    svc = FaceRecognitionService.get_instance()
    import asyncio
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, svc.identify_frame, frame)

    if not result.get("identified") or not result.get("name"):
        return JSONResponse(
            {"success": False, "error": "Face not recognized"},
            status_code=401,
        )

    face_label = result["name"]
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT u_id, name, line_id FROM "user" WHERE face_label=$1 AND user_active=TRUE',
            face_label,
        )

    if not row:
        return JSONResponse(
            {
                "success": False,
                "error": "Face recognized but no account found",
                "face_label": face_label,
            },
            status_code=404,
        )

    token = _sign({"u_id": row["u_id"], "name": row["name"]})

    # Also set a cookie for legacy compatibility
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_MAX_AGE, httponly=True)

    return {
        "success": True,
        "token": token,
        "user": {
            "id": row["u_id"],
            "name": row["name"],
            "face_label": face_label,
            "line_id": row["line_id"],
        },
    }


@router.get("/me")
async def me(request: Request):
    """Return current face-auth user from Bearer token or cookie."""
    token = None
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
    if not token:
        token = request.cookies.get(SESSION_COOKIE)

    if not token:
        return JSONResponse({"authenticated": False}, status_code=401)

    data = _unsign(token)
    if not data:
        return JSONResponse({"authenticated": False}, status_code=401)

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT u_id, name, face_label, line_id FROM "user" WHERE u_id=$1 AND user_active=TRUE',
            data.get("u_id"),
        )

    if not row:
        return JSONResponse({"authenticated": False}, status_code=401)

    return {
        "authenticated": True,
        "user": {
            "id": row["u_id"],
            "name": row["name"],
            "face_label": row["face_label"],
            "line_id": row["line_id"],
        },
    }


from pydantic import BaseModel

class RegisterPayload(BaseModel):
    name: str
    face_label: str
    email: str = ""
    password: str = ""   # for email login fallback
    line_id: str = ""
    age: str = ""
    gender: str = ""
    addres: str = ""
    supabase_id: str = ""

@router.post("/register")
async def register(payload: RegisterPayload):
    """Create a local DB user so face-login can find them later."""
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            password_hash = _hash_password(payload.password) if payload.password else None
            u_id = await conn.fetchval(
                'INSERT INTO "user" (name, face_label, email, password_hash, line_id, supabase_id) VALUES ($1,$2,$3,$4,$5,$6) RETURNING u_id',
                payload.name.strip(),
                payload.face_label.strip().lower(),
                payload.email.strip().lower() if payload.email else None,
                password_hash,
                payload.line_id or None,
                payload.supabase_id or None,
            )
            age_val = int(payload.age) if payload.age.isdigit() else None
            await conn.execute(
                "INSERT INTO detail (u_id, age, gender, addres) VALUES ($1,$2,$3,$4)",
                u_id,
                age_val,
                payload.gender or None,
                payload.addres or None,
            )
        except Exception as exc:
            return JSONResponse(
                {"success": False, "error": str(exc)},
                status_code=400,
            )

    token = _sign({"u_id": u_id, "name": payload.name.strip()})
    return {"success": True, "u_id": u_id, "token": token}


class EmailLoginPayload(BaseModel):
    email: str
    password: str

@router.post("/email-login")
async def email_login(payload: EmailLoginPayload):
    """Login with email + password — returns a face token (same auth system)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT u_id, name, password_hash FROM "user" WHERE email=$1 AND user_active=TRUE',
            payload.email.strip().lower(),
        )
    if not row or not row["password_hash"]:
        return JSONResponse({"success": False, "error": "Invalid email or password"}, status_code=401)
    if not _verify_password(payload.password, row["password_hash"]):
        return JSONResponse({"success": False, "error": "Invalid email or password"}, status_code=401)
    token = _sign({"u_id": row["u_id"], "name": row["name"]})
    return {"success": True, "token": token, "user": {"id": row["u_id"], "name": row["name"]}}


class LinkPayload(BaseModel):
    face_label: str
    face_token: str  # fresh token from /face-login proves physical face ownership

@router.post("/link-account")
async def link_account(
    payload: LinkPayload,
    user: dict = Depends(get_current_user),
):
    """Link an existing local DB user (by face_label) to the current Supabase account.
    Requires a fresh face_token from /face-login as proof of face ownership to prevent IDOR."""
    face_label = payload.face_label.strip().lower()
    supabase_id = user["sub"]

    # Verify the caller physically controls this face — unsign the fresh face token
    face_data = _unsign(payload.face_token)
    if not face_data:
        return JSONResponse(
            {"success": False, "error": "Invalid or expired face token. Please complete face login first."},
            status_code=401,
        )

    pool = get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            'SELECT u_id, supabase_id FROM "user" WHERE face_label=$1 AND user_active=TRUE',
            face_label,
        )
        if not existing:
            return JSONResponse(
                {"success": False, "error": "No local user found with that name. Check your registered name."},
                status_code=404,
            )
        # The face token's u_id must match the face_label's u_id — prevents one user claiming another's face
        if face_data.get("u_id") != existing["u_id"]:
            return JSONResponse(
                {"success": False, "error": "Face token does not match the requested account."},
                status_code=403,
            )
        if existing["supabase_id"] and existing["supabase_id"] != supabase_id:
            return JSONResponse(
                {"success": False, "error": "This name is already linked to a different account."},
                status_code=409,
            )
        await conn.execute(
            'UPDATE "user" SET supabase_id=$1 WHERE face_label=$2',
            supabase_id, face_label,
        )
    return {"success": True, "u_id": existing["u_id"]}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"success": True}
