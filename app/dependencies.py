from fastapi import HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from itsdangerous import URLSafeTimedSerializer, BadSignature
from app.config import SECRET_KEY

security = HTTPBearer(auto_error=False)

_face_signer = URLSafeTimedSerializer(SECRET_KEY)
_FACE_SESSION_MAX_AGE = 60 * 60 * 8  # 8 hours


def _unsign_face_token(token: str) -> dict | None:
    try:
        return _face_signer.loads(token, max_age=_FACE_SESSION_MAX_AGE)
    except BadSignature:
        return None


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Authorization header missing")
    face_data = _unsign_face_token(credentials.credentials)
    if face_data and face_data.get("u_id"):
        return {"u_id": face_data["u_id"], "name": face_data.get("name", "")}
    raise HTTPException(status_code=401, detail="Invalid or expired token")
