"""Overdose protection (「防止重複服藥」, services/dose_safety.py): the four rules and their defaults, the patient's
switch, the paths that start or record a dose, family alerts only with evidence, the LINE confirmation notes, and the
3 Oct 2026 incident (three test reminders recorded three doses of one medicine in about an hour).
"""

import asyncio
import datetime
import json
import os
import sys
import types
import uuid
from zoneinfo import ZoneInfo

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

sys.path.insert(0, os.path.dirname(__file__))
import dose_facts  # noqa: E402
from test_dose_confirmation import FakeDB, FakePool  # noqa: E402
from test_dose_due_rule import IntakeConn  # noqa: E402

from app.jobs import taken_confirmation_job  # noqa: E402
from app.routers import api_notify, medicines  # noqa: E402
from app.services import dose_confirmation, dose_safety, intake_repository, schedule  # noqa: E402

UTC = datetime.timezone.utc
TAIPEI = ZoneInfo("Asia/Taipei")
HOUR = datetime.timedelta(hours=1)
ALLEGRA = {"morning": True, "noon": True, "night": True, "bedtime": True}   # 08:00, 12:00, 20:00, 22:00
ONCE_A_DAY = {"morning": True}


def taipei(day: int, hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 10, day, hour, minute, tzinfo=TAIPEI)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def clock(monkeypatch):
    state = types.SimpleNamespace(now=taipei(3, 0, 5))
    monkeypatch.setattr(schedule, "_now", lambda: state.now.astimezone(UTC))
    return state


def dose(day, hour, minute=0, status="pending", taken_at=None):
    return {"intake_time_stamp": taipei(day, hour, minute).astimezone(UTC), "intake_stats": status,
            "actual_intake_time": taken_at.astimezone(UTC) if taken_at else None}


def allegra_day(day=3, taken=None):
    """The four allegra doses of a day, ids day*100 + hour; taken={hour: when} marks them taken."""
    taken = taken or {}
    return {day * 100 + hour: dose(day, hour, status="taken" if hour in taken else "pending",
                                   taken_at=taken.get(hour))
            for hour in (8, 12, 20, 22)}


def all_four_taken(day=3):
    return allegra_day(day, {hour: taipei(day, hour) for hour in (8, 12, 20, 22)})


def judge(intakes, intk_id, at, schedule_time=ALLEGRA, protection=True, language=None, confirmations=None,
          started_at=None, **med):
    """dose_safety.evaluate on the FACTS_SQL row of `intk_id` (one medicine, patient 7) at `at`."""
    world = {i: {"u_id": 7, "med_id": 1, **row} for i, row in intakes.items()}
    meds = {1: {"med_name": "allegra", "schedule_time": schedule_time, **med}}
    (row,) = dose_facts.rows((7, [intk_id], at.astimezone(UTC), "Asia/Taipei"), world, meds,
                             protection=protection, language=language, confirmations=confirmations)
    return dose_safety.evaluate(row, at, started_at=started_at)


# ── The limits a medicine gets ───────────────────────────────────────────────

@pytest.mark.parametrize("schedule_time, own, minutes", [
    (ALLEGRA, None, 60),                                    # 20:00 and 22:00 are 2 h apart
    (ONCE_A_DAY, None, 12 * 60),                            # one time a day: 12 h
    ({"morning": True, "night": True}, None, 6 * 60),
    ({"custom_times": ["23:00", "01:00"]}, None, 60),       # across midnight: 23:00 -> 01:00 is 2 h
    ({"morning": True, "weekdays": [1]}, None, 12 * 60),
    (None, None, 240), ({}, None, 240),                     # unscheduled (as needed): 4 h
    (ALLEGRA, 90, 90), (None, 30, 30),                      # the medicine's own minimum
])
def test_minimum_gap_defaults(schedule_time, own, minutes):
    assert dose_safety.min_gap(schedule_time, own) == datetime.timedelta(minutes=minutes)


@pytest.mark.parametrize("schedule_time, own, limit", [
    (ALLEGRA, None, 4), (ONCE_A_DAY, None, 1), ({"noon": True, "custom_times": ["09:30"]}, None, 2),
    ({"morning": True, "weekdays": [1, 3]}, None, 1),       # a weekday without doses still allows a late dose
    (None, None, None), ({}, None, None),                   # unscheduled: no limit unless set
    (None, 3, 3), (ALLEGRA, 2, 2),
])
def test_daily_maximum_defaults(schedule_time, own, limit):
    assert dose_safety.daily_max(schedule_time, own) == limit


def test_a_missed_dose_expires_halfway_to_the_next():
    assert dose_safety.expires_at(taipei(3, 8), taipei(3, 12)) == taipei(3, 10)
    assert dose_safety.expires_at(taipei(3, 22), taipei(4, 8)) == taipei(4, 3)   # last of the day: the next day's first
    assert dose_safety.expires_at(taipei(3, 20), None) is None                  # no later dose: never
    assert dose_safety.expires_at(None, taipei(3, 8)) is None
    assert dose_safety.expires_at(taipei(3, 8), taipei(3, 8)) is None


# ── The rules, in order, and the switch ─────────────────────────────────────

def test_each_rule_refuses_with_its_reason_and_sentence():
    # R1: 22:00 right after the 20:00 dose is due only from 21:00.
    refused = judge(allegra_day(), 322, taipei(3, 20, 30))
    assert isinstance(refused, schedule.DoseNotDueYet)
    body = refused.body()
    assert body["due_from"] == "2026-10-03T21:00:00+08:00" and body["scheduled_time"] == "2026-10-03T22:00:00+08:00"
    assert body["reply"] == "現在還不是吃這個藥的時間，晚上9點以後才可以。"
    assert body["speech_text"] == "现在还不是吃这个药的时间，晚上9点以后才可以。"
    # R2: a dose taken at 00:05 (a test reminder); another one at 00:20 is too soon.
    intakes = {**allegra_day(), 1: dose(3, 0, 5, status="taken", taken_at=taipei(3, 0, 5)), 2: dose(3, 0, 20)}
    refused = judge(intakes, 2, taipei(3, 0, 20))
    assert isinstance(refused, dose_safety.DoseTooSoon)
    body = refused.body()
    assert body["detail"] == "dose_too_soon" and body["last_taken_at"] == "2026-10-03T00:05:00+08:00"
    assert body["next_allowed_at"] == "2026-10-03T01:05:00+08:00" and body["min_interval_minutes"] == 60
    assert body["reply"] == "這個藥您凌晨12點05分已經吃過了，請先不要再吃。"
    assert body["speech_text"] == "这个药您凌晨12点05分已经吃过了，请先不要再吃。"
    assert judge(intakes, 2, taipei(3, 1, 5)) is None                       # an hour later (the gap is inclusive)
    # R3: four allegra doses taken today; a fifth (an ad-hoc dose at 23:30) is over the maximum.
    refused = judge({**all_four_taken(), 9: dose(3, 23, 30)}, 9, taipei(3, 23, 30))
    assert isinstance(refused, dose_safety.DailyMaxReached)
    body = refused.body()
    assert body["taken_today"] == 4 and body["max_daily_doses"] == 4
    assert body["next_allowed_at"] == "2026-10-04T00:00:00+08:00"
    assert body["reply"] == "今天的 allegra 已經吃滿 4 次了，請不要再吃。"
    # R4: 08:00 missed; at 10:30 it expired at 10:00 (halfway to 12:00), and the 12:00 dose is the one to take.
    refused = judge(allegra_day(), 308, taipei(3, 10, 30))
    assert isinstance(refused, dose_safety.DoseExpired)
    body = refused.body()
    assert body["expired_at"] == "2026-10-03T10:00:00+08:00"
    assert body["reply"] == "早上8點的藥已經錯過了，請不要補吃，等下一次就好。"
    assert judge(allegra_day(), 312, taipei(3, 10, 30)) is None


