from app.database import get_pool
from app.services.line_service import LineService


async def check_refill_reminders():
    """
    Daily 8am: find medications with pills_remaining <= 7,
    send LINE alerts to family contacts.
    """
    pool = get_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT m.med_id, m.med_name, m.pills_remaining, m.u_id, u.name AS patient_name
            FROM medication m
            JOIN "user" u ON u.u_id = m.u_id
            WHERE m.is_active = TRUE AND m.pills_remaining <= 7
            """
        )

        if not rows:
            return

        line_svc = LineService.get_instance()

        for row in rows:
            family = await conn.fetch(
                """
                SELECT line_id FROM family_contacts
                WHERE u_id = $1 AND notify_missed = TRUE AND verified = TRUE
                """,
                row["u_id"],
            )

            for contact in family:
                if contact["line_id"]:
                    line_svc.send_text(
                        contact["line_id"],
                        f"💊 藥量提醒\n{row['patient_name']} 的 {row['med_name']} 只剩下 {row['pills_remaining']} 顆，請安排領藥或購買。",
                    )
