"""Leased robot tasks: enqueue rules, leasing, transitions, lease expiry and the reminder hook."""

import asyncio
import dataclasses
import json
import os
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

sys.path.insert(0, os.path.dirname(__file__))
import dose_facts  # noqa: E402

from app import config  # noqa: E402
from app.jobs import missed_dose_job, reachy_task_job  # noqa: E402
from app.services import ai_settings, consent_service, dose_safety, outbox, reachy_tasks, schedule  # noqa: E402

T0 = datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc)          # 08:00 in Taipei
OPEN = ("queued", "leased", "searching", "in_progress")
ROBOT_CONSENT = {"core": {"granted": True, "terms_version": config.TERMS_VERSION},
                 "robot_camera": {"granted": True, "terms_version": config.TERMS_VERSION}}
DEVICE = str(uuid.uuid4())
OTHER_DEVICE = str(uuid.uuid4())


class _Transaction:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.depth += 1
        return self

    async def __aexit__(self, *args):
        self.conn.depth -= 1
        return False


class FakeDB:
    """Just enough of reachy_task / reachy_device / intake to exercise the service SQL."""

    def __init__(self):
        self.now = T0
        self.depth = 0
        self.tasks = []
        self.devices = {7: {"device_id": DEVICE, "auto_record": False, "live_voice": False, "last_seen_at": T0,
                            "revoked": False}}
        self.users = {7: "Pearl"}
        self.call_names = {}   # the Reachy card's call name per patient ("user".reachy_call_name)
        self.doses = {
            101: {"intk_id": 101, "med_id": 2, "med_name": "Metformin", "pill_description": "white round",
                  "dose_form": "solid_oral", "units_per_dose": Decimal("1.00"), "intake_stats": "pending",
                  "intake_time_stamp": T0},
            102: {"intk_id": 102, "med_id": 1, "med_name": "Aspirin", "pill_description": None,
                  "dose_form": "liquid", "units_per_dose": Decimal("1.00"), "intake_stats": "pending",
                  "intake_time_stamp": T0},
            103: {"intk_id": 103, "med_id": 3, "med_name": "Zinc", "pill_description": None,
                  "dose_form": "solid_oral", "units_per_dose": Decimal("2.00"), "intake_stats": "taken",
                  "intake_time_stamp": T0},
        }
        self.executed = []
        self.outbox_now = None
        self.due_checks = []
        self.protection = True

    # helpers
    def transaction(self):
        return _Transaction(self)

    def acquire(self):
        db = self

        class _Acquire:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *args):
                return False
        return _Acquire()

    def open_task(self, u_id, slot):
        return next((t for t in self.tasks if t["u_id"] == u_id and t["slot_time"] == slot
                     and t["status"] in OPEN), None)

    def by_id(self, task_id):
        return next((t for t in self.tasks if str(t["task_id"]) == str(task_id)), None)

    def facts(self, args):
        """dose_safety.FACTS_SQL over the doses (all patient 7's)."""
        meds = {d["med_id"]: {"med_name": d["med_name"]} for d in self.doses.values()}
        return dose_facts.rows(args, self.doses, meds, protection=self.protection)

    def startable(self, intk_id):
        """dose_safety.startable_sql: due and not expired, under the patient's switch."""
        (row,) = self.facts((7, [intk_id], self.now, "Asia/Taipei"))
        expiry = dose_safety.expires_at(row["intake_time_stamp"], row["next_time"])
        return not row["protection"] or (schedule.is_due(row["intake_time_stamp"], self.now, row["previous_time"])
                                         and (expiry is None or self.now < expiry))

    def expired(self, intk_id, at):
        """dose_safety.expired_sql under the switch (protected_sql): R4 at `at`."""
        (row,) = self.facts((7, [intk_id], at, "Asia/Taipei"))
        expiry = dose_safety.expires_at(row["intake_time_stamp"], row["next_time"])
        return row["protection"] and expiry is not None and at >= expiry

    def retry_due(self, task, u_id):
        """reachy_tasks._retry_due_sql: patient u_id's in-progress task marked retry_due, not expired, with a marked
        dose still open and startable."""
        retry = json.loads(task["detail"] or "{}").get("retry_due")
        return (task["u_id"] == u_id and task["status"] == "in_progress" and retry is not None
                and datetime.fromisoformat(retry["expires_at"]) > self.now
                and any(i in self.doses and self.doses[i]["intake_stats"] in ("pending", "missed")
                        and self.startable(i) for i in retry["intk_ids"]))

    def is_open(self, intk_id):
        return intk_id in self.doses and self.doses[intk_id]["intake_stats"] in ("pending", "missed")

    def leasable(self, task, u_id):
        """reachy_tasks._leasable_sql: patient u_id's queued task, not expired, whose open doses may all start now.
        Only open doses hold a task back: a taken one past its halfway point does not. A meal retry (a not_before)
        only from that time on, and only with a dose still open."""
        not_before = task.get("not_before")
        return (task["u_id"] == u_id and task["status"] == "queued" and task["expires_at"] > self.now
                and all(self.startable(i) for i in task["intk_ids"] if self.is_open(i))
                and (not_before is None or (not_before <= self.now
                                            and any(self.is_open(i) for i in task["intk_ids"]))))

    def meal_retry_waiting(self, intk_id):
        """reachy_tasks.meal_retry_waiting_sql: a meal retry that has not reached the patient yet (queued, leased or
        searching) holds the dose, while the patient's robot is online (a heartbeat within OFFLINE_AFTER_SECONDS).
        One with the patient (in_progress) no longer does."""
        device = self.devices.get(7)
        online = (device is not None and not device["revoked"] and device["last_seen_at"] is not None
                  and device["last_seen_at"] >= self.now - timedelta(seconds=reachy_tasks.OFFLINE_AFTER_SECONDS))
        return online and any(t.get("not_before") is not None and t["status"] in ("queued", "leased", "searching")
                              and intk_id in t["intk_ids"] for t in self.tasks)

    # asyncpg surface
    async def fetchval(self, query, *args):
        if "SELECT device_id FROM reachy_device" in query:
            device = self.devices.get(args[0])
            return device["device_id"] if device and not device["revoked"] else None
        if "INSERT INTO reachy_task" in query:
            assert "expires_at, not_before) VALUES" in query   # the meal retry's time is a column, not a timer
            task_id, u_id, slot, intk_ids, reason, expires, not_before = args
            if self.open_task(u_id, slot):
                return None
            self.tasks.append({"task_id": task_id, "u_id": u_id, "slot_time": slot, "intk_ids": intk_ids,
                               "reason": reason, "attempt": 1, "status": "queued", "lease_owner": None,
                               "lease_until": None, "created_at": self.now, "finished_at": None,
                               "expires_at": expires, "detail": None, "not_before": not_before})
            return task_id
        if "SET attempt = attempt + 1" in query:
            task = self.open_task(args[0], args[1])
            if task and task["status"] == "queued":
                task["attempt"] += 1
                task["expires_at"] = max(task["expires_at"], args[2])
                return task["task_id"]
            return None
        if "SELECT task_id FROM reachy_task WHERE u_id = $1 AND slot_time = $2" in query:
            task = self.open_task(args[0], args[1])
            return task["task_id"] if task and task["status"] != "queued" else None
        if 'SELECT name FROM "user"' in query:
            return self.users.get(args[0])
        if "FROM notification_outbox" in query and "kind = 'robot_offline'" in query:
            self.outbox_now = args[1] + timedelta(hours=6)
            return 1 if any(n["u_id"] == args[0] and n["at"] > args[1] for n in self.notices) else None
        if "SELECT EXISTS (SELECT 1 FROM reachy_task t WHERE" in query:   # task_due
            assert reachy_tasks._leasable_sql() in query and len(args) == 1
            assert f"OR EXISTS ({reachy_tasks._retry_due_sql()})" in query
            return any(self.leasable(t, args[0]) and any(
                i in self.doses and self.doses[i]["intake_stats"] in ("pending", "missed") for i in t["intk_ids"])
                for t in self.tasks) or any(self.retry_due(t, args[0]) for t in self.tasks)
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        if "SET status = 'leased'" in query:
            assert "FOR UPDATE SKIP LOCKED" in query
            assert "AND NOT EXISTS (SELECT 1 FROM intake i" in query
            assert (f"AND i.intake_stats IN ('pending','missed') "
                    f"AND NOT ({dose_safety.startable_sql('i', 'NOW()')})") in query and len(args) == 2
            assert "AND (t.not_before IS NULL OR (t.not_before <= NOW() AND EXISTS (SELECT 1 FROM intake" in query

            assert "ORDER BY (t.reason = 'checkin'), t.slot_time, t.created_at" in query   # medicine first
            queued = sorted((t for t in self.tasks if self.leasable(t, args[0])),
                            key=lambda t: (t["reason"] == "checkin", t["slot_time"], t["created_at"]))
            if not queued:
                return None
            task = queued[0]
            task.update(status="leased", lease_owner=args[1], lease_until=self.now + timedelta(seconds=60))
            return dict(task)
        if 'SELECT u.name, d.auto_record, d.device_id IS NOT NULL AS paired' in query:
            device = self.devices.get(args[0])
            active = device if device and not device["revoked"] else None
            return {"name": self.users[args[0]], "auto_record": active["auto_record"] if active else None,
                    "paired": active is not None}
        if query == 'SELECT reachy_call_name, name FROM "user" WHERE u_id = $1':   # memory.call_name
            return {"reachy_call_name": self.call_names.get(args[0]), "name": self.users.get(args[0])}
        if "WHERE u_id = $1 AND lease_owner = $2::uuid" in query:
            tasks = sorted((t for t in self.tasks if t["u_id"] == args[0] and t["lease_owner"] == args[1]
                            and t["status"] in ("leased", "searching", "in_progress")),
                           key=lambda t: t["slot_time"])
            return dict(tasks[0]) if tasks else None
        if "WHERE task_id = $1::uuid AND u_id = $2 FOR UPDATE" in query:
            task = self.by_id(args[0])
            return dict(task) if task and task["u_id"] == args[1] else None
        if "UPDATE reachy_task SET status = $2" in query:
            task = self.by_id(args[0])
            task["status"] = args[1]
            if args[2] is not None:
                task["detail"] = args[2]
            if "finished_at = NOW()" in query:
                task.update(finished_at=self.now, lease_until=None)
            else:
                task["lease_until"] = self.now + timedelta(seconds=60)
            return dict(task)
        if "FROM notification_settings WHERE u_id = $1" in query:   # manual_expires_at: no settings row, the defaults
            return None
        raise AssertionError(query)

    async def fetch(self, query, *args):
        if dose_facts.is_facts(query):
            assert args[0] == 7
            self.due_checks.append(list(args[1]))
            return self.facts(args)
        if "AS meal_retry_waiting" in query:   # missed_dose_job: the pending doses, each with its meal retry's hold
            assert f"{reachy_tasks.meal_retry_waiting_sql('i')} AS meal_retry_waiting" in query
            assert "meal.status IN ('queued','leased','searching')" in query   # what meal_retry_waiting models
            assert ("meal_device.revoked_at IS NULL AND meal_device.last_seen_at >= "
                    f"NOW() - INTERVAL '{reachy_tasks.OFFLINE_AFTER_SECONDS} seconds'") in query
            return [{"id": i, "u_id": 7, "intake_time_stamp": d["intake_time_stamp"], "reminder_sent": True,
                     "missed_reminders_sent": d.get("missed_reminders_sent", 0), "med_id": d["med_id"],
                     "med_name": d["med_name"], "patient_name": self.users[7], "patient_line_id": "U1",
                     "remind_before_minutes": 5, "remind_after_minutes": 10, "remind_after_retries": 3,
                     "notify_family_on_missed": False, "meal_retry_waiting": self.meal_retry_waiting(i)}
                    for i, d in sorted(self.doses.items()) if d["intake_stats"] == "pending"]
        if "SELECT intk_id FROM intake WHERE u_id = $1 AND intk_id = ANY($2::int[])" in query:   # retry_due's open doses
            return [{"intk_id": i} for i in sorted(args[1])
                    if i in self.doses and self.doses[i]["intake_stats"] in ("pending", "missed")]
        if "AS meal_retries" in query:   # _queue_meal_retry: the open doses not eaten for, their meal retries so far
            assert f"{reachy_tasks._meal_retry_sql('i', reachy_tasks._OPEN_SQL)} AS waiting" in query and args[0] == 7
            meal = [t for t in self.tasks if t.get("not_before") is not None]
            return [{"intk_id": i, "intake_time_stamp": self.doses[i]["intake_time_stamp"],
                     "meal_retries": sum(i in t["intk_ids"] for t in meal),
                     "waiting": any(i in t["intk_ids"] and t["status"] in OPEN for t in meal)}
                    for i in sorted(args[1]) if self.is_open(i)]
        if "FROM intake i JOIN medication m" in query:
            assert "ORDER BY m.med_name, m.med_id" in query
            assert "m.schedule_time, m.instructions, i.intake_time_stamp" in query   # what after_meal reads
            rows = [{"schedule_time": None, "instructions": None, **self.doses[i]} for i in args[1] if i in self.doses]
            return sorted(rows, key=lambda d: (d["med_name"], d["med_id"]))
        if "JOIN reachy_device d" in query:
            now, seen_before = args
            result, seen = [], set()
            for t in sorted(self.tasks, key=lambda t: (t["u_id"], t["slot_time"])):
                device = self.devices.get(t["u_id"])
                if (t["u_id"] in seen or not device or device["revoked"] or t["status"] != "queued"
                        or t["slot_time"] > now or t["expires_at"] <= now):
                    continue
                if device["last_seen_at"] is None or device["last_seen_at"] < seen_before:
                    seen.add(t["u_id"])
                    result.append({"u_id": t["u_id"], "slot_time": t["slot_time"]})
            return result
        raise AssertionError(query)

    async def execute(self, query, *args):
        self.executed.append((query, args, self.depth))
        if "SET status = 'expired'" in query:
            for t in self.tasks:
                if t["status"] in OPEN and t["expires_at"] <= args[0]:
                    t.update(status="expired", finished_at=args[0], lease_until=None)
        elif "SET status = 'queued'" in query:
            for t in self.tasks:
                if t["status"] in ("leased", "searching", "in_progress") and t["lease_until"] < args[0]:
                    t.update(status="queued", lease_owner=None, lease_until=None)
        elif "'nothing_to_prompt'" in query:   # maintenance: a meal retry whose doses were all resolved meanwhile
            assert "WHERE t.status = 'queued' AND t.not_before IS NOT NULL AND NOT EXISTS" in query
            for t in self.tasks:
                if (t["status"] == "queued" and t.get("not_before") is not None
                        and not any(self.is_open(i) for i in t["intk_ids"])):
                    detail = json.loads(t["detail"] or "{}")
                    t.update(status="aborted", finished_at=args[0], detail=json.dumps({**detail,
                                                                                       "reason": "nothing_to_prompt"}))
        elif "'dose_expired'" in query:   # maintenance: a meal retry with an open dose expired before the lease
            assert "WHERE t.status = 'queued' AND t.not_before IS NOT NULL AND EXISTS" in query
            assert (f"AND {dose_safety.protected_sql('i')} "
                    f"AND {dose_safety.expired_sql('i', '$1::timestamptz')})") in query
            for t in self.tasks:
                if (t["status"] == "queued" and t.get("not_before") is not None
                        and any(self.is_open(i) and self.expired(i, args[0]) for i in t["intk_ids"])):
                    detail = json.loads(t["detail"] or "{}")
                    t.update(status="aborted", finished_at=args[0], detail=json.dumps({**detail,
                                                                                       "reason": "dose_expired"}))
        elif "SET lease_until = NOW()" in query:
            for t in self.tasks:
                if t["lease_owner"] == args[0] and t["status"] in ("leased", "searching", "in_progress"):
                    t["lease_until"] = self.now + timedelta(seconds=60)
        elif "SET expires_at = GREATEST(expires_at" in query:   # extend_for_companion: a companion heartbeat
            assert "WHERE lease_owner = $1::uuid AND status = 'in_progress'" in query
            for t in self.tasks:
                if t["lease_owner"] == args[0] and t["status"] == "in_progress":
                    t["expires_at"] = max(t["expires_at"], self.now + timedelta(
                        seconds=reachy_tasks.COMPANION_TASK_EXTEND_SECONDS))
        elif "UPDATE intake SET missed_reminders_sent = missed_reminders_sent + 1" in query:   # the job's retry count
            for i in args[0]:
                self.doses[i]["missed_reminders_sent"] = self.doses[i].get("missed_reminders_sent", 0) + 1
        elif "jsonb_build_object('retry_due'" in query:   # enqueue_reachy_task: a missed retry for a held task
            task = self.by_id(args[0])
            if task["status"] == "in_progress":
                detail = json.loads(task["detail"] or "{}")
                task["detail"] = json.dumps({**detail, "retry_due": {"intk_ids": list(args[1]), "expires_at": args[2]}})
        elif "UPDATE intake SET" in query or "INSERT INTO notification" in query:
            pass
        else:
            raise AssertionError(query)


