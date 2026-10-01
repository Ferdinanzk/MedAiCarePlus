"""Single transaction for medication status, stock, emotion and monitor evidence."""

import json

from app.database import get_pool
from app.services.emotion_service import LABELS


async def commit_monitored(state, candidate: dict, method: str) -> dict:
    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT intk_id, med_id, intake_stats FROM intake WHERE intk_id=$1 AND u_id=$2 FOR UPDATE",
            state.intk_id, state.u_id)
        if not row or row["intake_stats"] not in ("pending", "missed"):
            # A retried request can arrive after its first transaction has
            # committed. Recheck the event after taking the intake row lock so
            # the retry is idempotent instead of reporting a false stock or
            # status conflict.
            existing = await conn.fetchrow(
                "SELECT event_id, outcome FROM monitor_event WHERE event_id=$1::uuid AND u_id=$2 FOR UPDATE",
                candidate["event_id"], state.u_id)
            if existing:
                return {"event_id": str(existing["event_id"]), "status": existing["outcome"], "already_recorded": True}
            raise ValueError("Selected dose is no longer available")
        existing = await conn.fetchrow(
            "SELECT event_id, outcome FROM monitor_event WHERE event_id=$1::uuid AND u_id=$2 FOR UPDATE",
            candidate["event_id"], state.u_id)
        if existing:
            return {"event_id": str(existing["event_id"]), "status": existing["outcome"], "already_recorded": True}
        stock = await conn.fetchval(
            "UPDATE medication SET pills_remaining=pills_remaining-1 "
            "WHERE med_id=$1 AND u_id=$2 AND pills_remaining>0 RETURNING pills_remaining",
            row["med_id"], state.u_id)
        if stock is None:
            raise ValueError("No pills remain for this medication")
        probabilities = candidate.get("emotion_probabilities")
        emot_id = None
        if probabilities:
            label = max(LABELS, key=lambda name: probabilities[name])
            emot_id = await conn.fetchval(
                "INSERT INTO emotion (u_id, emotion_type, emotion_score, context) "
                "VALUES ($1,$2,$3,'during_ingestion') RETURNING emot_id",
                state.u_id, label.capitalize(), float(probabilities[label]))
        await conn.execute(
            "UPDATE intake SET intake_stats='taken', actual_intake_time=NOW(), "
            "detection_confidence=$1, detection_method=$2, emot_id=$3 WHERE intk_id=$4",
            candidate["confidence"], method, emot_id, state.intk_id)
        await conn.execute(
            "INSERT INTO monitor_event (event_id, session_id, u_id, intk_id, previous_status, "
            "identity_distance, detector_score, detector_band, emotion_probabilities, outcome) "
            "VALUES ($1::uuid,$2::uuid,$3,$4,$5,$6,$7,$8,$9::jsonb,'taken')",
            candidate["event_id"], state.session_id, state.u_id, state.intk_id,
            row["intake_stats"], candidate.get("identity_distance"), candidate["confidence"],
            candidate["decision"], json.dumps(probabilities) if probabilities else None)
        return {"event_id": candidate["event_id"], "status": "taken", "emotion_id": emot_id,
                "pills_remaining": stock, "already_recorded": False}


async def undo_monitored(state, event_id: str) -> dict:
    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        event = await conn.fetchrow(
            "SELECT event_id, outcome, previous_status, intk_id FROM monitor_event "
            "WHERE event_id=$1::uuid AND u_id=$2 "
            "AND ($3::uuid IS NULL OR session_id=$3::uuid) FOR UPDATE",
            event_id, state.u_id, state.session_id)
        if not event:
            raise ValueError("Event was not found")
        if event["outcome"] == "rejected":
            return {"event_id": event_id, "status": "rejected", "already_corrected": True}
        row = await conn.fetchrow(
            "SELECT med_id, intake_stats FROM intake WHERE intk_id=$1 AND u_id=$2 FOR UPDATE",
            event["intk_id"], state.u_id)
        if not row or row["intake_stats"] != "taken":
            raise ValueError("Dose status changed after this event")
        await conn.execute(
            "UPDATE intake SET intake_stats=$1, actual_intake_time=NULL, emot_id=NULL, "
            "detection_confidence=NULL, detection_method=NULL, taken_notified=FALSE WHERE intk_id=$2",
            event["previous_status"], event["intk_id"])
        await conn.execute("UPDATE medication SET pills_remaining=pills_remaining+1 WHERE med_id=$1", row["med_id"])
        await conn.execute("UPDATE monitor_event SET outcome='rejected', corrected_at=NOW() WHERE event_id=$1::uuid",
                           event_id)
        return {"event_id": event_id, "status": "rejected", "already_corrected": False}


async def transition_intake(u_id: int, intk_id: int, new_status: str, *, method: str = "manual") -> dict:
    if new_status not in ("taken", "skipped", "missed", "pending"):
        raise ValueError("Invalid intake status")
    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT intk_id, med_id, intake_stats FROM intake WHERE intk_id=$1 AND u_id=$2 FOR UPDATE",
            intk_id, u_id)
        if not row:
            raise ValueError("Intake record not found")
        previous = row["intake_stats"]
        # Checked under the row lock: a dose waiting for a caregiver's LINE answer
        # is settled only by that answer (or its expiry), never by a manual edit.
        if previous == "pending_confirmation":
            raise ValueError("awaiting_caregiver_confirmation")
        if previous == new_status:
            return {"intk_id": intk_id, "status": new_status, "changed": False}
        if new_status == "taken":
            stock = await conn.fetchval(
                "UPDATE medication SET pills_remaining=pills_remaining-1 WHERE med_id=$1 "
                "AND u_id=$2 AND pills_remaining>0 RETURNING pills_remaining", row["med_id"], u_id)
            if stock is None:
                raise ValueError("No pills remain for this medication")
        elif previous == "taken":
            await conn.execute("UPDATE medication SET pills_remaining=pills_remaining+1 WHERE med_id=$1", row["med_id"])
        await conn.execute(
            "UPDATE intake SET intake_stats=$1::varchar, "
            "actual_intake_time=CASE WHEN $1::varchar='taken' THEN NOW() ELSE NULL END, "
            "detection_method=CASE WHEN $1::varchar='taken' THEN $2::varchar ELSE NULL END, "
            "detection_confidence=NULL WHERE intk_id=$3::int",
            new_status, method, intk_id)
        if previous == "taken" and new_status != "taken":
            # A later manual correction must retire the evidence row. Without
            # this, /recent and the global undo endpoint can act on a stale
            # monitored event after the dose has been skipped or reset.
            await conn.execute(
                "UPDATE monitor_event SET outcome='rejected', corrected_at=NOW() "
                "WHERE u_id=$1 AND intk_id=$2 AND outcome='taken'",
                u_id, intk_id)
        return {"intk_id": intk_id, "status": new_status, "changed": True}