def test_refusals_are_not_value_errors_and_the_app_answers_each_with_409():
    from app.main import app, dose_refused

    refused = judge(allegra_day(), 308, taipei(3, 10, 30))
    assert not isinstance(refused, ValueError) and isinstance(refused, schedule.DoseRefused)
    assert app.exception_handlers[schedule.DoseRefused] is dose_refused
    response = run(dose_refused(None, refused))
    assert response.status_code == 409 and json.loads(response.body)["detail"] == "dose_expired"


def test_the_sentence_follows_the_patients_language():
    refused = judge(allegra_day(), 308, taipei(3, 10, 30), language="en")
    body = refused.body()
    assert body["reply"] == "The 8 am dose was missed, so please don't make it up; just wait for the next one."
    assert body["speech_text"] == body["reply"]
    body = judge(allegra_day(), 322, taipei(3, 20, 30), language="en").body()
    assert body["reply"] == "It's not time for this medicine yet; you can take it after 9 pm."
    intakes = {**allegra_day(), 1: dose(3, 0, 5, status="taken", taken_at=taipei(3, 0, 5)), 2: dose(3, 0, 20)}
    assert judge(intakes, 2, taipei(3, 0, 20), language="en").body()["reply"] == (
        "You already took this medicine at 12:05 am, so please don't take it again yet.")
    assert judge({**all_four_taken(), 9: dose(3, 23, 30)}, 9, taipei(3, 23, 30), language="en").body()["reply"] == (
        "You've already taken allegra 4 times today, so please don't take any more.")


def test_a_due_time_on_another_day_says_which_day():
    # The 08:00 dose after midnight: due from 06:00 "tomorrow" when asked at 23:30 the day before.
    refused = judge(allegra_day(4), 408, taipei(3, 23, 30))
    assert refused.body()["reply"] == "現在還不是吃這個藥的時間，明天早上6點以後才可以。"
    assert judge(allegra_day(4), 408, taipei(3, 23, 30), language="en").body()["reply"].endswith(
        "after 6 am tomorrow.")


def test_with_protection_off_no_rule_refuses_anything():
    too_soon = {1: dose(3, 0, 5, status="taken", taken_at=taipei(3, 0, 5)), 2: dose(3, 0, 20)}
    cases = [(allegra_day(), 322, taipei(3, 20, 30)), (allegra_day(), 308, taipei(3, 10, 30)),
             ({**all_four_taken(), 9: dose(3, 23, 30)}, 9, taipei(3, 23, 30)), (allegra_day(4), 408, taipei(3, 0, 5)),
             (too_soon, 2, taipei(3, 0, 20))]
    for intakes, intk_id, at in cases:
        assert judge(intakes, intk_id, at) is not None
        assert judge(intakes, intk_id, at, protection=False) is None


def test_the_gap_counts_from_the_taken_dose_nearest_the_moment_judged():
    """A caregiver's answer is judged when the patient was asked; a dose taken after that moment counts too."""
    intakes = {1: dose(3, 8, status="pending_confirmation"),
               2: dose(3, 8, 30, status="taken", taken_at=taipei(3, 8, 30))}
    refused = judge(intakes, 1, taipei(3, 8), schedule_time=None)
    assert isinstance(refused, dose_safety.DoseTooSoon) and refused.last_taken_at == taipei(3, 8, 30)


# ── Midnight in Taipei ───────────────────────────────────────────────────────

def test_a_bedtime_dose_taken_after_midnight_counts_for_its_own_day():
    """22:00 taken at 00:30: allowed (it expires at 03:00), and the next day's four doses are all still allowed."""
    day3 = allegra_day(3, {8: taipei(3, 8), 12: taipei(3, 12), 20: taipei(3, 20)})
    intakes = {**day3, **allegra_day(4)}
    assert judge(intakes, 322, taipei(4, 0, 30)) is None
    intakes[322].update(intake_stats="taken", actual_intake_time=taipei(4, 0, 30).astimezone(UTC))
    for hour in (8, 12, 20):
        assert judge(intakes, 400 + hour, taipei(4, hour)) is None
        intakes[400 + hour].update(intake_stats="taken", actual_intake_time=taipei(4, hour).astimezone(UTC))
    assert judge(intakes, 422, taipei(4, 22)) is None                 # the 00:30 dose counted for 3 Oct
    # Had it not been taken by 03:00 (halfway to 08:00), it stays missed.
    assert isinstance(judge({**day3, **allegra_day(4)}, 322, taipei(4, 3, 0)), dose_safety.DoseExpired)
    assert judge({**day3, **allegra_day(4)}, 322, taipei(4, 2, 59)) is None


def test_the_daily_maximum_resets_at_midnight_in_taipei_not_utc():
    """Two a day (own maximum, 30 min apart at least). 23:30 and 23:50 on 3 Oct and 00:30 on 4 Oct are all on the
    same UTC day (15:30-16:30 UTC), but 00:30 is another day for the patient."""
    intakes = {1: dose(3, 23, 30, status="taken", taken_at=taipei(3, 23, 30)),
               2: dose(3, 23, 50, status="taken", taken_at=taipei(3, 23, 50)),
               3: dose(3, 23, 55), 4: dose(4, 0, 30)}
    refused = judge(intakes, 3, taipei(3, 23, 55), schedule_time=None, max_daily_doses=2, min_interval_minutes=30)
    assert isinstance(refused, dose_safety.DailyMaxReached)
    assert refused.body()["next_allowed_at"] == "2026-10-04T00:00:00+08:00"
    assert judge(intakes, 4, taipei(4, 0, 30), schedule_time=None, max_daily_doses=2, min_interval_minutes=30) is None
    assert dose_safety.FACTS_SQL.count("AT TIME ZONE $4::text") == 2


# ── The 3 Oct 2026 incident ──────────────────────────────────────────────────

