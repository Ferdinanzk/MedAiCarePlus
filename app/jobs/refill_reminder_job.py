from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import MEDCARE_TIMEZONE
from app.database import get_pool
from app.services import schedule
from app.services.line_service import LineService

REFILL_DAYS = 7          # warn when the stock lasts this many days or fewer
UNSCHEDULED_DOSES = 7    # as-needed medicines: warn when this many doses or fewer are left


def needs_refill(row, today) -> dict | None:
    """The supply estimate when this medication should be refilled, else None."""
    supply = schedule.supply(row["pills_remaining"], row["units_per_dose"], row["schedule_time"], today)
    if supply["days_left"] is not None:
        return supply if supply["days_left"] <= REFILL_DAYS else None
    if row["pills_remaining"] <= (row["units_per_dose"] or 1) * UNSCHEDULED_DOSES:
        return supply
    return None


def _amount(value) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


async def check_refill_reminders():
    """
    Daily 8am: find medications whose stock lasts REFILL_DAYS days or fewer at the
    scheduled rate (or, for unscheduled medicines, that have few doses left), and
    notify family contacts.
    """
    pool = get_pool()
    today = datetime.now(ZoneInfo(MEDCARE_TIMEZONE)).date()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT m.med_id, m.med_name, m.pills_remaining, m.units_per_dose, m.schedule_time,
                   m.u_id, u.name AS patient_name
            FROM medication m
            JOIN "user" u ON u.u_id = m.u_id
            WHERE m.is_active = TRUE
            """
        )

        due = [(row, supply) for row in rows if (supply := needs_refill(row, today)) is not None]
        if not due:
            return

        line_svc = LineService.get_instance()

        for row, supply in due:
            family = await conn.fetch(
                """
                SELECT line_id FROM family_contacts
                WHERE u_id = $1 AND notify_missed = TRUE AND verified = TRUE
                """,
                row["u_id"],
            )
            left = _amount(row["pills_remaining"])
            if supply["days_left"] is not None:
                text = (f"💊 藥量提醒\n{row['patient_name']} 的 {row['med_name']} 剩下 {left} 顆，"
                        f"約可再用 {supply['days_left']} 天（預計 {supply['run_out_date']} 用完），請安排領藥或購買。")
            else:
                text = (f"💊 藥量提醒\n{row['patient_name']} 的 {row['med_name']} 只剩下 {left} 顆，"
                        f"請安排領藥或購買。")
            for contact in family:
                if contact["line_id"]:
                    line_svc.send_text(contact["line_id"], text)
