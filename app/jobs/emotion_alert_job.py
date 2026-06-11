from datetime import datetime, timedelta, timezone
from app.database import get_pool
from app.services.line_service import LineService

# Minimum model confidence for a Sad/Angry reading to alert family. The emotion
# model's real-world Sad/Angry scores top out around ~0.45 (the original 0.6 was
# calibrated against a dev set that produced 0.6–0.9), so 0.6 was unreachable and
# the alert NEVER fired in production. Lowered to 0.4 to match the model's actual
# output. Treat this as a model-dependent hyperparameter: re-calibrate whenever
# the emotion model / its input pipeline changes. See bug note
# 60-bug-fixes/2026-06-11_medaicareplus_emotion-alert-threshold-too-high.
EMOTION_ALERT_MIN_SCORE = 0.4


async def check_negative_emotions():
    """
    Check recent emotion logs. If Sad or Angry detected with score >=
    EMOTION_ALERT_MIN_SCORE, send LINE alerts to family contacts with
    notify_emotion=TRUE. Run every 30 minutes via scheduler.
    """
    pool = get_pool()
    now = datetime.now(timezone.utc)
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
              AND e.emotion_score >= $2
            """,
            cutoff, EMOTION_ALERT_MIN_SCORE,
        )

        if not rows:
            # Observability: if Sad/Angry readings exist in the window but all fell
            # below the threshold, log it so a too-high threshold can't silently
            # disable the whole feature (this is exactly how the 0.6 bug hid).
            below = await conn.fetchval(
                "SELECT COUNT(*) FROM emotion WHERE emotion_type IN ('Sad','Angry') "
                "AND time_stamp >= $1 AND emotion_score < $2",
                cutoff, EMOTION_ALERT_MIN_SCORE,
            )
            if below:
                print(
                    f"[emotion_alert] {below} Sad/Angry reading(s) in the last 30min "
                    f"below threshold {EMOTION_ALERT_MIN_SCORE}; no family alert sent."
                )
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
