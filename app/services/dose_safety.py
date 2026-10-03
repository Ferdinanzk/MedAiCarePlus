"""Overdose protection (「防止重複服藥」): whether a dose may be started or recorded as taken now.

On 3 Oct 2026 three test reminders recorded three doses of one medicine in about an hour. Medication-safety guidance
(ISMP/CMS dose windows; MedlinePlus, the NHS and Taiwan's 對半原則, "never double up") and pill dispensers agree on
four rules. They are checked here, and only here, for every path that starts a dose (robot task and lease, camera
session) or records one taken (camera commit, manual tap, caregiver confirmation, Take Now, the legacy page), under
the intake row lock:

  R1 due        Not before DOSE_EARLY_MINUTES ahead of its time, nor before halfway from the same medicine's previous
                dose (schedule.due_from).
  R2 gap        Not within the minimum gap of the same medicine's nearest taken dose, measured between the times the
                pills were actually taken (min_gap).
  R3 daily max  Not more taken doses of the medicine on one local day than it allows (daily_max). A dose counts on the
                day it was scheduled (an ad-hoc dose: the day it was taken), so a bedtime dose taken after midnight
                does not use up the next day's.
  R4 expiry     A dose not taken by halfway to the same medicine's next scheduled dose stays missed (expires_at).
                Taken late, close to the next one, it would double up. An ad-hoc dose never expires (is_ad_hoc).

For R2 and R3 a dose waiting for a caregiver's answer counts as taken when the robot asked (dose_confirmation
created_at): every robot dose goes to family confirmation today, and up to 2 h without an answer must not leave room
for a second pill. A camera session's dose is judged for R1 and R4 at the moment the session started (it was allowed
then), so a pill swallowed just after its dose expired is still recorded and spaces the next one. A caregiver's answer
is judged when the patient was asked, with the doses there were then: an ad-hoc dose made later moves no halfway point.

Each patient can switch the protection off (notification_settings.overdose_protection, on by default). Off, nothing
is refused and no double-dose alert is sent: the behaviour before 3 Oct. A refusal is a schedule.DoseRefused, which
main.py answers with 409 and refusal_body(): the reason, the times, and one sentence for the patient (`reply`, and
`speech_text` for the robot's voice).
"""

import datetime
import logging
import re
from zoneinfo import ZoneInfo

from app.config import MEDCARE_TIMEZONE
from app.database import get_pool
from app.services import conversation, outbox, schedule
from app.services.context_info import time_of_day

log = logging.getLogger(__name__)

_TZ = ZoneInfo(MEDCARE_TIMEZONE)
DAY = datetime.timedelta(days=1)
# A medicine without dose times (as needed) keeps 4 hours between doses unless its own minimum is set.
UNSCHEDULED_GAP = datetime.timedelta(minutes=240)
# Caregivers' confirmation requests say when the medicine was last recorded (within a day) and how early the dose is.
LAST_NOTE_WITHIN = DAY
EARLY_NOTE_AFTER = datetime.timedelta(minutes=30)
# A suspected double dose of one dose alerts family at most once in this long (a rolling window, not clock hours).
ALERT_EVERY = datetime.timedelta(hours=1)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# The robot's Matcha voice reads Chinese characters only: a medicine named in Latin letters is "this medicine".
_CJK = re.compile(r"[㐀-鿿]")


def _aware(when: datetime.datetime) -> datetime.datetime:
    return when if when.tzinfo is not None else when.replace(tzinfo=datetime.timezone.utc)


def _local(when: datetime.datetime) -> datetime.datetime:
    return _aware(when).astimezone(_TZ)


# ── The limits a medicine gets ───────────────────────────────────────────────

def min_gap(schedule_time, min_interval_minutes: int | None = None) -> datetime.timedelta:
    """R2: the medicine's own minimum, or half the shortest gap between its daily dose times (across midnight too,
    so one time a day gives 12 h), or UNSCHEDULED_GAP without dose times. Allegra at 08/12/20/22: 60 minutes."""
    if min_interval_minutes:
        return datetime.timedelta(minutes=int(min_interval_minutes))
    minutes = [int(t[:2]) * 60 + int(t[3:]) for t in schedule.dose_times(schedule_time)]
    if not minutes:
        return UNSCHEDULED_GAP
    gaps = [later - earlier for earlier, later in zip(minutes, minutes[1:])] + [minutes[0] + 1440 - minutes[-1]]
    return datetime.timedelta(minutes=min(gaps)) / 2


