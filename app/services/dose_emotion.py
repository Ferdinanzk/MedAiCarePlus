"""Facial expression during each camera dose session: one result per (session, dose), whatever the dose's outcome.

MonitorSession scores the verified patient's face all through a dose session (services/monitor_service.py), also
while a hand covers the mouth (the pill going in), but marks those samples `occluded`. The seed-43 model was trained
on uncovered faces, and a covered mouth shifts it systematically, not randomly: measured on public faces with
synthetic occluders, 'happy' falls and 'surprise' and 'angry' rise (angry >= 0.6 in 3-7 % of covered crops, 0.5 %
uncovered). So a dose's result prefers the uncovered samples from just before the hand's approach and just after it
comes away (PRE_SECONDS / POST_SECONDS around the intake event), and reports how much of that window was covered.

A result is written once per dose session (table dose_emotion), when the session resolves the dose: a camera commit,
a request to family (the robot's confirmation), the patient's claim. The write waits POST_SECONDS after the event so
the result has its after-samples, or happens when the session ends first (also for a dose the session left pending).
It never blocks or fails the dose recording: it runs after it, in the background, and logs its own errors. It is
written only while the patient's consent stands (core, plus robot_camera for a robot's session).

Analysis results only (core notice section 4, "Mood: facial-expression analysis results at medication time"); no
image is kept, and a result never covers more than the session's last KEEP_SECONDS of scoring. Not an alert source:
emotion_alert_job reads only the `emotion` table (the one 'during_ingestion' row a camera commit writes, as before),
never these rows.
"""

import asyncio
import datetime
import json
import logging
import time

from app.database import get_pool
from app.services import consent_service, schedule
from app.services.emotion_service import LABELS

log = logging.getLogger(__name__)

PRE_SECONDS = 5.0             # before the hand's approach: the face as the patient picks up the pill
POST_SECONDS = 5.0            # after the hand comes away: the face right after swallowing
TIMELINE_PAD_SECONDS = 10.0   # the stored timeline covers the event plus this much on each side
TIMELINE_MAX_POINTS = 60
MIN_UNOCCLUDED = 2            # uncovered samples around the event needed for an 'event' result
MOSTLY_OCCLUDED = 0.5         # covered share from which the result is shown as less reliable
MIN_RELIABLE_SAMPLES = 4      # fewer faces behind a result, or a weaker top class (of 7), is shown as uncertain
MIN_RELIABLE_SCORE = 0.4
KEEP_SECONDS = 120.0          # samples a session keeps, and the most a result covers (totals only count samples)
SAMPLE_CAP = 1000
FINALIZE_SLACK = 0.5          # the delayed write runs this long after the after-window closes
# A confirmation request notes its resolution inside the request's transaction (dose_confirmation.create): the
# delayed write reads the dose's outcome at least this long after, once that transaction has committed.
RESOLVE_FLOOR_SECONDS = 2.0

OUTCOMES = ("recorded", "taken_other", "sent_to_family", "patient_claim", "skipped", "unresolved")
BASES = ("event", "event_during", "session", "occluded_only", "none")

_tasks: set = set()


# ── samples (called by monitor_service under the session lock) ──

def new_totals() -> dict:
    """Whole-session sample counts; the sample list (and so every average) keeps only KEEP_SECONDS."""
    return {"n": 0, "occluded": 0, "server": 0, "robot": 0}


def _vector(probabilities: dict) -> list[float]:
    return [float(probabilities.get(name, 0.0)) for name in LABELS]


def add_sample(state, t: float, probabilities: dict, occluded: bool, source: str) -> None:
    """One scored face of the session's patient: t is the monotonic arrival time of its frame's landmark packet."""
    samples = state.emotion_samples
    samples.append((t, probabilities, bool(occluded), source))
    cutoff = t - KEEP_SECONDS
    if samples[0][0] < cutoff or len(samples) > SAMPLE_CAP:
        samples[:] = [sample for sample in samples if sample[0] >= cutoff][-SAMPLE_CAP:]
    totals = state.emotion_totals
    totals["n"] += 1
    totals[source if source in ("server", "robot") else "server"] += 1
    if occluded:
        totals["occluded"] += 1