@pytest.fixture
def db(monkeypatch):
    db = FakeDB()
    consent = {7: ROBOT_CONSENT}

    async def fetch_state(conn, u_id):
        assert conn is db
        return consent.get(u_id, {})

    db.consent = consent
    db.notices = []

    async def enqueue_to_contacts(conn, u_id, **kwargs):
        db.notices.append({"u_id": u_id, "in_transaction": conn.depth > 0, "at": db.outbox_now, **kwargs})
        return 1

    monkeypatch.setattr(consent_service, "fetch_state", fetch_state)
    monkeypatch.setattr(outbox, "enqueue_to_contacts", enqueue_to_contacts)
    monkeypatch.setattr(reachy_tasks, "get_pool", lambda: db)
    monkeypatch.setattr(schedule, "_now", lambda: db.now)   # the due rule runs on the fake database's clock
    return db


def run(coro):
    return asyncio.run(coro)


def enqueue(db, reason="upcoming", slot=T0, intk_ids=(101, 102, 103), expires=T0 + timedelta(minutes=40)):
    return run(reachy_tasks.enqueue_reachy_task(db, 7, slot, list(intk_ids), reason, expires))


# ── Enqueue ──────────────────────────────────────────────────────────────────

def test_enqueue_creates_one_open_task_per_slot_and_bumps_queued_attempt(db):
    first = enqueue(db)
    second = enqueue(db, reason="missed_retry")
    assert first and second == first
    assert len(db.tasks) == 1 and db.tasks[0]["attempt"] == 2
    other_slot = enqueue(db, slot=T0 + timedelta(hours=4))
    assert other_slot != first and len(db.tasks) == 2


@pytest.mark.parametrize("status", ["leased", "searching", "in_progress"])
def test_enqueue_never_rearms_or_resets_an_active_task(db, status):
    task_id = enqueue(db)
    db.tasks[0].update(status=status, lease_owner=DEVICE, lease_until=T0 + timedelta(seconds=60))
    before = dict(db.tasks[0])
    assert enqueue(db, reason="missed_retry", expires=T0 + timedelta(hours=2)) == task_id
    after = dict(db.tasks[0])
    if status == "in_progress":   # only marked for the retry (task_due, then queued when it ends)
        assert json.loads(after.pop("detail")) == {"retry_due": {"intk_ids": [101, 102, 103],
                                                                 "expires_at": (T0 + timedelta(hours=2)).isoformat()}}
        before.pop("detail")
    assert after == before and len(db.tasks) == 1


@pytest.mark.parametrize("status", ["not_found", "aborted"])
def test_retry_after_finished_task_creates_a_new_task(db, status):
    first = enqueue(db)
    db.tasks[0]["status"] = status
    second = enqueue(db, reason="missed_retry")
    assert second != first and db.by_id(second)["status"] == "queued"


def test_enqueue_refused_without_device(db):
    db.devices[7]["revoked"] = True
    assert enqueue(db) is None and db.tasks == []


@pytest.mark.parametrize("state", [
    {},
    {"core": ROBOT_CONSENT["core"]},
    {**ROBOT_CONSENT, "robot_camera": {"granted": False, "terms_version": config.TERMS_VERSION}},
    {**ROBOT_CONSENT, "robot_camera": {"granted": True, "terms_version": "old"}},
])
def test_enqueue_refused_without_current_robot_consent(db, state):
    db.consent[7] = state
    assert enqueue(db) is None and db.tasks == []


@pytest.mark.parametrize("reason", ["upcoming", "missed_retry", "manual"])
def test_enqueue_refuses_a_dose_not_due_yet_for_every_caller(db, reason):
    db.doses[102]["intake_time_stamp"] = T0 + schedule.DOSE_EARLY + timedelta(minutes=1)
    with pytest.raises(schedule.DoseNotDueYet) as refused:
        enqueue(db, reason=reason, slot=T0 + timedelta(hours=2))
    assert refused.value.intk_id == 102 and db.tasks == []
    db.doses[102]["intake_time_stamp"] = T0 + schedule.DOSE_EARLY   # exactly DOSE_EARLY ahead: due
    assert enqueue(db, reason=reason, slot=T0 + timedelta(hours=2)) and len(db.tasks) == 1


def test_checkin_task_has_no_dose_to_check(db):
    assert enqueue(db, reason="checkin", intk_ids=()) and db.due_checks == []


def test_a_queued_task_renders_the_patients_lines_ahead(db, monkeypatch):
    from app.services import voice_lines
    ahead = []
    monkeypatch.setattr(voice_lines, "render_ahead_for_patient", lambda u_id: ahead.append(u_id))
    assert enqueue(db, reason="checkin", intk_ids=()) and ahead == [7]
    assert enqueue(db) and ahead == [7, 7]                   # a dose task's slot ends with a check-in too
    db.devices[7]["revoked"] = True
    assert enqueue(db, slot=T0 + timedelta(hours=8)) is None and ahead == [7, 7]   # no task: nothing to render for


# ── Lease, payload, restart recovery ─────────────────────────────────────────

