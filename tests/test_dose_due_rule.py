"""One rule on every path: a scheduled dose is not started (robot task, camera session) or recorded as taken (camera,
caregiver confirmation, manual tap, the patient's claim) more than DOSE_EARLY_MINUTES before its time.

Includes the 3 Oct 2026 incident: test alerts sent between 00:05 and 00:17 used that day's 08:00, 12:00 and 20:00
doses, and family confirmations recorded all three as taken by 01:18.
"""

import asyncio
import datetime
import os
import sys
import types
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

sys.path.insert(0, os.path.dirname(__file__))
import dose_facts  # noqa: E402
from test_dose_confirmation import FakeDB  # noqa: E402

from app import config  # noqa: E402
from app.routers import api_medications, medicines  # noqa: E402
from app.services import dose_confirmation, dose_safety, intake_repository, schedule  # noqa: E402

UTC = datetime.timezone.utc
TAIPEI = ZoneInfo("Asia/Taipei")
ALLEGRA = {"morning": True, "noon": True, "night": True, "bedtime": True}   # 08:00, 12:00, 20:00, 22:00


def taipei(day: int, hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 10, day, hour, minute, tzinfo=TAIPEI)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def clock(monkeypatch):
    """The rule's clock, starting at 00:05 on 3 Oct 2026 in Taipei (the first test alert)."""
    state = types.SimpleNamespace(now=taipei(3, 0, 5))
    monkeypatch.setattr(schedule, "_now", lambda: state.now.astimezone(UTC))
    return state


# ── The rule ─────────────────────────────────────────────────────────────────

def test_default_window_is_120_minutes():
    if "DOSE_EARLY_MINUTES" not in os.environ:
        assert config.DOSE_EARLY_MINUTES == 120
    assert schedule.DOSE_EARLY == datetime.timedelta(minutes=config.DOSE_EARLY_MINUTES)


def test_due_from_the_window_before_its_time(clock):
    clock.now = taipei(3, 6, 0)
    assert schedule.is_due(taipei(3, 8, 0))                    # exactly DOSE_EARLY ahead
    assert not schedule.is_due(taipei(3, 8, 1))
    assert schedule.is_due(taipei(2, 22, 0))                   # overdue
    assert schedule.is_due(None)                               # no time: an ad-hoc dose is always due
    assert schedule.is_due(datetime.datetime(2026, 10, 2, 23, 0))   # naive values are UTC (07:00 Taipei)
    assert schedule.due_from(taipei(3, 8, 0)) == taipei(3, 6, 0)
    assert schedule.due_by() == taipei(3, 8, 0)
    assert schedule.is_due(taipei(3, 20, 0), now=taipei(3, 18, 0))   # an explicit moment wins over the clock


def test_a_dose_is_not_due_before_halfway_from_the_same_medicines_previous_dose(clock):
    """The user's allegra is at 08:00, 12:00, 20:00 and 22:00. 20:00 and 22:00 are exactly DOSE_EARLY apart, so
    without the halfway bound both were due from 20:00 and could be recorded minutes apart."""
    night, bedtime = taipei(3, 20), taipei(3, 22)
    assert schedule.due_from(bedtime, night) == taipei(3, 21)
    clock.now = taipei(3, 20, 5)
    assert not schedule.is_due(bedtime, previous=night)
    assert schedule.is_due(bedtime)                                    # the window alone would have allowed it
    clock.now = taipei(3, 21)
    assert schedule.is_due(bedtime, previous=night)                    # halfway is inclusive, like the window
    # A wide gap leaves the window as it is (12:00 after 08:00: from 10:00; 08:00 after 22:00: from 06:00).
    assert schedule.due_from(taipei(3, 12), taipei(3, 8)) == taipei(3, 10)
    assert schedule.due_from(taipei(3, 8), taipei(2, 22)) == taipei(3, 6)
    # Custom times an hour apart: each from halfway, never all three at 08:00.
    assert [schedule.due_from(taipei(3, h), taipei(3, h - 1)) for h in (9, 10)] == [taipei(3, 8, 30),
                                                                                   taipei(3, 9, 30)]
    assert schedule.due_from(bedtime, bedtime) == taipei(3, 20)        # a previous that is not earlier is ignored
    with pytest.raises(schedule.DoseNotDueYet) as refused:
        schedule.require_due(bedtime, 8, now=taipei(3, 20, 59), previous=night)
    assert refused.value.body()["due_from"] == "2026-10-03T21:00:00+08:00"


