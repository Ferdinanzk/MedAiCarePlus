"""Quick fix 6 (20 Oct 2026, .superpowers/quickfix/CONTRACT6.md): the Reachy card's "Demo dose: Acetal 500mg" button.

POST /api/reachy/demo-dose makes (or reuses) the patient's demo medicine, clears its earlier doses and sends Reachy at
once for a fresh one, all in one transaction. The fake database keeps rows and, like PostgreSQL, puts every table back
when a transaction or savepoint fails, so the rollback cases show what a refused press leaves behind. What the fake
cannot show (the SQL itself, the cascade of monitor_event and dose_emotion rows) was run on PostgreSQL 16.
"""

import asyncio
import copy
import json
import os
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

sys.path.insert(0, os.path.dirname(__file__))
import dose_facts  # noqa: E402

from app import config  # noqa: E402
from app.config import PUBLIC_PORT  # noqa: E402
from app.dependencies import get_current_user  # noqa: E402
from app.jobs.missed_dose_job import _slot_key  # noqa: E402
from app.routers import api_reachy  # noqa: E402
from app.services import consent_service, dose_confirmation, dose_safety, outbox, reachy_tasks, schedule  # noqa: E402
from app.services import voice_lines  # noqa: E402

NOW = datetime(2026, 10, 20, 4, 2, 13, 500000, tzinfo=timezone.utc)   # 12:02:13.5 in Taipei: lunch
CURRENT = {"granted": True, "terms_version": config.TERMS_VERSION}
ROBOT_CONSENT = {"core": CURRENT, "robot_camera": CURRENT}
OPEN = ("queued", "leased", "searching", "in_progress")
DEVICE_ID = uuid.uuid4()
CONTACT = {"id": 31, "u_id": 7, "name": "Daughter", "line_id": "U-family", "verified": True,
           "relationship": "daughter"}
NOON = json.dumps({"morning": False, "noon": True, "night": False, "bedtime": False})   # a real medicine at 12:00


class FakeDeadlock(Exception):
    """What asyncpg raises when PostgreSQL ends a transaction to break a deadlock (DeadlockDetectedError)."""
    sqlstate = "40P01"


class _Transaction:
    """A transaction or a savepoint: when the block raises, every table is put back as it was at its start."""

    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        self.saved = copy.deepcopy(self.db.tables())
        self.db.depth += 1
        return self

    async def __aexit__(self, exc_type, *args):
        self.db.depth -= 1
        if exc_type is not None:
            self.db.restore(self.saved)
        return False