def _incident_conn(protection=True):
    """Allegra's four doses on 3 Oct and the three doses the test reminders produced at 00:05, 00:09 and 00:17."""
    intakes = {**allegra_day(), 1: dose(3, 0, 5), 2: dose(3, 0, 9), 3: dose(3, 0, 17)}
    return IntakeConn(intakes, med={"schedule_time": ALLEGRA}, protection=protection)


def test_3_oct_three_doses_of_one_medicine_within_an_hour_are_refused(clock, monkeypatch):
    conn = _incident_conn()
    monkeypatch.setattr(intake_repository, "get_pool", lambda: conn)
    clock.now = taipei(3, 0, 5)
    assert run(intake_repository.transition_intake(7, 1, "taken"))["status"] == "taken"
    for moment, intk_id in ((taipei(3, 0, 9), 2), (taipei(3, 0, 17), 3)):
        clock.now = moment
        with pytest.raises(dose_safety.DoseTooSoon):
            run(intake_repository.transition_intake(7, intk_id, "taken"))
    # Nor any of the day's scheduled doses at that hour: not due (and the 08:00 one would be too soon as well).
    for intk_id in (308, 312, 320):
        with pytest.raises(schedule.DoseNotDueYet):
            run(intake_repository.transition_intake(7, intk_id, "taken"))
    assert conn.stock == 9 and [i for i, r in conn.intakes.items() if r["intake_stats"] == "taken"] == [1]


def test_3_oct_with_protection_off_records_as_before(clock, monkeypatch):
    conn = _incident_conn(protection=False)
    monkeypatch.setattr(intake_repository, "get_pool", lambda: conn)
    for moment, intk_id in ((taipei(3, 0, 5), 1), (taipei(3, 0, 9), 2), (taipei(3, 0, 17), 3), (taipei(3, 0, 18), 308)):
        clock.now = moment
        assert run(intake_repository.transition_intake(7, intk_id, "taken"))["status"] == "taken"
    assert conn.stock == 6


# ── Paths: manual tap and camera commit (alerts only with evidence) ─────────

def _session(intk_id):
    return types.SimpleNamespace(u_id=7, intk_id=intk_id, session_id=str(uuid.uuid4()), clip_enabled=False,
                                 event_started_at=None)


def _candidate():
    return {"event_id": str(uuid.uuid4()), "confidence": .9, "decision": "confirmed"}


@pytest.fixture
def family(monkeypatch):
    """The family side (contacts, outbox, notifications) of alerts that commit on their own connection."""
    db = FakeDB()
    monkeypatch.setattr(dose_safety, "get_pool", lambda: FakePool(db))
    return db


def _alerts(db):
    return [row for row in db.outbox if row["kind"] == "double_dose_alert"]


def test_a_refused_button_press_only_shows_its_sentence(clock, monkeypatch, family):
    conn = IntakeConn(allegra_day(3, {20: taipei(3, 20, 5)}), med={"schedule_time": ALLEGRA})
    monkeypatch.setattr(intake_repository, "get_pool", lambda: conn)
    clock.now = taipei(3, 21, 0)
    with pytest.raises(dose_safety.DoseTooSoon):
        run(intake_repository.transition_intake(7, 322, "taken"))
    assert _alerts(family) == [] and conn.stock == 10


def test_a_camera_commit_of_a_second_dose_alerts_family_once_an_hour(clock, monkeypatch, family):
    conn = IntakeConn(allegra_day(3, {20: taipei(3, 20, 5)}), med={"schedule_time": ALLEGRA})
    monkeypatch.setattr(intake_repository, "get_pool", lambda: conn)
    clock.now = taipei(3, 21, 0)
    for _ in range(2):    # the camera sees the hand-to-mouth gesture twice
        with pytest.raises(dose_safety.DoseTooSoon):
            run(intake_repository.commit_monitored(_session(322), _candidate(), "reachy_prompted"))
    alerts = _alerts(family)
    assert [a["recipient_contact_id"] for a in alerts] == [1, 2]      # verified, missed-dose alerts on, not the patient
    second = int(clock.now.timestamp())
    assert {a["dedupe_key"] for a in alerts} == {f"double_dose:322:{second}:1", f"double_dose:322:{second}:2"}
    assert all(a["priority"] == 0 for a in alerts)
    text = alerts[0]["payload"]["messages"][0]["text"]
    assert text.startswith("⚠️ 可能重複服藥：Pearl 在 21:00 可能又吃了 allegra，但 20:05 已經記錄過一次（至少要間隔 1 小時）。"
                           "請確認藥盒，如有疑慮請聯絡藥師或撥 119。")
    assert "Possible double dose: Pearl may have taken allegra again at 21:00" in text and "1 h apart" in text
    assert len(family.notifications) == 1 and conn.stock == 10
    # From 21:05, an hour after the 20:05 pill, the camera records the 22:00 dose.
    clock.now = taipei(3, 21, 5)
    assert run(intake_repository.commit_monitored(_session(322), _candidate(), "auto"))["status"] == "taken"
    assert len(_alerts(family)) == 2 and conn.stock == 9


def test_the_alert_goes_once_per_dose_per_hour(clock, family):
    """A rolling hour from the last alert for the dose, not clock hours: 10:59 and 11:01 are one alert."""
    def refused(intk_id):
        return dose_safety.DoseTooSoon(last_taken_at=taipei(3, 20, 5), gap=HOUR, intk_id=intk_id, med_name="allegra",
                                       scheduled_time=taipei(3, 22), at=clock.now, u_id=7)

    def alert(intk_id, hour, minute):
        clock.now = taipei(3, hour, minute)       # the outbox row's created_at is the database's NOW()
        return run(dose_safety.alert_family(family, refused(intk_id), now=clock.now))

    assert alert(322, 20, 59) == 2
    assert alert(322, 21, 1) == 0                 # another clock hour, but within the hour
    assert alert(322, 21, 58) == 0
    assert alert(322, 21, 59) == 2                # an hour after the first
    assert alert(9, 21, 59) == 2                  # another dose
    not_due = judge(allegra_day(), 322, taipei(3, 20, 30))
    assert run(dose_safety.alert_family(family, not_due, now=taipei(3, 23, 0))) == 0
    assert len(family.notifications) == 3
    # Within the hour the refusal still says family has been told.
    repeat = refused(322)
    clock.now = taipei(3, 22, 0)
    assert run(dose_safety.alert_family(family, repeat, now=clock.now)) == 0 and repeat.family_alerted


