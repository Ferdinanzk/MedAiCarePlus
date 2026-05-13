from datetime import datetime, timedelta
from app.database import get_pool
from app.services.line_service import LineService


async def send_weekly_summaries():
    """
    Sundays 9am: calculate weekly adherence and send summary to family contacts.
    """
    pool = get_pool()
    now = datetime.utcnow()
    week_ago = now - timedelta(days=7)

    async with pool.acquire() as conn:
        patients = await conn.fetch(
            "SELECT id, name FROM profiles"
        )

        line_svc = LineService.get_instance()

        for patient in patients:
            rows = await conn.fetch(
                """
                SELECT status FROM intake_logs
                WHERE patient_id = $1 AND scheduled_time >= $2
                """,
                patient["id"], week_ago,
            )

            total = len(rows)
            taken = sum(1 for r in rows if r["status"] == "taken")
            adherence = (taken / total * 100) if total > 0 else 0

            emotion_rows = await conn.fetch(
                """
                SELECT emotion_type FROM emotion_logs
                WHERE patient_id = $1 AND recorded_at >= $2
                ORDER BY recorded_at DESC LIMIT 7
                """,
                patient["id"], week_ago,
            )

            emotion_summary = "穩定" if not emotion_rows else f"{len(emotion_rows)} 次記錄"

            family = await conn.fetch(
                """
                SELECT line_id FROM family_contacts
                WHERE patient_id = $1 AND notify_weekly = TRUE AND verified = TRUE
                """,
                patient["id"],
            )

            for contact in family:
                if contact["line_id"]:
                    line_svc.send_weekly_summary(
                        contact["line_id"],
                        patient["name"],
                        adherence,
                        emotion_summary,
                    )