class FakeDB:
    """Patient 7's medication, intake, reachy_task and dose_confirmation rows, and their paired robot."""

    def __init__(self):
        self.depth = 0
        self.device = DEVICE_ID          # None: not paired
        self.consent = dict(ROBOT_CONSENT)
        self.protection = True
        self.meds = {}                   # med_id -> row
        self.intakes = {}                # intk_id -> row
        self.tasks = []
        self.confirmations = {}          # confirmation_id (text) -> row
        self.events = []                 # monitor_event and dose_emotion rows: {"table", "intk_id"}
        self.statements = []             # (first words of each statement, transaction depth)
        self.abort_errors = []           # raised, one per statement, by the press's abort of earlier demo tasks
        self._ids = {"med": 50, "intake": 900}   # sequences: never put back by a rollback, as in PostgreSQL

    def tables(self):
        return {"meds": self.meds, "intakes": self.intakes, "tasks": self.tasks,
                "confirmations": self.confirmations, "events": self.events}

    def restore(self, saved):
        for name, value in saved.items():
            setattr(self, name, value)

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

    def next_id(self, kind):
        self._ids[kind] += 1
        return self._ids[kind]

    # rows the tests start from
    def add_med(self, name="Acetal 500mg", demo=True, **fields):
        med_id = self.next_id("med")
        self.meds[med_id] = {"med_id": med_id, "u_id": 7, "med_name": name, "schedule_time": None,
                             "instructions": None, "pills_remaining": Decimal("20.00"), "is_active": True,
                             "archived_at": None, "prescription_meta": json.dumps({"demo": "acetal"}) if demo else None,
                             "dose_form": "solid_oral", "units_per_dose": Decimal("1.00"),
                             "min_interval_minutes": None, "max_daily_doses": None, **fields}
        return med_id

    def add_dose(self, med_id, stamp, status="pending", **fields):
        intk_id = self.next_id("intake")
        self.intakes[intk_id] = {"intk_id": intk_id, "u_id": 7, "med_id": med_id, "intake_stats": status,
                                 "intake_time_stamp": stamp, "notify_stats": "pending", "reminder_sent": False,
                                 "actual_intake_time": None, **fields}
        return intk_id

    def add_task(self, intk_ids, status="queued", slot=None, reason="manual"):
        task = {"task_id": uuid.uuid4(), "u_id": 7, "slot_time": slot or NOW - timedelta(hours=3),
                "intk_ids": list(intk_ids), "reason": reason, "status": status, "attempt": 1,
                "expires_at": NOW + timedelta(minutes=30), "finished_at": None, "lease_until": None, "detail": None}
        self.tasks.append(task)
        return task

    def add_request(self, intk_ids, previous):
        confirmation_id = str(uuid.uuid4())
        self.confirmations[confirmation_id] = {
            "confirmation_id": uuid.UUID(confirmation_id), "u_id": 7, "intk_ids": list(intk_ids),
            "previous_status": json.dumps(previous), "source": "degraded", "evidence": None,
            "created_at": NOW - timedelta(minutes=30), "reminded_at": None, "resolution": None,
            "resolved_by": None, "resolved_at": None}
        for intk_id in intk_ids:
            self.intakes[intk_id]["intake_stats"] = "pending_confirmation"
        return confirmation_id

    def demo_meds(self):
        return [m for m in self.meds.values() if json.loads(m["prescription_meta"] or "{}").get("demo") == "acetal"]

    def facts(self, args):
        meds = {med_id: {"med_name": m["med_name"], "schedule_time": m["schedule_time"],
                         "min_interval_minutes": m["min_interval_minutes"], "max_daily_doses": m["max_daily_doses"]}
                for med_id, m in self.meds.items()}
        return dose_facts.rows(args, self.intakes, meds, protection=self.protection,
                               confirmations=list(self.confirmations.values()))

    def _note(self, query):
        self.statements.append((" ".join(query.split()[:3]), self.depth))

    # asyncpg surface
    async def fetchval(self, query, *args):
        self._note(query)
        if query == "SELECT device_id FROM reachy_device WHERE u_id = $1 AND revoked_at IS NULL":
            return self.device if args[0] == 7 else None
        if query.startswith("SELECT med_id FROM medication WHERE u_id = $1 AND prescription_meta->>'demo' = $2"):
            assert "ORDER BY med_id LIMIT 1" in query
            found = sorted(m["med_id"] for m in self.meds.values() if m["u_id"] == args[0]
                           and json.loads(m["prescription_meta"] or "{}").get("demo") == args[1])
            return found[0] if found else None
        if query.startswith("INSERT INTO medication"):
            assert self.depth > 0
            u_id, name, schedule_time, instructions, stock, meta = args
            assert "'solid_oral', 1) RETURNING med_id" in query and "TRUE, $6::jsonb" in query
            return self.add_med(name, demo=False, u_id=u_id, schedule_time=schedule_time, instructions=instructions,
                                pills_remaining=stock, prescription_meta=meta)
        if query.startswith("INSERT INTO intake"):
            assert "VALUES ($1, $2, $3, 'pending', 'pending', TRUE) RETURNING intk_id" in query and self.depth > 0
            intk_id = self.add_dose(args[1], args[2], reminder_sent=True)
            return intk_id
        if "INSERT INTO reachy_task" in query:   # reachy_tasks._enqueue
            task_id, u_id, slot, intk_ids, reason, expires_at, not_before = args
            if any(t["u_id"] == u_id and t["slot_time"] == slot and t["status"] in OPEN for t in self.tasks):
                return None   # one open task per patient and slot (one_open_task_per_slot)
            self.tasks.append({"task_id": uuid.UUID(task_id), "u_id": u_id, "slot_time": slot,
                               "intk_ids": list(intk_ids), "reason": reason, "status": "queued", "attempt": 1,
                               "expires_at": expires_at, "finished_at": None, "lease_until": None, "detail": None,
                               "not_before": not_before})
            return uuid.UUID(task_id)
        if "SET attempt = attempt + 1" in query:
            task = next((t for t in self.tasks if t["u_id"] == args[0] and t["slot_time"] == args[1]
                         and t["status"] == "queued"), None)
            if task is None:
                return None
            task.update(attempt=task["attempt"] + 1, expires_at=max(task["expires_at"], args[2]))
            return task["task_id"]
        if query.startswith("SELECT task_id FROM reachy_task WHERE u_id = $1 AND slot_time = $2"):
            task = next((t for t in self.tasks if t["u_id"] == args[0] and t["slot_time"] == args[1]
                         and t["status"] in ("leased", "searching", "in_progress")), None)
            return task["task_id"] if task else None
        if query == "SELECT u_id FROM dose_confirmation WHERE confirmation_id = $1::uuid":
            row = self.confirmations.get(args[0])
            return row["u_id"] if row else None
        if query == 'SELECT name FROM "user" WHERE u_id = $1':
            return "Pearl"
        if query == "SELECT name FROM family_contacts WHERE id = $1":
            return CONTACT["name"] if args[0] == CONTACT["id"] else None
        if query.startswith("SELECT intk_id FROM intake WHERE u_id = $1 AND intake_stats IN "):   # the press's slot
            assert ("intake_stats IN ('pending','missed','pending_confirmation') "
                    "AND intake_time_stamp >= $2 AND intake_time_stamp < $3 ORDER BY intk_id LIMIT 1") in query
            found = sorted(i for i, row in self.intakes.items() if row["u_id"] == args[0]
                           and row["intake_stats"] in ("pending", "missed", "pending_confirmation")
                           and args[1] <= row["intake_time_stamp"] < args[2])
            return found[0] if found else None
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        self._note(query)
        if "FROM notification_settings WHERE u_id = $1" in query:   # manual_expires_at: the defaults
            return None
        if query == "SELECT task_id, status, slot_time, intk_ids FROM reachy_task WHERE task_id = $1::uuid":
            task = next(t for t in self.tasks if str(t["task_id"]) == str(args[0]))
            return {key: task[key] for key in ("task_id", "status", "slot_time", "intk_ids")}
        if "FROM dose_confirmation WHERE confirmation_id = $1::uuid FOR UPDATE" in query:   # dose_confirmation._lock
            row = self.confirmations.get(args[0])
            return dict(row) if row else None
        if query.startswith("SELECT id, u_id, name, line_id, verified, relationship FROM family_contacts"):
            return dict(CONTACT) if args[0] == CONTACT["id"] else None
        if query.startswith("SELECT intake_time_stamp FROM intake WHERE intk_id = $1 AND u_id = $2"):   # Use Reachy
            row = self.intakes.get(args[0])
            ok = row is not None and row["u_id"] == args[1] and row["intake_stats"] in ("pending", "missed")
            return {"intake_time_stamp": row["intake_time_stamp"]} if ok else None
        raise AssertionError(query)

    async def fetch(self, query, *args):
        self._note(query)
        if dose_facts.is_facts(query):
            assert args[0] == 7
            return self.facts(args)
        if query == "SELECT intk_id FROM intake WHERE u_id = $1 AND med_id = $2 ORDER BY intk_id":
            return [{"intk_id": i} for i, row in sorted(self.intakes.items())
                    if row["u_id"] == args[0] and row["med_id"] == args[1]]
        if query.startswith("SELECT confirmation_id FROM dose_confirmation WHERE u_id = $1 AND resolution IS NULL"):
            assert "AND intk_ids && $2::int[] ORDER BY created_at" in query
            rows = sorted((c for c in self.confirmations.values() if c["u_id"] == args[0]
                           and c["resolution"] is None and set(c["intk_ids"]) & set(args[1])),
                          key=lambda c: c["created_at"])
            return [{"confirmation_id": c["confirmation_id"]} for c in rows]
        if query.startswith("SELECT intk_id, med_id, intake_stats FROM intake WHERE intk_id = ANY($1::int[])"):
            return [{key: row[key] for key in ("intk_id", "med_id", "intake_stats")}
                    for i, row in sorted(self.intakes.items()) if i in args[0] and row["u_id"] == args[1]]
        if query.startswith("SELECT i.intk_id, i.intake_time_stamp, m.med_name FROM intake i JOIN medication m"):
            rows = [{"intk_id": i, "intake_time_stamp": row["intake_time_stamp"],
                     "med_name": self.meds[row["med_id"]]["med_name"]}
                    for i, row in self.intakes.items() if i in args[0]]
            return sorted(rows, key=lambda r: (r["intake_time_stamp"], r["intk_id"]))
        if query.startswith("SELECT i.intk_id FROM intake i JOIN medication m ON m.med_id = i.med_id"):
            # Use Reachy's doses of the slot (dose_safety.allowed then judges them; startable_sql is not modelled).
            rows = [(self.meds[row["med_id"]]["med_name"], row["med_id"], i) for i, row in self.intakes.items()
                    if row["u_id"] == args[0] and row["intake_stats"] in ("pending", "missed")
                    and args[1] <= row["intake_time_stamp"] < args[2]]
            return [{"intk_id": i} for _, _, i in sorted(rows)]
        raise AssertionError(query)

    async def execute(self, query, *args):
        self._note(query)
        if query == "SELECT pg_advisory_xact_lock(hashtext('reachy_demo_dose'), $1)":
            assert self.depth > 0   # held until the press commits or rolls back
            return "SELECT 1"
        if query.startswith("UPDATE reachy_task SET status = 'aborted'"):
            # Only tasks whose doses are all demo doses (<@), and that hold one (&&: an empty array is inside any).
            assert ("WHERE u_id = $1 AND status IN ('queued','leased','searching','in_progress') "
                    "AND intk_ids && $2::int[] AND intk_ids <@ $2::int[]") in query
            assert "jsonb_build_object('reason', 'demo_reset')" in query
            if self.abort_errors:
                raise self.abort_errors.pop(0)
            for task in self.tasks:
                ids, demo = set(task["intk_ids"]), set(args[1])
                if task["u_id"] == args[0] and task["status"] in OPEN and ids & demo and ids <= demo:
                    detail = json.loads(task["detail"] or "{}")
                    task.update(status="aborted", finished_at=NOW, lease_until=None,
                                detail=json.dumps({**detail, "reason": "demo_reset"}))
            return "UPDATE"
        if query.startswith("UPDATE reachy_task SET intk_ids = ARRAY("):   # a task that also holds a real dose
            assert ("FROM unnest(intk_ids) WITH ORDINALITY AS kept(intk_id, n) WHERE intk_id <> ALL($2::int[]) "
                    "ORDER BY n) WHERE u_id = $1 AND status IN ('queued','leased','searching','in_progress') "
                    "AND intk_ids && $2::int[] AND NOT intk_ids <@ $2::int[]") in query
            for task in self.tasks:
                ids, demo = set(task["intk_ids"]), set(args[1])
                if task["u_id"] == args[0] and task["status"] in OPEN and ids & demo and not ids <= demo:
                    task["intk_ids"] = [i for i in task["intk_ids"] if i not in demo]
            return "UPDATE"
        if query == "UPDATE intake SET intake_stats=$1 WHERE intk_id=$2":   # dose_confirmation._restore
            self.intakes[args[1]]["intake_stats"] = args[0]
            return "UPDATE 1"
        if query.startswith("UPDATE dose_confirmation SET resolution=$2, resolved_by=$3, resolved_at=$4"):
            self.confirmations[args[0]].update(resolution=args[1], resolved_by=args[2], resolved_at=args[3])
            return "UPDATE 1"
        if query == "DELETE FROM intake WHERE u_id = $1 AND intk_id = ANY($2::int[])":
            assert self.depth > 0
            gone = {i for i, row in self.intakes.items() if row["u_id"] == args[0] and i in args[1]}
            self.intakes = {i: row for i, row in self.intakes.items() if i not in gone}
            self.events = [e for e in self.events if e["intk_id"] not in gone]   # ON DELETE CASCADE
            return f"DELETE {len(gone)}"
        if query.startswith("UPDATE medication SET med_name = $2, schedule_time = $3::jsonb, instructions = $4, "):
            assert ("dose_form = 'solid_oral', units_per_dose = 1, min_interval_minutes = NULL, "
                    "max_daily_doses = NULL, is_active = TRUE, archived_at = NULL, "
                    "pills_remaining = CASE WHEN pills_remaining < $5 THEN $6 ELSE pills_remaining END "
                    "WHERE med_id = $1") in query
            med_id, name, schedule_time, instructions, low, stock = args
            med = self.meds[med_id]
            med.update(med_name=name, schedule_time=schedule_time, instructions=instructions, dose_form="solid_oral",
                       units_per_dose=Decimal("1.00"), min_interval_minutes=None, max_daily_doses=None,
                       is_active=True, archived_at=None)
            if med["pills_remaining"] < low:
                med["pills_remaining"] = stock
            return "UPDATE 1"
        raise AssertionError(query)