def test_lease_next_leases_oldest_queued_and_returns_payload(db):
    later = enqueue(db, slot=T0 + timedelta(hours=4), expires=T0 + timedelta(hours=5))
    first = enqueue(db)
    task = run(reachy_tasks.lease_next(7, DEVICE, 0))
    assert task["task_id"] == first and task["status"] == "leased"
    assert set(task) == {"task_id", "slot_time", "reason", "attempt", "status", "expires_at",
                         "patient_name", "call_name", "auto_record", "microphone", "checkin", "live", "doses"}
    assert task["patient_name"] == "Pearl" and task["auto_record"] is False
    assert task["call_name"] == "Pearl"  # no call name on the Reachy card: the registered name (memory.call_name)
    assert task["live"] is False         # no live test switch, robot switch or live model in the fixture
    assert task["microphone"] is False   # no robot_microphone consent in the fixture
    assert task["checkin"] is False      # nor the check-in scopes
    assert [d["med_name"] for d in task["doses"]] == ["Aspirin", "Metformin", "Zinc"]
    assert set(task["doses"][0]) == {"intk_id", "med_id", "med_name", "pill_description", "dose_form",
                                     "units_per_dose", "intake_stats", "supported", "after_meal"}
    assert [d["supported"] for d in task["doses"]] == [False, True, False]
    assert db.by_id(first)["lease_until"] == T0 + timedelta(seconds=60)
    assert run(reachy_tasks.lease_next(7, DEVICE, 0))["task_id"] == later
    assert run(reachy_tasks.lease_next(7, DEVICE, 0)) is None


def test_the_payload_carries_the_name_reachy_calls_the_patient(db):
    """call_name (memory.call_name) next to the registered patient_name: the robot's own voice says it when the
    named dose line has no copy in the live voice (quick fixes of 19 Oct 2026)."""
    enqueue(db)
    db.call_names[7] = "阿嬤"
    task = run(reachy_tasks.lease_next(7, DEVICE, 0))
    assert task["call_name"] == "阿嬤" and task["patient_name"] == "Pearl"
    db.call_names[7] = None
    db.users[7] = "Demo Patient 1790963685"     # a registered name Reachy would not say: no name
    assert run(reachy_tasks.current_task(7, DEVICE))["call_name"] is None


@pytest.mark.parametrize("local, expected", [
    ("10:29", False), ("10:30", True), ("12:00", True), ("14:30", True), ("14:31", False),
    ("16:29", False), ("16:30", True), ("20:00", True), ("21:30", True), ("21:31", False),
    ("08:00", False), ("22:00", False)])
def test_an_after_meal_dose_counts_only_at_lunch_or_dinner_local_time(local, expected):
    """Quick fixes 2 of 19 Oct 2026: Reachy asks 「您有吃飯了嗎？」 before such a dose. Lunch is 10:30-14:30 and
    dinner 16:30-21:30 in MEDCARE_TIMEZONE (Taipei: UTC+8), both ends included."""
    hours, minutes = map(int, local.split(":"))
    at = T0 + timedelta(hours=hours - 8, minutes=minutes)   # T0 is 08:00 in Taipei
    assert reachy_tasks.after_meal({"after_meals": True}, None, at) is expected
    assert reachy_tasks.after_meal(None, "每日三次，飯後服用", at) is expected
    assert reachy_tasks.after_meal('{"after_meals": true}', "", at) is expected   # asyncpg hands JSONB back as text
    assert reachy_tasks.after_meal({"after_meals": False, "noon": True}, "餐前服用", at) is False
    assert reachy_tasks.after_meal({}, None, at) is False


def test_an_after_meal_dose_is_marked_by_its_schedule_or_its_instructions():
    lunch = T0 + timedelta(hours=4)   # 12:00 in Taipei
    assert reachy_tasks.after_meal(None, "餐後一小時服用", lunch) is True
    assert reachy_tasks.after_meal({"after_meals": True, "noon": True}, "睡前", lunch) is True
    assert reachy_tasks.after_meal({"before_meals": True}, "睡前", lunch) is False
    assert reachy_tasks.after_meal(None, None, lunch) is False
    assert reachy_tasks.after_meal({"after_meals": True}, None, None) is False   # no dose time: never a meal time


@pytest.mark.parametrize("schedule_time, instructions", [
    ({"after_meals": True, "before_meals": True}, None),
    ('{"after_meals": true, "before_meals": true}', None),           # asyncpg hands JSONB back as text
    ({"before_meals": True}, "每日三次，飯後服用"),
    (None, "空腹（飯前1小時或飯後2小時）服用"),
    ({"after_meals": True}, "空腹服用"),
    ({"after_meals": True, "noon": True}, "飯前"),
    ({"after_meals": True}, "飯前30分鐘服用"),
    (None, "餐前服用，飯後亦可"),
    ({"after_meals": True}, "餐前服用"),
    ({"after_meals": True}, "請空腹服用，勿與牛奶併服"),   # a 勿 elsewhere keeps the 空腹
    ({"after_meals": True}, "特別：空腹服用")])
def test_an_empty_stomach_or_before_meal_dose_is_never_after_a_meal(schedule_time, instructions):
    """Quick fixes 3 of 19 Oct 2026: no meal question for such a medicine. schedule_time.before_meals, or 空腹, 飯前 or
    餐前 anywhere in the instructions, wins over after_meals and over 飯後 or 餐後 in the same words."""
    lunch = T0 + timedelta(hours=4)   # 12:00 in Taipei
    assert reachy_tasks.after_meal(schedule_time, instructions, lunch) is False


@pytest.mark.parametrize("schedule_time, instructions", [
    (None, "飯後服用，避免空腹"),
    (None, "不可空腹，飯後服用"),
    ({"after_meals": True}, "勿空腹服用"),
    ({"after_meals": True}, "請勿空腹服用"),
    (None, "飯後服用，不宜空腹")])
def test_a_warning_against_an_empty_stomach_keeps_the_meal_question(schedule_time, instructions):
    """Review of quick fixes 3: 「飯後服用，避免空腹」, 「不可空腹，飯後服用」 and 「勿空腹服用」 say "take it with food" (the
    pharmacy bag's warning on NSAIDs and aspirin). Such a 空腹 (ocr_parsing.NOT_EMPTY_STOMACH) is no empty-stomach
    medicine: the meal question stays, as in quick fixes 2."""
    lunch = T0 + timedelta(hours=4)   # 12:00 in Taipei
    assert reachy_tasks.after_meal(schedule_time, instructions, lunch) is True


def test_each_dose_of_the_payload_says_whether_it_is_taken_after_a_meal(db):
    """after_meal per dose, in the leased task and in tasks/current: the robot asks about the meal before the
    medicine prompt when any dose of the slot still to prompt has it."""
    db.doses[101].update(schedule_time={"after_meals": True}, intake_time_stamp=T0 + timedelta(hours=4))   # 12:00
    db.doses[102].update(instructions="飯後服用", intake_time_stamp=T0 + timedelta(hours=4))
    db.doses[103].update(schedule_time={"after_meals": True})   # 08:00: breakfast is no lunch or dinner
    db.now = T0 + timedelta(hours=4)
    enqueue(db, slot=T0 + timedelta(hours=4), expires=T0 + timedelta(hours=5))
    task = run(reachy_tasks.lease_next(7, DEVICE, 0))
    assert {d["intk_id"]: d["after_meal"] for d in task["doses"]} == {101: True, 102: True, 103: False}
    db.doses[102]["instructions"] = "飯前服用"
    current = run(reachy_tasks.current_task(7, DEVICE))
    assert {d["intk_id"]: d["after_meal"] for d in current["doses"]} == {101: True, 102: False, 103: False}
    db.doses[101]["instructions"] = "空腹（飯前1小時或飯後2小時）服用"   # quick fixes 3: no meal question
    current = run(reachy_tasks.current_task(7, DEVICE))
    assert {d["intk_id"]: d["after_meal"] for d in current["doses"]} == {101: False, 102: False, 103: False}
    assert all(set(d) >= {"after_meal"} and "schedule_time" not in d and "instructions" not in d
               for d in current["doses"])   # only the answer goes to the robot


def test_lease_next_skips_expired_tasks(db):
    enqueue(db, expires=T0 - timedelta(seconds=1))
    assert run(reachy_tasks.lease_next(7, DEVICE, 0)) is None


def test_a_queued_task_for_a_later_dose_waits_until_it_is_due(db):
    """3 Oct 2026: the 20:00 dose's task, queued at 00:14, was leased at once. Such a task now waits."""
    db.doses[104] = {"intk_id": 104, "med_id": 2, "med_name": "Metformin", "pill_description": None,
                     "dose_form": "solid_oral", "units_per_dose": Decimal("1.00"), "intake_stats": "pending",
                     "intake_time_stamp": T0 + timedelta(hours=12)}
    db.tasks.append({"task_id": "early", "u_id": 7, "slot_time": T0 + timedelta(hours=12), "intk_ids": [104],
                     "reason": "manual", "attempt": 1, "status": "queued", "lease_owner": None, "lease_until": None,
                     "created_at": T0, "finished_at": None, "expires_at": T0 + timedelta(hours=13), "detail": None})
    assert run(reachy_tasks.lease_next(7, DEVICE, 0)) is None
    db.now = T0 + timedelta(hours=12) - schedule.DOSE_EARLY - timedelta(seconds=1)
    assert run(reachy_tasks.lease_next(7, DEVICE, 0)) is None
    db.now += timedelta(seconds=1)
    assert run(reachy_tasks.lease_next(7, DEVICE, 0))["task_id"] == "early"


def _next_metformin(db, hours=2):
    """Metformin again `hours` after dose 101 (T0): 20:00 and 22:00 in the user's schedule."""
    db.doses[105] = {**db.doses[101], "intk_id": 105, "intake_time_stamp": T0 + timedelta(hours=hours)}


def test_enqueue_refuses_the_next_dose_of_a_medicine_until_halfway(db):
    _next_metformin(db)
    with pytest.raises(schedule.DoseNotDueYet) as refused:
        enqueue(db, reason="manual", slot=T0 + timedelta(hours=2), intk_ids=(105,))
    assert refused.value.due_from == T0 + timedelta(hours=1) and db.tasks == []
    db.now = T0 + timedelta(hours=1)
    assert enqueue(db, reason="manual", slot=T0 + timedelta(hours=2), intk_ids=(105,)) and len(db.tasks) == 1


def test_a_queued_task_for_the_next_dose_of_a_medicine_waits_until_halfway(db):
    _next_metformin(db)
    db.tasks.append({"task_id": "next", "u_id": 7, "slot_time": T0 + timedelta(hours=2), "intk_ids": [105],
                     "reason": "manual", "attempt": 1, "status": "queued", "lease_owner": None, "lease_until": None,
                     "created_at": T0, "finished_at": None, "expires_at": T0 + timedelta(hours=3), "detail": None})
    assert run(reachy_tasks.lease_next(7, DEVICE, 0)) is None          # within DOSE_EARLY, before halfway
    db.now = T0 + timedelta(hours=1)
    assert run(reachy_tasks.lease_next(7, DEVICE, 0))["task_id"] == "next"


