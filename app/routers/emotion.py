import asyncio
import json
import cv2
import numpy as np
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from app.database import get_pool
from app.services.emotion_service import EmotionService
from app.routers.auth import current_user

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def emotion_page(request: Request):
    user = current_user(request)
    if not user:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/auth/login")
    return templates.TemplateResponse("emotion.html", {"request": request, "user": user})


@router.get("/history")
async def emotion_history(request: Request, limit: int = 20):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT emot_id, emotion_type, emotion_score, note, time_stamp "
            "FROM emotion WHERE u_id=$1 ORDER BY time_stamp DESC LIMIT $2",
            user["u_id"], limit
        )
    return [dict(r) for r in rows]


@router.post("/save")
async def save_emotion(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    body = await request.json()
    emotion_type = body.get("emotion_type")
    emotion_score = float(body.get("emotion_score", 0))
    note = body.get("note")
    if emotion_type not in ("Angry", "Happy", "Neutral", "Sad"):
        return JSONResponse({"error": "Invalid emotion_type"}, status_code=400)
    pool = get_pool()
    async with pool.acquire() as conn:
        emot_id = await conn.fetchval(
            "INSERT INTO emotion (u_id, emotion_type, emotion_score, note) "
            "VALUES ($1,$2,$3,$4) RETURNING emot_id",
            user["u_id"], emotion_type, emotion_score, note
        )
    return {"emot_id": emot_id}


@router.websocket("/ws/emotion")
async def ws_emotion(websocket: WebSocket):
    await websocket.accept()
    svc = EmotionService.get_instance()
    loop = asyncio.get_event_loop()
    try:
        while True:
            data = await websocket.receive()
            if "bytes" in data and data["bytes"]:
                nparr = np.frombuffer(data["bytes"], np.uint8)
                frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                if frame is not None:
                    result = await loop.run_in_executor(None, svc.predict_frame, frame)
                    await websocket.send_text(json.dumps(result))
            elif "text" in data:
                msg = json.loads(data["text"])
                if msg.get("action") == "ping":
                    await websocket.send_text(json.dumps({"action": "pong"}))
    except WebSocketDisconnect:
        pass