def unoccluded_mean(samples, since: float) -> dict | None:
    """Mean probabilities of the uncovered samples from `since` on (a commit's candidate), or None."""
    vectors = [_vector(sample[1]) for sample in samples
               if sample[0] >= since and not (len(sample) > 2 and sample[2])]
    mean = _mean(vectors)
    return {name: float(value) for name, value in zip(LABELS, mean)} if mean else None


# ── the per-dose result (pure) ──

def _mean(vectors: list[list[float]]) -> list[float] | None:
    if not vectors:
        return None
    return [sum(column) / len(vectors) for column in zip(*vectors)]


def _named(vector: list[float] | None) -> dict | None:
    return {name: round(value, 4) for name, value in zip(LABELS, vector)} if vector else None


def _dominant(vector: list[float] | None) -> tuple[str | None, float | None]:
    """Soft vote: the class with the highest mean probability, as the camera commit does."""
    if not vector:
        return None, None
    index = max(range(len(LABELS)), key=lambda i: vector[i])
    return LABELS[index], round(vector[index], 4)


def _occluded(sample) -> bool:
    return bool(sample[2]) if len(sample) > 2 else False


def _share(samples) -> float | None:
    return round(sum(1 for s in samples if _occluded(s)) / len(samples), 3) if samples else None


def _phase(samples) -> dict:
    """One phase (before / during / after the intake event). Its probabilities are the uncovered samples' mean, or,
    when every sample was covered, all of them with occluded=True (biased: shown as less reliable)."""
    clear = [_vector(s[1]) for s in samples if not _occluded(s)]
    occluded = not clear and bool(samples)
    mean = _mean(clear) if clear else _mean([_vector(s[1]) for s in samples])
    dominant, score = _dominant(mean)
    return {"n": len(samples), "n_unoccluded": len(clear), "occluded_share": _share(samples),
            "probabilities": _named(mean), "dominant": dominant, "score": score, "occluded": occluded}


def _timeline(samples, low: float, high: float, origin: float) -> list[list]:
    """[seconds from origin, class index, its score, mouth covered] per sample, at most TIMELINE_MAX_POINTS."""
    points = []
    for sample in samples:
        if low <= sample[0] <= high:
            vector = _vector(sample[1])
            index = max(range(len(LABELS)), key=lambda i: vector[i])
            points.append([round(sample[0] - origin, 2), index, round(vector[index], 3), _occluded(sample)])
    if len(points) > TIMELINE_MAX_POINTS:
        last = len(points) - 1
        points = [points[round(i * last / (TIMELINE_MAX_POINTS - 1))] for i in range(TIMELINE_MAX_POINTS)]
    return points


def _scored_by(totals: dict) -> str:
    server, robot = totals.get("server", 0), totals.get("robot", 0)
    if server and robot:
        return "mixed"
    return "server" if server else "robot" if robot else "none"