class FakePool:
    def __init__(self, db):
        self.db = db

    def acquire(self):
        return self.db.acquire()


@pytest.fixture
def env(monkeypatch):
    from app.main import app

    db = FakeDB()
    renders, replies = [], []

    async def get_state(u_id):
        return db.consent

    async def fetch_state(conn, u_id):
        assert conn is db
        return db.consent

    async def enqueue(conn, **kwargs):
        replies.append(kwargs)

    monkeypatch.setattr(app, "dependency_overrides", {get_current_user: lambda: {"u_id": 7, "name": "Pearl"}})
    monkeypatch.setattr(api_reachy, "get_pool", lambda: FakePool(db))
    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(consent_service, "fetch_state", fetch_state)
    monkeypatch.setattr(schedule, "_now", lambda: NOW)
    monkeypatch.setattr(voice_lines, "render_ahead_for_patient", lambda u_id: renders.append(u_id))
    monkeypatch.setattr(outbox, "enqueue", enqueue)
    monkeypatch.setattr(outbox, "wake", lambda: None)
    return types.SimpleNamespace(db=db, renders=renders, replies=replies,
                                 browser=TestClient(app, base_url=f"http://testserver:{PUBLIC_PORT}"))


def _press(env):
    return env.browser.post("/api/reachy/demo-dose")


