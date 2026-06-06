from collections import defaultdict
from datetime import datetime, timedelta, timezone
from app.database import get_pool
from app.services.line_service import LineService
# Reuse the same slot-bucketing + list-rendering the missed-dose job uses so a
# "taken" confirmation groups exactly like a missed alert (one message per slot).
from app.jobs.missed_dose_job import _slot_key, _format_med_list

# A family "taken" batch is flushed once this many minutes have elapsed since the
# earliest still-unconfirmed taken pill in a slot — so pills taken close together
# (it takes time to swallow several pills) collapse into ONE message, and later
# takes start the next batch.
BATCH_WINDOW_MINUTES = 5

# Only confirm recently-taken pills. Prevents a flood of confirmations for
# historical 'taken' rows the first time taken_notified is introduced, and keeps
# the working set small. Also makes the job restart-safe (taken_notified persists).
RECENCY_HOURS = 2


async def check_taken_confirmations():
    """
    Called every 1 minute (self-gates on the 5-minute batch window).

    Mirrors the missed-dose job but for SUCCESS: as the patient works through a
    slot's pills, the successfully-taken ones are batched and pushed to family as
    a single grouped message — not one per pill. Pills that are never taken are
    handled by the missed-dose job; once every pill in a slot is taken, there is
    nothing left to confirm and the batching stops on its own.
    """
    pool = get_pool()
    now = datetime.now(timezone.utc)
    recency_cutoff = now - timedelta(hours=RECENCY_HOURS)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT i.intk_id AS id, i.u_id, i.intake_time_stamp, i.actual_intake_time,
                   m.med_name, u.name AS patient_name,
                   COALESCE(ns.notify_family_on_taken, TRUE) AS notify_family_on_taken
            FROM intake i
            JOIN medication m ON m.med_id = i.med_id
            JOIN "user" u ON u.u_id = i.u_id
            LEFT JOIN notification_settings ns ON ns.u_id = i.u_id
            WHERE i.intake_stats = 'taken'
              AND i.taken_notified = FALSE
              AND i.actual_intake_time IS NOT NULL
              AND i.actual_intake_time >= $1
            """,
            recency_cutoff,
        )

        if not rows:
            return

        line_svc = LineService.get_instance()

        # Group taken-but-unconfirmed intakes by (u_id, 5-minute scheduled slot).
        groups: dict[tuple, list] = defaultdict(list)
        for r in rows:
            slot = _slot_key(r["intake_time_stamp"])
            groups[(r["u_id"], slot)].append(r)

        # Fetch verified family contacts (notify_taken=TRUE) once per user.
        family_cache: dict[int, list] = {}

        for (u_id, slot_time), group_rows in groups.items():
            intk_ids = [r["id"] for r in group_rows]
            first = group_rows[0]

            # Master toggle off: mark confirmed (so we stop re-querying) and skip.
            if not first["notify_family_on_taken"]:
                await conn.execute(
                    "UPDATE intake SET taken_notified = TRUE WHERE intk_id = ANY($1::int[])",
                    intk_ids,
                )
                continue

            # 5-minute batch gate: wait until the window has elapsed since the
            # earliest unconfirmed taken pill in this slot, so more pills taken in
            # the same session join the same batch.
            earliest = min(r["actual_intake_time"] for r in group_rows)
            if (now - earliest).total_seconds() < BATCH_WINDOW_MINUTES * 60:
                continue

            med_names = [r["med_name"] for r in group_rows]
            slot_label = slot_time.strftime("%H:%M")
            med_list_str = _format_med_list(med_names)

            if u_id not in family_cache:
                family_cache[u_id] = await conn.fetch(
                    "SELECT line_id, name FROM family_contacts "
                    "WHERE u_id = $1 AND notify_taken = TRUE AND verified = TRUE "
                    "AND relationship IS DISTINCT FROM 'user'",
                    u_id,
                )

            for contact in family_cache[u_id]:
                if contact["line_id"]:
                    line_svc.send_taken_confirmation(
                        contact["line_id"],
                        first["patient_name"],
                        med_list_str,
                        slot_label,
                    )
                    await conn.execute(
                        "INSERT INTO notification (u_id, category, type, message) "
                        "VALUES ($1, 'family', 'taken_confirmation', $2)",
                        u_id,
                        f"Taken confirmation for {slot_label} slot ({len(med_names)} med(s)) "
                        f"sent to family: {contact['name']}",
                    )

            await conn.execute(
                "UPDATE intake SET taken_notified = TRUE WHERE intk_id = ANY($1::int[])",
                intk_ids,
            )