def summarize(samples, totals: dict, event: tuple[float, float] | None, now: float) -> dict:
    """The dose's result from a session's samples, its totals and its latest intake event (start, end), monotonic.
    `now` is the moment the result describes (finalize: the session's last activity, so a session ended long after
    its last frame is still described by its frames).

    Basis, in order of preference:
      event         uncovered samples in the PRE_SECONDS before the approach and the POST_SECONDS after the hand
                    came away (at least MIN_UNOCCLUDED);
      event_during  otherwise any uncovered sample in that window, the intake itself included;
      session       otherwise (or with no event: a claim, a dose left pending) the uncovered samples of the
                    KEEP_SECONDS before `now`, never a longer stretch of the session;
      occluded_only no uncovered sample there: the covered ones (systematically biased, shown as less reliable);
      none          nothing was scored.
    """
    samples = sorted(samples, key=lambda s: s[0])
    recent = [s for s in samples if now - KEEP_SECONDS <= s[0] <= now]
    n_total = int(totals.get("n", 0))
    unoccluded_total = n_total - int(totals.get("occluded", 0))
    basis, mean, basis_samples, phases = None, None, 0, None
    if event is not None:
        start, end = event
        before = [s for s in samples if start - PRE_SECONDS <= s[0] < start]
        during = [s for s in samples if start <= s[0] <= end]
        after = [s for s in samples if end < s[0] <= end + POST_SECONDS]
        window = before + during + after
        phases = {"before": _phase(before), "during": _phase(during), "after": _phase(after),
                  "event_seconds": round(end - start, 2)}
        share = _share(window)
        around = [_vector(s[1]) for s in before + after if not _occluded(s)]
        clear_window = [_vector(s[1]) for s in window if not _occluded(s)]
        if len(around) >= MIN_UNOCCLUDED:
            basis, mean, basis_samples = "event", _mean(around), len(around)
        elif clear_window:
            basis, mean, basis_samples = "event_during", _mean(clear_window), len(clear_window)
        timeline = {"origin": "event_start",
                    "points": _timeline(samples, start - TIMELINE_PAD_SECONDS, end + TIMELINE_PAD_SECONDS, start)}
    else:
        window = recent
        share = _share(recent)
        timeline = {"origin": "resolved", "points": _timeline(samples, now - KEEP_SECONDS, now, now)}
    if basis is None:
        clear_recent = [_vector(s[1]) for s in recent if not _occluded(s)]
        covered = window or recent
        if clear_recent:
            basis, mean, basis_samples = "session", _mean(clear_recent), len(clear_recent)
        elif covered:
            basis, mean, basis_samples = "occluded_only", _mean([_vector(s[1]) for s in covered]), len(covered)
        else:
            basis = "none"
    dominant, score = _dominant(mean)
    return {"samples": n_total, "unoccluded_samples": unoccluded_total, "occluded_share": share,
            "basis": basis, "basis_samples": basis_samples, "probabilities": _named(mean),
            "dominant": dominant, "dominant_score": score, "phases": phases, "timeline": timeline,
            "scored_by": _scored_by(totals)}


def outcome_for(intake_status: str | None, confirmation_source: str | None, recorded_here: bool) -> str:
    """How the session's dose was resolved: a confirmation request made during the session decides it (a patient
    claim or a request to family, whatever family answered since); otherwise the dose's status."""
    if confirmation_source is not None:
        return "patient_claim" if confirmation_source == "patient_claim" else "sent_to_family"
    if intake_status == "taken":
        return "recorded" if recorded_here else "taken_other"
    if intake_status == "pending_confirmation":
        return "sent_to_family"
    if intake_status == "skipped":
        return "skipped"
    return "unresolved"


def mostly_occluded(basis: str | None, occluded_share) -> bool:
    return basis == "occluded_only" or (occluded_share is not None and float(occluded_share) >= MOSTLY_OCCLUDED)


def uncertain(basis_samples, score, mostly_covered: bool = False) -> bool:
    """Thin evidence, shown as less reliable like a covered mouth: fewer than MIN_RELIABLE_SAMPLES faces behind the
    result, or a top class below MIN_RELIABLE_SCORE. A mostly covered result is flagged as that instead."""
    if mostly_covered or score is None:
        return False
    return int(basis_samples or 0) < MIN_RELIABLE_SAMPLES or float(score) < MIN_RELIABLE_SCORE


def phase_flags(phases) -> dict | None:
    """The before / during / after breakdown with each phase's `uncertain` flag (its uncovered faces)."""
    if not isinstance(phases, dict):
        return phases
    for name in ("before", "during", "after"):
        phase = phases.get(name)
        if isinstance(phase, dict):
            phase["uncertain"] = uncertain(phase.get("n_unoccluded"), phase.get("score"), bool(phase.get("occluded")))
    return phases


def chip(value) -> dict | None:
    """The per-dose summary the /today and history rows carry (a JSONB object; asyncpg returns it as text)."""
    if value is None:
        return None
    data = json.loads(value) if isinstance(value, (str, bytes)) else dict(value)
    data["mostly_occluded"] = mostly_occluded(data.get("basis"), data.get("occluded_share"))
    data["uncertain"] = uncertain(data.get("basis_samples"), data.get("score"), data["mostly_occluded"])
    return data