def test_a_camera_commit_over_the_daily_maximum_alerts_family(clock, monkeypatch, family):
    conn = IntakeConn({**all_four_taken(), 9: dose(3, 23, 30)}, med={"schedule_time": ALLEGRA})
    monkeypatch.setattr(intake_repository, "get_pool", lambda: conn)
    clock.now = taipei(3, 23, 30)
    with pytest.raises(dose_safety.DailyMaxReached):
        run(intake_repository.commit_monitored(_session(9), _candidate(), "confirmed_by_user"))
    (alert, _) = _alerts(family)
    assert "但今天已經記錄 4 次（每天最多 4 次）" in alert["payload"]["messages"][0]["text"]
    assert "already recorded 4 times today (at most 4 a day)" in alert["payload"]["messages"][0]["text"]


@pytest.mark.parametrize("intk_id, at, reason", [
    (322, taipei(3, 20, 30), schedule.DoseNotDueYet),    # early: not a second dose
    (308, taipei(3, 10, 30), dose_safety.DoseExpired),   # late: not a second dose either
])
def test_a_camera_commit_refused_as_early_or_missed_does_not_alert(clock, monkeypatch, family, intk_id, at, reason):
    conn = IntakeConn(allegra_day(), med={"schedule_time": ALLEGRA})
    monkeypatch.setattr(intake_repository, "get_pool", lambda: conn)
    clock.now = at
    with pytest.raises(reason):
        run(intake_repository.commit_monitored(_session(intk_id), _candidate(), "auto"))
    assert _alerts(family) == []


def test_with_protection_off_the_camera_records_a_second_dose_and_nobody_is_alerted(clock, monkeypatch, family):
    conn = IntakeConn(allegra_day(3, {20: taipei(3, 20, 5)}), med={"schedule_time": ALLEGRA}, protection=False)
    monkeypatch.setattr(intake_repository, "get_pool", lambda: conn)
    clock.now = taipei(3, 20, 10)
    assert run(intake_repository.commit_monitored(_session(322), _candidate(), "auto"))["status"] == "taken"
    assert _alerts(family) == [] and conn.stock == 9


def test_a_failed_alert_never_hides_the_refusal(clock, monkeypatch):
    conn = IntakeConn(allegra_day(3, {20: taipei(3, 20, 5)}), med={"schedule_time": ALLEGRA})
    monkeypatch.setattr(intake_repository, "get_pool", lambda: conn)

    def broken():
        raise RuntimeError("pool down")

    monkeypatch.setattr(dose_safety, "get_pool", broken)
    clock.now = taipei(3, 21, 0)
    with pytest.raises(dose_safety.DoseTooSoon):
        run(intake_repository.commit_monitored(_session(322), _candidate(), "auto"))


def test_the_legacy_page_passes_a_refusal_on_to_the_409_handler(monkeypatch):
    class _Conn:
        async def fetchrow(self, query, *args):
            assert dose_safety.open_sql("intake", "$4::timestamptz") in query
            return {"intk_id": 5}

    class _Pool:
        def acquire(self):
            conn = _Conn()

            class _Acquire:
                async def __aenter__(self):
                    return conn

                async def __aexit__(self, *args):
                    return False
            return _Acquire()

    async def transition(u_id, intk_id, status):
        raise dose_safety.DoseTooSoon(last_taken_at=taipei(3, 8), gap=HOUR, intk_id=intk_id)

    monkeypatch.setattr(medicines, "current_user", lambda request: {"u_id": 7})
    monkeypatch.setattr(medicines, "get_pool", lambda: _Pool())
    monkeypatch.setattr(medicines, "transition_intake", transition)
    with pytest.raises(dose_safety.DoseTooSoon):
        run(medicines._update_intake(None, 40, "taken"))


# ── Paths: the robot's confirmation request and the caregiver's answer ─────

def _med40(db, schedule_time=ALLEGRA):
    db.meds[40].update(schedule_time=schedule_time, med_name="allegra", pills_remaining=10)
    db.intakes = {}


def test_the_robot_cannot_ask_family_to_confirm_a_second_dose(clock):
    db = FakeDB()
    _med40(db)
    db.intakes = {200: {**FakeDB._intake(7, 40, "taken"), "intake_time_stamp": taipei(3, 20).astimezone(UTC),
                        "actual_intake_time": taipei(3, 20, 5).astimezone(UTC)},
                  201: {**FakeDB._intake(7, 40, "pending"), "intake_time_stamp": taipei(3, 22).astimezone(UTC)}}
    clock.now = taipei(3, 21, 0)
    with pytest.raises(dose_safety.DoseTooSoon) as refused:
        run(dose_confirmation.create(db, u_id=7, task_id=None, intk_ids=[201], source="patient_claim",
                                     evidence={"said_done": True}))
    assert refused.value.u_id == 7 and refused.value.intk_id == 201   # the endpoint alerts family with it
    assert db.intakes[201]["intake_stats"] == "pending" and db.confirmations == {} and db.outbox == []
    clock.now = taipei(3, 21, 5)
    assert run(dose_confirmation.create(db, u_id=7, task_id=None, intk_ids=[201], source="patient_claim",
                                        evidence={"said_done": True})) in db.confirmations


def _asked(db, intk_ids, created_at, source="patient_claim"):
    confirmation_id = str(uuid.uuid4())
    for intk_id in intk_ids:
        db.intakes[intk_id]["intake_stats"] = "pending_confirmation"
    db.confirmations[confirmation_id] = {
        "confirmation_id": confirmation_id, "u_id": 7, "task_id": None, "intk_ids": list(intk_ids),
        "previous_status": {str(i): "pending" for i in intk_ids}, "source": source, "evidence": None,
        "created_at": created_at.astimezone(UTC), "reminded_at": None, "resolved_at": None, "resolution": None,
        "resolved_by": None}
    return confirmation_id


def test_a_caregivers_taken_is_stored_as_when_the_patient_was_asked(clock):
    """Asked at 08:00, answered at 09:30: the dose was taken at 08:00, and the next gap counts from then."""
    db = FakeDB()
    _med40(db)
    db.intakes = {308: {**FakeDB._intake(7, 40, "pending"), "intake_time_stamp": taipei(3, 8).astimezone(UTC)},
                  312: {**FakeDB._intake(7, 40, "pending"), "intake_time_stamp": taipei(3, 12).astimezone(UTC)}}
    confirmation_id = _asked(db, [308], taipei(3, 8))
    clock.now = taipei(3, 9, 30)
    data = dose_confirmation.sign_postback(dose_confirmation.ACTION, confirmation_id, "taken", 1)
    result = run(dose_confirmation.handle_postback(db, data, "U-amy"))
    assert result["intk_ids"] == [308] and result["refused"] == []
    assert db.intakes[308]["actual_intake_time"] == taipei(3, 8).astimezone(UTC)
    (row,) = dose_facts.rows((7, [312], taipei(3, 10).astimezone(UTC), "Asia/Taipei"), db.intakes, db.meds)
    assert row["last_taken_at"] == taipei(3, 8).astimezone(UTC)


