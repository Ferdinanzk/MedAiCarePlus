import random
import string
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from app.dependencies import get_current_user
from app.services.line_service import LineService
from app.database import get_pool

router = APIRouter(prefix="/api/notify", tags=["notify-api"])


def _generate_code(length: int = 6) -> str:
    """Generate a random alphanumeric verification code."""
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


@router.post("/generate-code")
async def generate_verification_code(
    contact_id: int,
    user: dict = Depends(get_current_user),
):
    """Generate a verification code for a family contact."""
    code = _generate_code()
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE family_contacts SET verification_code = $1, verified = FALSE WHERE id = $2 AND patient_id = $3",
            code, contact_id, user["sub"],
        )
    return {"code": code}


@router.post("/webhook/line")
async def line_webhook(request: Request):
    """
    Handle incoming messages from LINE bot.
    Caregivers send verification codes here to link their LINE user_id.
    """
    body = await request.json()
    events = body.get("events", [])

    pool = get_pool()
    line_svc = LineService.get_instance()

    for event in events:
        if event.get("type") != "message":
            continue

        msg = event["message"]
        if msg.get("type") != "text":
            continue

        text = msg.get("text", "").strip().upper()
        line_user_id = event["source"].get("userId", "")

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, patient_id, name
                FROM family_contacts
                WHERE verification_code = $1
                """,
                text,
            )

            if row:
                await conn.execute(
                    """
                    UPDATE family_contacts
                    SET line_id = $1, verified = TRUE, verified_at = NOW(), verification_code = NULL
                    WHERE id = $2
                    """,
                    line_user_id, row["id"],
                )

                patient = await conn.fetchrow(
                    "SELECT name FROM profiles WHERE id = $1", row["patient_id"]
                )

                line_svc.send_verification_success(
                    line_user_id,
                    patient["name"] if patient else "Patient",
                )
            else:
                line_svc.send_text(
                    line_user_id,
                    "驗證碼無效，請確認後重新輸入。/ Invalid verification code.",
                )

    return {"status": "ok"}


@router.post("/missed-dose")
async def notify_missed_dose(
    line_id: str,
    patient_name: str,
    medication_name: str,
    scheduled_time: str,
    user: dict = Depends(get_current_user),
):
    """Send a missed-dose alert to a family contact's LINE."""
    svc = LineService.get_instance()
    result = svc.send_missed_dose_alert(line_id, patient_name, medication_name, scheduled_time)
    return result


@router.post("/emotion-alert")
async def notify_emotion_alert(
    line_id: str,
    patient_name: str,
    emotion: str,
    score: float,
    user: dict = Depends(get_current_user),
):
    """Send a low-emotion alert to a family contact's LINE."""
    svc = LineService.get_instance()
    result = svc.send_emotion_alert(line_id, patient_name, emotion, score)
    return result


@router.post("/weekly-summary")
async def notify_weekly_summary(
    line_id: str,
    patient_name: str,
    adherence: float,
    emotion_summary: str,
    user: dict = Depends(get_current_user),
):
    """Send a weekly health summary to a family contact's LINE."""
    svc = LineService.get_instance()
    result = svc.send_weekly_summary(line_id, patient_name, adherence, emotion_summary)
    return result


@router.get("/status")
async def notify_status(user: dict = Depends(get_current_user)):
    """Check if LINE notifications are configured."""
    return {
        "configured": LineService._available,
        "channel_token_set": bool(LineService.get_instance()),
    }