def test_lease_next_long_polls_until_a_task_arrives(db, monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            db.tasks.append({
                "task_id": "late", "u_id": 7, "slot_time": T0, "intk_ids": [101], "reason": "manual",
                "attempt": 1, "status": "queued", "lease_owner": None, "lease_until": None,
                "created_at": T0, "finished_at": None, "expires_at": T0 + timedelta(hours=1), "detail": None})

    monkeypatch.setattr(reachy_tasks.asyncio, "sleep", fake_sleep)
    task = run(reachy_tasks.lease_next(7, DEVICE, 25))
    assert task["task_id"] == "late" and sleeps == [1.0, 1.0]


def test_restart_recovery_rereads_dose_status(db):
    enqueue(db)
    run(reachy_tasks.lease_next(7, DEVICE, 0))
    db.doses[101]["intake_stats"] = "pending_confirmation"
    current = run(reachy_tasks.current_task(7, DEVICE))
    status = {d["intk_id"]: d["intake_stats"] for d in current["doses"]}
    assert status == {101: "pending_confirmation", 102: "pending", 103: "taken"}
    assert run(reachy_tasks.current_task(7, OTHER_DEVICE)) is None


# ── Transitions ──────────────────────────────────────────────────────────────

def test_legal_transition_chain(db):
    task_id = enqueue(db)
    run(reachy_tasks.lease_next(7, DEVICE, 0))
    for status in ("searching", "in_progress", "completed"):
        assert run(reachy_tasks.set_status(7, DEVICE, task_id, status, None))["status"] == status
    assert db.by_id(task_id)["finished_at"] == T0


@pytest.mark.parametrize("path,illegal", [
    ((), "in_progress"), ((), "completed"), ((), "not_found"), ((), "queued"),
    (("searching",), "completed"), (("searching",), "leased"),
    (("searching", "in_progress"), "searching"),
    (("aborted",), "searching"), (("searching", "not_found"), "completed"),
])
def test_illegal_transitions_raise(db, path, illegal):
    task_id = enqueue(db)
    run(reachy_tasks.lease_next(7, DEVICE, 0))
    for status in path:
        run(reachy_tasks.set_status(7, DEVICE, task_id, status, None))
    with pytest.raises(ValueError):
        run(reachy_tasks.set_status(7, DEVICE, task_id, illegal, None))


def test_transition_requires_lease_owner_and_own_task(db):
    task_id = enqueue(db)
    run(reachy_tasks.lease_next(7, DEVICE, 0))
    with pytest.raises(ValueError):
        run(reachy_tasks.set_status(7, OTHER_DEVICE, task_id, "searching", None))
    with pytest.raises(reachy_tasks.TaskNotFound):
        run(reachy_tasks.set_status(8, DEVICE, task_id, "searching", None))
    with pytest.raises(reachy_tasks.TaskNotFound):
        run(reachy_tasks.set_status(7, DEVICE, "not-a-uuid", "searching", None))


def test_repeated_status_is_idempotent(db):
    task_id = enqueue(db)
    run(reachy_tasks.lease_next(7, DEVICE, 0))
    run(reachy_tasks.set_status(7, DEVICE, task_id, "searching", None))
    assert run(reachy_tasks.set_status(7, DEVICE, task_id, "searching", None))["status"] == "searching"


def test_abort_robot_offline_sends_one_notice_through_outbox(db):
    task_id = enqueue(db)
    run(reachy_tasks.lease_next(7, DEVICE, 0))
    run(reachy_tasks.set_status(7, DEVICE, task_id, "aborted", {"reason": "robot_offline"}))
    assert len(db.notices) == 1
    notice = db.notices[0]
    assert notice["kind"] == "robot_offline" and notice["contact_flag"] == "notify_missed"
    assert notice["in_transaction"]
    assert notice["messages"][0]["type"] == "text" and "08:00" in notice["messages"][0]["text"]


# ── Heartbeat leases and the minute job ──────────────────────────────────────

def test_extend_leases_only_touches_this_devices_open_tasks(db):
    mine = enqueue(db)
    run(reachy_tasks.lease_next(7, DEVICE, 0))
    db.now = T0 + timedelta(seconds=50)
    run(reachy_tasks.extend_leases(db, DEVICE))
    assert db.by_id(mine)["lease_until"] == T0 + timedelta(seconds=110)
    run(reachy_tasks.extend_leases(db, OTHER_DEVICE))
    assert db.by_id(mine)["lease_until"] == T0 + timedelta(seconds=110)


@pytest.mark.parametrize("status", ["leased", "searching", "in_progress"])
def test_lease_expiry_requeues_task(db, status):
    task_id = enqueue(db)
    run(reachy_tasks.lease_next(7, DEVICE, 0))
    db.by_id(task_id)["status"] = status
    run(reachy_tasks.maintenance(now=T0 + timedelta(seconds=30)))
    assert db.by_id(task_id)["status"] == status
    run(reachy_tasks.maintenance(now=T0 + timedelta(seconds=61)))
    task = db.by_id(task_id)
    assert task["status"] == "queued" and task["lease_owner"] is None
    db.now = T0 + timedelta(seconds=62)
    assert run(reachy_tasks.lease_next(7, OTHER_DEVICE, 0))["task_id"] == task_id


def test_maintenance_expires_tasks_past_expiry(db):
    queued = enqueue(db, expires=T0 + timedelta(minutes=10))
    leased = enqueue(db, slot=T0 + timedelta(hours=1), expires=T0 + timedelta(minutes=10))
    db.by_id(leased).update(status="in_progress", lease_owner=DEVICE, lease_until=T0 + timedelta(hours=2))
    run(reachy_tasks.maintenance(now=T0 + timedelta(minutes=10)))
    assert db.by_id(queued)["status"] == "expired" and db.by_id(leased)["status"] == "expired"


def test_offline_notice_when_slot_passes_with_task_still_queued(db):
    enqueue(db, expires=T0 + timedelta(hours=7))
    db.devices[7]["last_seen_at"] = T0 - timedelta(minutes=5)
    run(reachy_tasks.maintenance(now=T0 - timedelta(minutes=1)))            # slot not reached yet
    assert db.notices == []
    run(reachy_tasks.maintenance(now=T0 + timedelta(minutes=1)))
    assert len(db.notices) == 1
    notice = db.notices[0]
    assert notice["kind"] == "robot_offline" and notice["contact_flag"] == "notify_missed"
    assert notice["priority"] == 1 and notice["in_transaction"]
    text = notice["messages"][0]["text"]
    assert text.index("Reachy 目前離線") < text.index("Reachy is offline")
    # At most one notice per 6 hours, even across a dedupe-window boundary.
    run(reachy_tasks.maintenance(now=T0 + timedelta(minutes=2)))
    run(reachy_tasks.maintenance(now=T0 + timedelta(hours=5, minutes=59)))
    assert len(db.notices) == 1
    run(reachy_tasks.maintenance(now=T0 + timedelta(hours=6, minutes=2)))
    assert len(db.notices) == 2 and db.notices[1]["dedupe_prefix"] != notice["dedupe_prefix"]


def test_no_offline_notice_while_heartbeat_is_fresh(db):
    enqueue(db)
    db.devices[7]["last_seen_at"] = T0 + timedelta(seconds=30)
    run(reachy_tasks.maintenance(now=T0 + timedelta(minutes=1)))
    assert db.notices == []


def test_job_wrapper_runs_maintenance(monkeypatch):
    calls = []

    async def maintenance(now=None):
        calls.append(now)

    monkeypatch.setattr(reachy_tasks, "maintenance", maintenance)
    run(reachy_task_job.run_reachy_task_maintenance())
    assert calls == [None]


def test_supported_dose_rule():
    assert reachy_tasks.is_supported("solid_oral", Decimal("1.00"))
    assert not reachy_tasks.is_supported("solid_oral", Decimal("0.50"))
    assert not reachy_tasks.is_supported("solid_oral", 2)
    assert not reachy_tasks.is_supported("liquid", 1)
    assert not reachy_tasks.is_supported(None, None)


# ── Reminder hook ────────────────────────────────────────────────────────────

class _ReminderConn:
    def __init__(self, rows):
        self.rows = rows
        self.depth = 0
        self.log = []
        self.queries = []
        self.facts_asked = []
        self.updated = []   # (the statement up to its WHERE, its arguments)

    def transaction(self):
        return _Transaction(self)

    async def fetch(self, query, *args):
        if dose_facts.is_facts(query):
            # The job's rows hold the facts a test needs (previous_time, and optionally the others).
            u_id, ids, at, zone = args
            self.facts_asked.append((list(ids), at))
            return [{"intk_id": r["id"], "u_id": r["u_id"], "med_id": r["med_id"], "intake_stats": "pending",
                     "intake_time_stamp": r["intake_time_stamp"], "previous_time": r.get("previous_time"),
                     "next_time": r.get("next_time"), "med_name": r["med_name"], "schedule_time": None,
                     "min_interval_minutes": None, "max_daily_doses": r.get("max_daily_doses"),
                     "protection": r.get("protection", True),
                     "language": None, "last_taken_at": r.get("last_taken_at"),
                     "taken_that_day": r.get("taken_that_day", 0)} for r in self.rows if r["id"] in ids]
        if "FROM intake i" in query:
            self.queries.append(query)
            return self.rows
        return []

    async def execute(self, query, *args):
        self.log.append(("execute", query.split(" WHERE")[0], self.depth))
        self.updated.append((query.split(" WHERE")[0], args))


class _ReminderPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *args):
                return False
        return _Acquire()


class _Line:
    def send_text(self, *args):
        pass

    def send_missed_dose_alert(self, *args, **kwargs):
        pass


def _reminder_rows(slot, reminder_sent=False, retries_sent=0):
    return [{"id": intk_id, "u_id": 7, "intake_time_stamp": slot, "previous_time": None, "reminder_sent": reminder_sent,
             "missed_reminders_sent": retries_sent, "med_id": intk_id, "med_name": f"Med {intk_id}",
             "patient_name": "Pearl", "patient_line_id": "U1", "remind_before_minutes": 5,
             "remind_after_minutes": 10, "remind_after_retries": 3, "notify_family_on_missed": False,
             "meal_retry_waiting": False}
            for intk_id in (101, 102)]


@pytest.mark.parametrize("offset,reason,flag_sql", [
    (timedelta(minutes=5), "upcoming", "UPDATE intake SET reminder_sent = TRUE"),         # 0-5 min ahead
    (timedelta(minutes=-10), "missed_retry", "UPDATE intake SET missed_reminders_sent"),  # 10-15 min late
])
def test_reminder_flag_and_task_enqueue_share_a_transaction(monkeypatch, offset, reason, flag_sql):
    now = datetime.now(timezone.utc)
    slot = missed_dose_job._slot_key(now) + offset
    conn = _ReminderConn(_reminder_rows(slot, reminder_sent=reason != "upcoming"))
    calls = []

    async def fake_enqueue(c, u_id, slot_time, intk_ids, task_reason, expires_at, at=None):
        calls.append((u_id, slot_time, intk_ids, task_reason, expires_at, c.depth, at))
        c.log.append(("enqueue", task_reason, c.depth))
        return "task"

    monkeypatch.setattr(missed_dose_job, "get_pool", lambda: _ReminderPool(conn))
    monkeypatch.setattr(missed_dose_job, "enqueue_reachy_task", fake_enqueue)
    monkeypatch.setattr(missed_dose_job.LineService, "get_instance", classmethod(lambda cls: _Line()))
    run(missed_dose_job.check_missed_doses())
    assert len(calls) == 1
    u_id, slot_time, intk_ids, task_reason, expires_at, depth, at = calls[0]
    assert (u_id, slot_time, intk_ids, task_reason) == (7, slot, [101, 102], reason)
    assert expires_at == slot + timedelta(minutes=10 * (3 + 1))
    # Judged on the job's clock, the moment the doses were picked by (conn.facts_asked).
    assert at == conn.facts_asked[0][1]
    # In a savepoint inside the reminder's transaction: a refusal there never rolls the flag back.
    assert depth == 2
    flag = [entry for entry in conn.log if entry[0] == "execute" and entry[1].startswith(flag_sql)]
    assert flag and flag[0][2] == 1
    # the flag update and the enqueue are adjacent inside the same transaction block
    index = conn.log.index(flag[0])
    assert conn.log[index + 1] == ("enqueue", reason, 2)