def _demo(env):
    (med,) = env.db.demo_meds()
    return med


def _doses_of(env, med_id):
    return {i: row for i, row in env.db.intakes.items() if row["med_id"] == med_id}


def _state(env):
    return copy.deepcopy(env.db.tables())


# ── 1. A paired robot ────────────────────────────────────────────────────────

def test_without_a_paired_robot_the_press_is_409_and_changes_nothing(env):
    med_id = env.db.add_med(archived_at=NOW - timedelta(days=1), is_active=False, pills_remaining=Decimal("2.00"))
    taken = env.db.add_dose(med_id, NOW - timedelta(hours=1), "taken")
    env.db.add_task([taken], status="in_progress")
    env.db.device = None
    before = _state(env)
    response = _press(env)
    assert (response.status_code, response.json()) == (409, {"detail": "robot_not_paired"})
    assert _state(env) == before and env.renders == []
    env.db.meds.clear()
    assert _press(env).status_code == 409 and env.db.meds == {}   # nor is the demo medicine made


# ── 2. The demo medicine ─────────────────────────────────────────────────────

def test_the_first_press_makes_the_demo_medicine_with_no_dose_times(env):
    response = _press(env)
    assert response.status_code == 200
    med = _demo(env)
    assert {key: med[key] for key in ("u_id", "med_name", "instructions", "pills_remaining", "is_active",
                                      "dose_form", "units_per_dose")} == {
        "u_id": 7, "med_name": "Acetal 500mg", "instructions": "示範藥物（飯後服用），用來測試服藥流程",
        "pills_remaining": Decimal("30"), "is_active": True, "dose_form": "solid_oral",
        "units_per_dose": Decimal("1.00")}
    assert json.loads(med["prescription_meta"]) == {"demo": "acetal"}
    assert json.loads(med["schedule_time"]) == {"morning": False, "noon": False, "night": False, "bedtime": False,
                                                "after_meals": True, "before_meals": False, "custom_times": []}
    assert schedule.dose_times(med["schedule_time"]) == []           # no doses are generated for it
    assert schedule.occurrences(med["schedule_time"], NOW) == []
    for word in ("空腹", "飯前", "餐前"):
        assert word not in med["instructions"]
    # Its stock never comes near the as-needed refill alert (7 doses or fewer).
    from app.jobs import refill_reminder_job
    assert refill_reminder_job.needs_refill({**med, "schedule_time": med["schedule_time"]}, NOW.date()) is None


def test_a_later_press_reuses_the_demo_medicine_makes_it_active_and_tops_it_up_below_10(env):
    med_id = env.db.add_med(is_active=False, archived_at=NOW - timedelta(days=2), pills_remaining=Decimal("9.50"))
    assert _press(env).status_code == 200
    assert [m["med_id"] for m in env.db.demo_meds()] == [med_id]     # one demo medicine, the same one
    med = env.db.meds[med_id]
    assert (med["is_active"], med["archived_at"], med["pills_remaining"]) == (True, None, Decimal("30"))
    med["pills_remaining"] = Decimal("10.00")
    assert _press(env).status_code == 200
    assert env.db.meds[med_id]["pills_remaining"] == Decimal("10.00")   # 10 or more is left as it is


def test_a_medicine_of_the_same_name_without_the_demo_flag_is_never_touched(env):
    own = env.db.add_med(demo=False, pills_remaining=Decimal("3.00"), is_active=False,
                         archived_at=NOW - timedelta(days=5), prescription_meta=json.dumps({"course_end": "2026-10-01"}))
    own_dose = env.db.add_dose(own, NOW - timedelta(minutes=20))
    own_task = env.db.add_task([own_dose])
    before = copy.deepcopy((env.db.meds[own], env.db.intakes[own_dose], own_task))
    body = _press(env).json()
    assert body["med_id"] != own and _demo(env)["med_id"] == body["med_id"]
    assert (env.db.meds[own], env.db.intakes[own_dose], own_task) == before