def daily_max(schedule_time, max_daily_doses: int | None = None) -> int | None:
    """R3: the medicine's own maximum, or its number of dose times a day (also on a weekday without doses, so a late
    dose of a weekly medicine is not refused after midnight). None (no limit) for an unscheduled medicine."""
    if max_daily_doses:
        return int(max_daily_doses)
    return len(schedule.dose_times(schedule_time)) or None


def is_ad_hoc(scheduled_time: datetime.datetime | None) -> bool:
    """An ad-hoc dose (Take Now with no scheduled dose open): scheduled rows are generated on whole minutes
    (schedule.occurrences), an ad-hoc row carries the moment it was made, to the microsecond (ad_hoc_sql)."""
    return scheduled_time is not None and (scheduled_time.second, scheduled_time.microsecond) != (0, 0)


def expires_at(scheduled_time: datetime.datetime | None,
               next_time: datetime.datetime | None) -> datetime.datetime | None:
    """R4: halfway to the same medicine's next scheduled dose (for the last dose of a day, the next day's first). A
    dose with no later dose (unscheduled, or the end of the generated schedule) never expires, nor does an ad-hoc one:
    it was started when the patient chose, and left pending it is no missed dose to make up."""
    if scheduled_time is None or next_time is None or is_ad_hoc(scheduled_time):
        return None
    scheduled_time, next_time = _aware(scheduled_time), _aware(next_time)
    if next_time <= scheduled_time:
        return None
    return scheduled_time + (next_time - scheduled_time) / 2


# ── SQL forms, for queries that pick doses ───────────────────────────────────

def ad_hoc_sql(alias: str = "i") -> str:
    """is_ad_hoc in SQL: intake row `alias` is not on a whole minute."""
    return f"(date_trunc('minute', {alias}.intake_time_stamp) <> {alias}.intake_time_stamp)"


def next_sql(alias: str = "i") -> str:
    """The same medicine's next scheduled dose time after intake row `alias` (any status). Ad-hoc rows are left
    out: one made (or abandoned) after a dose must not move that dose's halfway point."""
    return (f"(SELECT MIN(next_dose.intake_time_stamp) FROM intake next_dose WHERE next_dose.u_id = {alias}.u_id "
            f"AND next_dose.med_id = {alias}.med_id AND next_dose.intake_time_stamp > {alias}.intake_time_stamp "
            f"AND NOT {ad_hoc_sql('next_dose')})")


def protected_sql(alias: str = "i") -> str:
    """The patient's switch for intake row `alias` (on without a settings row)."""
    return (f"COALESCE((SELECT ns.overdose_protection FROM notification_settings ns WHERE ns.u_id = {alias}.u_id), "
            "TRUE)")


def expired_sql(alias: str = "i", now_sql: str = "NOW()") -> str:
    """R4 (expires_at) in SQL, whatever the switch says."""
    return (f"(NOT {ad_hoc_sql(alias)} AND COALESCE({alias}.intake_time_stamp + ({next_sql(alias)} "
            f"- {alias}.intake_time_stamp) / 2 <= {now_sql}, FALSE))")


def open_sql(alias: str = "i", now_sql: str = "NOW()") -> str:
    """The doses a pill taken at `now_sql` may count for (Take Now, the legacy page): due, and not expired while
    protection is on. With protection off the due window still decides which dose a pill belongs to; it refuses
    nothing (a pill with no due dose becomes an ad-hoc dose)."""
    return (f"({schedule.due_sql(alias, now_sql)} AND NOT ({protected_sql(alias)} "
            f"AND {expired_sql(alias, now_sql)}))")


def startable_sql(alias: str = "i", now_sql: str = "NOW()") -> str:
    """R1 and R4 under the switch, for queries that pick doses to start (manual robot task). The gap and the daily
    maximum need the medicine's schedule; check() applies them to what such a query picked."""
    return (f"(NOT {protected_sql(alias)} OR ({schedule.due_sql(alias, now_sql)} "
            f"AND NOT {expired_sql(alias, now_sql)}))")


def _claimed_sql(alias: str) -> str:
    """Dose `alias` waits for a caregiver's answer on a request made at or before the moment judged ($3). Of two such
    requests for one medicine, the earlier one counts against the later one, not the other way round."""
    return (f"EXISTS (SELECT 1 FROM dose_confirmation claim WHERE claim.u_id = {alias}.u_id "
            f"AND {alias}.intk_id = ANY(claim.intk_ids) AND claim.resolution IS NULL "
            "AND claim.created_at <= $3::timestamptz)")