def test_reminder_further_ahead_than_the_early_window_sends_line_but_no_robot_task(monkeypatch):
    """remind_before_minutes can exceed DOSE_EARLY_MINUTES through the API: the LINE reminder still goes (and its
    flag is kept), but the robot is not sent for a dose that is not due."""
    now = datetime.now(timezone.utc)
    slot = missed_dose_job._slot_key(now) + schedule.DOSE_EARLY + timedelta(minutes=30)
    rows = _reminder_rows(slot)
    for row in rows:
        row["remind_before_minutes"] = int(schedule.DOSE_EARLY.total_seconds() // 60) + 60
    conn = _ReminderConn(rows)
    calls, sent = [], []

    async def fake_enqueue(*args, **kwargs):
        calls.append(args)

    line = _Line()
    line.send_text = lambda *args: sent.append(args)
    monkeypatch.setattr(missed_dose_job, "get_pool", lambda: _ReminderPool(conn))
    monkeypatch.setattr(missed_dose_job, "enqueue_reachy_task", fake_enqueue)
    monkeypatch.setattr(missed_dose_job.LineService, "get_instance", classmethod(lambda cls: line))
    run(missed_dose_job.check_missed_doses())
    assert sent and calls == []
    assert any(entry[0] == "execute" and entry[1].startswith("UPDATE intake SET reminder_sent = TRUE")
               for entry in conn.log)


@pytest.mark.parametrize("gap,robot", [(timedelta(minutes=8), False), (timedelta(hours=2), True)])
def test_upcoming_robot_task_waits_for_halfway_from_the_same_medicines_previous_dose(monkeypatch, gap, robot):
    """Custom times 8 minutes apart: at the 5-minute reminder the second dose is not due yet (from halfway, 4
    minutes before), so the LINE reminder goes alone and the robot comes with the first overdue retry."""
    slot = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)                 # 20:00 in Taipei

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return slot - timedelta(minutes=5)                               # the reminder at 19:55

    rows = _reminder_rows(slot)
    for row in rows:
        row["previous_time"] = slot - gap
    conn = _ReminderConn(rows)
    calls, sent = [], []

    async def fake_enqueue(*args, **kwargs):
        calls.append(args)

    line = _Line()
    line.send_text = lambda *args: sent.append(args)
    monkeypatch.setattr(missed_dose_job, "datetime", _Clock)
    monkeypatch.setattr(missed_dose_job, "get_pool", lambda: _ReminderPool(conn))
    monkeypatch.setattr(missed_dose_job, "enqueue_reachy_task", fake_enqueue)
    monkeypatch.setattr(missed_dose_job.LineService, "get_instance", classmethod(lambda cls: line))
    run(missed_dose_job.check_missed_doses())
    assert sent and bool(calls) is robot
    # Overdose protection decides (dose_safety.allowed), on the job's clock, for the slot's doses together.
    assert conn.facts_asked == [([101, 102], slot - timedelta(minutes=5))]


def _run_reminders(monkeypatch, conn, enqueue=None):
    """check_missed_doses over `conn`; returns (the robot's dose lists, the patient's LINE texts)."""
    calls, sent = [], []

    async def fake_enqueue(c, u_id, slot_time, intk_ids, reason, expires_at, at=None):
        calls.append(list(intk_ids))
        if enqueue is not None:
            await enqueue(c)

    line = _Line()
    line.send_text = lambda line_id, text: sent.append(text)
    monkeypatch.setattr(missed_dose_job, "get_pool", lambda: _ReminderPool(conn))
    monkeypatch.setattr(missed_dose_job, "enqueue_reachy_task", fake_enqueue)
    monkeypatch.setattr(missed_dose_job.LineService, "get_instance", classmethod(lambda cls: line))
    run(missed_dose_job.check_missed_doses())
    return calls, sent


@pytest.mark.parametrize("protection", [True, False])
def test_the_robot_and_the_reminder_leave_out_a_dose_too_soon_after_the_last_one(monkeypatch, protection):
    """Med 101 taken late at 19:40 (an ad-hoc dose; it keeps 4 h between doses): at the 20:00 reminder the robot
    comes for Med 102 alone (one medicine does not keep it from the others), and the LINE reminder asks for Med 102
    and says why not Med 101. With protection off, both as before."""
    now = datetime.now(timezone.utc)
    slot = missed_dose_job._slot_key(now) + timedelta(minutes=5)
    rows = _reminder_rows(slot)
    rows[0].update(last_taken_at=now - timedelta(minutes=20))
    for row in rows:
        row["protection"] = protection
    calls, sent = _run_reminders(monkeypatch, _ReminderConn(rows))
    (text,) = sent
    # On the patient's clock (the database's UTC once said "14:00" for the 22:00 dose).
    assert f"您預定於 {slot.astimezone(missed_dose_job._TZ):%H:%M} 服用" in text
    if protection:
        assert calls == [[102]]
        asked, held = text.split("請準時服用。")
        assert "Med 102" in asked and "Med 101" not in asked
        assert held.startswith("\n⚠️ Med 101：這個藥您") and held.endswith("已經吃過了，請先不要再吃。")
    else:
        assert calls == [[101, 102]] and "⚠️" not in text


def test_an_overdue_reminder_never_asks_for_a_dose_missed_past_halfway(monkeypatch):
    """Doses 15 minutes apart (custom times): at the first overdue retry Med 101's dose has expired. The patient is
    told not to make it up, not "please take it as soon as possible", and the robot comes for Med 102 only."""
    now = datetime.now(timezone.utc)
    slot = missed_dose_job._slot_key(now) - timedelta(minutes=10)
    rows = _reminder_rows(slot, reminder_sent=True)
    rows[0]["next_time"] = slot + timedelta(minutes=15)          # expired 7.5 minutes after its time
    calls, sent = _run_reminders(monkeypatch, _ReminderConn(rows))
    (text,) = sent
    asked, held = text.split("請盡快服用。")
    assert "Med 102" in asked and "Med 101" not in asked
    assert "⚠️ Med 101：" in held and "已經錯過了，請不要補吃，等下一次就好。" in held
    assert calls == [[102]]


def test_a_reminder_where_every_dose_is_held_back_still_says_why_and_sends_no_robot(monkeypatch):
    now = datetime.now(timezone.utc)
    slot = missed_dose_job._slot_key(now) + timedelta(minutes=5)
    rows = _reminder_rows(slot)
    for row in rows:
        row.update(taken_that_day=3, max_daily_doses=3)
    calls, sent = _run_reminders(monkeypatch, _ReminderConn(rows))
    (text,) = sent
    assert "請準時服用" not in text and text.count("⚠️") == 2 and "已經吃滿 3 次了，請不要再吃。" in text
    assert calls == []


def test_a_dose_refused_while_the_robot_is_sent_never_undoes_the_reminder(monkeypatch):
    """Something changed between the verdicts and the robot (e.g. the patient tapped a dose taken meanwhile): the
    refusal stays in the robot's savepoint, the reminder flag commits and the run goes on."""
    now = datetime.now(timezone.utc)
    slot = missed_dose_job._slot_key(now) + timedelta(minutes=5)
    conn = _ReminderConn(_reminder_rows(slot))

    async def refuse(c):
        raise dose_safety.DoseTooSoon(last_taken_at=now, gap=timedelta(hours=4), intk_id=101)

    calls, sent = _run_reminders(monkeypatch, conn, enqueue=refuse)
    assert calls == [[101, 102]] and sent
    assert any(entry[0] == "execute" and entry[1].startswith("UPDATE intake SET reminder_sent = TRUE")
               for entry in conn.log)


class _FamilyConn(_ReminderConn):
    """_ReminderConn whose patient has one verified family contact for missed-dose alerts."""

    async def fetch(self, query, *args):
        if "FROM family_contacts" in query:
            return [{"line_id": "UF", "name": "Daughter"}]
        return await super().fetch(query, *args)


_A_MINUTE_BEFORE = object()


def _run_at(monkeypatch, moment, rows, last_run=_A_MINUTE_BEFORE):
    """check_missed_doses at `moment` over `rows` (family on), the job's previous run at `last_run` (default: a minute
    before, as in normal operation; None: the first run since the process started); returns (robot calls, patient
    texts, family alerts, the conn)."""
    if last_run is _A_MINUTE_BEFORE:
        last_run = moment - timedelta(minutes=1)
    monkeypatch.setattr(missed_dose_job, "_last_run_at", last_run)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    for row in rows:
        row["notify_family_on_missed"] = True
    conn = _FamilyConn(rows)
    calls, sent, family = [], [], []

    async def fake_enqueue(c, u_id, slot_time, intk_ids, reason, expires_at, at=None):
        calls.append((list(intk_ids), reason))

    line = _Line()
    line.send_text = lambda line_id, text: sent.append(text)
    line.send_missed_dose_alert = lambda *args, **kwargs: family.append((args, kwargs))
    monkeypatch.setattr(missed_dose_job, "datetime", _Clock)
    monkeypatch.setattr(missed_dose_job, "get_pool", lambda: _ReminderPool(conn))
    monkeypatch.setattr(missed_dose_job, "enqueue_reachy_task", fake_enqueue)
    monkeypatch.setattr(missed_dose_job.LineService, "get_instance", classmethod(lambda cls: line))
    run(missed_dose_job.check_missed_doses())
    return calls, sent, family, conn


SLOT_20 = datetime(2026, 10, 16, 10, 5, tzinfo=timezone.utc)            # 18:05 in Taipei


@pytest.mark.parametrize("last_run", [
    None,                                   # the first run since the server came back (16 Oct: the process restarted)
    SLOT_20 + timedelta(minutes=5),         # the job's last run came before the slot's first retry (+10 min)
])
def test_after_an_outage_a_slot_past_its_last_retry_is_only_reported_missed_while_offline(monkeypatch, last_run):
    """16 Oct 2026: the server came back at 22:57 and the first run sent the 18:05 slot a "(10m) please take it as
    soon as possible" text and a robot task, then the missed alerts within a second. A slot first seen past its last
    retry gets neither: only the missed alerts, which say it was missed while the system was offline."""
    rows = _reminder_rows(SLOT_20, reminder_sent=False, retries_sent=0)
    calls, sent, family, conn = _run_at(monkeypatch, SLOT_20 + timedelta(hours=4, minutes=52), rows, last_run)
    assert calls == []                                              # no robot for a slot 4.5 h old
    (text,) = sent                                                  # the missed alert alone
    assert "請盡快服用" not in text and "逾時" not in text
    assert text.startswith("❌ 用藥未完成") and "系統離線" in text
    ((args, kwargs),) = family
    assert args[0] == "UF" and kwargs == {"offline": True}
    executed = [entry[1] for entry in conn.log if entry[0] == "execute"]
    assert not any(q.startswith("UPDATE intake SET missed_reminders_sent") for q in executed)
    assert any(q.startswith("UPDATE intake SET intake_stats = 'missed'") for q in executed)
    assert not any("missed_reminder'" in q for q in executed)


