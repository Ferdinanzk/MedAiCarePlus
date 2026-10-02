from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import MEDCARE_TIMEZONE
from app.database import get_pool
from app.services import adherence as adherence_rules
from app.services.line_service import LineService


async def send_weekly_summaries():
    """
    Sundays 9am: calculate the past week's adherence and send summary to family contacts.
    Only doses that were already due count (adherence.daily_counts); future doses never do.
    """
    pool = get_pool()
    now = datetime.now(ZoneInfo(MEDCARE_TIMEZONE))
    week_ago = now - timedelta(days=7)

    async with pool.acquire() as conn:
        patients = await conn.fetch(
            'SELECT u_id AS id, name FROM "user"'
        )

        line_svc = LineService.get_instance()

        for patient in patients:
            # Only verified doses count: 'pending_confirmation' (awaiting a
            # caregiver's answer) is not taken for adherence.
            week = adherence_rules.totals(
                await adherence_rules.daily_counts(conn, patient["id"], week_ago, now, now))
            adherence = week["adherence"] or 0

            emotion_rows = await conn.fetch(
                """
                SELECT emotion_type FROM emotion
                WHERE u_id = $1 AND time_stamp >= $2
                ORDER BY time_stamp DESC LIMIT 7
                """,
                patient["id"], week_ago,
            )

            emotion_summary = "穩定" if not emotion_rows else f"{len(emotion_rows)} 次記錄"

            family = await conn.fetch(
                """
                SELECT line_id FROM family_contacts
                WHERE u_id = $1 AND notify_weekly = TRUE AND verified = TRUE
                  AND relationship IS DISTINCT FROM 'user'
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