def test_due_sql_is_the_same_rule_with_the_previous_dose():
    sql = schedule.due_sql("i", "$4::timestamptz")
    early = f"make_interval(secs => {int(schedule.DOSE_EARLY.total_seconds())})"
    assert sql == (f"i.intake_time_stamp <= $4::timestamptz + LEAST({early}, "
                   f"COALESCE((i.intake_time_stamp - {schedule.previous_sql('i')}) / 2, {early}))")
    assert "prev_dose.u_id = i.u_id AND prev_dose.med_id = i.med_id" in schedule.previous_sql("i")
    assert "prev_dose.intake_time_stamp < i.intake_time_stamp" in schedule.previous_sql("i")


def test_not_due_error_names_the_patients_times_and_is_not_a_value_error(clock):
    with pytest.raises(schedule.DoseNotDueYet) as refused:
        schedule.require_due(taipei(3, 20, 0).astimezone(UTC), 7)
    assert not isinstance(refused.value, ValueError)   # routers' generic ValueError -> 409 must not swallow it
    assert isinstance(refused.value, schedule.DoseRefused)
    assert refused.value.body() == {"detail": "dose_not_due_yet", "intk_id": 7, "med_name": None,
                                    "scheduled_time": "2026-10-03T20:00:00+08:00",
                                    "due_from": "2026-10-03T18:00:00+08:00",
                                    "reply": "現在還不是吃這個藥的時間，晚上6點以後才可以。", "language": "zh-TW",
                                    "after_intake": False,
                                    "speech_text": "现在还不是吃这个药的时间，晚上6点以后才可以。"}
    schedule.require_due(taipei(3, 2, 0))   # due: no error


def test_app_answers_409_with_the_dose_time(clock):
    import json

    from app.main import app, dose_refused

    assert app.exception_handlers[schedule.DoseRefused] is dose_refused
    response = run(dose_refused(None, schedule.DoseNotDueYet(taipei(3, 8, 0), 5)))
    assert response.status_code == 409
    assert json.loads(response.body) == {"detail": "dose_not_due_yet", "intk_id": 5, "med_name": None,
                                         "scheduled_time": "2026-10-03T08:00:00+08:00",
                                         "due_from": "2026-10-03T06:00:00+08:00",
                                         "reply": "現在還不是吃這個藥的時間，早上6點以後才可以。",
                                         "language": "zh-TW", "after_intake": False,
                                         "speech_text": "现在还不是吃这个药的时间，早上6点以后才可以。"}


# ── intake_repository: manual/app tap and camera commit ─────────────────────

class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class IntakeConn:
    """intake / medication / monitor_event, enough for transition_intake and commit_monitored. All rows are one
    medicine (med 1) of patient 7; a taken dose gets the rule's clock as its actual time."""

    def __init__(self, intakes, med=None, protection=True):
        self.intakes = intakes
        self.med = {"med_name": "allegra", **(med or {})}
        self.protection = protection
        self.stock = Decimal("10")
        self.writes = []
        self.facts_queries = []
        self.locks = []         # dose_safety.LOCK_SQL, before the facts on recording paths

    def transaction(self):
        return _Transaction()

    def acquire(self):
        conn = self

        class _Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *args):
                return False
        return _Acquire()

    async def fetchrow(self, query, *args):
        if "FROM intake WHERE intk_id=$1 AND u_id=$2" in query:
            assert "FOR UPDATE" in query
            row = self.intakes.get(args[0])
            if not row or args[1] != 7:
                return None
            return {"intk_id": args[0], "med_id": 1, "units_taken": None, **row}
        if "FROM monitor_event" in query:
            return None
        if "UPDATE medication SET pills_remaining=pills_remaining-units_per_dose" in query:
            self.stock -= 1
            self.writes.append("stock")
            return {"pills_remaining": self.stock, "units_per_dose": Decimal("1")}
        raise AssertionError(query)

    async def fetch(self, query, *args):
        assert dose_facts.is_facts(query), query
        # Recording locks the medication row first, in a statement of its own.
        assert len(self.locks) > len(self.facts_queries), "facts read before the medication row was locked"
        self.facts_queries.append(args)
        intakes = {i: {"u_id": 7, "med_id": 1, **r} for i, r in self.intakes.items()}
        return dose_facts.rows(args, intakes, {1: self.med}, protection=self.protection)

    async def execute(self, query, *args):
        if dose_facts.is_lock(query):
            self.locks.append(args)
            return "SELECT"
        self.writes.append(query.split(" WHERE")[0])
        if query.startswith("UPDATE intake SET intake_stats=$1::varchar"):
            row = self.intakes[args[2]]
            row["intake_stats"] = args[0]
            row["actual_intake_time"] = schedule.current_time() if args[0] == "taken" else None
        elif query.startswith("UPDATE intake SET intake_stats='taken'"):
            assert "taken_notified=FALSE" in query         # recorded again: family hears of it again
            self.intakes[args[3]].update(intake_stats="taken", actual_intake_time=schedule.current_time())