def parse_json(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else value


# The outcomes a per-dose chip ("expression while taking") is shown for: the session recorded the dose, saw it
# taken another way, or sent it to family / the patient claimed it. Not for a dose the session left pending or
# skipped; those results are listed on the Emotion page with their outcome.
CHIP_OUTCOMES = ("recorded", "taken_other", "sent_to_family", "patient_claim")

# Selects the summary of a dose's best result for /today and /history: only while the dose is taken or waiting for
# family (an undo, or family answering 'not taken', removes the chip), one with data over none, then the newest.
# The arguments are SQL expressions for the dose's intk_id, its owner's u_id and its current intake_stats.
CHIP_SQL = """LEFT JOIN LATERAL (
    SELECT jsonb_build_object('dominant', de.dominant, 'score', de.dominant_score,
                              'occluded_share', de.occluded_share, 'basis', de.basis,
                              'basis_samples', de.basis_samples, 'samples', de.samples,
                              'unoccluded_samples', de.unoccluded_samples, 'outcome', de.outcome) AS dose_emotion
    FROM dose_emotion de
    WHERE de.intk_id = {intk_id} AND de.u_id = {u_id} AND {status} IN ('taken', 'pending_confirmation')
      AND de.outcome IN ({outcomes})
    ORDER BY (de.basis = 'none'), de.created_at DESC
    LIMIT 1) dose_emotion_best ON TRUE"""


def chip_join(intk_id: str, u_id: str, status: str) -> str:
    return CHIP_SQL.format(intk_id=intk_id, u_id=u_id, status=status,
                           outcomes=", ".join(f"'{outcome}'" for outcome in CHIP_OUTCOMES))


# ── writing it (background; never raises) ──

FACTS_SQL = (
    "SELECT i.intake_stats, i.detection_method, "
    "(SELECT dc.source FROM dose_confirmation dc WHERE dc.u_id = i.u_id AND i.intk_id = ANY(dc.intk_ids) "
    "AND dc.created_at >= $3 ORDER BY dc.created_at DESC LIMIT 1) AS confirmation_source, "
    "EXISTS (SELECT 1 FROM monitor_event e WHERE e.session_id = $4::uuid AND e.intk_id = i.intk_id "
    "AND e.outcome = 'taken') AS recorded_here "
    "FROM intake i WHERE i.intk_id = $1 AND i.u_id = $2")

INSERT_SQL = (
    "INSERT INTO dose_emotion (u_id, intk_id, session_id, client_type, scored_by, outcome, intake_status, "
    "detection_method, confirmation_source, session_started_at, resolved_at, samples, unoccluded_samples, "
    "occluded_share, basis, basis_samples, probabilities, dominant, dominant_score, phases, timeline) "
    "VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17::jsonb, $18, $19, "
    "$20::jsonb, $21::jsonb) ON CONFLICT (session_id, intk_id) DO NOTHING RETURNING dose_emotion_id")


def _spawn(coro) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def _dose_session(state) -> bool:
    return getattr(state, "intk_id", None) is not None and hasattr(state, "emotion_totals")


def note_resolution(state) -> None:
    """The session just resolved its dose (a camera commit, a request to family, a claim): write the result once the
    after-window has passed, unless the session ends first. In memory only; never raises."""
    try:
        if not _dose_session(state) or state.dose_emotion_done:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return      # a synchronous caller (no event loop): nothing can be written
        if state.dose_emotion_resolved_at is None:
            state.dose_emotion_resolved_at = schedule.current_time()
        if state.dose_emotion_timer is not None:
            return
        end = state.last_event[1] if state.last_event else time.monotonic()
        delay = max(RESOLVE_FLOOR_SECONDS, end + POST_SECONDS + FINALIZE_SLACK - time.monotonic())
        state.dose_emotion_timer = _spawn(_finalize_later(state, delay))
    except Exception:
        log.exception("dose emotion: could not schedule the result of session %s", getattr(state, "session_id", "?"))


def note_resolution_for(u_id: int, intk_ids) -> None:
    """A confirmation request (the robot's, or a patient claim) for doses of this patient: the live session of one
    of them resolved it. Called inside dose_confirmation.create's transaction, so it only touches memory."""
    try:
        from app.services.monitor_service import registry   # monitor_service imports this module

        state = registry.sessions.get(registry.by_user.get(u_id) or "")
        if state is not None and not state.ended and state.intk_id in {int(i) for i in intk_ids}:
            note_resolution(state)
    except Exception:
        log.exception("dose emotion: could not note the confirmation of doses %s", intk_ids)


async def _finalize_later(state, delay: float) -> None:
    await asyncio.sleep(delay)
    await finalize(state, "resolved")


def finalize_soon(state, reason: str = "session_end") -> None:
    """The session is ending: write its result now (a pending delayed write is replaced). Never raises."""
    try:
        if not _dose_session(state) or state.dose_emotion_done:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return      # a synchronous caller (no event loop): nothing can be written
        timer = state.dose_emotion_timer
        if timer is not None and not timer.done():
            timer.cancel()
        state.dose_emotion_timer = None
        _spawn(finalize(state, reason))
    except Exception:
        log.exception("dose emotion: could not finalize session %s", getattr(state, "session_id", "?"))


def _consent_scopes(state) -> tuple[str, ...]:
    return ("core", "robot_camera") if getattr(state, "client_type", None) == "reachy" else ("core",)


async def finalize(state, reason: str) -> bool:
    """Write the session's dose result once (idempotent per session and dose). True when a row was written."""
    if not _dose_session(state) or state.dose_emotion_done:
        return False
    state.dose_emotion_done = True     # before any await: one write per session
    timer = state.dose_emotion_timer
    if timer is not None and timer is not asyncio.current_task() and not timer.done():
        timer.cancel()
    try:
        totals = dict(state.emotion_totals)
        samples = list(state.emotion_samples)
        resolved_at = state.dose_emotion_resolved_at
        if totals["n"] == 0 and resolved_at is None:
            return False    # the camera never scored the patient's face and nothing resolved the dose here
        # The result describes the session up to its last activity (its newest landmark packet or scored face), not
        # up to now: a session swept 10 minutes after its tab closed keeps its frames' timeline and time.
        clock = time.monotonic()
        anchor = min(clock, max([getattr(state, "last_activity_at", clock)] + [sample[0] for sample in samples]))
        summary = summarize(samples, totals, state.last_event, anchor)
        async with get_pool().acquire() as conn:
            # A mood result is stored only while the patient's consent stands (withdrawing it ends the session's
            # requests, so an idle sweep or a pending timer may be what ends the session).
            consents = await consent_service.fetch_state(conn, state.u_id)
            if not all(consent_service.is_current(consents, scope) for scope in _consent_scopes(state)):
                log.info("dose emotion for dose %s (session %s) not written: consent is no longer current",
                         state.intk_id, state.session_id)
                return False
            facts = await conn.fetchrow(FACTS_SQL, state.intk_id, state.u_id, state.started_at, state.session_id)
            if facts is None:
                return False
            outcome = outcome_for(facts["intake_stats"], facts["confirmation_source"], bool(facts["recorded_here"]))
            ended_at = schedule.current_time() - datetime.timedelta(seconds=clock - anchor)
            written = await conn.fetchval(
                INSERT_SQL, state.u_id, state.intk_id, state.session_id, state.client_type, summary["scored_by"],
                outcome, facts["intake_stats"], facts["detection_method"], facts["confirmation_source"],
                state.started_at, resolved_at or ended_at, summary["samples"],
                summary["unoccluded_samples"], summary["occluded_share"], summary["basis"], summary["basis_samples"],
                json.dumps(summary["probabilities"]) if summary["probabilities"] else None,
                summary["dominant"], summary["dominant_score"],
                json.dumps(summary["phases"]) if summary["phases"] else None, json.dumps(summary["timeline"]))
        if written is not None:
            # No expression class in the log: it is mood data, kept only in the table (export, deletion).
            log.info("dose emotion for dose %s (session %s, %s): outcome %s, basis %s, %d/%d uncovered samples",
                     state.intk_id, state.session_id, reason, outcome, summary["basis"],
                     summary["unoccluded_samples"], summary["samples"])
        return written is not None
    except Exception:
        log.exception("dose emotion for dose %s of session %s was not written", getattr(state, "intk_id", None),
                      getattr(state, "session_id", None))
        return False