@pytest.mark.parametrize("retries,retries_sent,late", [
    (3, 2, timedelta(minutes=30, seconds=20)),    # the third retry and the missed alert in one normal run
    (1, 0, timedelta(minutes=10, seconds=40)),    # one retry: it and the missed alert come in the same run
    (3, 0, timedelta(minutes=31)),                # first seen just past the last retry: within the job's grace
])
def test_a_slot_reached_on_time_keeps_its_last_retry_and_plain_missed_alerts(monkeypatch, retries, retries_sent,
                                                                             late):
    rows = _reminder_rows(SLOT_20, reminder_sent=True, retries_sent=retries_sent)
    for row in rows:
        row["remind_after_retries"] = retries
    calls, sent, family, conn = _run_at(monkeypatch, SLOT_20 + late, rows)
    assert [reason for _, reason in calls] == ["missed_retry"]
    retry, missed = sent
    assert "請盡快服用" in retry and missed.startswith("❌ 用藥未完成") and "系統離線" not in missed
    ((args, kwargs),) = family
    assert kwargs == {"offline": False}


def test_a_dose_back_from_family_confirmation_late_is_not_called_missed_while_offline(monkeypatch):
    """A dose sent to family before its first retry comes back 'pending' when family answer 'not taken' or the
    request expires after 2 h (dose_confirmation._restore), with its retry count still 0: the job reads only
    'pending' rows, so it counted none meanwhile. The job ran all along, so its missed alerts must not say the
    system was offline."""
    rows = _reminder_rows(SLOT_20, reminder_sent=True, retries_sent=0)
    moment = SLOT_20 + timedelta(hours=2, minutes=1)
    calls, sent, family, conn = _run_at(monkeypatch, moment, rows)          # the previous run a minute ago
    assert sent and not any("系統離線" in text for text in sent)
    assert sent[-1].startswith("❌ 用藥未完成")
    ((args, kwargs),) = family
    assert kwargs == {"offline": False}
    executed = [entry[1] for entry in conn.log if entry[0] == "execute"]
    assert any(q.startswith("UPDATE intake SET intake_stats = 'missed'") for q in executed)
    assert missed_dose_job._last_run_at == moment                            # this run is the next one's evidence


def test_the_family_alert_says_the_dose_was_missed_while_the_system_was_offline():
    from app.services.line_service import LineService

    sent = []
    svc = LineService.__new__(LineService)
    svc.send_text = lambda to, text: sent.append(text) or {}
    svc.send_missed_dose_alert("UF", "Pearl", "Med 101", "2026-10-16 18:05", offline=True)
    svc.send_missed_dose_alert("UF", "Pearl", "Med 101", "2026-10-16 18:05")
    offline, plain = sent
    assert "系統離線" in offline and "請關心確認狀況。" in offline
    assert "系統離線" not in plain and plain.endswith("請關心確認狀況。")


# ── Overdose protection on enqueue and lease ─────────────────────────────────

def test_enqueue_refuses_a_second_dose_too_soon_after_the_last(db):
    """Metformin (no times: 4 h between doses) taken ad hoc 30 minutes ago."""
    earlier = T0 - timedelta(minutes=30)
    db.doses[106] = {**db.doses[101], "intk_id": 106, "intake_stats": "taken", "intake_time_stamp": earlier,
                     "actual_intake_time": earlier}
    with pytest.raises(dose_safety.DoseTooSoon) as refused:
        enqueue(db, intk_ids=(101,))
    assert refused.value.intk_id == 101 and db.tasks == []
    db.protection = False
    assert enqueue(db, intk_ids=(101,)) and len(db.tasks) == 1


def test_a_queued_task_whose_dose_expired_is_not_leased(db):
    """101 (08:00) with the next Metformin at 10:00 expires at 09:00: the robot must not ask for it any more."""
    _next_metformin(db)
    enqueue(db, intk_ids=(101,), expires=T0 + timedelta(hours=3))
    db.now = T0 + timedelta(hours=1)
    assert run(reachy_tasks.lease_next(7, DEVICE, 0)) is None
    db.protection = False
    assert run(reachy_tasks.lease_next(7, DEVICE, 0))["doses"][0]["intk_id"] == 101


def test_a_taken_dose_past_its_halfway_point_does_not_hold_back_the_slots_other_doses(db):
    """101 (Metformin 08:00) was taken; at 09:00 it is past halfway to the 10:00 one. Expiry is a matter of time
    alone, so only open doses decide: the slot's Aspirin (102) still gets the robot."""
    _next_metformin(db)
    enqueue(db, intk_ids=(101, 102), expires=T0 + timedelta(hours=3))
    db.doses[101]["intake_stats"] = "taken"
    db.now = T0 + timedelta(hours=1)
    task = run(reachy_tasks.lease_next(7, DEVICE, 0))
    assert task is not None and {d["intk_id"] for d in task["doses"]} == {101, 102}


def test_with_protection_off_a_task_for_a_later_dose_is_leased_at_once(db):
    """The behaviour before 3 Oct, when the patient chose it: a test alert at midnight starts the 20:00 dose."""
    db.protection = False
    db.doses[104] = {**db.doses[101], "intk_id": 104, "intake_time_stamp": T0 + timedelta(hours=12)}
    assert enqueue(db, reason="manual", slot=T0 + timedelta(hours=12), intk_ids=(104,),
                   expires=T0 + timedelta(hours=13))
    assert run(reachy_tasks.lease_next(7, DEVICE, 0))["doses"][0]["intk_id"] == 104


# ── Medicine first: task_due for the heartbeat (companion spec 3.4) ─────────

def test_task_due_when_a_medicine_task_waits_during_a_chat(db):
    # The robot is chatting in a check-in task it leased; that task is not queued, so it never counts.
    db.tasks.append({"task_id": "chat", "u_id": 7, "slot_time": T0 - timedelta(minutes=30), "intk_ids": [],
                     "reason": "checkin", "attempt": 1, "status": "in_progress", "lease_owner": DEVICE,
                     "lease_until": T0 + timedelta(seconds=60), "created_at": T0 - timedelta(minutes=30),
                     "finished_at": None, "expires_at": T0 + timedelta(minutes=15), "detail": None})
    assert run(reachy_tasks.task_due(db, 7)) is False
    enqueue(db)                                         # the 08:00 doses, due now
    assert run(reachy_tasks.task_due(db, 7)) is True
    assert run(reachy_tasks.task_due(db, 8)) is False   # another patient's robot is told nothing of it


def test_a_queued_checkin_or_a_task_with_nothing_left_to_take_is_no_medicine(db):
    enqueue(db, reason="checkin", slot=T0 + timedelta(minutes=5), intk_ids=())   # 'Talk to Reachy now' again
    db.tasks.append({"task_id": "done", "u_id": 7, "slot_time": T0, "intk_ids": [103], "reason": "manual",
                     "attempt": 1, "status": "queued", "lease_owner": None, "lease_until": None, "created_at": T0,
                     "finished_at": None, "expires_at": T0 + timedelta(hours=1), "detail": None})   # 103 is taken
    assert run(reachy_tasks.task_due(db, 7)) is False
    assert run(reachy_tasks.lease_next(7, DEVICE, 0))["task_id"] == "done"   # leased after the chat, as today


def test_a_missed_retry_during_a_held_task_is_due_and_queued_when_it_ends(db):
    # The 08:00 dose task left its doses pending and went on as a companion session, which holds the slot's task in
    # progress; the missed-dose job's +10 min retry for that slot comes meanwhile.
    task_id = enqueue(db, intk_ids=(101, 102))
    assert run(reachy_tasks.lease_next(7, DEVICE, 0))["task_id"] == task_id
    for status in ("searching", "in_progress"):
        run(reachy_tasks.set_status(7, DEVICE, task_id, status, None))
    db.now = T0 + timedelta(minutes=10)
    assert enqueue(db, reason="missed_retry", intk_ids=(101, 102)) == task_id   # the slot's task is held: none queued
    assert [t["task_id"] for t in db.tasks] == [task_id]
    assert run(reachy_tasks.task_due(db, 7)) is True   # but the doses still owed are medicine first
    db.doses[102]["intake_stats"] = "taken"            # one of them taken meanwhile
    run(reachy_tasks.set_status(7, DEVICE, task_id, "completed", {"outcomes": {"checkin": "medicine"}}))
    (retry,) = [t for t in db.tasks if t["task_id"] != task_id]   # queued as the task ended, open doses only
    assert (retry["reason"], retry["intk_ids"], retry["status"]) == ("missed_retry", [101], "queued")
    assert retry["expires_at"] == T0 + timedelta(minutes=40)       # the retry's own expiry
    assert run(reachy_tasks.lease_next(7, DEVICE, 0))["task_id"] == retry["task_id"]


@pytest.mark.parametrize("outcomes, queued", [
    ({"101": "meal_not_eaten", "102": "meal_not_eaten"}, None),
    ({"101": "meal_not_eaten", "102": "no_answer"}, [102])])
def test_a_task_ended_by_meal_not_eaten_queues_no_retry_for_those_doses(db, outcomes, queued):
    """Review of quick fixes 3: the robot has just said 「等您吃過飯，我再來陪您吃藥。」 and rests (the outcome
    meal_not_eaten). A missed-dose retry that marked the held task meanwhile is not queued for such a dose as the task
    ends, or Reachy would wake again at once; the missed-dose job's next retry still comes."""
    task_id = enqueue(db, intk_ids=(101, 102))
    assert run(reachy_tasks.lease_next(7, DEVICE, 0))["task_id"] == task_id
    for status in ("searching", "in_progress"):
        run(reachy_tasks.set_status(7, DEVICE, task_id, status, None))
    db.now = T0 + timedelta(minutes=10)
    assert enqueue(db, reason="missed_retry", intk_ids=(101, 102)) == task_id   # marked retry_due
    run(reachy_tasks.set_status(7, DEVICE, task_id, "completed", {"outcomes": outcomes}))
    retries = [t for t in db.tasks if t["task_id"] != task_id and t.get("not_before") is None]
    assert [t["intk_ids"] for t in retries] == ([queued] if queued else [])


# ── Meal retries (quick fix 4 of 20 Oct 2026) ────────────────────────────────

def _completed_with(db, task_id, outcomes):
    """The robot leases task_id, finds the patient and reports it completed with these outcomes per dose."""
    assert run(reachy_tasks.lease_next(7, DEVICE, 0))["task_id"] == task_id
    for status in ("searching", "in_progress"):
        run(reachy_tasks.set_status(7, DEVICE, task_id, status, None))
    run(reachy_tasks.set_status(7, DEVICE, task_id, "completed", {"outcomes": outcomes}))


def _meal_retries(db):
    return [t for t in db.tasks if t.get("not_before") is not None]