def _previous_then_sql(alias: str) -> str:
    """schedule.previous_sql as of the moment judged ($3): an ad-hoc dose made after it did not exist then (a
    caregiver's answer is judged when the patient was asked). Judged now, it is the same."""
    return (f"(SELECT MAX(prev_dose.intake_time_stamp) FROM intake prev_dose WHERE prev_dose.u_id = {alias}.u_id "
            f"AND prev_dose.med_id = {alias}.med_id AND prev_dose.intake_time_stamp < {alias}.intake_time_stamp "
            f"AND NOT ({ad_hoc_sql('prev_dose')} AND prev_dose.intake_time_stamp > $3::timestamptz))")


# Everything the rules need about intake rows $2 of patient $1, judged at $3, with $4 the patient's time zone.
# previous_time and next_time are the medicine's neighbouring doses as of $3 (next_sql: scheduled ones only).
# last_taken_at is the other taken dose of the medicine nearest to $3 (a caregiver's answer is judged at an earlier
# moment, so a dose taken after it counts too), or a dose waiting for a caregiver's answer, at the time the robot
# asked (last_taken_pending); taken_that_day counts the other doses, taken or waiting so, scheduled on the row's
# local day. language is that of the patient's latest check-in (the robot's setting), for the patient's sentence.
FACTS_SQL = (
    "SELECT i.intk_id, i.u_id, i.med_id, i.intake_stats, i.intake_time_stamp, "
    f"{_previous_then_sql('i')} AS previous_time, {next_sql('i')} AS next_time, "
    "m.med_name, m.schedule_time, m.min_interval_minutes, m.max_daily_doses, "
    f"{protected_sql('i')} AS protection, "
    "(SELECT c.language FROM conversation c WHERE c.u_id = i.u_id ORDER BY c.started_at DESC LIMIT 1) AS language, "
    "recent.taken_at AS last_taken_at, COALESCE(recent.awaiting, FALSE) AS last_taken_pending, "
    "(SELECT COUNT(*) FROM intake d WHERE d.u_id = i.u_id AND d.med_id = i.med_id AND d.intk_id <> i.intk_id "
    f"AND (d.intake_stats = 'taken' OR (d.intake_stats = 'pending_confirmation' AND {_claimed_sql('d')})) "
    "AND (d.intake_time_stamp AT TIME ZONE $4::text)::date = (i.intake_time_stamp AT TIME ZONE $4::text)::date) "
    "AS taken_that_day "
    "FROM intake i JOIN medication m ON m.med_id = i.med_id "
    "LEFT JOIN LATERAL (SELECT doses.taken_at, doses.awaiting FROM ("
    "SELECT t.actual_intake_time AS taken_at, FALSE AS awaiting FROM intake t WHERE t.u_id = i.u_id "
    "AND t.med_id = i.med_id AND t.intk_id <> i.intk_id "
    "AND t.intake_stats = 'taken' AND t.actual_intake_time IS NOT NULL "
    "UNION ALL "
    "SELECT claim.created_at, TRUE FROM intake t JOIN dose_confirmation claim ON claim.u_id = t.u_id "
    "AND t.intk_id = ANY(claim.intk_ids) AND claim.resolution IS NULL AND claim.created_at <= $3::timestamptz "
    "WHERE t.u_id = i.u_id AND t.med_id = i.med_id AND t.intk_id <> i.intk_id "
    "AND t.intake_stats = 'pending_confirmation'"
    ") doses ORDER BY ABS(EXTRACT(EPOCH FROM (doses.taken_at - $3::timestamptz))), doses.taken_at DESC "
    "LIMIT 1) recent ON TRUE "
    "WHERE i.u_id = $1 AND i.intk_id = ANY($2::int[]) ORDER BY i.intake_time_stamp, i.intk_id"
)
# Recording paths lock the medicines' rows first, in a statement of their own, so two doses of one medicine recorded
# at once can't both pass R2/R3. Not FOR UPDATE on FACTS_SQL itself: under READ COMMITTED a statement reads the
# snapshot taken when it started, so after waiting for the lock it would still miss the dose just committed.
LOCK_SQL = ("SELECT m.med_id FROM medication m WHERE m.med_id IN (SELECT i.med_id FROM intake i WHERE i.u_id = $1 "
            "AND i.intk_id = ANY($2::int[])) ORDER BY m.med_id FOR UPDATE")


# ── Refusals ─────────────────────────────────────────────────────────────────

