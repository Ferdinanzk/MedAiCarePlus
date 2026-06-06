from datetime import datetime, timedelta
from app.database import get_pool
from app.services.line_service import LineService


async def check_negative_emotions():
    """
    Check recent emotion logs. If Sad or Angry detected with score >= 0.6,
    send LINE alerts to family contacts with notify_emotion=TRUE.
    Run every 30 minutes via scheduler.
    """
    pool = get_pool()
    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=30)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.emot_id, e.u_id, e.emotion_type, e.emotion_score,
                   u.name AS patient_name, u.line_id AS patient_line_id
            FROM emotion e
            JOIN "user" u ON u.u_id = e.u_id
            WHERE e.emotion_type IN ('Sad', 'Angry')
              AND e.time_stamp >= $1
              AND e.emotion_score >= 0.6
            """,
            cutoff,
        )

        if not rows:
            return

        line_svc = LineService.get_instance()

        for row in rows:
            # Query the user's notification preferences
            prefs = await conn.fetchrow(
                "SELECT notify_family_on_bad_mood FROM notification_settings WHERE u_id = $1",
                row["u_id"]
            )
            notify_family = prefs["notify_family_on_bad_mood"] if prefs else True

            # Notify family contacts (category family)
            if notify_family:
                family = await conn.fetch(
                    """
                    SELECT line_id, name
                    FROM family_contacts
                    WHERE u_id = $1 AND notify_emotion = TRUE AND verified = TRUE
                      AND relationship IS DISTINCT FROM 'user'
                    """,
                    row["u_id"],
                )

                for contact in family:
                    if contact["line_id"]:
                        line_svc.send_emotion_alert(
                            contact["line_id"],
                            row["patient_name"],
                            row["emotion_type"],
                            row["emotion_score"],
                        )
                        await conn.execute(
                            "INSERT INTO notification (u_id, category, type, message) VALUES ($1, 'family', 'emotion_alert', $2)",
                            row["u_id"], f"Emotion alert ({row['emotion_type']}) sent to family: {contact['name']}"
                        )

            # Also notify patient themselves (category user)
            if row["patient_line_id"]:
                line_svc.send_text(
                    row["patient_line_id"],
                    (
                        f"💙 情緒提醒\n"
                        f"我們檢測到您今天的情緒較低落 ({row['emotion_type']})。\n"
                        f"請多加關注自己的身心狀況，適時休息或與家人聊聊。"
                    ),
                )
                await conn.execute(
                    "INSERT INTO notification (u_id, category, type, message) VALUES ($1, 'user', 'emotion_alert', $2)",
                    row["u_id"], f"Negative emotion alert ({row['emotion_type']}) logged for user"
                )