def test_job_still_reports_a_dose_family_confirmed_late():
    """The dose's time is when the patient was asked, maybe more than RECENCY_HOURS ago; the answer is recent."""
    import inspect

    source = inspect.getsource(taken_confirmation_job.check_taken_confirmations)
    assert "GREATEST(i.actual_intake_time, dc.resolved_at) >= $1" in source
    assert "SELECT c.evidence, c.resolved_by, c.resolved_at FROM dose_confirmation c" in source


@pytest.mark.parametrize("taken, reason, words", [
    # An ad-hoc dose recorded at 21:00; the robot's 22:00 request was filed at 21:30.
    ({200: taipei(3, 21, 0)}, "dose_too_soon", ("距離上一次記錄的服藥時間太近", "came too soon after the last")),
    ({200: taipei(3, 8), 201: taipei(3, 12), 202: taipei(3, 20), 203: taipei(3, 20, 30)}, "daily_max_reached",
     ("當天已經達到每日服用上限", "over the medicine's daily maximum")),
])
def test_a_caregivers_taken_for_a_second_dose_records_nothing_and_alerts_family(clock, taken, reason, words):
    db = FakeDB()
    _med40(db)
    for intk_id, when in taken.items():
        db.intakes[intk_id] = {**FakeDB._intake(7, 40, "taken"), "intake_time_stamp": when.astimezone(UTC),
                               "actual_intake_time": when.astimezone(UTC)}
    db.intakes[322] = {**FakeDB._intake(7, 40, "pending"), "intake_time_stamp": taipei(3, 22).astimezone(UTC)}
    confirmation_id = _asked(db, [322], taipei(3, 21, 30), source="degraded")
    clock.now = taipei(3, 22, 0)
    data = dose_confirmation.sign_postback(dose_confirmation.ACTION, confirmation_id, "taken", 1)
    result = run(dose_confirmation.handle_postback(db, data, "U-amy"))
    assert result["intk_ids"] == [] and result["refused"] == [{"intk_id": 322, "detail": reason}]
    assert result["resolution"] == "expired" and result["not_due"] == []
    assert db.intakes[322]["intake_stats"] == "pending" and db.meds[40]["pills_remaining"] == 10
    alerts = _alerts(db)
    assert [a["recipient_contact_id"] for a in alerts] == [1, 2] and alerts[0]["priority"] == 0
    assert "Pearl 在 21:30 可能又吃了 allegra" in alerts[0]["payload"]["messages"][0]["text"]   # when asked
    ack = next(r for r in db.outbox if r["dedupe_key"] == f"dose_confirm_ack:{confirmation_id}:1")
    text = ack["payload"]["messages"][0]["text"]
    assert text.startswith("未記錄：") and words[0] in text and words[1] in text
    notice = next(r for r in db.outbox if r["dedupe_key"] == f"dose_confirm_answered:{confirmation_id}:2")
    assert "answered: taken (" not in notice["payload"]["messages"][0]["text"]


def test_a_caregivers_taken_for_an_expired_dose_records_nothing_without_an_alert(clock):
    db = FakeDB()
    _med40(db)
    db.intakes = {308: {**FakeDB._intake(7, 40, "pending"), "intake_time_stamp": taipei(3, 8).astimezone(UTC)},
                  312: {**FakeDB._intake(7, 40, "pending"), "intake_time_stamp": taipei(3, 12).astimezone(UTC)}}
    confirmation_id = _asked(db, [308], taipei(3, 10, 30))      # asked after it expired at 10:00 (a stale request)
    result = run(dose_confirmation.resolve(db, confirmation_id, 1, "taken"))
    assert result["refused"] == [{"intk_id": 308, "detail": "dose_expired"}] and _alerts(db) == []


def test_with_protection_off_a_caregivers_taken_records_a_second_dose(clock):
    db = FakeDB()
    _med40(db)
    db.protection = False
    db.intakes = {200: {**FakeDB._intake(7, 40, "taken"), "intake_time_stamp": taipei(3, 20).astimezone(UTC),
                        "actual_intake_time": taipei(3, 20, 5).astimezone(UTC)},
                  322: {**FakeDB._intake(7, 40, "pending"), "intake_time_stamp": taipei(3, 22).astimezone(UTC)}}
    confirmation_id = _asked(db, [322], taipei(3, 20, 15))
    result = run(dose_confirmation.resolve(db, confirmation_id, 1, "taken"))
    assert result["intk_ids"] == [322] and result["refused"] == [] and _alerts(db) == []


# ── The LINE confirmation request says when the medicine was last recorded ──

def _request_text(db):
    return next(r for r in db.outbox if r["kind"] == "dose_confirm")["payload"]["messages"][0]["text"]


def test_the_request_gives_the_last_recorded_dose_and_how_early_this_one_is(clock):
    db = FakeDB()
    _med40(db)
    db.intakes = {200: {**FakeDB._intake(7, 40, "taken"), "intake_time_stamp": taipei(3, 8).astimezone(UTC),
                        "actual_intake_time": taipei(3, 8, 5).astimezone(UTC)},
                  201: {**FakeDB._intake(7, 40, "pending"), "intake_time_stamp": taipei(3, 12).astimezone(UTC)}}
    clock.now = taipei(3, 10, 35)
    run(dose_confirmation.create(db, u_id=7, task_id=None, intk_ids=[201], source="uncertain_detection",
                                 evidence=None))
    text = _request_text(db)
    assert "藥物：allegra\n上次記錄：08:05（2.5 小時前）\n比排定時間早 1.5 小時\n" in text
    assert "Medication: allegra\nLast recorded: 08:05 (2.5 h ago)\n1.5 h before its scheduled time\n" in text


def test_the_notes_show_with_protection_off_too_and_name_each_medicine(clock):
    db = FakeDB()
    _med40(db)
    db.protection = False
    db.meds[41].update(pills_remaining=5)
    db.intakes = {200: {**FakeDB._intake(7, 40, "taken"), "intake_time_stamp": taipei(3, 20).astimezone(UTC),
                        "actual_intake_time": taipei(3, 20, 5).astimezone(UTC)},
                  322: {**FakeDB._intake(7, 40, "pending"), "intake_time_stamp": taipei(3, 22).astimezone(UTC)},
                  323: {**FakeDB._intake(7, 41, "pending"), "intake_time_stamp": taipei(3, 22).astimezone(UTC)}}
    clock.now = taipei(3, 20, 30)                     # protection would refuse: too soon, and not due yet
    run(dose_confirmation.create(db, u_id=7, task_id=None, intk_ids=[322, 323], source="patient_claim",
                                 evidence=None))
    text = _request_text(db)
    assert "allegra 上次記錄：20:05（25 分鐘前）" in text and "allegra: Last recorded: 20:05 (25 min ago)" in text
    assert "Aspirin 比排定時間早 1.5 小時" in text and "Aspirin: 1.5 h before its scheduled time" in text