def _job_on(db, monkeypatch, online=True):
    """check_missed_doses at db.now over the fake database itself (the real enqueue_reachy_task; the job's previous
    run a minute before; the robot's last heartbeat 10 s before, or 2 minutes before when not online); returns the
    patient's LINE texts."""
    db.devices[7]["last_seen_at"] = db.now - timedelta(seconds=10 if online else 120)
    sent = []
    line = _Line()
    line.send_text = lambda line_id, text: sent.append(text)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return db.now

    monkeypatch.setattr(missed_dose_job, "datetime", _Clock)
    monkeypatch.setattr(missed_dose_job, "_last_run_at", db.now - timedelta(minutes=1))
    monkeypatch.setattr(missed_dose_job, "get_pool", lambda: db)
    monkeypatch.setattr(missed_dose_job.LineService, "get_instance", classmethod(lambda cls: line))
    run(missed_dose_job.check_missed_doses())
    return sent


def test_not_eaten_yet_queues_one_meal_retry_leasable_20_minutes_after_the_report(db):
    """The user, 20 Oct 2026: "yes reachy would remind after another 20min". The robot reports the 08:00 task
    completed with meal_not_eaten for both doses at 08:10: one task for both, leasable from 08:30 (not at 08:29), that
    expires like a manual task from that time (MANUAL_TASK_MIN_MINUTES later, as that beats the slot's window)."""
    assert (reachy_tasks.MEAL_RETRY_MINUTES, reachy_tasks.MEAL_RETRY_LIMIT) == (20, 3)
    task_id = enqueue(db, intk_ids=(101, 102))
    db.now = T0 + timedelta(minutes=10)
    _completed_with(db, task_id, {"101": "meal_not_eaten", "102": "meal_not_eaten"})
    (retry,) = _meal_retries(db)
    due = T0 + timedelta(minutes=30)
    assert (retry["reason"], retry["intk_ids"], retry["status"]) == ("missed_retry", [101, 102], "queued")
    assert retry["not_before"] == due and retry["slot_time"] == due   # its own slot: never re-armed by the slot's
    assert retry["expires_at"] == due + timedelta(minutes=reachy_tasks.MANUAL_TASK_MIN_MINUTES)
    db.now = due - timedelta(minutes=1)
    assert run(reachy_tasks.task_due(db, 7)) is False   # nor does it end a chat before its time
    assert run(reachy_tasks.lease_next(7, DEVICE, 0)) is None
    db.now = due
    assert run(reachy_tasks.task_due(db, 7)) is True
    task = run(reachy_tasks.lease_next(7, DEVICE, 0))
    assert task["task_id"] == retry["task_id"] and [d["intk_id"] for d in task["doses"]] == [102, 101]


def test_a_meal_retry_reported_soon_after_the_slot_keeps_the_slots_window(db):
    """Reported at 08:02 (Reachy came with the 08:00 task): leasable from 08:22, and it expires with the slot's
    missed window (08:40: remind_after_minutes x (retries + 1)), later than 08:22 + MANUAL_TASK_MIN_MINUTES."""
    task_id = enqueue(db, intk_ids=(101,))
    db.now = T0 + timedelta(minutes=2)
    _completed_with(db, task_id, {"101": "meal_not_eaten"})
    (retry,) = _meal_retries(db)
    assert retry["not_before"] == T0 + timedelta(minutes=22) and retry["expires_at"] == T0 + timedelta(minutes=40)


def test_a_meal_retry_survives_a_restart_because_its_not_before_is_stored():
    """No timer in the server's memory: the not_before is a column of reachy_task (added idempotently to an older
    database), and the lease reads it there. A row as the database hands it back after a restart, never queued by
    this process, still waits for its time."""
    schema = open(os.path.join(os.path.dirname(__file__), "..", "sql", "init.sql"), encoding="utf-8").read()
    statement = "ALTER TABLE reachy_task ADD COLUMN IF NOT EXISTS not_before TIMESTAMPTZ;"
    assert statement in schema and schema.index(statement) > schema.index("CREATE TABLE IF NOT EXISTS reachy_task")
    assert "t.not_before <= NOW()" in reachy_tasks._leasable_sql()


def test_a_meal_retry_row_found_after_a_restart_waits_for_its_time(db):
    due = T0 + timedelta(minutes=30)
    db.tasks.append({"task_id": "meal", "u_id": 7, "slot_time": due, "intk_ids": [101], "reason": "missed_retry",
                     "attempt": 1, "status": "queued", "lease_owner": None, "lease_until": None, "created_at": T0,
                     "finished_at": None, "expires_at": due + timedelta(minutes=15), "detail": None,
                     "not_before": due})
    db.now = due - timedelta(minutes=1)
    assert run(reachy_tasks.lease_next(7, DEVICE, 0)) is None
    db.now = due
    assert run(reachy_tasks.lease_next(7, DEVICE, 0))["task_id"] == "meal"


def test_at_most_three_meal_retries_per_dose(db):
    """Counted from the stored tasks: the "not yet" in the original task and in the first two meal retries each
    queue one; the "not yet" in the third queues none, and the missed-dose job handles the dose as before."""
    task_id = enqueue(db, intk_ids=(101,))
    for count in (1, 2, 3, 3):
        _completed_with(db, task_id, {"101": "meal_not_eaten"})
        retries = _meal_retries(db)
        assert len(retries) == count
        task_id = retries[-1]["task_id"]
        db.now = retries[-1]["not_before"]
    assert all(t["status"] == "completed" for t in _meal_retries(db))   # none left waiting for the job to defer to
    assert run(reachy_tasks.lease_next(7, DEVICE, 0)) is None


def test_two_doses_in_one_slot_only_the_one_not_eaten_for_is_retried(db):
    """Quick fixes 3's minor finding: in one slot, Reachy prompted the empty-stomach medicine (sent to family) and
    the patient had not eaten for the after-meal one."""
    task_id = enqueue(db, intk_ids=(101, 102))
    db.doses[102]["intake_stats"] = "pending_confirmation"
    _completed_with(db, task_id, {"101": "meal_not_eaten", "102": "confirmation_requested"})
    (retry,) = _meal_retries(db)
    assert retry["intk_ids"] == [101]


def test_a_dose_resolved_before_the_report_or_already_waiting_gets_no_new_meal_retry(db):
    """A dose taken (in the app) before the robot's report gets none, and a dose another open meal retry already waits
    for gets no second one ('Use Reachy' pressed during the wait, and "not yet" again): nothing is prompted twice."""
    task_id = enqueue(db, intk_ids=(101, 102))
    db.doses[102]["intake_stats"] = "taken"
    _completed_with(db, task_id, {"101": "meal_not_eaten", "102": "meal_not_eaten"})
    (retry,) = _meal_retries(db)
    assert retry["intk_ids"] == [101]
    manual = enqueue(db, reason="manual", slot=T0, intk_ids=(101,))   # 'Use Reachy' on the 08:00 dose
    _completed_with(db, manual, {"101": "meal_not_eaten"})
    assert _meal_retries(db) == [retry]


@pytest.mark.parametrize("status", ["taken", "pending_confirmation", "skipped"])
def test_a_meal_retry_whose_dose_was_resolved_meanwhile_is_cancelled_and_never_leased(db, status):
    """Taken in the app or through another path, sent to family, or skipped while Reachy waited for the meal: the
    robot is never woken for it (the lease skips it) and the minute job cancels it."""
    task_id = enqueue(db, intk_ids=(101,))
    _completed_with(db, task_id, {"101": "meal_not_eaten"})
    (retry,) = _meal_retries(db)
    db.doses[101]["intake_stats"] = status
    db.now = retry["not_before"]
    assert run(reachy_tasks.task_due(db, 7)) is False
    assert run(reachy_tasks.lease_next(7, DEVICE, 0)) is None
    run(reachy_tasks.maintenance(now=db.now))
    assert retry["status"] == "aborted" and json.loads(retry["detail"]) == {"reason": "nothing_to_prompt"}


def test_a_meal_retry_with_one_dose_left_prompts_only_that_one(db):
    task_id = enqueue(db, intk_ids=(101, 102))
    _completed_with(db, task_id, {"101": "meal_not_eaten", "102": "meal_not_eaten"})
    (retry,) = _meal_retries(db)
    db.doses[102]["intake_stats"] = "taken"
    db.now = retry["not_before"]
    run(reachy_tasks.maintenance(now=db.now))
    assert retry["status"] == "queued"   # still a dose to take
    task = run(reachy_tasks.lease_next(7, DEVICE, 0))
    assert {d["intk_id"]: d["intake_stats"] for d in task["doses"]} == {101: "pending", 102: "taken"}


def test_no_meal_retry_for_a_dose_that_expires_before_reachy_could_come_back(db):
    """Overdose protection judges the doses at the meal retry's time: Metformin again at 08:30 makes the 08:00 dose
    expire at 08:15, before 08:30, so none is queued and the missed-dose job handles it at once."""
    _next_metformin(db, hours=0.5)
    task_id = enqueue(db, intk_ids=(101,))
    db.now = T0 + timedelta(minutes=5)
    _completed_with(db, task_id, {"101": "meal_not_eaten"})
    assert _meal_retries(db) == []


@pytest.mark.parametrize("status", ["not_found", "aborted"])
def test_only_a_completed_task_queues_a_meal_retry(db, status):
    task_id = enqueue(db, intk_ids=(101,))
    run(reachy_tasks.lease_next(7, DEVICE, 0))
    run(reachy_tasks.set_status(7, DEVICE, task_id, "searching", None))
    run(reachy_tasks.set_status(7, DEVICE, task_id, status, {"outcomes": {"101": "meal_not_eaten"}}))
    assert _meal_retries(db) == []


def test_a_dose_left_pending_in_a_meal_retry_gets_the_jobs_next_retry_and_ends_the_chat(db, monkeypatch):
    """Review of quick fix 4: a meal retry holds its dose only until Reachy is with the patient. At 07:56 the patient
    says "not yet", and the job's +10 min retry is counted silently while the meal retry waits. At 08:16 the meal
    retry is leased, the patient says "eaten", the watch sees nothing and the dose is left pending; the robot goes on
    to the check-in, whose heartbeats keep the task in progress (extend_for_companion). At +20 min the job handles
    the dose as for any dose task: its LINE text and a robot retry for the slot, which is medicine first (task_due),
    so Reachy ends the chat with 「該吃藥囉」 and leases it next."""
    assert "in_progress" not in reachy_tasks.meal_retry_waiting_sql("i")
    db.doses[102]["intake_stats"] = "taken"            # the slot's one dose to take: Metformin (101)
    db.now = T0 - timedelta(minutes=4)
    task_id = enqueue(db, intk_ids=(101,))
    _completed_with(db, task_id, {"101": "meal_not_eaten"})
    (meal,) = _meal_retries(db)
    db.now = T0 + timedelta(minutes=10)
    assert _job_on(db, monkeypatch) == [] and len(db.tasks) == 2   # held: no text and no robot, its retry counted
    db.now = meal["not_before"]
    assert run(reachy_tasks.lease_next(7, DEVICE, 0))["task_id"] == meal["task_id"]
    for status in ("searching", "in_progress"):
        run(reachy_tasks.set_status(7, DEVICE, meal["task_id"], status, None))
    db.now = T0 + timedelta(minutes=17)
    run(reachy_tasks.extend_for_companion(db, DEVICE))   # the check-in's heartbeat, after the dose was left pending
    db.now = T0 + timedelta(minutes=20)
    (text,) = _job_on(db, monkeypatch)
    assert "逾時 20 分鐘" in text and "Metformin" in text and "請盡快服用" in text
    (retry,) = [t for t in db.tasks if t["task_id"] not in (task_id, meal["task_id"])]
    assert (retry["reason"], retry["slot_time"], retry["intk_ids"], retry["status"]) == (
        "missed_retry", T0, [101], "queued")
    assert run(reachy_tasks.task_due(db, 7)) is True
    run(reachy_tasks.set_status(7, DEVICE, meal["task_id"], "completed",
                                {"outcomes": {"101": "help_left_pending", "checkin": "medicine"}}))
    assert run(reachy_tasks.lease_next(7, DEVICE, 0))["task_id"] == retry["task_id"]


