import jwt
from fastapi import HTTPException, Depends, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.config import SUPABASE_JWT_SECRET

security = HTTPBearer(auto_error=False)


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """
    Verify Supabase JWT from Authorization: Bearer <token> header.
    Returns dict with at least { 'sub': user_id } on success.
    Raises 401 if token is missing, expired, or invalid.
    """
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Authorization header missing")

    token = credentials.credentials

    if not SUPABASE_JWT_SECRET:
        # Development fallback: accept any token, extract sub if possible
        try:
            payload = jwt.decode(token, options={"verify_signature": False}, algorithms=["HS256"])
            return {"sub": payload.get("sub", ""), "email": payload.get("email", "")}
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid token format")

    try:
        payload = jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",
        )
        return {
            "sub": payload.get("sub", ""),
            "email": payload.get("email", ""),
            "role": payload.get("role", ""),
        }
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