def test_no_notes_without_a_recent_dose_or_an_early_request(clock):
    db = FakeDB()
    _med40(db)
    db.intakes = {200: {**FakeDB._intake(7, 40, "taken"), "intake_time_stamp": taipei(2, 7).astimezone(UTC),
                        "actual_intake_time": taipei(2, 7).astimezone(UTC)},     # more than a day ago
                  308: {**FakeDB._intake(7, 40, "pending"), "intake_time_stamp": taipei(3, 8).astimezone(UTC)}}
    clock.now = taipei(3, 7, 45)                                                  # 15 minutes early
    run(dose_confirmation.create(db, u_id=7, task_id=None, intk_ids=[308], source="patient_claim", evidence=None))
    text = _request_text(db)
    assert "上次記錄" not in text and "Last recorded" not in text and "比排定時間早" not in text


def test_the_reminder_repeats_the_notes(clock, monkeypatch):
    db = FakeDB()
    _med40(db)
    db.intakes = {200: {**FakeDB._intake(7, 40, "taken"), "intake_time_stamp": taipei(3, 8).astimezone(UTC),
                        "actual_intake_time": taipei(3, 8).astimezone(UTC)},
                  312: {**FakeDB._intake(7, 40, "pending"), "intake_time_stamp": taipei(3, 12).astimezone(UTC)}}
    confirmation_id = _asked(db, [312], taipei(3, 11))
    monkeypatch.setattr(dose_confirmation, "get_pool", lambda: FakePool(db))
    run(dose_confirmation.maintenance(now=taipei(3, 12, 1).astimezone(UTC)))
    reminder = next(r for r in db.outbox if r["dedupe_key"].startswith(f"dose_confirm_reminder:{confirmation_id}:"))
    assert "上次記錄：08:00（3 小時前）" in reminder["payload"]["messages"][0]["text"]


# ── The switch in the settings ───────────────────────────────────────────────

class _SettingsConn:
    def __init__(self, stored=None):
        self.stored = stored        # the notification_settings row, or None
        self.outbox, self.notifications, self.saved = [], [], None

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *args):
                return False
        return _Tx()

    async def fetchrow(self, query, *args):
        assert "overdose_protection" in query
        return dict(self.stored) if self.stored is not None else None

    async def fetchval(self, query, *args):
        if "SELECT overdose_protection FROM notification_settings" in query:
            assert "FOR UPDATE" in query
            return self.stored["overdose_protection"] if self.stored else None
        if 'SELECT name FROM "user"' in query:
            return "Pearl"
        if "INSERT INTO notification_outbox" in query:
            self.outbox.append(args)
            return len(self.outbox)
        raise AssertionError(query)

    async def fetch(self, query, *args):
        assert "FROM family_contacts" in query and "relationship IS DISTINCT FROM 'user'" in query
        assert "notify_missed" not in query          # every verified family contact
        return [{"id": 1, "line_id": "U-amy"}, {"id": 2, "line_id": "U-ben"}]

    async def execute(self, query, *args):
        if "INSERT INTO notification_settings" in query:
            assert "overdose_protection = EXCLUDED.overdose_protection" in query
            self.saved = args
            self.stored = {"overdose_protection": args[7]}
        elif "INSERT INTO notification (" in query:
            self.notifications.append(args)
        else:
            raise AssertionError(query)


def _settings(conn, monkeypatch, **payload):
    monkeypatch.setattr(api_notify, "get_pool", lambda: FakePool(conn))
    body = {"remind_before_minutes": 5, "remind_after_minutes": 10, "remind_after_retries": 3,
            "notify_family_on_missed": True, "notify_family_on_bad_mood": True, **payload}
    return run(api_notify.update_notification_settings(api_notify.NotificationSettingsPayload(**body),
                                                       {"u_id": 7}))


def test_settings_show_protection_on_by_default(monkeypatch):
    class _New(_SettingsConn):
        async def execute(self, query, *args):
            assert "INSERT INTO notification_settings (u_id)" in query

    monkeypatch.setattr(api_notify, "get_pool", lambda: FakePool(_New()))
    assert run(api_notify.get_notification_settings({"u_id": 7}))["overdose_protection"] is True
    monkeypatch.setattr(api_notify, "get_pool", lambda: FakePool(_SettingsConn({"overdose_protection": False})))
    assert run(api_notify.get_notification_settings({"u_id": 7}))["overdose_protection"] is False


def test_turning_protection_off_tells_family_on_line_and_on_again_does_not(monkeypatch):
    conn = _SettingsConn()        # no settings row yet: on
    assert _settings(conn, monkeypatch, overdose_protection=False) == {"success": True, "overdose_protection": False}
    assert conn.saved[7] is False and len(conn.notifications) == 1
    assert [args[2] for args in conn.outbox] == [1, 2]                     # recipient contact ids
    assert {args[4] for args in conn.outbox} == {"overdose_protection_off"}
    text = json.loads(conn.outbox[0][6])["messages"][0]["text"]
    assert text.startswith("⚠️ Pearl 已關閉「防止重複服藥」保護。") and "turned off overdose protection" in text
    # Saving again while off, or turning it back on, sends nothing more.
    _settings(conn, monkeypatch, overdose_protection=False)
    assert _settings(conn, monkeypatch, overdose_protection=True)["overdose_protection"] is True
    assert len(conn.outbox) == 2 and len(conn.notifications) == 1


def test_a_client_that_does_not_send_the_switch_keeps_it(monkeypatch):
    conn = _SettingsConn({"overdose_protection": False})
    assert _settings(conn, monkeypatch)["overdose_protection"] is False and conn.outbox == []
    conn = _SettingsConn()
    assert _settings(conn, monkeypatch)["overdose_protection"] is True and conn.outbox == []


def test_settings_accept_put_as_well_as_post():
    from app.main import app

    methods = {method for route in app.routes if getattr(route, "path", "") == "/api/notify/settings"
               for method in getattr(route, "methods", ())}
    assert {"GET", "POST", "PUT"} <= methods


# ── SQL forms ────────────────────────────────────────────────────────────────

