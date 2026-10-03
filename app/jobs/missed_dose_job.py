import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from app.config import MEDCARE_TIMEZONE
from app.database import get_pool
from app.services import dose_safety, schedule
from app.services.line_service import LineService
from app.services.reachy_tasks import enqueue_reachy_task

log = logging.getLogger(__name__)
_TZ = ZoneInfo(MEDCARE_TIMEZONE)

# Group size for intake time-slot bucketing. Intakes whose intake_time_stamp
# falls within the same 5-minute window for the same user are treated as one
# "slot" and a single LINE message is sent for the whole slot (per phase).
SLOT_WINDOW_MINUTES = 5


def _slot_key(intake_time: datetime) -> datetime:
    """Truncate an intake timestamp to the nearest SLOT_WINDOW_MINUTES boundary.

    All intakes within the same window for the same user get grouped together
    so a single LINE notification covers every pill in that meal/slot instead
    of one notification per pill.
    """
    minute = (intake_time.minute // SLOT_WINDOW_MINUTES) * SLOT_WINDOW_MINUTES
    return intake_time.replace(minute=minute, second=0, microsecond=0)


def _format_med_list(med_names: list[str]) -> str:
    """Render a numbered list of medication names for a grouped message."""
    if len(med_names) == 1:
        return med_names[0]
    return "\n".join(f"{i + 1}. {name}" for i, name in enumerate(med_names))


def _reminder_text(head: str, intro: str, closing: str, names: list[str], held: list[tuple]) -> str:
    """A patient reminder that asks for the doses overdose protection allows (`names`) and, for each dose it holds
    back (`held`: (name, refusal)), says why instead: never "please take it" for a dose the app and the robot would
    refuse, such as one already missed past halfway to the next ("don't make it up")."""
    lines = [head]
    if names:
        lines += [intro, _format_med_list(names), closing]
    lines += [f"⚠️ {name}：{dose_safety.reply_text(refused, 'zh-TW')}" for name, refused in held]
    return "\n".join(lines)


async def check_missed_doses():
    """
    Called every 1 minute.
    Handles (grouped by (u_id, 5-minute time-slot) so multiple pills in the
    same slot produce ONE notification, not one per pill):
    1. Upcoming reminders (remind_before_minutes before slot_time) -> category user.
    2. Overdue retry warnings (+10, +20, etc. mins after slot_time) -> category user.
    3. Final missed alerts (after max retries) -> category user & category family.
    """
    pool = get_pool()
    now = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT i.intk_id AS id, i.u_id, i.intake_time_stamp,
                   i.reminder_sent,
                   i.missed_reminders_sent, m.med_id, m.med_name,
                   u.name AS patient_name, u.line_id AS patient_line_id,
                   COALESCE(ns.remind_before_minutes, 5) AS remind_before_minutes,
                   COALESCE(ns.remind_after_minutes, 10) AS remind_after_minutes,
                   COALESCE(ns.remind_after_retries, 3) AS remind_after_retries,
                   COALESCE(ns.notify_family_on_missed, TRUE) AS notify_family_on_missed
            FROM intake i
            JOIN medication m ON m.med_id = i.med_id
            JOIN "user" u ON u.u_id = i.u_id
            LEFT JOIN notification_settings ns ON ns.u_id = i.u_id
            WHERE i.intake_stats = 'pending' AND m.is_active = TRUE
            """
        )

        if not rows:
            return

        line_svc = LineService.get_instance()

        # --- Group intakes by (u_id, 5-minute slot) ---
        # For each group we track:
        #   - the list of rows in the group
        #   - the bucket's med_id list (for the grouped message)
        #   - aggregated reminder_sent / missed_reminders_sent flags
        groups: dict[tuple, list] = defaultdict(list)
        for r in rows:
            slot = _slot_key(r["intake_time_stamp"])
            groups[(r["u_id"], slot)].append(r)

        # Cache verified family contacts per user so we only fetch them once
        # (per group) instead of once per intake.
        family_cache: dict[int, list] = {}

        for (u_id, slot_time), group_rows in groups.items():
            intk_ids = [r["id"] for r in group_rows]
            med_names = [r["med_name"] for r in group_rows]
            # The patient's clock: the database hands times back in UTC ("14:00" was sent for the 22:00 dose).
            local_slot = slot_time.astimezone(_TZ)
            slot_label = local_slot.strftime("%H:%M")
            med_list_str = _format_med_list(med_names)

            # The per-group settings (assume they are uniform across the group
            # since the group is one user; just read from the first row).
            first = group_rows[0]
            remind_before_minutes = first["remind_before_minutes"]
            remind_before_secs = remind_before_minutes * 60
            remind_after_minutes = first["remind_after_minutes"]
            remind_after_secs = remind_after_minutes * 60
            remind_after_retries = first["remind_after_retries"]
            notify_family_on_missed = first["notify_family_on_missed"]
            patient_line_id = first["patient_line_id"]
            patient_name = first["patient_name"]

            # Group-level reminder state: a reminder has been sent for the
            # group if it has been sent for EVERY intake in the group.
            all_reminder_sent = all(r["reminder_sent"] for r in group_rows)
            # Group-level retry counter: take the max across the group so
            # the group advances in lockstep through retries.
            group_retry_count = max(r["missed_reminders_sent"] for r in group_rows)

            time_to_slot = (slot_time - now).total_seconds()
            time_since_slot = (now - slot_time).total_seconds()
            # A Reachy task for this slot stays useful until the missed window ends.
            task_expires_at = slot_time + timedelta(minutes=remind_after_minutes * (remind_after_retries + 1))
            # Overdose protection, judged on this run's clock when a reminder goes out: the robot is sent for the
            # slot's doses it allows (the others' medicines don't keep it from the rest), and the patient's LINE
            # reminder asks only for those, saying why for each one held back. A remind-before longer than
            # DOSE_EARLY_MINUTES (possible only through the API) holds every dose back as not due: the LINE reminder
            # goes alone and the robot comes with the first overdue retry. The same holds for a dose only minutes
            # after the same medicine's previous one (due from halfway between them), one too soon after a dose taken
            # late, one over its daily maximum, and one missed past halfway to the next (never made up).
            async def protection_verdicts() -> tuple[list[int], list[str], list[tuple]]:
                verdicts = await dose_safety.verdicts(conn, u_id, intk_ids, at=now)
                allowed = [r["id"] for r in group_rows if verdicts.get(r["id"]) is None]
                names = [r["med_name"] for r in group_rows if r["id"] in allowed]
                held = [(r["med_name"], verdicts[r["id"]]) for r in group_rows if verdicts.get(r["id"]) is not None]
                return allowed, names, held

            async def send_robot(allowed: list[int], reason: str) -> None:
                # In a savepoint: should a dose be refused after all (changed since the verdicts), the reminder flag
                # still commits and the rest of this run goes on; the robot is simply not sent.
                if not allowed:
                    return
                try:
                    async with conn.transaction():
                        await enqueue_reachy_task(conn, u_id, slot_time, allowed, reason, task_expires_at, at=now)
                except schedule.DoseRefused as refused:
                    log.info("no robot for the %s slot of patient %s: dose %s refused (%s)", slot_label, u_id,
                             refused.intk_id, refused.detail)

            # --- 1. Upcoming Reminder (one message per slot) ---
            if (
                remind_before_minutes > 0
                and not all_reminder_sent
                and 0 < time_to_slot <= remind_before_secs
            ):
                allowed, names, held = await protection_verdicts()
                if patient_line_id:
                    msg = _reminder_text("🔔 用藥提醒", f"您預定於 {slot_label} 服用以下藥物：", "請準時服用。",
                                         names, held)
                    line_svc.send_text(patient_line_id, msg)
                    await conn.execute(
                        "INSERT INTO notification (u_id, category, type, message) "
                        "VALUES ($1, 'user', 'upcoming_reminder', $2)",
                        u_id,
                        f"Upcoming reminder for {slot_label} slot ({len(med_names)} med(s)): {', '.join(med_names)}",
                    )
                # The reminder flag and the robot task commit together.
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE intake SET reminder_sent = TRUE WHERE intk_id = ANY($1::int[])",
                        intk_ids,
                    )
                    await send_robot(allowed, "upcoming")

            # --- 2. Missed Warnings (one message per retry tick per slot) ---
            if time_since_slot > 0:
                if (
                    time_since_slot >= (group_retry_count + 1) * remind_after_secs
                    and group_retry_count < remind_after_retries
                ):
                    time_passed_mins = int((group_retry_count + 1) * remind_after_minutes)
                    allowed, names, held = await protection_verdicts()
                    if patient_line_id:
                        msg = _reminder_text("⚠️ 逾時用藥提醒", f"您已逾時 {time_passed_mins} 分鐘未服用以下藥物：",
                                             "請盡快服用。", names, held)
                        line_svc.send_text(patient_line_id, msg)
                        await conn.execute(
                            "INSERT INTO notification (u_id, category, type, message) "
                            "VALUES ($1, 'user', 'missed_reminder', $2)",
                            u_id,
                            f"Missed warning ({time_passed_mins}m) for {slot_label} slot ({len(med_names)} med(s)): {', '.join(med_names)}",
                        )
                    async with conn.transaction():
                        await conn.execute(
                            "UPDATE intake SET missed_reminders_sent = missed_reminders_sent + 1 "
                            "WHERE intk_id = ANY($1::int[])",
                            intk_ids,
                        )
                        await send_robot(allowed, "missed_retry")

                # --- 3. Final Missed Alert (one per slot per recipient) ---
                if time_since_slot >= remind_after_retries * remind_after_secs:
                    await conn.execute(
                        "UPDATE intake SET intake_stats = 'missed', notify_stats = 'sent' "
                        "WHERE intk_id = ANY($1::int[])",
                        intk_ids,
                    )

                    # Notify family contacts (one message per family member, listing
                    # all missed meds).
                    if notify_family_on_missed:
                        if u_id not in family_cache:
                            family_cache[u_id] = await conn.fetch(
                                "SELECT line_id, name FROM family_contacts "
                                "WHERE u_id = $1 AND notify_missed = TRUE AND verified = TRUE "
                                "AND relationship IS DISTINCT FROM 'user'",
                                u_id,
                            )
                        for contact in family_cache[u_id]:
                            if contact["line_id"]:
                                line_svc.send_missed_dose_alert(
                                    contact["line_id"],
                                    patient_name,
                                    med_list_str,  # group: comma-joined med names
                                    local_slot.strftime("%Y-%m-%d %H:%M"),
                                )
                                await conn.execute(
                                    "INSERT INTO notification (u_id, category, type, message) "
                                    "VALUES ($1, 'family', 'missed_alert', $2)",
                                    u_id,
                                    f"Missed alert for {slot_label} slot ({len(med_names)} med(s)) sent to family: {contact['name']}",
                                )

                    # Notify patient (one message, listing all missed meds).
                    if patient_line_id:
                        msg = (
                            f"❌ 用藥未完成\n"
                            f"您已錯過 {slot_label} 的預定用藥\n"
                            f"藥物：\n{med_list_str}\n"
                            f"時間：{local_slot.strftime('%Y-%m-%d %H:%M')}\n"
                            f"系統已通知您的家人聯絡人。"
                        )
                        line_svc.send_text(patient_line_id, msg)
                        await conn.execute(
                            "INSERT INTO notification (u_id, category, type, message) "
                            "VALUES ($1, 'user', 'missed_alert', $2)",
                            u_id,
                            f"Missed dose final alert for {slot_label} slot ({len(med_names)} med(s)): {', '.join(med_names)}",
                        )