def test_while_the_robot_is_offline_a_meal_retry_holds_nothing_back(db, monkeypatch):
    """Review of quick fix 4: the robot went offline after the patient's "not yet". The queued meal retry held the dose
    until it expired, so the patient got no LINE reminder before the missed alert, while the robot-offline notice
    told family 「LINE 用藥提醒仍會照常發送」. The hold lasts only while the patient's robot is online (a heartbeat
    within OFFLINE_AFTER_SECONDS): offline, the job's retry comes as before, and once it is back the hold is too."""
    db.doses[102]["intake_stats"] = "taken"
    db.now = T0 - timedelta(minutes=4)
    task_id = enqueue(db, intk_ids=(101,))
    _completed_with(db, task_id, {"101": "meal_not_eaten"})
    db.now = T0 + timedelta(minutes=10)
    (text,) = _job_on(db, monkeypatch, online=False)
    assert "逾時 10 分鐘" in text and "Metformin" in text
    (retry,) = [t for t in db.tasks if t["slot_time"] == T0 and t["status"] == "queued"]
    assert (retry["reason"], retry["intk_ids"]) == ("missed_retry", [101])
    run(reachy_tasks.maintenance(now=db.now))
    (notice,) = db.notices
    assert notice["kind"] == "robot_offline" and "LINE 用藥提醒仍會照常發送" in notice["messages"][0]["text"]
    db.now = T0 + timedelta(minutes=20)
    assert _job_on(db, monkeypatch) == []   # online again: held, its retry counted


def test_a_meal_retry_whose_dose_expired_before_the_lease_is_cancelled(db, monkeypatch):
    """Review of quick fix 4: overdose protection judged the dose only at not_before. Metformin again at 08:40 makes
    the 08:00 dose expire at 08:20. The meal retry may be leased from 08:16, but the robot leases nothing in time
    (busy, or offline), and from 08:20 the lease never hands it out. The minute job cancels it (dose_expired), so the
    missed-dose job takes the dose at once instead of being held until the retry's own expiry (08:40)."""
    db.doses[105] = {**db.doses[101], "intk_id": 105, "intake_time_stamp": T0 + timedelta(minutes=40)}
    db.doses[102]["intake_stats"] = "taken"
    db.now = T0 - timedelta(minutes=4)
    task_id = enqueue(db, intk_ids=(101,))
    _completed_with(db, task_id, {"101": "meal_not_eaten"})
    (meal,) = _meal_retries(db)
    assert meal["not_before"] == T0 + timedelta(minutes=16) and meal["expires_at"] == T0 + timedelta(minutes=40)
    db.now = T0 + timedelta(minutes=10)
    assert _job_on(db, monkeypatch) == []   # held, its retry counted
    db.now = T0 + timedelta(minutes=19)
    run(reachy_tasks.maintenance(now=db.now))
    assert meal["status"] == "queued"       # its dose still startable
    db.now = T0 + timedelta(minutes=20)
    assert run(reachy_tasks.lease_next(7, DEVICE, 0)) is None
    run(reachy_tasks.maintenance(now=db.now))
    assert meal["status"] == "aborted" and json.loads(meal["detail"]) == {"reason": "dose_expired"}
    (text,) = _job_on(db, monkeypatch)
    assert "⚠️ Metformin：" in text and "已經錯過了" in text and "請盡快服用" not in text
    assert [t["task_id"] for t in db.tasks] == [task_id, meal["task_id"]]   # no robot for an expired dose


def _waiting_rows(slot, retries_sent, waiting=(101,)):
    rows = _reminder_rows(slot, reminder_sent=True, retries_sent=retries_sent)
    for row in rows:
        row["meal_retry_waiting"] = row["id"] in waiting
    return rows


def _updated(conn, statement):
    return [args for query, args in conn.updated if query.startswith(statement)]


def test_the_missed_dose_job_leaves_a_dose_a_meal_retry_waits_for_alone(monkeypatch):
    """Med 101 waits for its meal retry, Med 102 of the same slot does not. At +10 min the retry, its LINE text and
    the robot are for Med 102 alone; at +30 min only Med 102 is marked missed and reported. Med 101's retries are
    still counted (the slot's timeline goes on), but it gets no reminder, robot retry or missed mark."""
    calls, sent, family, conn = _run_at(monkeypatch, SLOT_20 + timedelta(minutes=10, seconds=5),
                                        _waiting_rows(SLOT_20, 0))
    assert reachy_tasks.meal_retry_waiting_sql("i") + " AS meal_retry_waiting" in conn.queries[0]
    assert calls == [([102], "missed_retry")]
    (text,) = sent
    assert "Med 102" in text and "Med 101" not in text
    assert _updated(conn, "UPDATE intake SET missed_reminders_sent") == [([101, 102],)]
    calls, sent, family, conn = _run_at(monkeypatch, SLOT_20 + timedelta(minutes=30, seconds=20),
                                        _waiting_rows(SLOT_20, 2))
    assert calls == [([102], "missed_retry")]
    retry, missed = sent
    assert "Med 101" not in retry and "Med 101" not in missed and "Med 102" in missed
    assert _updated(conn, "UPDATE intake SET intake_stats = 'missed'") == [([102],)]
    ((args, kwargs),) = family
    assert args[2] == "Med 102"


def test_the_missed_dose_job_is_silent_while_every_dose_of_the_slot_waits(monkeypatch):
    for late, retries_sent in ((timedelta(minutes=10, seconds=5), 0), (timedelta(minutes=31), 3)):
        rows = [row for row in _waiting_rows(SLOT_20, retries_sent) if row["id"] == 101]
        calls, sent, family, conn = _run_at(monkeypatch, SLOT_20 + late, rows)
        assert calls == [] and sent == [] and family == []
        assert _updated(conn, "UPDATE intake SET intake_stats = 'missed'") == []
        assert not any(query.startswith("INSERT INTO notification") for query, _ in conn.updated)


def test_the_missed_dose_job_handles_the_dose_as_before_once_the_meal_retry_has_run(monkeypatch):
    """The third meal retry ended (or expired, or found nobody) at +65 min: no meal retry waits any more. The job goes
    on where the slot's timeline is: its retries were counted, so no late 「請盡快服用」 and no robot, only the missed
    alerts."""
    rows = [row for row in _waiting_rows(SLOT_20, 3, waiting=()) if row["id"] == 101]
    calls, sent, family, conn = _run_at(monkeypatch, SLOT_20 + timedelta(minutes=65), rows)
    assert calls == []
    (missed,) = sent
    assert missed.startswith("❌ 用藥未完成") and "Med 101" in missed and "系統離線" not in missed
    assert _updated(conn, "UPDATE intake SET intake_stats = 'missed'") == [([101],)]
    ((args, kwargs),) = family
    assert args[2] == "Med 101" and kwargs == {"offline": False}
    rows = [row for row in _waiting_rows(SLOT_20, 1, waiting=()) if row["id"] == 101]   # it ended at +25 min
    calls, sent, family, conn = _run_at(monkeypatch, SLOT_20 + timedelta(minutes=25), rows)
    assert calls == [([101], "missed_retry")] and len(sent) == 1 and "請盡快服用" in sent[0] and family == []


# ── Live voice flag (spec 2026-10-12 E1) ─────────────────────────────────────

CHECKIN_CONSENT = {**ROBOT_CONSENT, **{scope: {"granted": True, "terms_version": config.TERMS_VERSION}
                                       for scope in reachy_tasks.CHECKIN_SCOPES}}


@pytest.fixture
def live_ready(db, monkeypatch):
    """Everything a live check-in needs: the .env test switch, a paired robot, check-in consent and a checked live
    model on an OpenAI connection. The robot's own live switch stays off: it no longer counts."""
    openai = ai_settings.Connection(2, "OpenAI", "openai", "https://api.openai.com/v1", "sk-proj-live-4444", "4444",
                                    True, "OpenAI")
    live = ai_settings.LiveSetting(2, "gpt-live-1", "marin", T0, 1180, None)
    monkeypatch.setattr(ai_settings, "_snapshot", ai_settings.Snapshot({2: openai}, {}, {}, live=live))
    monkeypatch.setattr(config, "LIVE_VOICE_TEST", True)
    from app.services import voice_lines
    # the live flag's tests are not about the render-ahead: nothing is rendered here
    monkeypatch.setattr(voice_lines, "render_ahead_for_patient", lambda u_id: None)
    db.consent[7] = CHECKIN_CONSENT
    return db


@pytest.mark.parametrize("reason, intk_ids", [("checkin", ()), ("upcoming", (101, 102, 103))])
def test_a_task_says_live_when_every_condition_holds(live_ready, reason, intk_ids):
    enqueue(live_ready, reason=reason, intk_ids=intk_ids)   # a dose task's check-in follows the same rule
    task = run(reachy_tasks.lease_next(7, DEVICE, 0))
    assert task["checkin"] is True and task["live"] is True
    assert "sk-proj-live-4444" not in str(task)   # the robot never sees the OpenAI key


def test_live_is_the_default_whatever_the_robots_old_live_switch_says(live_ready):
    """13 Oct 2026: a robot paired to another patient had the Reachy card's "Live voice (test)" switch off, and its
    check-ins silently ran the text chat. Live voice is now the default for every paired robot."""
    for switch in (False, True):
        live_ready.devices[7]["live_voice"] = switch
        enqueue(live_ready, reason="checkin", intk_ids=())
        task = run(reachy_tasks.lease_next(7, DEVICE, 0))
        assert task["live"] is True
        live_ready.tasks.clear()


@pytest.mark.parametrize("missing", ["test_switch", "checkin_consent", "passed_check", "device_revoked"])
def test_a_task_says_no_live_when_any_condition_is_missing(live_ready, monkeypatch, missing):
    enqueue(live_ready, reason="checkin", intk_ids=())
    if missing == "test_switch":
        monkeypatch.setattr(config, "LIVE_VOICE_TEST", False)
    elif missing == "checkin_consent":   # safety alerts withdrawn: no check-in at all, so no live one either
        live_ready.consent[7] = {**CHECKIN_CONSENT,
                                 "safety_alerts": {"granted": False, "terms_version": config.TERMS_VERSION}}
    elif missing == "passed_check":
        snapshot = ai_settings.current()
        monkeypatch.setattr(ai_settings, "_snapshot", dataclasses.replace(
            snapshot, live=dataclasses.replace(snapshot.live, last_error="live_auth")))
    else:
        live_ready.devices[7]["revoked"] = True
    task = run(reachy_tasks.lease_next(7, DEVICE, 0))
    assert task["live"] is False