def _today():
    return {5: {"intake_stats": "pending", "intake_time_stamp": taipei(3, 8).astimezone(UTC)},
            6: {"intake_stats": "pending", "intake_time_stamp": taipei(3, 12).astimezone(UTC)},
            7: {"intake_stats": "pending", "intake_time_stamp": taipei(3, 20).astimezone(UTC)}}


def test_manual_taken_is_refused_before_due_but_skip_is_allowed(clock, monkeypatch):
    conn = IntakeConn(_today())
    monkeypatch.setattr(intake_repository, "get_pool", lambda: conn)
    with pytest.raises(schedule.DoseNotDueYet) as refused:
        run(intake_repository.transition_intake(7, 5, "taken"))
    assert refused.value.intk_id == 5
    assert conn.intakes[5]["intake_stats"] == "pending" and conn.stock == 10 and conn.writes == []
    # Skipping a later dose takes no pill (e.g. the doctor stopped it today).
    assert run(intake_repository.transition_intake(7, 7, "skipped"))["changed"] is True
    clock.now = taipei(3, 6, 0)
    assert run(intake_repository.transition_intake(7, 5, "taken"))["status"] == "taken"
    assert conn.stock == 9


def test_the_bedtime_dose_cannot_be_taken_right_after_the_night_dose(clock, monkeypatch):
    """20:00 taken at 20:05; the 22:00 dose of the same medicine (allegra, 08/12/20/22) is due from 21:00, and an
    hour (the default gap: half its shortest gap) after the 20:05 pill, from 21:05."""
    intakes = {**_today(), 8: {"intake_stats": "pending", "intake_time_stamp": taipei(3, 22).astimezone(UTC)}}
    conn = IntakeConn(intakes, med={"schedule_time": ALLEGRA})
    monkeypatch.setattr(intake_repository, "get_pool", lambda: conn)
    clock.now = taipei(3, 20, 5)
    assert run(intake_repository.transition_intake(7, 7, "taken"))["status"] == "taken"
    with pytest.raises(schedule.DoseNotDueYet) as refused:
        run(intake_repository.transition_intake(7, 8, "taken"))
    assert refused.value.body()["due_from"] == "2026-10-03T21:00:00+08:00"
    candidate = {"event_id": str(uuid.uuid4()), "confidence": .9, "decision": "confirmed"}
    with pytest.raises(schedule.DoseNotDueYet):
        run(intake_repository.commit_monitored(_session(8), candidate, "auto"))
    assert conn.intakes[8]["intake_stats"] == "pending" and conn.stock == 9
    clock.now = taipei(3, 21, 0)
    with pytest.raises(dose_safety.DoseTooSoon) as refused:
        run(intake_repository.transition_intake(7, 8, "taken"))
    body = refused.value.body()
    assert body["last_taken_at"] == "2026-10-03T20:05:00+08:00"
    assert body["next_allowed_at"] == "2026-10-03T21:05:00+08:00"
    assert body["reply"] == "這個藥您晚上8點05分已經吃過了，請先不要再吃。"
    clock.now = taipei(3, 21, 5)
    assert run(intake_repository.transition_intake(7, 8, "taken"))["status"] == "taken"
    assert conn.stock == 8


def _session(intk_id):
    return types.SimpleNamespace(u_id=7, intk_id=intk_id, session_id=str(uuid.uuid4()), clip_enabled=False,
                                 event_started_at=None)


def test_camera_commit_is_refused_before_due(clock, monkeypatch):
    conn = IntakeConn(_today())
    monkeypatch.setattr(intake_repository, "get_pool", lambda: conn)
    candidate = {"event_id": str(uuid.uuid4()), "confidence": .9, "decision": "confirmed"}
    for method in ("auto", "confirmed_by_user", "reachy_prompted"):
        with pytest.raises(schedule.DoseNotDueYet):
            run(intake_repository.commit_monitored(_session(6), candidate, method))
    assert conn.intakes[6]["intake_stats"] == "pending" and conn.stock == 10 and conn.writes == []
    clock.now = taipei(3, 10, 30)
    assert run(intake_repository.commit_monitored(_session(6), candidate, "auto"))["status"] == "taken"
    assert conn.stock == 9


