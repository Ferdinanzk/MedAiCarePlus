"""Reachy device tokens: issued once at pairing, only their SHA-256 is stored.

A device token authorises only /api/device/* (private port) and only for its own
u_id. get_current_user never accepts one, and main.py rejects any "rdv1." bearer
token on the public port. WebSocket routes use get_device_ws: HTTPBearer, and so
get_device, raises TypeError there (it needs a Request).
"""

import hashlib

from fastapi import Depends, HTTPException, WebSocket
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.security.utils import get_authorization_scheme_param
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import SECRET_KEY
from app.database import get_pool
from app.services import consent_service

TOKEN_PREFIX = "rdv1."
WS_BAD_KEY = 4401      # WebSocket close code: a missing, invalid or revoked device token
WS_NO_CONSENT = 4403   # WebSocket close code: the patient's robot consent is not current
_signer = URLSafeTimedSerializer(SECRET_KEY, salt="reachy-device")
_bearer = HTTPBearer(auto_error=False)


def issue_token(device_id: str, u_id: int) -> str:
    return TOKEN_PREFIX + _signer.dumps({"d": str(device_id), "u": int(u_id)})


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _claims(token: str) -> dict | None:
    if not token.startswith(TOKEN_PREFIX):
        return None
    try:
        claims = _signer.loads(token[len(TOKEN_PREFIX):])
    except BadSignature:
        return None
    if not isinstance(claims, dict) or not isinstance(claims.get("d"), str) or type(claims.get("u")) is not int:
        return None
    return claims


def _unauthorised() -> HTTPException:
    return HTTPException(status_code=401, detail="Invalid or revoked device token")


async def _lookup(credentials: HTTPAuthorizationCredentials | None) -> dict:
    """The device row for a signed token, including revoked rows (caller decides)."""
    if not credentials or not credentials.credentials:
        raise _unauthorised()
    token = credentials.credentials
    claims = _claims(token)
    if claims is None:
        raise _unauthorised()
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            'SELECT d.device_id, d.u_id, d.auto_record, d.revoked_at, u.face_label, u.name '
            'FROM reachy_device d JOIN "user" u ON u.u_id = d.u_id '
            'WHERE d.token_hash = $1 AND u.user_active = TRUE',
            hash_token(token))
    if not row or str(row["device_id"]) != claims["d"] or row["u_id"] != claims["u"]:
        raise _unauthorised()
    return {"device_id": str(row["device_id"]), "u_id": row["u_id"], "auto_record": bool(row["auto_record"]),
            "face_label": row["face_label"], "name": row["name"], "revoked": row["revoked_at"] is not None}


def robot_consent_current(state: dict) -> bool:
    return consent_service.is_current(state, "core") and consent_service.is_current(state, "robot_camera")


async def get_device(credentials: HTTPAuthorizationCredentials = Depends(_bearer)) -> dict:
    """{"device_id","u_id","auto_record","face_label","name"}; 401 invalid/revoked, 403 without robot consent."""
    device = await _lookup(credentials)
    if device.pop("revoked"):
        raise _unauthorised()
    if not robot_consent_current(await consent_service.get_state(device["u_id"])):
        raise HTTPException(status_code=403, detail="consent_required")
    return device


async def get_device_for_heartbeat(credentials: HTTPAuthorizationCredentials = Depends(_bearer)) -> dict:
    """Like get_device, but a revoked device or withdrawn consent yields stop_all instead of an error."""
    device = await _lookup(credentials)
    device["consent_current"] = robot_consent_current(await consent_service.get_state(device["u_id"]))
    return device


async def get_device_ws(websocket: WebSocket) -> dict | None:
    """get_device for a WebSocket route: the same device dict, under the same lookup, revoked and consent rules.

    The token comes from the upgrade request's Authorization header (parsed as HTTPBearer does), never from the URL,
    which ends up in logs. On a failure the socket is accepted and then closed with WS_BAD_KEY (4401, reason
    invalid_token) or WS_NO_CONSENT (4403, reason consent_required), so the robot sees the code (a close before the
    accept reaches it only as HTTP 403), and None is returned: the route then just returns. On success the socket is
    left for the route to accept."""
    scheme, token = get_authorization_scheme_param(websocket.headers.get("authorization"))
    credentials = (HTTPAuthorizationCredentials(scheme=scheme, credentials=token)
                   if scheme.lower() == "bearer" and token else None)
    try:
        device = await _lookup(credentials)
    except HTTPException:
        device = None
    if device is None or device.pop("revoked"):
        await websocket.accept()
        await websocket.close(code=WS_BAD_KEY, reason="invalid_token")
        return None
    if not robot_consent_current(await consent_service.get_state(device["u_id"])):
        await websocket.accept()
        await websocket.close(code=WS_NO_CONSENT, reason="consent_required")
        return None
    return device