# ── 3. Earlier demo doses are cleared ────────────────────────────────────────

def test_earlier_demo_doses_their_open_tasks_and_requests_are_cleared_first(env):
    med_id = env.db.add_med()
    other_med = env.db.add_med("Metformin", demo=False)
    taken = env.db.add_dose(med_id, NOW - timedelta(minutes=40), "taken", actual_intake_time=NOW - timedelta(minutes=39))
    waiting = env.db.add_dose(med_id, NOW - timedelta(minutes=30, seconds=-7))
    pending = env.db.add_dose(med_id, NOW - timedelta(minutes=10, seconds=-3))
    real = env.db.add_dose(other_med, NOW - timedelta(minutes=30))
    request = env.db.add_request([waiting, real], {str(waiting): "pending", str(real): "missed"})
    held = env.db.add_task([waiting, real], status="in_progress")
    queued = env.db.add_task([pending], slot=NOW - timedelta(minutes=15))
    done = env.db.add_task([taken], status="completed")
    others = env.db.add_task([real], slot=NOW - timedelta(hours=1))
    env.db.events += [{"table": "monitor_event", "intk_id": taken}, {"table": "dose_emotion", "intk_id": waiting},
                      {"table": "dose_emotion", "intk_id": real}]

    body = _press(env).json()

    assert set(_doses_of(env, med_id)) == {body["intk_id"]}            # every earlier demo dose is gone
    assert env.db.events == [{"table": "dose_emotion", "intk_id": real}]
    assert queued["status"] == "aborted" and json.loads(queued["detail"])["reason"] == "demo_reset"
    # The task that also holds the real dose keeps it, and Reachy's visit for it (review of quick fix 6).
    assert (held["status"], held["intk_ids"], held["detail"]) == ("in_progress", [real], None)
    assert done["status"] == "completed" and others["status"] == "queued"
    assert env.db.confirmations[request]["resolution"] == "expired"
    # The real dose in that request goes back to what it was, as when a request expires on its own.
    assert env.db.intakes[real]["intake_stats"] == "missed"


def test_a_press_right_after_a_taken_demo_dose_works_with_overdose_protection_on(env):
    """Without the clearing, the taken dose 5 minutes ago would refuse the next one as too soon (4 h without dose
    times)."""
    med_id = env.db.add_med()
    env.db.add_dose(med_id, NOW - timedelta(minutes=6, seconds=1), "taken", actual_intake_time=NOW - timedelta(minutes=5))
    with pytest.raises(dose_safety.DoseTooSoon):
        asyncio.run(dose_safety.check(env.db, 7, [env.db.add_dose(med_id, NOW)], at=NOW))
    assert env.db.protection is True
    for _ in range(3):
        response = _press(env)
        assert response.status_code == 200, response.json()
        assert set(_doses_of(env, med_id)) == {response.json()["intk_id"]}


@pytest.mark.parametrize("answer", ["taken", "not_taken"])
def test_a_family_button_pressed_later_on_a_cleared_request_does_not_error(env, answer):
    med_id = env.db.add_med()
    waiting = env.db.add_dose(med_id, NOW - timedelta(minutes=30, seconds=-7))
    request = env.db.add_request([waiting], {str(waiting): "pending"})
    assert _press(env).status_code == 200
    data = dose_confirmation.sign_postback(dose_confirmation.ACTION, request, answer, CONTACT["id"])
    result = asyncio.run(dose_confirmation.handle_postback(env.db, data, CONTACT["line_id"]))
    assert result == {"status": "already_resolved", "resolution": "expired", "resolved_by": None}
    (reply,) = env.replies
    assert reply["recipient_line_id"] == CONTACT["line_id"] and reply["kind"] == "dose_confirm_reply"
    # The request's doses are gone, so it has no slot to name, and the reply leaves that part out (review of quick
    # fix 6: it read 「（Pearl ）」 and "Pearl's  dose").
    assert reply["messages"][0]["text"] == "此確認請求已逾時（Pearl）。\nThis request for Pearl's dose has expired."
    assert waiting not in env.db.intakes                               # nothing recorded or brought back
    (fresh,) = _doses_of(env, med_id).values()
    assert fresh["intake_stats"] == "pending"


# ── 4. to 6. A fresh dose, Reachy sent at once, the answer ───────────────────

def test_a_press_makes_one_ad_hoc_dose_and_a_manual_task_holding_only_it(env):
    response = _press(env)
    assert response.status_code == 200
    body = response.json()
    med = _demo(env)
    (dose,) = _doses_of(env, med["med_id"]).values()
    (task,) = env.db.tasks
    assert body == {"task_id": str(task["task_id"]), "status": "queued", "intk_id": dose["intk_id"],
                    "med_id": med["med_id"], "med_name": "Acetal 500mg"}
    assert (dose["intake_stats"], dose["notify_stats"], dose["reminder_sent"]) == ("pending", "pending", True)
    assert dose["intake_time_stamp"] == NOW and dose_safety.is_ad_hoc(dose["intake_time_stamp"])
    assert dose_safety.expires_at(dose["intake_time_stamp"], NOW + timedelta(hours=1)) is None   # it never expires
    assert task["reason"] == "manual" and task["intk_ids"] == [dose["intk_id"]]
    assert task["slot_time"] == _slot_key(NOW) == datetime(2026, 10, 20, 4, 0, tzinfo=timezone.utc)
    # As 'Use Reachy' sets it: the end of the slot's missed window (10 min x 4) or 15 minutes from now.
    assert task["expires_at"] == max(_slot_key(NOW) + timedelta(minutes=40), NOW + timedelta(minutes=15))
    assert env.renders == [7]                                          # through enqueue_reachy_task


