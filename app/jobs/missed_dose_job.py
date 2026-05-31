from datetime import datetime, timezone
from app.database import get_pool
from app.services.line_service import LineService

async def check_missed_doses():
    """
    Called every 1 minute.
    Handles:
    1. Upcoming reminders (remind_before_minutes before intake_time) -> category user.
    2. Overdue retry warnings (+10, +20, etc. mins after intake_time) -> category user.
    3. Final missed alerts (after max retries) -> category user & category family.
    """
    pool = get_pool()
    now = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT i.intk_id AS id, i.u_id, i.intake_time_stamp, i.reminder_sent, i.missed_reminders_sent,
                   m.med_name, u.name AS patient_name, u.line_id AS patient_line_id,
                   COALESCE(ns.remind_before_minutes, 5) AS remind_before_minutes,
                   COALESCE(ns.remind_after_minutes, 10) AS remind_after_minutes,
                   COALESCE(ns.remind_after_retries, 3) AS remind_after_retries,
                   COALESCE(ns.notify_family_on_missed, TRUE) AS notify_family_on_missed
            FROM intake i
            JOIN medication m ON m.med_id = i.med_id
            JOIN "user" u ON u.u_id = i.u_id
            LEFT JOIN notification_settings ns ON ns.u_id = i.u_id
            WHERE i.intake_stats = 'pending'
            """
        )

        if not rows:
            return

        line_svc = LineService.get_instance()

        for row in rows:
            u_id = row["u_id"]
            intk_id = row["id"]
            intake_time = row["intake_time_stamp"]
            if intake_time.tzinfo is None:
                intake_time = intake_time.replace(tzinfo=timezone.utc)

            # --- 1. Upcoming Reminder Check (e.g. 5m before) ---
            time_to_intake = (intake_time - now).total_seconds()
            remind_before_minutes = row["remind_before_minutes"]
            remind_before_secs = remind_before_minutes * 60

            if remind_before_minutes > 0 and not row["reminder_sent"] and 0 < time_to_intake <= remind_before_secs:
                if row["patient_line_id"]:
                    msg = f"🔔 用藥提醒\n您預定於 {intake_time.strftime('%H:%M')} 服用藥物：{row['med_name']}，請準時服用。"
                    line_svc.send_text(row["patient_line_id"], msg)

                    await conn.execute(
                        "INSERT INTO notification (u_id, category, type, message) VALUES ($1, 'user', 'upcoming_reminder', $2)",
                        u_id, f"Upcoming reminder sent for {row['med_name']}"
                    )
                await conn.execute("UPDATE intake SET reminder_sent = TRUE WHERE intk_id = $1", intk_id)

            # --- 2. Missed Warnings / Final Missed Alerts ---
            time_since_intake = (now - intake_time).total_seconds()
            if time_since_intake > 0:
                retries_sent = row["missed_reminders_sent"]
                retries_allowed = row["remind_after_retries"]
                remind_after_secs = row["remind_after_minutes"] * 60

                # Send intermediate warnings to the user
                if time_since_intake >= (retries_sent + 1) * remind_after_secs and retries_sent < retries_allowed:
                    time_passed_mins = int((retries_sent + 1) * row["remind_after_minutes"])
                    if row["patient_line_id"]:
                        msg = f"⚠️ 逾時用藥提醒\n您已逾時 {time_passed_mins} 分鐘未服用藥物：{row['med_name']}，請盡快服用。"
                        line_svc.send_text(row["patient_line_id"], msg)

                        await conn.execute(
                            "INSERT INTO notification (u_id, category, type, message) VALUES ($1, 'user', 'missed_reminder', $2)",
                            u_id, f"Missed warning ({time_passed_mins}m) sent for {row['med_name']}"
                        )
                    await conn.execute(
                        "UPDATE intake SET missed_reminders_sent = missed_reminders_sent + 1 WHERE intk_id = $1",
                        intk_id
                    )

                # Final missed alert: mark as missed, alert user + family
                if time_since_intake >= retries_allowed * remind_after_secs:
                    await conn.execute(
                        "UPDATE intake SET intake_stats = 'missed', notify_stats = 'sent' WHERE intk_id = $1",
                        intk_id
                    )

                    # Notify family contacts (category family)
                    if row["notify_family_on_missed"]:
                        family = await conn.fetch(
                            "SELECT line_id, name FROM family_contacts WHERE u_id = $1 AND notify_missed = TRUE AND verified = TRUE",
                            u_id
                        )
                        for contact in family:
                            if contact["line_id"]:
                                line_svc.send_missed_dose_alert(
                                    contact["line_id"],
                                    row["patient_name"],
                                    row["med_name"],
                                    intake_time.strftime("%Y-%m-%d %H:%M")
                                )
                                await conn.execute(
                                    "INSERT INTO notification (u_id, category, type, message) VALUES ($1, 'family', 'missed_alert', $2)",
                                    u_id, f"Missed alert for {row['med_name']} sent to family: {contact['name']}"
                                )

                    # Notify patient (category user)
                    if row["patient_line_id"]:
                        msg = f"❌ 用藥未完成\n您已錯過預定用藥\n藥物: {row['med_name']}\n時間: {intake_time.strftime('%Y-%m-%d %H:%M')}\n系統已通知您的家人聯絡人。"
                        line_svc.send_text(row["patient_line_id"], msg)
                        await conn.execute(
                            "INSERT INTO notification (u_id, category, type, message) VALUES ($1, 'user', 'missed_alert', $2)",
                            u_id, f"Missed dose final alert logged for {row['med_name']}"
                        )