# ── The 3 Oct 2026 incident, end to end through dose confirmation ───────────

def _incident_db() -> FakeDB:
    db = FakeDB()
    db.meds[40]["pills_remaining"] = 20
    db.intakes = {intk_id: {**FakeDB._intake(7, 40, "pending"), "intake_time_stamp": taipei(3, hour).astimezone(UTC)}
                  for intk_id, hour in ((5, 8), (6, 12), (7, 20))}
    return db


def test_midnight_test_alerts_can_no_longer_ask_family_to_confirm_todays_doses(clock):
    """00:05, 00:09 and 00:17: the robot filed a patient claim / degraded camera request for each dose."""
    db = _incident_db()
    for moment, intk_id, source in ((taipei(3, 0, 5), 5, "patient_claim"), (taipei(3, 0, 9), 6, "degraded"),
                                    (taipei(3, 0, 17), 7, "patient_claim")):
        clock.now = moment
        with pytest.raises(schedule.DoseNotDueYet) as refused:
            run(dose_confirmation.create(db, u_id=7, task_id=None, intk_ids=[intk_id], source=source,
                                         evidence={"said_done": True}))
        assert refused.value.intk_id == intk_id
    assert {i: r["intake_stats"] for i, r in db.intakes.items()} == {5: "pending", 6: "pending", 7: "pending"}
    assert db.confirmations == {} and db.outbox == [] and db.meds[40]["pills_remaining"] == 20
    # From 06:00 the 08:00 dose may be confirmed; the 12:00 one still may not.
    clock.now = taipei(3, 6, 0)
    run(dose_confirmation.create(db, u_id=7, task_id=None, intk_ids=[5], source="patient_claim", evidence=None))
    assert db.intakes[5]["intake_stats"] == "pending_confirmation"
    with pytest.raises(schedule.DoseNotDueYet):
        run(dose_confirmation.create(db, u_id=7, task_id=None, intk_ids=[6], source="degraded", evidence=None))


def test_confirmations_made_before_the_rule_record_nothing_when_family_answers_taken():
    """The three requests that were open on 3 Oct: answered 'taken' at 00:06, 00:09 and 01:18."""
    db = _incident_db()
    asked = {5: taipei(3, 0, 5), 6: taipei(3, 0, 9), 7: taipei(3, 0, 17)}
    for intk_id, created in asked.items():
        confirmation_id = str(uuid.uuid4())
        db.intakes[intk_id]["intake_stats"] = "pending_confirmation"
        db.confirmations[confirmation_id] = {
            "confirmation_id": confirmation_id, "u_id": 7, "task_id": None, "intk_ids": [intk_id],
            "previous_status": {str(intk_id): "pending"}, "source": "patient_claim", "evidence": None,
            "created_at": created.astimezone(UTC), "reminded_at": None, "resolved_at": None, "resolution": None,
            "resolved_by": None}
        data = dose_confirmation.sign_postback(dose_confirmation.ACTION, confirmation_id, "taken", 1)
        result = run(dose_confirmation.handle_postback(db, data, "U-amy"))
        assert result["status"] == "resolved" and result["not_due"] == [intk_id] and result["intk_ids"] == []
    assert {i: r["intake_stats"] for i, r in db.intakes.items()} == {5: "pending", 6: "pending", 7: "pending"}
    assert all(r["detection_method"] is None for r in db.intakes.values())
    assert db.meds[40]["pills_remaining"] == 20
    acks = [row for row in db.outbox if row["dedupe_key"].startswith("dose_confirm_ack:")]
    assert len(acks) == 3
    for ack in acks:
        text = ack["payload"]["messages"][0]["text"]
        assert text.startswith("未記錄：Pearl 10/03 ") and "Not recorded" in text and "was not due yet" in text
    # The other caregiver (Ben) is not told "answered: taken" for a dose that was not recorded,
    notices = [row for row in db.outbox if row["dedupe_key"].startswith("dose_confirm_answered:")]
    assert len(notices) == 3
    for notice in notices:
        text = notice["payload"]["messages"][0]["text"]
        assert "沒有記錄為已服用" in text and "was not due yet, so it was not recorded" in text
        assert "answered: taken (" not in text
    # and a request that recorded nothing is closed as expired, so a late answer hears "expired".
    assert {c["resolution"] for c in db.confirmations.values()} == {"expired"}
    late_id = next(iter(db.confirmations))
    data = dose_confirmation.sign_postback(dose_confirmation.ACTION, late_id, "taken", 2)
    assert run(dose_confirmation.handle_postback(db, data, "U-ben"))["status"] == "already_resolved"
    late = [row for row in db.outbox if row["dedupe_key"] == f"dose_confirm_late:{late_id}:2"]
    assert "已逾時" in late[0]["payload"]["messages"][0]["text"]