def test_a_press_on_a_whole_minute_still_makes_an_ad_hoc_dose(env, monkeypatch):
    whole = NOW.replace(second=0, microsecond=0)
    monkeypatch.setattr(schedule, "_now", lambda: whole)
    body = _press(env).json()
    stamp = env.db.intakes[body["intk_id"]]["intake_time_stamp"]
    assert stamp == whole + timedelta(microseconds=1) and dose_safety.is_ad_hoc(stamp)


def test_overdose_protection_judges_the_dose_as_for_use_reachy(env, monkeypatch):
    asked = []
    real_check = dose_safety.check

    async def check(conn, u_id, intk_ids, **kwargs):
        asked.append((list(intk_ids), kwargs.get("at"), conn.depth > 0))
        return await real_check(conn, u_id, intk_ids, **kwargs)

    monkeypatch.setattr(dose_safety, "check", check)
    body = _press(env).json()
    # The route's own check, then enqueue_reachy_task's, both at the press's moment and inside its transaction.
    assert asked == [([body["intk_id"]], NOW, True), ([body["intk_id"]], NOW, True)]


def test_without_the_robots_consent_the_press_is_403_and_rolls_back(env):
    med_id = env.db.add_med(pills_remaining=Decimal("4.00"), is_active=False)
    earlier = env.db.add_dose(med_id, NOW - timedelta(minutes=12, seconds=-5))
    task = env.db.add_task([earlier], slot=NOW - timedelta(minutes=15))
    request = env.db.add_request([earlier], {str(earlier): "pending"})
    env.db.consent = {"core": CURRENT}
    before = _state(env)
    response = _press(env)
    assert (response.status_code, response.json()) == (403, {"detail": "robot_consent_required"})
    assert _state(env) == before                                       # earlier dose, task, request and stock kept
    env.db.meds.clear()
    env.db.intakes.clear()
    assert _press(env).status_code == 403 and env.db.meds == {} and env.db.intakes == {}
    # (the rollback put back copies: `task` itself saw the abort that was undone)
    assert [t["task_id"] for t in env.db.tasks] == [task["task_id"]] and env.db.tasks[0]["status"] == "queued"
    assert env.db.confirmations[request]["resolution"] is None


def test_a_slot_another_task_holds_is_409_slot_busy_and_rolls_back(env):
    """A real dose's task already open for the same 5-minute slot: enqueue_reachy_task answers that task, which does
    not hold the demo dose, so the press is refused rather than say Reachy comes for it."""
    real_med = env.db.add_med("Metformin", demo=False)
    real = env.db.add_dose(real_med, _slot_key(NOW))
    busy = env.db.add_task([real], status="in_progress", slot=_slot_key(NOW), reason="upcoming")
    med_id = env.db.add_med()
    earlier = env.db.add_dose(med_id, NOW - timedelta(minutes=50, seconds=-1))
    before = _state(env)
    response = _press(env)
    assert (response.status_code, response.json()) == (409, {"detail": "slot_busy"})
    assert _state(env) == before and busy["intk_ids"] == [real] and earlier in env.db.intakes


def test_the_whole_press_is_one_transaction(env):
    env.db.add_dose(env.db.add_med(), NOW - timedelta(minutes=30, seconds=-2))
    assert _press(env).status_code == 200
    assert env.db.statements and all(depth > 0 for _, depth in env.db.statements)


# ── 7. After that, the normal flow ───────────────────────────────────────────

@pytest.mark.parametrize("local, expected", [
    ("08:00", False), ("10:30", True), ("12:02", True), ("15:30", False), ("18:45", True), ("21:30", True),
    ("22:00", False)])
def test_the_demo_dose_asks_the_meal_question_at_lunch_and_dinner(env, local, expected):
    """reachy_tasks.task_payload's after_meal reads the medicine's schedule_time and instructions with the dose's
    time: the demo medicine is an after-meal one, so Reachy asks 「您有吃飯了嗎？」 at lunch and dinner only."""
    _press(env)
    med = _demo(env)
    hour, minute = map(int, local.split(":"))
    when = datetime(2026, 10, 20, hour, minute, 7, tzinfo=timezone(timedelta(hours=8)))
    assert reachy_tasks.after_meal(med["schedule_time"], med["instructions"], when) is expected


# ── Review of quick fix 6 ────────────────────────────────────────────────────

def _real_noon_dose(env, status="pending"):
    """A real 12:00 dose still open after its upcoming task ended (the patient had not eaten yet, or declined: the
    robot reported the task completed), with the press at 12:02, in the same 5-minute slot."""
    real = env.db.add_dose(env.db.add_med("Metformin", demo=False, schedule_time=NOON), _slot_key(NOW), status,
                           reminder_sent=True)
    env.db.add_task([real], status="completed", slot=_slot_key(NOW), reason="upcoming")
    return real


def _locks(env):
    """How many times the press began: each attempt takes the advisory lock first."""
    return sum(1 for words, _ in env.db.statements if words.startswith("SELECT pg_advisory_xact_lock"))


@pytest.mark.parametrize("status", ["pending", "missed", "pending_confirmation"])
def test_a_real_dose_still_open_in_the_slot_is_409_slot_busy_and_rolls_back(env, status):
    """The other way round from a task already holding the slot: no task holds it, but a real dose of the slot is still
    open. A demo task there would hold the slot (one open task per slot), so the real dose's next visit, 'Use Reachy'
    or the missed-dose job's retry, would get the demo task instead. A dose waiting for family counts too: family's
    'not taken' or the request's expiry makes it pending again."""
    real = _real_noon_dose(env)
    if status == "pending_confirmation":
        env.db.add_request([real], {str(real): "pending"})
    else:
        env.db.intakes[real]["intake_stats"] = status
    med_id = env.db.add_med()
    earlier = env.db.add_dose(med_id, NOW - timedelta(minutes=50, seconds=-1))
    env.db.add_task([earlier], slot=_slot_key(NOW - timedelta(minutes=50)))
    before = _state(env)
    response = _press(env)
    assert (response.status_code, response.json()) == (409, {"detail": "slot_busy"})
    assert _state(env) == before and env.renders == []                 # earlier dose, its task, the stock: all kept


