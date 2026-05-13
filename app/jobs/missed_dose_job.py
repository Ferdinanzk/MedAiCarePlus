from datetime import datetime, timedelta
from app.database import get_pool
from app.services.line_service import LineService


async def check_missed_doses():
    """
    Every 15 minutes: find pending doses that are 30+ minutes past scheduled_time,
    mark them as 'missed', and send LINE alerts to family contacts.
    """
    pool = get_pool()
    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=30)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT il.id, il.patient_id, il.scheduled_time, m.name AS med_name,
                   p.name AS patient_name
            FROM intake_logs il
            JOIN medications m ON m.id = il.medication_id
            JOIN profiles p ON p.id = il.patient_id
            WHERE il.status = 'pending'
              AND il.scheduled_time < $1
              AND il.notified = FALSE
            """,
            cutoff,
        )

        if not rows:
            return

        line_svc = LineService.get_instance()

        for row in rows:
            await conn.execute(
                "UPDATE intake_logs SET status = 'missed', notified = TRUE, notified_at = NOW() WHERE id = $1",
                row["id"],
            )

            family = await conn.fetch(
                """
                SELECT line_id, name
                FROM family_contacts
                WHERE patient_id = $1 AND notify_missed = TRUE AND verified = TRUE
                """,
                row["patient_id"],
            )

            for contact in family:
                if contact["line_id"]:
                    line_svc.send_missed_dose_alert(
                        contact["line_id"],
                        row["patient_name"],
                        row["med_name"],
                        row["scheduled_time"].strftime("%Y-%m-%d %H:%M"),
                    )