# ── Take Now, the legacy page and today's list ──────────────────────────────

class _Rows:
    def __init__(self, rows=None, row=None):
        self.rows, self.row, self.calls = rows or [], row, []

    def acquire(self):
        conn = self

        class _Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *args):
                return False
        return _Acquire()

    async def fetch(self, query, *args):
        return self.rows

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        return self.row

    async def execute(self, query, *args):
        self.calls.append((query, args))


def test_today_lists_from_when_each_dose_is_due(monkeypatch):
    stamp = taipei(3, 8).astimezone(UTC)
    rows = [{"intake_id": 5, "id": 5, "med_id": 40, "name": "Metformin", "status": "pending",
             "scheduled_time": stamp, "previous_time": None, "schedule_time": None, "use_before": None},
            {"intake_id": 8, "id": 8, "med_id": 40, "name": "Metformin", "status": "pending",
             "scheduled_time": taipei(3, 22), "previous_time": taipei(3, 20), "schedule_time": None,
             "use_before": None},
            {"intake_id": 9, "id": 9, "med_id": 40, "name": "Metformin", "status": "pending",
             "scheduled_time": None, "previous_time": None, "schedule_time": None, "use_before": None}]
    conn = _Rows(rows=rows)
    seen = []

    async def fetch(query, *args):
        seen.append(query)
        return rows

    conn.fetch = fetch
    monkeypatch.setattr(api_medications, "get_pool", lambda: conn)
    today = run(api_medications.today_medications({"u_id": 7}, "2026-10-03"))
    assert f"{schedule.previous_sql('i')} AS previous_time" in seen[0]
    assert today[0]["due_from"] == stamp - schedule.DOSE_EARLY and today[2]["due_from"] is None
    assert today[1]["due_from"] == taipei(3, 21)                       # halfway from the 20:00 dose
    assert all("previous_time" not in row for row in today)


@pytest.mark.parametrize("status", ["taken", "skipped"])
def test_legacy_page_records_through_the_rules(monkeypatch, status):
    """/medicines/{id}/taken wrote intake_stats directly: no due check, no stock, any user's row. Now it goes through
    transition_intake (overdose protection included) for today's open dose a pill counts for."""
    conn = _Rows(row=None)
    calls = []

    async def transition(u_id, intk_id, new_status):
        calls.append((u_id, intk_id, new_status))
        return {"intk_id": intk_id, "status": new_status, "changed": True}

    monkeypatch.setattr(medicines, "current_user", lambda request: {"u_id": 7})
    monkeypatch.setattr(medicines, "get_pool", lambda: conn)
    monkeypatch.setattr(medicines, "transition_intake", transition)
    response = run(medicines._update_intake(None, 40, status))
    assert response.status_code == 409 and calls == []          # no open dose today: nothing recorded or created
    query, args = conn.calls[0]
    assert "u_id=$2" in query and "intake_stats IN ('pending','missed')" in query
    med_id, u_id, local_start, now, day_end = args
    assert (med_id, u_id) == (40, 7) and day_end == local_start + datetime.timedelta(days=1)
    # 'taken': a dose a pill taken now counts for first (open_sql), else today's nearest open dose, so that the
    # rules say why it is refused (or, with protection off, record it as before). Skipping any dose of today is
    # allowed. Nearest to now, the earlier one on a tie: the same choice as Take Now and the app's dueDose.
    nearest = "ABS(EXTRACT(EPOCH FROM (intake_time_stamp - $4::timestamptz))), intake_time_stamp LIMIT 1"
    first = f"ORDER BY ({dose_safety.open_sql('intake', '$4::timestamptz')}) DESC, "
    assert (first + nearest in query) == (status == "taken")
    assert (f"ORDER BY {nearest}" in query) == (status == "skipped")
    conn.row = {"intk_id": 5}
    assert run(medicines._update_intake(None, 40, status)) == {"status": status, "med_id": 40}
    assert calls == [(7, 5, status)]