@pytest.mark.parametrize("offset, status", [
    (timedelta(0), "taken"), (timedelta(0), "skipped"),
    (timedelta(minutes=-5), "pending"), (timedelta(minutes=5), "pending")])
def test_a_real_dose_done_or_in_another_slot_leaves_the_press_alone(env, offset, status):
    real = env.db.add_dose(env.db.add_med("Metformin", demo=False, schedule_time=NOON), _slot_key(NOW) + offset, status)
    response = _press(env)
    assert response.status_code == 200, response.json()
    assert env.db.intakes[real]["intake_stats"] == status


def test_use_reachy_on_the_real_dose_of_the_slot_brings_reachy_for_that_dose(env):
    """Path (a) of the review: with the demo task open for the slot, 'Use Reachy' on the real 12:00 dose answered 200
    with the demo task (enqueue_reachy_task re-armed it), and that task never held the real dose."""
    real = _real_noon_dose(env)
    _press(env)
    response = env.browser.post("/api/reachy/tasks", json={"intk_id": real})
    assert response.status_code == 200, response.json()
    assert response.json()["intk_ids"] == [real]
    (task,) = [t for t in env.db.tasks if t["status"] in OPEN]
    assert task["intk_ids"] == [real] and task["reason"] == "manual"


def test_the_missed_dose_jobs_retry_for_the_real_dose_of_the_slot_holds_that_dose(env):
    """Path (b): the job's 12:10 retry (its send_robot) only re-armed a demo task still queued for the slot (the robot
    busy or offline), so Reachy came and prompted Acetal alone."""
    real = _real_noon_dose(env)
    _press(env)
    task_id = asyncio.run(reachy_tasks.enqueue_reachy_task(
        env.db, 7, _slot_key(NOW), [real], "missed_retry", _slot_key(NOW) + timedelta(minutes=40),
        at=_slot_key(NOW) + timedelta(minutes=10)))
    task = next(t for t in env.db.tasks if str(t["task_id"]) == task_id)
    assert (task["intk_ids"], task["reason"], task["status"]) == ([real], "missed_retry", "queued")


@pytest.mark.parametrize("status", OPEN)
def test_a_press_keeps_a_task_that_also_holds_a_real_dose_and_takes_only_the_demo_doses_out(env, status):
    """Path (c): a task holding a real dose and a demo dose (made for their slot by the job or 'Use Reachy') was
    aborted as a whole by the next press, which cancelled Reachy's visit for the real dose, mid-prompt when it was in
    progress. Now the demo doses leave it, in order, and the rest of it stays as it was; a task holding demo doses
    alone is aborted as before."""
    real_med, other_med = env.db.add_med("Metformin", demo=False), env.db.add_med("Zinc", demo=False)
    med_id = env.db.add_med()
    slot = datetime(2026, 10, 20, 3, 30, tzinfo=timezone.utc)          # 11:30
    first_demo = env.db.add_dose(med_id, slot + timedelta(seconds=7))
    real = env.db.add_dose(real_med, slot)
    second_demo = env.db.add_dose(med_id, slot + timedelta(minutes=2, seconds=3))
    other_real = env.db.add_dose(other_med, slot)
    mixed = env.db.add_task([first_demo, real, second_demo, other_real], status=status, slot=slot)
    alone_dose = env.db.add_dose(med_id, slot - timedelta(minutes=30, seconds=-9))
    alone = env.db.add_task([alone_dose], status=status, slot=slot - timedelta(minutes=30))
    before = copy.deepcopy(mixed)
    assert _press(env).status_code == 200
    assert mixed["intk_ids"] == [real, other_real]
    assert {k: v for k, v in mixed.items() if k != "intk_ids"} == {k: v for k, v in before.items() if k != "intk_ids"}
    assert alone["status"] == "aborted" and json.loads(alone["detail"])["reason"] == "demo_reset"
    assert env.db.intakes[real]["intake_stats"] == env.db.intakes[other_real]["intake_stats"] == "pending"


@pytest.mark.parametrize("status", OPEN)
def test_a_press_never_touches_an_open_check_in_task(env, status):
    """A check-in task holds no dose, and an empty array is inside every array (<@): the abort also needs an overlap
    (&&), or each press would end 'Talk to Reachy now' (checked on PostgreSQL too)."""
    env.db.add_dose(env.db.add_med(), NOW - timedelta(minutes=50, seconds=-1))
    checkin = env.db.add_task([], status=status, slot=NOW - timedelta(minutes=1), reason="checkin")
    before = copy.deepcopy(checkin)
    assert _press(env).status_code == 200
    assert checkin == before


