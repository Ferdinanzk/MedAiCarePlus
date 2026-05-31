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
            SELECT i.intk_id AS id, i.u_id, i.intake_time_stamp AS scheduled_time,
                   m.med_name, u.name AS patient_name
            FROM intake i
            JOIN medication m ON m.med_id = i.med_id
            JOIN "user" u ON u.u_id = i.u_id
            WHERE i.intake_stats = 'pending'
              AND i.intake_time_stamp < $1
              AND i.notify_stats = 'pending'
            """,
            cutoff,
        )

        if not rows:
            return

        line_svc = LineService.get_instance()

        for row in rows:
            await conn.execute(
                "UPDATE intake SET intake_stats = 'missed', notify_stats = 'sent' WHERE intk_id = $1",
                row["id"],
            )

            family = await conn.fetch(
                """
                SELECT line_id, name
                FROM family_contacts
                WHERE u_id = $1 AND notify_missed = TRUE AND verified = TRUE
                """,
                row["u_id"],
            )

            for contact in family:
                if contact["line_id"]:
                    line_svc.send_missed_dose_alert(
                        contact["line_id"],
                        row["patient_name"],
                        row["med_name"],
                        row["scheduled_time"].strftime("%Y-%m-%d %H:%M"),
                    )

            # Also notify patient themselves
            patient_line_id = await conn.fetchval(
                'SELECT line_id FROM "user" WHERE u_id = $1',
                row["u_id"],
            )
            if patient_line_id:
                line_svc.send_text(
                    patient_line_id,
                    (
                        f"⚠️ 用藥提醒\n"
                        f"您錯過了預定用藥\n"
                        f"藥物: {row['med_name']}\n"
                        f"預定時間: {row['scheduled_time'].strftime('%Y-%m-%d %H:%M')}\n"
                        f"請盡快補服或聯繫家人。"
                    ),
                )