def test_the_sql_forms_are_the_same_rules():
    expired = dose_safety.expired_sql("i", "NOW()")
    ad_hoc = dose_safety.ad_hoc_sql("i")
    assert ad_hoc == "(date_trunc('minute', i.intake_time_stamp) <> i.intake_time_stamp)"
    assert expired == (f"(NOT {ad_hoc} AND COALESCE(i.intake_time_stamp + ({dose_safety.next_sql('i')} "
                       "- i.intake_time_stamp) / 2 <= NOW(), FALSE))")
    assert "next_dose.u_id = i.u_id AND next_dose.med_id = i.med_id" in dose_safety.next_sql("i")
    assert "next_dose.intake_time_stamp > i.intake_time_stamp" in dose_safety.next_sql("i")
    assert f"AND NOT {dose_safety.ad_hoc_sql('next_dose')}" in dose_safety.next_sql("i")
    protected = dose_safety.protected_sql("i")
    assert protected == ("COALESCE((SELECT ns.overdose_protection FROM notification_settings ns "
                         "WHERE ns.u_id = i.u_id), TRUE)")
    due = schedule.due_sql("i", "NOW()")
    assert dose_safety.startable_sql("i", "NOW()") == f"(NOT {protected} OR ({due} AND NOT {expired}))"
    assert dose_safety.open_sql("i", "NOW()") == f"({due} AND NOT ({protected} AND {expired}))"
    for column in ("previous_time", "next_time", "protection", "language", "last_taken_at", "last_taken_pending",
                   "taken_that_day"):
        assert f"AS {column}" in dose_safety.FACTS_SQL
    assert "t.intake_stats = 'taken' AND t.actual_intake_time IS NOT NULL" in dose_safety.FACTS_SQL
    # A dose waiting for family counts, at the time the robot asked, when asked at or before the moment judged.
    assert "t.intake_stats = 'pending_confirmation'" in dose_safety.FACTS_SQL
    assert "claim.resolution IS NULL AND claim.created_at <= $3::timestamptz" in dose_safety.FACTS_SQL
    # The previous dose as of the moment judged: an ad-hoc dose made later did not exist then.
    assert "AND prev_dose.intake_time_stamp > $3::timestamptz" in dose_safety.FACTS_SQL
    # The lock is a statement of its own, before the read: FOR UPDATE in the reading statement would leave it
    # reading the snapshot from before it waited (READ COMMITTED), missing a dose committed meanwhile.
    assert "FOR UPDATE" not in dose_safety.FACTS_SQL
    assert dose_safety.LOCK_SQL.endswith("ORDER BY m.med_id FOR UPDATE")


def test_the_schema_adds_the_switch_and_the_limits():
    with open(os.path.join(os.path.dirname(__file__), "..", "sql", "init.sql"), encoding="utf-8") as handle:
        sql = handle.read()
    assert ("ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS overdose_protection BOOLEAN NOT NULL "
            "DEFAULT TRUE;") in sql
    assert "ALTER TABLE medication ADD COLUMN IF NOT EXISTS min_interval_minutes INTEGER;" in sql
    assert "ALTER TABLE medication ADD COLUMN IF NOT EXISTS max_daily_doses INTEGER;" in sql
    assert "CHECK (min_interval_minutes IS NULL OR min_interval_minutes BETWEEN 30 AND 2880)" in sql
    assert "CHECK (max_daily_doses IS NULL OR max_daily_doses BETWEEN 1 AND 24)" in sql


# ── A dose waiting for family's answer counts (every robot dose today) ──────

def _claim(intk_ids, created_at, resolution=None):
    return {"u_id": 7, "intk_ids": list(intk_ids), "resolution": resolution, "created_at": created_at.astimezone(UTC)}


def test_a_dose_waiting_for_family_counts_as_taken_when_the_robot_asked():
    """Allegra's 20:00 dose went to family at 20:50 (the robot can't verify at 10 fps). At 21:00 the 22:00 dose is
    due, but a pill then would be 10 minutes after the one the robot saw: too soon, measured from 20:50."""
    intakes = {**allegra_day(), 320: dose(3, 20, status="pending_confirmation")}
    claims = [_claim([320], taipei(3, 20, 50))]
    refused = judge(intakes, 322, taipei(3, 21), confirmations=claims)
    assert isinstance(refused, dose_safety.DoseTooSoon) and refused.last_taken_at == taipei(3, 20, 50)
    assert refused.pending and refused.body()["last_taken_pending"] is True
    assert judge(intakes, 322, taipei(3, 21, 50), confirmations=claims) is None
    # It counts for the daily maximum too: once a day, waiting for family = today's dose.
    once = {1: dose(3, 8, status="pending_confirmation"), 2: dose(3, 21, 30)}
    refused = judge(once, 2, taipei(3, 21, 30), schedule_time=ONCE_A_DAY, confirmations=[_claim([1], taipei(3, 8))])
    assert isinstance(refused, dose_safety.DailyMaxReached)
    # A request family has answered, or one made after the moment judged, does not count.
    assert judge(intakes, 322, taipei(3, 21), confirmations=[_claim([320], taipei(3, 20, 50), "denied")]) is None
    assert judge(intakes, 322, taipei(3, 21), confirmations=[_claim([320], taipei(3, 21, 10))]) is None


def test_take_now_while_the_robots_dose_waits_for_family_is_too_soon(clock):
    """The reviewer's case: a once-daily medicine, the robot's dose sent to family at 08:00, Take Now at 08:00:10.
    The ad-hoc dose it would make is refused (and rolled back), and the patient hears why."""
    db = FakeDB()
    _med40(db, ONCE_A_DAY)
    db.meds[40]["max_daily_doses"] = 2      # so the gap decides, not the daily maximum
    db.intakes = {308: {**FakeDB._intake(7, 40, "pending"), "intake_time_stamp": taipei(3, 8).astimezone(UTC)}}
    clock.now = taipei(3, 8, 0)
    run(dose_confirmation.create(db, u_id=7, task_id=None, intk_ids=[308], source="auto_record_off", evidence=None))
    assert db.intakes[308]["intake_stats"] == "pending_confirmation" and db.locks
    ad_hoc = taipei(3, 8, 0).astimezone(UTC) + datetime.timedelta(seconds=10, microseconds=123)
    db.intakes[9] = {**FakeDB._intake(7, 40, "pending"), "intake_time_stamp": ad_hoc}
    clock.now = ad_hoc
    with pytest.raises(dose_safety.DoseTooSoon) as refused:
        run(dose_safety.check(db, 7, [9]))
    assert refused.value.pending and refused.value.body()["reply"] == "這個藥您早上8點已經吃過了，請先不要再吃。"


def test_family_answering_taken_after_a_second_pill_alerts_instead_of_calling_it_missed(clock):
    """The other half of the reviewer's case: the second pill got recorded (an ad-hoc dose at 08:00:10, e.g. with
    protection off then). Family's later 'taken' for the 08:00 dose is judged with the doses there were when the
    robot asked: the ad-hoc row moves no halfway point (it is not 'missed'), and the pill 10 s later is a second
    dose, so nothing more is recorded and family is alerted."""
    db = FakeDB()
    _med40(db, ONCE_A_DAY)
    db.meds[40]["max_daily_doses"] = 2      # so the gap decides, not the daily maximum
    db.intakes = {308: {**FakeDB._intake(7, 40, "pending"), "intake_time_stamp": taipei(3, 8).astimezone(UTC)},
                  408: {**FakeDB._intake(7, 40, "pending"), "intake_time_stamp": taipei(4, 8).astimezone(UTC)}}
    confirmation_id = _asked(db, [308], taipei(3, 8), source="auto_record_off")
    ad_hoc = taipei(3, 8).astimezone(UTC) + datetime.timedelta(seconds=10, microseconds=123)
    db.intakes[9] = {**FakeDB._intake(7, 40, "taken"), "intake_time_stamp": ad_hoc, "actual_intake_time": ad_hoc}
    (row,) = dose_facts.rows((7, [308], taipei(3, 8).astimezone(UTC), "Asia/Taipei"), db.intakes, db.meds)
    assert row["next_time"] == taipei(4, 8).astimezone(UTC)          # not the ad-hoc dose
    clock.now = taipei(3, 9, 0)
    result = run(dose_confirmation.resolve(db, confirmation_id, 1, "taken"))
    assert result["refused"] == [{"intk_id": 308, "detail": "dose_too_soon"}] and result["intk_ids"] == []
    assert [a["recipient_contact_id"] for a in _alerts(db)] == [1, 2]


