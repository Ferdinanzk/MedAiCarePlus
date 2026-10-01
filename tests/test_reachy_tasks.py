"""Leased robot tasks: enqueue rules, leasing, transitions, lease expiry and the reminder hook."""

import asyncio
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.jobs import missed_dose_job, reachy_task_job
from app.services import consent_service, outbox, reachy_tasks

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
                  "dose_form": "solid_oral", "units_per_dose": Decimal("1.00"), "intake_stats": "pending"},
            102: {"intk_id": 102, "med_id": 1, "med_name": "Aspirin", "pill_description": None,
                  "dose_form": "liquid", "units_per_dose": Decimal("1.00"), "intake_stats": "pending"},
            103: {"intk_id": 103, "med_id": 3, "med_name": "Zinc", "pill_description": None,
                  "dose_form": "solid_oral", "units_per_dose": Decimal("2.00"), "intake_stats": "taken"},
        }
        self.executed = []
        self.outbox_now = None

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
            queued = sorted((t for t in self.tasks if t["u_id"] == args[0] and t["status"] == "queued"
                             and t["expires_at"] > self.now), key=lambda t: (t["slot_time"], t["created_at"]))
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


# ── Lease, payload, restart recovery ─────────────────────────────────────────

def test_lease_next_leases_oldest_queued_and_returns_payload(db):
    later = enqueue(db, slot=T0 + timedelta(hours=4), expires=T0 + timedelta(hours=5))
    first = enqueue(db)
    task = run(reachy_tasks.lease_next(7, DEVICE, 0))
    assert task["task_id"] == first and task["status"] == "leased"
    assert set(task) == {"task_id", "slot_time", "reason", "attempt", "status", "expires_at",
                         "patient_name", "auto_record", "doses"}
    assert task["patient_name"] == "Pearl" and task["auto_record"] is False
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

    def transaction(self):
        return _Transaction(self)

    async def fetch(self, query, *args):
        if "FROM intake i" in query:
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
    return [{"id": intk_id, "u_id": 7, "intake_time_stamp": slot, "reminder_sent": reminder_sent,
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

    async def fake_enqueue(c, u_id, slot_time, intk_ids, task_reason, expires_at):
        calls.append((u_id, slot_time, intk_ids, task_reason, expires_at, c.depth))
        c.log.append(("enqueue", task_reason, c.depth))
        return "task"

    monkeypatch.setattr(missed_dose_job, "get_pool", lambda: _ReminderPool(conn))
    monkeypatch.setattr(missed_dose_job, "enqueue_reachy_task", fake_enqueue)
    monkeypatch.setattr(missed_dose_job.LineService, "get_instance", classmethod(lambda cls: _Line()))
    run(missed_dose_job.check_missed_doses())
    assert len(calls) == 1
    u_id, slot_time, intk_ids, task_reason, expires_at, depth = calls[0]
    assert (u_id, slot_time, intk_ids, task_reason) == (7, slot, [101, 102], reason)
    assert expires_at == slot + timedelta(minutes=10 * (3 + 1))
    assert depth == 1
    flag = [entry for entry in conn.log if entry[0] == "execute" and entry[1].startswith(flag_sql)]
    assert flag and flag[0][2] == 1
    # the flag update and the enqueue are adjacent inside the same transaction block
    index = conn.log.index(flag[0])
    assert conn.log[index + 1] == ("enqueue", reason, 1)