def test_a_later_press_puts_back_what_makes_the_demo_medicine(env):
    """The Medications edit form keeps prescription_meta, so an edited demo medicine kept its flag and was reused as it
    was: renamed (the press still answered 'Acetal 500mg'), with dose times (whose generated doses every press then
    deleted, future ones included), before meals, a liquid of 2 units, with limits of its own. Each press now puts
    back what makes it the demo: Acetal 500mg, one tablet after meals, no dose time, the default limits."""
    med_id = env.db.add_med(
        "Acetal 1000mg", instructions="空腹服用", dose_form="liquid", units_per_dose=Decimal("2.00"),
        min_interval_minutes=60, max_daily_doses=2, pills_remaining=Decimal("12.00"),
        schedule_time=json.dumps({"morning": True, "noon": False, "night": False, "bedtime": False,
                                  "after_meals": False, "before_meals": True, "custom_times": ["15:00"]}))
    tomorrow = env.db.add_dose(med_id, datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc))   # generated, 08:00
    response = _press(env)
    assert response.status_code == 200, response.json()
    body = response.json()
    med = env.db.meds[med_id]
    assert (body["med_id"], body["med_name"]) == (med_id, "Acetal 500mg")
    assert {key: med[key] for key in ("med_name", "instructions", "dose_form", "units_per_dose", "min_interval_minutes",
                                      "max_daily_doses", "pills_remaining", "is_active")} == {
        "med_name": "Acetal 500mg", "instructions": "示範藥物（飯後服用），用來測試服藥流程", "dose_form": "solid_oral",
        "units_per_dose": Decimal("1.00"), "min_interval_minutes": None, "max_daily_doses": None,
        "pills_remaining": Decimal("12.00"), "is_active": True}            # 10 or more: the stock is left as it is
    assert json.loads(med["schedule_time"]) == api_reachy.DEMO_SCHEDULE
    assert schedule.dose_times(med["schedule_time"]) == []
    assert json.loads(med["prescription_meta"]) == {"demo": "acetal"}
    assert tomorrow not in env.db.intakes and set(_doses_of(env, med_id)) == {body["intk_id"]}
    assert reachy_tasks.after_meal(med["schedule_time"], med["instructions"], NOW) is True   # 12:02, lunch


def test_a_press_that_meets_a_deadlock_is_tried_once_more(env):
    """The press locks the earlier demo task, then that dose's rows; the robot's confirmation for the dose locks the
    dose first, then the task. When the two meet, PostgreSQL ends one of them (deadlock_detected, 40P01). When that is
    the press, it starts once more from the beginning and answers as one press, instead of a 500."""
    med_id = env.db.add_med()
    earlier = env.db.add_dose(med_id, NOW - timedelta(minutes=12, seconds=-5))
    task_id = env.db.add_task([earlier], slot=NOW - timedelta(minutes=15))["task_id"]
    env.db.abort_errors = [FakeDeadlock()]
    response = _press(env)
    assert response.status_code == 200, response.json()
    assert _locks(env) == 2
    assert len(env.db.demo_meds()) == 1 and set(_doses_of(env, med_id)) == {response.json()["intk_id"]}
    (old,) = [t for t in env.db.tasks if t["task_id"] == task_id]
    assert old["status"] == "aborted" and json.loads(old["detail"])["reason"] == "demo_reset"
    assert len([t for t in env.db.tasks if t["status"] in OPEN]) == 1


@pytest.mark.parametrize("errors, presses", [([FakeDeadlock(), FakeDeadlock()], 2), ([RuntimeError("lost")], 1)],
                         ids=["second deadlock", "other error"])
def test_a_second_deadlock_or_another_error_is_not_tried_again(env, errors, presses):
    earlier = env.db.add_dose(env.db.add_med(), NOW - timedelta(minutes=12, seconds=-5))
    env.db.add_task([earlier], slot=NOW - timedelta(minutes=15))
    env.db.abort_errors = list(errors)
    before = _state(env)
    with pytest.raises(type(errors[0])):
        _press(env)
    assert _locks(env) == presses and _state(env) == before


def test_a_late_answer_on_a_demo_request_already_answered_names_no_empty_slot(env):
    """A request family answered 'taken' for a demo dose stays answered, but the next press deletes its dose: a second
    press of the button then has no slot to name either."""
    med_id = env.db.add_med()
    taken = env.db.add_dose(med_id, NOW - timedelta(minutes=30, seconds=-7))
    request = env.db.add_request([taken], {str(taken): "pending"})
    env.db.confirmations[request].update(resolution="confirmed", resolved_by=CONTACT["id"], resolved_at=NOW)
    env.db.intakes[taken].update(intake_stats="taken", actual_intake_time=NOW - timedelta(minutes=30))
    assert _press(env).status_code == 200
    data = dose_confirmation.sign_postback(dose_confirmation.ACTION, request, "taken", CONTACT["id"])
    result = asyncio.run(dose_confirmation.handle_postback(env.db, data, CONTACT["line_id"]))
    assert result["status"] == "already_resolved" and result["resolution"] == "confirmed"
    (reply,) = env.replies
    assert reply["messages"][0]["text"] == ("此確認已由 Daughter 回覆：已服用（Pearl）。\n"
                                            "Already answered by Daughter: taken (Pearl's dose).")


def test_a_late_answer_on_a_cleared_request_that_also_held_a_real_dose_names_its_slot(env):
    med_id = env.db.add_med()
    waiting = env.db.add_dose(med_id, NOW - timedelta(minutes=30, seconds=-7))
    real = env.db.add_dose(env.db.add_med("Metformin", demo=False), NOW - timedelta(minutes=30))   # 11:32
    request = env.db.add_request([waiting, real], {str(waiting): "pending", str(real): "missed"})
    assert _press(env).status_code == 200
    data = dose_confirmation.sign_postback(dose_confirmation.ACTION, request, "not_taken", CONTACT["id"])
    asyncio.run(dose_confirmation.handle_postback(env.db, data, CONTACT["line_id"]))
    (reply,) = env.replies
    assert reply["messages"][0]["text"] == ("此確認請求已逾時（Pearl 10/20 11:32）。\n"
                                            "This request for Pearl's 10/20 11:32 dose has expired.")