# ── A camera session's dose is judged for due/expired when it started ───────

def _started(intk_id, at):
    return types.SimpleNamespace(u_id=7, intk_id=intk_id, session_id=str(uuid.uuid4()), clip_enabled=False,
                                 event_started_at=None, started_at=at.astimezone(UTC))


def test_a_pill_swallowed_just_after_its_dose_expired_is_still_recorded_and_spaces_the_next(clock, monkeypatch,
                                                                                          family):
    """Take Now at 09:58 picks the 08:00 dose (open until 10:00, halfway to 12:00); the camera sees the pill at
    10:01. Refusing it then would lose the evidence, and the 12:00 dose (due from 10:00) would allow a second pill
    a minute later."""
    conn = IntakeConn(allegra_day(), med={"schedule_time": ALLEGRA})
    monkeypatch.setattr(intake_repository, "get_pool", lambda: conn)
    clock.now = taipei(3, 10, 1)
    result = run(intake_repository.commit_monitored(_started(308, taipei(3, 9, 58)), _candidate(), "auto"))
    assert result["status"] == "taken" and conn.intakes[308]["intake_stats"] == "taken"
    clock.now = taipei(3, 10, 2)
    with pytest.raises(dose_safety.DoseTooSoon):
        run(intake_repository.transition_intake(7, 312, "taken"))
    # The gap and the daily maximum are still judged when the pill went down, whenever the session started.
    assert isinstance(judge(allegra_day(3, {8: taipei(3, 9, 30)}), 312, taipei(3, 10, 1), started_at=taipei(3, 10, 0)),
                      dose_safety.DoseTooSoon)


def test_a_session_started_after_expiry_is_refused_with_a_sentence_for_after_the_pill(clock, monkeypatch, family):
    conn = IntakeConn(allegra_day(), med={"schedule_time": ALLEGRA})
    monkeypatch.setattr(intake_repository, "get_pool", lambda: conn)
    clock.now = taipei(3, 10, 1)
    with pytest.raises(dose_safety.DoseExpired) as refused:
        run(intake_repository.commit_monitored(_started(308, taipei(3, 10, 0)), _candidate(), "auto"))
    body = refused.value.body()
    assert body["after_intake"] is True
    assert body["reply"] == "這次沒有記錄，因為早上8點的藥已經錯過了。如果您剛剛已經吃了，下一次的藥請先問過家人再吃。"
    assert _alerts(family) == []          # a missed dose is not a second dose


# ── What the patient hears ───────────────────────────────────────────────────

def test_after_the_pill_the_sentence_says_it_was_not_recorded_and_who_was_told():
    """A refusal on a path with evidence the patient already swallowed something (camera commit, the robot's request)
    does not say "don't take it"."""
    intakes = {**allegra_day(), 1: dose(3, 0, 5, status="taken", taken_at=taipei(3, 0, 5)), 2: dose(3, 0, 20)}
    refused = judge(intakes, 2, taipei(3, 0, 20))
    refused.after_intake = True
    assert refused.body()["reply"] == ("這次沒有記錄，因為這個藥您凌晨12點05分已經吃過了。"
                                       "如果覺得不舒服，請馬上告訴家人。")
    refused.family_alerted = True
    body = refused.body()
    assert body["reply"] == ("這次沒有記錄，因為這個藥您凌晨12點05分已經吃過了。已經通知家人；如果覺得不舒服，"
                             "請馬上告訴家人。")
    assert body["speech_text"].startswith("这次没有记录，因为这个药您凌晨12点05分已经吃过了。已经通知家人")
    english = judge(intakes, 2, taipei(3, 0, 20), language="en")
    english.after_intake = english.family_alerted = True
    assert english.body()["reply"] == ("This wasn't recorded: you already took this medicine at 12:05 am. "
                                       "Your family has been told. If you feel unwell, tell them right away.")
    late = judge(allegra_day(), 308, taipei(3, 10, 30))
    late.after_intake = True
    assert late.body()["reply"].startswith("這次沒有記錄，因為早上8點的藥已經錯過了。")


def test_a_medicine_named_in_chinese_is_named_aloud():
    """In a slot of several medicines "this medicine" can't tell them apart; a Chinese name the robot's voice can
    read is said (a name in Latin letters stays written only)."""
    intakes = {**allegra_day(), 1: dose(3, 0, 5, status="taken", taken_at=taipei(3, 0, 5)), 2: dose(3, 0, 20)}
    world = {i: {"u_id": 7, "med_id": 1, **row} for i, row in intakes.items()}
    (row,) = dose_facts.rows((7, [2], taipei(3, 0, 20).astimezone(UTC), "Asia/Taipei"), world,
                             {1: {"med_name": "普拿疼", "schedule_time": ALLEGRA}})
    body = dose_safety.evaluate(row, taipei(3, 0, 20)).body()
    assert body["reply"] == "普拿疼您凌晨12點05分已經吃過了，請先不要再吃。"
    assert body["speech_text"] == "普拿疼您凌晨12点05分已经吃过了，请先不要再吃。"
    assert body["language"] == "zh-TW"


# ── Recording again is news again ────────────────────────────────────────────

def test_a_dose_recorded_again_is_reported_to_family_again():
    """3 Oct: the test records were put back to pending with taken_notified still set, so the real 20:00 dose
    would have reached nobody. Every path that records 'taken' clears it, and the job's outbox key names the
    recording, so an earlier message for the same dose does not swallow the new one."""
    import inspect

    source = inspect.getsource(taken_confirmation_job.check_taken_confirmations)
    assert 'dedupe_key=f"taken:{u_id}:{min(intk_ids)}:{recorded}:{contact[\'id\']}"' in source
    # Nor does a dose recorded by the camera borrow an old (test) confirmation's "confirmed by" and evidence.
    assert "AND i.detection_method = 'caregiver_confirmed'" in source
    for function in (intake_repository._commit_monitored, intake_repository.transition_intake,
                     dose_confirmation.resolve):
        assert "taken_notified=FALSE" in inspect.getsource(function)