class DoseTooSoon(schedule.DoseRefused):
    """R2: 409 {"detail": "dose_too_soon", "last_taken_at", "next_allowed_at", ...}."""

    detail = "dose_too_soon"

    def __init__(self, *, last_taken_at: datetime.datetime, gap: datetime.timedelta, pending: bool = False,
                 **context):
        super().__init__(f"dose_too_soon: dose {context.get('intk_id')} within {gap} of a taken dose", **context)
        self.last_taken_at = _aware(last_taken_at)
        self.gap = gap
        # The last dose still waits for a caregiver's answer (last_taken_at is when the robot asked).
        self.pending = bool(pending)
        self.next_allowed_at = self.last_taken_at + gap

    def fields(self) -> dict:
        return {"last_taken_at": schedule.local_iso(self.last_taken_at),
                "next_allowed_at": schedule.local_iso(self.next_allowed_at),
                "min_interval_minutes": int(self.gap.total_seconds() // 60),
                "last_taken_pending": self.pending}


class DailyMaxReached(schedule.DoseRefused):
    """R3: 409 {"detail": "daily_max_reached", "next_allowed_at" (the next local day), ...}."""

    detail = "daily_max_reached"

    def __init__(self, *, taken: int, limit: int, day: datetime.date, **context):
        super().__init__(f"daily_max_reached: dose {context.get('intk_id')}, {taken} of {limit} on {day}", **context)
        self.taken, self.limit, self.day = taken, limit, day
        self.next_allowed_at = datetime.datetime.combine(day + DAY, datetime.time.min, tzinfo=_TZ)

    def fields(self) -> dict:
        return {"taken_today": self.taken, "max_daily_doses": self.limit,
                "next_allowed_at": schedule.local_iso(self.next_allowed_at)}


class DoseExpired(schedule.DoseRefused):
    """R4: 409 {"detail": "dose_expired", "expired_at", ...}."""

    detail = "dose_expired"

    def __init__(self, *, expired_at: datetime.datetime, **context):
        super().__init__(f"dose_expired: dose {context.get('intk_id')} expired at {expired_at}", **context)
        self.expired_at = _aware(expired_at)

    def fields(self) -> dict:
        return {"expired_at": schedule.local_iso(self.expired_at)}


# A refusal of these on a path with evidence that the patient swallowed something alerts family (alert_family).
DOUBLE_DOSE = (DoseTooSoon, DailyMaxReached)


def evaluate(row, at: datetime.datetime, *,
             started_at: datetime.datetime | None = None) -> schedule.DoseRefused | None:
    """The first rule a FACTS_SQL row breaks (expiry, due, daily maximum, gap), or None. None whenever the patient
    switched protection off.

    R2 and R3 are judged at `at`, when the pill went down. R1 and R4 (may this dose be taken at all) are judged at
    `started_at` instead when the dose's camera session began earlier: it was allowed then, and a pill swallowed a
    minute after the dose expired must still be recorded, or the next dose would not be spaced from it."""
    if not row.get("protection", True):
        return None
    at = _aware(at)
    when = min(at, _aware(started_at)) if started_at is not None else at
    scheduled = row["intake_time_stamp"]
    context = {"intk_id": row["intk_id"], "med_name": row.get("med_name"), "language": row.get("language"),
               "at": at, "u_id": row.get("u_id")}
    expiry = expires_at(scheduled, row.get("next_time"))
    if expiry is not None and when >= expiry:
        return DoseExpired(expired_at=expiry, scheduled_time=scheduled, **context)
    if not schedule.is_due(scheduled, when, row.get("previous_time")):
        return schedule.DoseNotDueYet(scheduled, row["intk_id"], row.get("previous_time"),
                                      **{key: value for key, value in context.items() if key != "intk_id"})
    limit = daily_max(row.get("schedule_time"), row.get("max_daily_doses"))
    taken = int(row.get("taken_that_day") or 0)
    if limit is not None and taken >= limit:
        day = _local(scheduled if scheduled is not None else at).date()
        return DailyMaxReached(taken=taken, limit=limit, day=day, scheduled_time=scheduled, **context)
    last = row.get("last_taken_at")
    gap = min_gap(row.get("schedule_time"), row.get("min_interval_minutes"))
    if last is not None and abs(at - _aware(last)) < gap:
        return DoseTooSoon(last_taken_at=last, gap=gap, pending=bool(row.get("last_taken_pending")),
                           scheduled_time=scheduled, **context)
    return None


async def facts(conn, u_id: int, intk_ids, at: datetime.datetime, *, lock: bool = False) -> list:
    """FACTS_SQL rows. lock=True first locks the medicines' rows (LOCK_SQL, its own statement, so the read after it
    sees what a transaction that held the lock committed)."""
    ids = [int(i) for i in intk_ids]
    if lock:
        await conn.execute(LOCK_SQL, u_id, ids)
    return await conn.fetch(FACTS_SQL, u_id, ids, _aware(at), MEDCARE_TIMEZONE)


async def check(conn, u_id: int, intk_ids, *, at: datetime.datetime | None = None, lock: bool = False,
                started_at: datetime.datetime | None = None, after_intake: bool = False) -> list:
    """Raise the first refusal among the listed doses (in time order); return their FACTS_SQL rows otherwise.
    Callers hold the intake row lock; paths that record a dose (taken, or waiting for a caregiver) pass lock=True.
    `at` defaults to now; `started_at` is when the dose's camera session began (evaluate). after_intake marks a
    refusal on a path with evidence the patient already swallowed something (its sentence says so)."""
    at = _aware(at or schedule.current_time())
    rows = await facts(conn, u_id, intk_ids, at, lock=lock)
    for row in rows:
        refused = evaluate(row, at, started_at=started_at)
        if refused is not None:
            refused.after_intake = after_intake
            raise refused
    return rows


async def verdicts(conn, u_id: int, intk_ids, *, at: datetime.datetime | None = None) -> dict:
    """{intk_id: the refusal, or None} for the listed doses, in time order, for callers that act on each dose."""
    at = _aware(at or schedule.current_time())
    return {row["intk_id"]: evaluate(row, at) for row in await facts(conn, u_id, intk_ids, at)}


async def allowed(conn, u_id: int, intk_ids, *, at: datetime.datetime | None = None) -> list[int]:
    """The listed doses no rule refuses, for callers that leave the others out rather than refuse."""
    return [intk_id for intk_id, refused in (await verdicts(conn, u_id, intk_ids, at=at)).items() if refused is None]


# ── What the patient hears ───────────────────────────────────────────────────

def _day_zh(day: datetime.date, today: datetime.date) -> str:
    if day == today:
        return ""
    if day == today + DAY:
        return "明天"
    if day == today - DAY:
        return "昨天"
    return f"{day.month}月{day.day}日"


def _day_en(day: datetime.date, today: datetime.date) -> str:
    if day == today:
        return ""
    if day == today + DAY:
        return " tomorrow"
    if day == today - DAY:
        return " yesterday"
    return f" on {day.day} {_MONTHS[day.month - 1]}"


def _day_word_en(day: datetime.date, today: datetime.date) -> str:
    return _day_en(day, today).strip() or "today"


def spoken_time_zh(when: datetime.datetime, ref: datetime.datetime) -> str:
    """「晚上9點」, 「凌晨12點05分」, 「明天早上6點」: the 12-hour clock people say, relative to the day of `ref`."""
    local = _local(when)
    period = time_of_day(local.hour)[0]
    return (f"{_day_zh(local.date(), _local(ref).date())}{period}{local.hour % 12 or 12}點"
            + (f"{local.minute:02d}分" if local.minute else ""))


def spoken_time_en(when: datetime.datetime, ref: datetime.datetime) -> str:
    """"9 pm", "12:05 am", "6 am tomorrow"."""
    local = _local(when)
    clock = f"{local.hour % 12 or 12}" + (f":{local.minute:02d}" if local.minute else "")
    return f"{clock} {'am' if local.hour < 12 else 'pm'}{_day_en(local.date(), _local(ref).date())}"


def _times_en(count: int) -> str:
    return {1: "once", 2: "twice"}.get(count, f"{count} times")


def _zh_name(name: str | None, spoken: bool) -> str | None:
    """The medicine's name in a Chinese sentence, or None for "this medicine": a name in Latin letters is only
    written, never spoken (the robot's voice can't read it)."""
    name = (name or "").strip()
    if not name or (spoken and not _CJK.search(name)):
        return None
    return name


def _zh_spaced(name: str) -> str:
    """Latin names get spaces in a Chinese sentence (「今天的 allegra 已經」), Chinese ones none (「今天的普拿疼已經」)."""
    return name if _CJK.search(name) else f" {name} "


def _zh_sentence(refused: schedule.DoseRefused, ref: datetime.datetime, spoken: bool) -> str:
    """Traditional Chinese. Names the medicine when the name is Chinese (a slot can hold several medicines); one in
    Latin letters only in the daily maximum's written sentence."""
    cjk = _zh_name(refused.med_name, spoken=True)
    med = cjk or "這個藥"
    tail = ("已經通知家人；如果覺得不舒服，請馬上告訴家人。" if refused.family_alerted
            else "如果覺得不舒服，請馬上告訴家人。")
    next_tail = "如果您剛剛已經吃了，下一次的藥請先問過家人再吃。"
    if isinstance(refused, schedule.DoseNotDueYet):
        when = spoken_time_zh(refused.due_from, ref)
        if refused.after_intake:
            return f"這次沒有記錄，因為還沒到吃{med}的時間（{when}以後才可以）。{next_tail}"
        return f"現在還不是吃{med}的時間，{when}以後才可以。"
    if isinstance(refused, DoseTooSoon):
        when = spoken_time_zh(refused.last_taken_at, ref)
        if refused.after_intake:
            return f"這次沒有記錄，因為{med}您{when}已經吃過了。{tail}"
        return f"{med}您{when}已經吃過了，請先不要再吃。"
    if isinstance(refused, DailyMaxReached):
        day = _day_zh(refused.day, _local(ref).date()) or "今天"
        name = _zh_name(refused.med_name, spoken)
        what = _zh_spaced(name) if name else "這個藥"
        if refused.after_intake:
            return f"這次沒有記錄，因為{day}的{what}已經吃滿 {refused.limit} 次了。{tail}"
        return f"{day}的{what}已經吃滿 {refused.limit} 次了，請不要再吃。"
    if isinstance(refused, DoseExpired):
        when = spoken_time_zh(refused.scheduled_time, ref)
        what = f"的{cjk}" if cjk else "的藥"
        if refused.after_intake:
            return f"這次沒有記錄，因為{when}{what}已經錯過了。{next_tail}"
        return f"{when}{what}已經錯過了，請不要補吃，等下一次就好。"
    return "這個藥現在不能記錄。"


def _en_sentence(refused: schedule.DoseRefused, ref: datetime.datetime) -> str:
    name = refused.med_name or "this medicine"
    tail = ("Your family has been told. If you feel unwell, tell them right away." if refused.family_alerted
            else "If you feel unwell, tell your family right away.")
    next_tail = "If you just took it, ask your family before taking the next one."
    if isinstance(refused, schedule.DoseNotDueYet):
        when = spoken_time_en(refused.due_from, ref)
        if refused.after_intake:
            return f"This wasn't recorded: it's not time for this medicine until {when}. {next_tail}"
        return f"It's not time for this medicine yet; you can take it after {when}."
    if isinstance(refused, DoseTooSoon):
        when = spoken_time_en(refused.last_taken_at, ref)
        if refused.after_intake:
            return f"This wasn't recorded: you already took this medicine at {when}. {tail}"
        return f"You already took this medicine at {when}, so please don't take it again yet."
    if isinstance(refused, DailyMaxReached):
        day = _day_word_en(refused.day, _local(ref).date())
        if refused.after_intake:
            return f"This wasn't recorded: you've already taken {name} {_times_en(refused.limit)} {day}. {tail}"
        return f"You've already taken {name} {_times_en(refused.limit)} {day}, so please don't take any more."
    if isinstance(refused, DoseExpired):
        when = spoken_time_en(refused.scheduled_time, ref)
        if refused.after_intake:
            return f"This wasn't recorded: the {when} dose had already been missed. {next_tail}"
        return f"The {when} dose was missed, so please don't make it up; just wait for the next one."
    return "This dose can't be recorded now."


def reply_text(refused: schedule.DoseRefused, language: str | None = None, *, spoken: bool = False) -> str:
    """One short sentence for the patient, in Traditional Chinese or English (`language`, else the patient's).
    On a path with evidence of a pill already swallowed (after_intake) it says the dose was not recorded, and asks
    them to tell family if unwell, instead of "don't take it". spoken=True: the form the robot's voice reads."""
    ref = refused.at or schedule.current_time()
    if (language or refused.language) == "en":
        return _en_sentence(refused, ref)
    return _zh_sentence(refused, ref, spoken)


def refusal_body(refused: schedule.DoseRefused) -> dict:
    """The 409 body: the reason, the dose and its times, the sentence and its language (`speech_text` is what the
    robot's voice reads: Simplified Chinese, or the English sentence)."""
    language = conversation.language_of(refused.language)
    return {"detail": refused.detail, "intk_id": refused.intk_id, "med_name": refused.med_name,
            "scheduled_time": schedule.local_iso(refused.scheduled_time), **refused.fields(),
            "reply": reply_text(refused, language), "language": language, "after_intake": refused.after_intake,
            "speech_text": conversation.speech_text(reply_text(refused, language, spoken=True), language)}


# ── What family reads ────────────────────────────────────────────────────────

def clock_text(when: datetime.datetime, ref: datetime.datetime) -> str:
    """"08:05", or "10/02 22:00" when it is not on the day of `ref`."""
    local = _local(when)
    return local.strftime("%H:%M") if local.date() == _local(ref).date() else local.strftime("%m/%d %H:%M")


def span(delta: datetime.timedelta) -> tuple[str, str]:
    """An exact length: ("1 小時 30 分鐘", "1 h 30 min"), ("45 分鐘", "45 min")."""
    hours, minutes = divmod(max(0, round(delta.total_seconds() / 60)), 60)
    zh = " ".join(part for part in (f"{hours} 小時" if hours else "", f"{minutes} 分鐘" if minutes or not hours else "")
                  if part)
    en = " ".join(part for part in (f"{hours} h" if hours else "", f"{minutes} min" if minutes or not hours else "")
                  if part)
    return zh, en


def about(delta: datetime.timedelta) -> tuple[str, str]:
    """A rounded length: minutes under an hour, else hours to the half hour ("2.5 小時", "2.5 h")."""
    minutes = max(0, round(delta.total_seconds() / 60))
    if minutes < 60:
        return f"{minutes} 分鐘", f"{minutes} min"
    hours = round(minutes / 30) / 2
    text = f"{hours:g}"
    return f"{text} 小時", f"{text} h"


def double_dose_text(patient: str, refused: schedule.DoseRefused, now: datetime.datetime) -> str:
    when = refused.at or now
    time = clock_text(when, now)
    med = refused.med_name or "藥物"
    tail_zh = "請確認藥盒，如有疑慮請聯絡藥師或撥 119。"
    tail_en = "Please check the pill box; if in doubt, call a pharmacist or 119."
    head_en = f"⚠️ Possible double dose: {patient} may have taken {refused.med_name or 'a medicine'} again at {time}"
    if isinstance(refused, DoseTooSoon):
        last = clock_text(refused.last_taken_at, when)
        gap_zh, gap_en = span(refused.gap)
        # A dose still waiting for a caregiver's answer was not "recorded"; it was seen or reported at that time.
        before_zh = (f"{last} 已經有一次在等家人確認" if refused.pending else f"{last} 已經記錄過一次")
        before_en = (f"a dose at {last} is still waiting for family to confirm it" if refused.pending
                     else f"a dose was already recorded at {last}")
        return (f"⚠️ 可能重複服藥：{patient} 在 {time} 可能又吃了 {med}，但 {before_zh}"
                f"（至少要間隔 {gap_zh}）。{tail_zh}\n\n"
                f"{head_en}, but {before_en} (doses must be at least {gap_en} apart). "
                f"{tail_en}")
    today = _local(when).date()
    day_zh, day_en = _day_zh(refused.day, today) or "今天", _day_word_en(refused.day, today)
    return (f"⚠️ 可能重複服藥：{patient} 在 {time} 可能又吃了 {med}，但{day_zh}已經記錄 {refused.taken} 次"
            f"（每天最多 {refused.limit} 次）。{tail_zh}\n\n"
            f"{head_en}, but it was already recorded {_times_en(refused.taken)} {day_en} "
            f"(at most {refused.limit} a day). {tail_en}")


async def alert_family(conn, refused: schedule.DoseRefused, now: datetime.datetime | None = None) -> int:
    """Suspected double dose: a priority-0 LINE alert, through the outbox, to the verified family contacts who get
    missed-dose alerts, at most once per dose in ALERT_EVERY (rolling: 10:59 and 11:01 are one alert). Callers use it
    only for a refusal on a path with evidence that the patient swallowed something (camera commit, the robot's
    confirmation request, a caregiver's 'taken'); a refused button press only shows its sentence. Refusals happen
    only with protection on, so off means no alerts. Sets refused.family_alerted when family has been told."""
    if not isinstance(refused, DOUBLE_DOSE) or refused.u_id is None or refused.intk_id is None:
        return 0
    now = _aware(now or schedule.current_time())
    recent = await conn.fetchval(
        "SELECT 1 FROM notification_outbox WHERE u_id = $1 AND kind = 'double_dose_alert' AND dedupe_key LIKE $2 "
        "AND created_at > $3 LIMIT 1",
        refused.u_id, f"double_dose:{refused.intk_id}:%", now - ALERT_EVERY)
    if recent:
        refused.family_alerted = True
        return 0
    patient = await conn.fetchval('SELECT name FROM "user" WHERE u_id = $1', refused.u_id) or "Patient"
    sent = await outbox.enqueue_to_contacts(
        conn, refused.u_id, kind="double_dose_alert", priority=0,
        messages=[{"type": "text", "text": double_dose_text(patient, refused, now)}],
        dedupe_prefix=f"double_dose:{refused.intk_id}:{int(now.timestamp())}", contact_flag="notify_missed")
    if sent:
        refused.family_alerted = True
        await conn.execute(
            "INSERT INTO notification (u_id, category, type, message) VALUES ($1, 'family', 'double_dose_alert', $2)",
            refused.u_id, f"Possible double dose of {refused.med_name or 'a medicine'} ({refused.detail}) "
                          f"sent to {sent} family contact(s).")
    return sent


async def alert_after(refused: schedule.DoseRefused) -> None:
    """alert_family for a path whose own transaction the refusal rolled back: the alert commits on its own. A failure
    is logged and never hides the refusal itself."""
    if not isinstance(refused, DOUBLE_DOSE):
        return
    try:
        async with get_pool().acquire() as conn, conn.transaction():
            sent = await alert_family(conn, refused)
        if sent:
            outbox.wake()
    except Exception:
        log.exception("double-dose alert for dose %s could not be queued", refused.intk_id)


def request_notes(rows, at: datetime.datetime) -> tuple[list[str], list[str]]:
    """Lines for a caregiver's confirmation request (FACTS_SQL rows judged at `at`): when each medicine was last
    recorded taken, within a day, and how early the dose is when that is more than 30 minutes. They are shown whether
    protection is on or off."""
    at = _aware(at)
    zh, en = [], []
    several = len({row["med_id"] for row in rows}) > 1
    for row in rows:
        name_zh, name_en = (f"{row['med_name']} ", f"{row['med_name']}: ") if several else ("", "")
        last = row.get("last_taken_at")
        if last is not None and at - LAST_NOTE_WITHIN <= _aware(last) <= at:
            ago_zh, ago_en = about(at - _aware(last))
            zh.append(f"{name_zh}上次記錄：{clock_text(last, at)}（{ago_zh}前）")
            en.append(f"{name_en}Last recorded: {clock_text(last, at)} ({ago_en} ago)")
        scheduled = row.get("intake_time_stamp")
        if scheduled is not None and _aware(scheduled) - at > EARLY_NOTE_AFTER:
            ahead_zh, ahead_en = about(_aware(scheduled) - at)
            zh.append(f"{name_zh}比排定時間早 {ahead_zh}")
            en.append(f"{name_en}{ahead_en} before its scheduled time")
    return zh, en


# ── The switch ───────────────────────────────────────────────────────────────

def protection_off_text(patient: str) -> str:
    return (f"⚠️ {patient} 已關閉「防止重複服藥」保護。之後太早、間隔太短、超過每日次數或已錯過的藥都不會再被擋下，"
            f"也不會再發出可能重複服藥的警示。如果這不是您們的決定，請到設定頁重新開啟。\n\n"
            f"⚠️ {patient} turned off overdose protection. Doses taken too early, too close together, over the daily "
            f"maximum or after they were missed will no longer be blocked, and no possible-double-dose alerts will "
            f"be sent. If this wasn't intended, please turn it back on in Settings.")


async def notify_protection_off(conn, u_id: int, now: datetime.datetime | None = None) -> int:
    """Turning protection off is recorded and told to every verified family contact on LINE (through the outbox),
    since it also stops their double-dose alerts. Turning it on again sends nothing."""
    now = _aware(now or datetime.datetime.now(datetime.timezone.utc))
    patient = await conn.fetchval('SELECT name FROM "user" WHERE u_id = $1', u_id) or "Patient"
    await conn.execute(
        "INSERT INTO notification (u_id, category, type, message) VALUES ($1, 'family', 'overdose_protection_off', $2)",
        u_id, "Overdose protection was turned off; family contacts were told on LINE.")
    return await outbox.enqueue_to_contacts(
        conn, u_id, kind="overdose_protection_off", priority=1,
        messages=[{"type": "text", "text": protection_off_text(patient)}],
        dedupe_prefix=f"overdose_off:{u_id}:{int(now.timestamp())}", contact_flag=None)
