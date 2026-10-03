"""Leased robot tasks: enqueue rules, leasing, transitions, lease expiry and the reminder hook."""

import asyncio
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
from app.services import consent_service, dose_safety, outbox, reachy_tasks, schedule  # noqa: E402

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
        self.devices = {7: {"device_id": DEVICE, "auto_record": False, "last_seen_at": T0, "revoked": False}}
        self.users = {7: "Pearl"}
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

    # asyncpg surface
    async def fetchval(self, query, *args):
        if "SELECT device_id FROM reachy_device" in query:
            device = self.devices.get(args[0])
            return device["device_id"] if device and not device["revoked"] else None
        if "INSERT INTO reachy_task" in query:
            task_id, u_id, slot, intk_ids, reason, expires = args
            if self.open_task(u_id, slot):
                return None
            self.tasks.append({"task_id": task_id, "u_id": u_id, "slot_time": slot, "intk_ids": intk_ids,
                               "reason": reason, "attempt": 1, "status": "queued", "lease_owner": None,
                               "lease_until": None, "created_at": self.now, "finished_at": None,
                               "expires_at": expires, "detail": None})
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
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        if "SET status = 'leased'" in query:
            assert "FOR UPDATE SKIP LOCKED" in query
            assert "AND NOT EXISTS (SELECT 1 FROM intake i" in query
            assert (f"AND i.intake_stats IN ('pending','missed') "
                    f"AND NOT ({dose_safety.startable_sql('i', 'NOW()')})") in query and len(args) == 2

            def due(task):
                # Only open doses hold a task back: a taken one past its halfway point does not.
                return all(self.startable(i) for i in task["intk_ids"]
                           if i in self.doses and self.doses[i]["intake_stats"] in ("pending", "missed"))

            queued = sorted((t for t in self.tasks if t["u_id"] == args[0] and t["status"] == "queued"
                             and t["expires_at"] > self.now and due(t)),
                            key=lambda t: (t["slot_time"], t["created_at"]))
            if not queued:
                return None
            task = queued[0]
            task.update(status="leased", lease_owner=args[1], lease_until=self.now + timedelta(seconds=60))
            return dict(task)
        if 'SELECT u.name, d.auto_record' in query:
            device = self.devices.get(args[0])
            return {"name": self.users[args[0]],
                    "auto_record": device["auto_record"] if device and not device["revoked"] else None}
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
        raise AssertionError(query)

    async def fetch(self, query, *args):
        if dose_facts.is_facts(query):
            assert args[0] == 7
            self.due_checks.append(list(args[1]))
            return self.facts(args)
        if "FROM intake i JOIN medication m" in query:
            assert "ORDER BY m.med_name, m.med_id" in query
            rows = [self.doses[i] for i in args[1] if i in self.doses]
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
        elif "SET lease_until = NOW()" in query:
            for t in self.tasks:
                if t["lease_owner"] == args[0] and t["status"] in ("leased", "searching", "in_progress"):
                    t["lease_until"] = self.now + timedelta(seconds=60)
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
    assert db.tasks[0] == before and len(db.tasks) == 1


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


# ── Lease, payload, restart recovery ─────────────────────────────────────────

def test_lease_next_leases_oldest_queued_and_returns_payload(db):
    later = enqueue(db, slot=T0 + timedelta(hours=4), expires=T0 + timedelta(hours=5))
    first = enqueue(db)
    task = run(reachy_tasks.lease_next(7, DEVICE, 0))
    assert task["task_id"] == first and task["status"] == "leased"
    assert set(task) == {"task_id", "slot_time", "reason", "attempt", "status", "expires_at",
                         "patient_name", "auto_record", "microphone", "checkin", "doses"}
    assert task["patient_name"] == "Pearl" and task["auto_record"] is False
    assert task["microphone"] is False   # no robot_microphone consent in the fixture
    assert task["checkin"] is False      # nor the check-in scopes
    assert [d["med_name"] for d in task["doses"]] == ["Aspirin", "Metformin", "Zinc"]
    assert set(task["doses"][0]) == {"intk_id", "med_id", "med_name", "pill_description", "dose_form",
                                     "units_per_dose", "intake_stats", "supported"}
    assert [d["supported"] for d in task["doses"]] == [False, True, False]
    assert db.by_id(first)["lease_until"] == T0 + timedelta(seconds=60)
    assert run(reachy_tasks.lease_next(7, DEVICE, 0))["task_id"] == later
    assert run(reachy_tasks.lease_next(7, DEVICE, 0)) is None


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

    def send_missed_dose_alert(self, *args):
        pass


def _reminder_rows(slot, reminder_sent=False, retries_sent=0):
    return [{"id": intk_id, "u_id": 7, "intake_time_stamp": slot, "previous_time": None, "reminder_sent": reminder_sent,
             "missed_reminders_sent": retries_sent, "med_id": intk_id, "med_name": f"Med {intk_id}",
             "patient_name": "Pearl", "patient_line_id": "U1", "remind_before_minutes": 5,
             "remind_after_minutes": 10, "remind_after_retries": 3, "notify_family_on_missed": False}
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
