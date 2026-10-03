import asyncio
import copy
import datetime
import json
import os
import sys
import types
import uuid

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

sys.path.insert(0, os.path.dirname(__file__))
import dose_facts  # noqa: E402

from app.services import dose_confirmation, outbox, schedule  # noqa: E402

UTC = datetime.timezone.utc
SLOT = datetime.datetime(2026, 9, 30, 0, 0, tzinfo=UTC)  # 08:00 Asia/Taipei


class _Transaction:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        self.db.snapshots.append(copy.deepcopy(self.db.state()))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        snapshot = self.db.snapshots.pop()
        if exc_type is not None:
            self.db.restore(snapshot)
        return False


class FakeDB:
    """Just enough of the schema for dose confirmation, postbacks and the outbox."""

    def __init__(self):
        self.users = {7: {"name": "Pearl"}, 8: {"name": "Other"}}
        self.contacts = {
            1: {"id": 1, "u_id": 7, "name": "Amy", "relationship": "child", "line_id": "U-amy",
                "verified": True, "notify_missed": True},
            2: {"id": 2, "u_id": 7, "name": "Ben", "relationship": "spouse", "line_id": "U-ben",
                "verified": True, "notify_missed": True},
            3: {"id": 3, "u_id": 7, "name": "Muted", "relationship": "child", "line_id": "U-muted",
                "verified": True, "notify_missed": False},
            4: {"id": 4, "u_id": 7, "name": "Unverified", "relationship": "child", "line_id": "U-unv",
                "verified": False, "notify_missed": True},
            5: {"id": 5, "u_id": 7, "name": "Pearl self", "relationship": "user", "line_id": "U-pearl",
                "verified": True, "notify_missed": True},
            6: {"id": 6, "u_id": 8, "name": "Stranger", "relationship": "child", "line_id": "U-stranger",
                "verified": True, "notify_missed": True},
        }
        self.meds = {
            40: {"u_id": 7, "med_name": "Metformin", "pills_remaining": 5},
            41: {"u_id": 7, "med_name": "Aspirin", "pills_remaining": 0},
            42: {"u_id": 8, "med_name": "Foreign", "pills_remaining": 5},
        }
        self.intakes = {
            100: self._intake(7, 40, "pending"),
            101: self._intake(7, 41, "missed"),
            102: self._intake(7, 40, "taken"),
            103: self._intake(8, 42, "pending"),
        }
        self.confirmations = {}
        self.settings = {}
        self.outbox = []
        self.notifications = []
        self.protection = True     # notification_settings.overdose_protection (bool or {u_id: bool})
        self.language = None       # the patient's latest check-in language
        self.locks = []            # dose_safety.LOCK_SQL calls
        self.snapshots = []

    @staticmethod
    def _intake(u_id, med_id, status):
        return {"u_id": u_id, "med_id": med_id, "intake_stats": status, "intake_time_stamp": SLOT,
                "actual_intake_time": None, "detection_method": None}

    def state(self):
        return {"meds": self.meds, "intakes": self.intakes, "confirmations": self.confirmations,
                "outbox": self.outbox, "notifications": self.notifications}

    def restore(self, snapshot):
        self.meds = snapshot["meds"]
        self.intakes = snapshot["intakes"]
        self.confirmations = snapshot["confirmations"]
        self.outbox = snapshot["outbox"]
        self.notifications = snapshot["notifications"]

    def transaction(self):
        return _Transaction(self)

    def _eligible(self, u_id):
        return [c for c in sorted(self.contacts.values(), key=lambda c: c["id"])
                if c["u_id"] == u_id and c["verified"] and c["notify_missed"] and c["line_id"]
                and c["relationship"] != "user"]

    # ── fetch ──
    async def fetch(self, query, *args):
        if dose_facts.is_facts(query):
            return dose_facts.rows(args, self.intakes, self.meds, protection=self.protection, language=self.language,
                                   confirmations=self.confirmations)
        if "FROM intake i JOIN medication m" in query and "intake_stats IN ('pending','missed')" in query:
            assert "FOR UPDATE OF i" in query
            u_id, ids = args
            return sorted(({"intk_id": i, "intake_stats": r["intake_stats"],
                            "intake_time_stamp": r["intake_time_stamp"], "med_name": self.meds[r["med_id"]]["med_name"]}
                           for i, r in self.intakes.items()
                           if i in ids and r["u_id"] == u_id and r["intake_stats"] in ("pending", "missed")),
                          key=lambda r: (r["intake_time_stamp"], r["intk_id"]))
        if "FROM intake i JOIN medication m" in query:
            (ids,) = args
            return [{"intk_id": i, "intake_time_stamp": r["intake_time_stamp"],
                     "med_name": self.meds[r["med_id"]]["med_name"]}
                    for i, r in sorted(self.intakes.items()) if i in ids]
        if "FROM family_contacts" in query and "notify_missed = TRUE" in query:
            return [{"id": c["id"], "name": c["name"], "line_id": c["line_id"]} for c in self._eligible(args[0])]
        if "FROM family_contacts" in query and "relationship IS DISTINCT FROM 'user'" in query:
            # outbox.enqueue_to_contacts without a contact flag: every verified family contact on LINE.
            return [{"id": c["id"], "line_id": c["line_id"]}
                    for c in sorted(self.contacts.values(), key=lambda c: c["id"])
                    if c["u_id"] == args[0] and c["verified"] and c["line_id"] and c["relationship"] != "user"]
        if "SELECT intk_id, med_id, intake_stats" in query and "FROM intake WHERE" in query:
            ids, u_id = args
            return [{"intk_id": i, "med_id": r["med_id"], "intake_stats": r["intake_stats"]}
                    for i, r in sorted(self.intakes.items()) if i in ids and r["u_id"] == u_id]
        if "FROM dose_confirmation dc" in query:
            rows = []
            for c in self.confirmations.values():
                if c["resolution"] is None:
                    s = self.settings.get(c["u_id"], {})
                    rows.append({"confirmation_id": uuid.UUID(c["confirmation_id"]), "created_at": c["created_at"],
                                 "reminded_at": c["reminded_at"],
                                 "remind_after_minutes": s.get("remind_after_minutes", 10),
                                 "remind_after_retries": s.get("remind_after_retries", 3)})
            return rows
        raise AssertionError(f"unexpected fetch: {query}")

    async def fetchrow(self, query, *args):
        if "UPDATE medication SET pills_remaining=pills_remaining-units_per_dose" in query:
            med_id, u_id = args
            med = self.meds[med_id]
            units = med.get("units_per_dose", 1)
            if med["u_id"] != u_id or med["pills_remaining"] < units:
                return None
            med["pills_remaining"] -= units
            return {"pills_remaining": med["pills_remaining"], "units_per_dose": units}
        if "FROM dose_confirmation WHERE confirmation_id" in query and "FOR UPDATE" in query:
            row = self.confirmations.get(str(args[0]))
            if row is None:
                return None
            result = dict(row)
            result["confirmation_id"] = uuid.UUID(row["confirmation_id"])
            result["previous_status"] = json.dumps(row["previous_status"])
            return result
        if "FROM family_contacts WHERE id" in query:
            contact = self.contacts.get(args[0])
            return dict(contact) if contact else None
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def fetchval(self, query, *args):
        if "INSERT INTO notification_outbox" in query:
            dedupe_key, u_id, contact_id, line_id, kind, priority, payload = args
            if any(row["dedupe_key"] == dedupe_key for row in self.outbox):
                return None
            self.outbox.append({"outbox_id": len(self.outbox) + 1, "dedupe_key": dedupe_key, "u_id": u_id,
                                "recipient_contact_id": contact_id, "recipient_line_id": line_id, "kind": kind,
                                "priority": priority, "payload": json.loads(payload),
                                "created_at": schedule.current_time()})
            return len(self.outbox)
        if "FROM notification_outbox" in query and "kind = 'double_dose_alert'" in query:
            # dose_safety.alert_family: an alert for this dose within the last hour (dedupe_key LIKE 'prefix%').
            u_id, pattern, since = args
            assert pattern.endswith("%")
            return 1 if any(row["u_id"] == u_id and row["kind"] == "double_dose_alert"
                            and row["dedupe_key"].startswith(pattern[:-1]) and row["created_at"] > since
                            for row in self.outbox) else None
        if 'FROM "user"' in query:
            return self.users[args[0]]["name"]
        if "SELECT u_id FROM dose_confirmation" in query:
            row = self.confirmations.get(str(args[0]))
            return row["u_id"] if row else None
        if "SELECT name FROM family_contacts" in query:
            contact = self.contacts.get(args[0])
            return contact["name"] if contact else None
        raise AssertionError(f"unexpected fetchval: {query}")

    async def execute(self, query, *args):
        if dose_facts.is_lock(query):
            # Recording paths lock the medicines' rows first (a dose waiting for family counts as taken).
            self.locks.append(args)
            return "SELECT"
        if "SET intake_stats='pending_confirmation'" in query:
            for intk_id in args[0]:
                self.intakes[intk_id]["intake_stats"] = "pending_confirmation"
            return "UPDATE"
        if "INSERT INTO dose_confirmation" in query:
            confirmation_id, u_id, task_id, intk_ids, previous, source, evidence = args
            self.confirmations[confirmation_id] = {
                "confirmation_id": confirmation_id, "u_id": u_id, "task_id": task_id, "intk_ids": list(intk_ids),
                "previous_status": json.loads(previous), "source": source,
                "evidence": json.loads(evidence) if evidence else None,
                "created_at": schedule.current_time(), "reminded_at": None, "resolved_at": None,
                "resolution": None, "resolved_by": None}
            return "INSERT"
        if "SET intake_stats='taken'" in query:
            # Stored as taken when the patient was asked (the confirmation's created_at), not when family answered.
            assert "actual_intake_time=$3" in query and "taken_notified=FALSE" in query
            row = self.intakes[args[0]]
            row.update(intake_stats="taken", actual_intake_time=args[2], detection_method="caregiver_confirmed")
            return "UPDATE"
        if "INSERT INTO notification (" in query:
            self.notifications.append({"u_id": args[0], "query": query, "args": args})
            return "INSERT"
        if "UPDATE intake SET intake_stats=$1" in query:
            status, intk_id = args
            self.intakes[intk_id]["intake_stats"] = status
            return "UPDATE"
        if "UPDATE dose_confirmation SET resolution" in query:
            confirmation_id, resolution, resolved_by, *rest = args
            self.confirmations[str(confirmation_id)].update(
                resolution=resolution, resolved_by=resolved_by, resolved_at=rest[0] if rest else "NOW")
            return "UPDATE"
        if "UPDATE dose_confirmation SET reminded_at" in query:
            confirmation_id, reminded_at = args
            self.confirmations[str(confirmation_id)]["reminded_at"] = reminded_at
            return "UPDATE"
        raise AssertionError(f"unexpected execute: {query}")


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def run(coro):
    return asyncio.run(coro)


def create(db, intk_ids=(100, 101), source="uncertain_detection", u_id=7, evidence=None):
    return run(dose_confirmation.create(db, u_id=u_id, task_id=None, intk_ids=list(intk_ids), source=source,
                                        evidence=evidence if evidence is not None else {"score": 0.31}))


def message_text(row):
    return "\n".join(m.get("text", "") + m.get("altText", "") for m in row["payload"]["messages"])


def buttons(row):
    return next(m for m in row["payload"]["messages"] if m["type"] == "template")


# ── signing ──

def test_sign_and_verify_round_trip():
    data = dose_confirmation.sign_postback("dose_confirm", "0b0c6a2e-1111-4222-8333-444455556666", "taken", 12)
    assert data.startswith("action=dose_confirm&id=0b0c6a2e-1111-4222-8333-444455556666&a=taken&c=12&s=")
    assert len(data.rsplit("s=", 1)[1]) == 16
    assert len(data) <= 300
    assert dose_confirmation.verify_postback(data) == {
        "action": "dose_confirm", "id": "0b0c6a2e-1111-4222-8333-444455556666", "answer": "taken", "contact_id": 12}


@pytest.mark.parametrize("mutate", [
    lambda d: d.replace("a=taken", "a=not_taken"),
    lambda d: d.replace("c=12", "c=13"),
    lambda d: d.replace("id=0b0c6a2e", "id=1b0c6a2e"),
    lambda d: d[:-1] + ("0" if d[-1] != "0" else "1"),
    lambda d: d.rsplit("&s=", 1)[0],
    lambda d: d + "&s=0000000000000000",
    lambda d: "",
    lambda d: "garbage",
    lambda d: d.rsplit("&s=", 1)[0] + "&s=%C3%A9",
    lambda d: d.replace("c=12", "c=%EF%BC%91%EF%BC%92"),
    lambda d: d.replace("action=dose_confirm", "action=ack_risk"),
])
def test_verify_rejects_tampered_data(mutate):
    data = dose_confirmation.sign_postback("dose_confirm", "0b0c6a2e-1111-4222-8333-444455556666", "taken", 12)
    assert dose_confirmation.verify_postback(mutate(data)) is None


def test_verify_rejects_signature_under_other_secret(monkeypatch):
    data = dose_confirmation.sign_postback("dose_confirm", "0b0c6a2e-1111-4222-8333-444455556666", "taken", 12)
    monkeypatch.setattr(dose_confirmation, "SECRET_KEY", "another-secret")
    assert dose_confirmation.verify_postback(data) is None


# ── create ──

def test_create_marks_pending_confirmation_and_enqueues_buttons_per_eligible_contact():
    db = FakeDB()
    confirmation_id = create(db, intk_ids=(100, 101, 102, 103))

    assert db.intakes[100]["intake_stats"] == "pending_confirmation"
    assert db.intakes[101]["intake_stats"] == "pending_confirmation"
    assert db.intakes[102]["intake_stats"] == "taken"      # not eligible
    assert db.intakes[103]["intake_stats"] == "pending"    # another patient's dose
    assert db.meds[40]["pills_remaining"] == 5              # no stock change
    row = db.confirmations[confirmation_id]
    assert row["intk_ids"] == [100, 101]
    assert row["previous_status"] == {"100": "pending", "101": "missed"}
    assert row["source"] == "uncertain_detection"
    assert row["evidence"] == {"score": 0.31}

    assert [r["recipient_contact_id"] for r in db.outbox] == [1, 2]
    for entry in db.outbox:
        assert entry["kind"] == "dose_confirm"
        assert entry["priority"] == 1
        assert entry["dedupe_key"] == f"dose_confirm:{confirmation_id}:{entry['recipient_contact_id']}"
        template = buttons(entry)
        assert template["template"]["type"] == "buttons"
        assert len(template["template"]["text"]) <= 160
        assert len(template["altText"]) <= 400
        for text in (template["template"]["text"], template["altText"]):
            assert "Pearl" in text and "Metformin" in text and "08:00" in text
        actions = template["template"]["actions"]
        assert [a["label"] for a in actions] == ["已服用 Taken", "未服用 Not taken"]
        assert all(a["type"] == "postback" for a in actions)
        parsed = [dose_confirmation.verify_postback(a["data"]) for a in actions]
        assert [p["answer"] for p in parsed] == ["taken", "not_taken"]
        assert {p["contact_id"] for p in parsed} == {entry["recipient_contact_id"]}
        assert {p["id"] for p in parsed} == {confirmation_id}


@pytest.mark.parametrize("source, words", [
    ("uncertain_detection", ("無法確認", "could not verify")),
    ("unsupported_dose", ("劑型", "dose type")),
    ("degraded", ("不夠流暢", "camera stream was not smooth enough")),
    ("auto_record_off", ("自動記錄", "automatic recording")),
    ("patient_claim", ("表示", "says")),
])
def test_create_message_names_reason(source, words):
    db = FakeDB()
    create(db, source=source)
    text = message_text(db.outbox[0])
    for word in words:
        assert word.lower() in text.lower()


def test_degraded_camera_is_a_frame_rate_not_picture_quality_and_quotes_it():
    """2 Oct 2026: the robot's stream ran at 10 fps; family read "the camera feed was too poor"."""
    db = FakeDB()
    create(db, source="degraded", evidence={"decision": "uncertain", "degraded": True, "landmark_fps": 10.1})
    text = message_text(db.outbox[0])
    assert "影像品質" not in text and "too poor" not in text
    assert "每秒 10.1 張畫面，需要 12 張" in text
    assert "not smooth enough to verify: 10.1 frames per second, 12 needed" in text


def test_degraded_buttons_card_keeps_its_whole_reason_with_two_long_medicine_names():
    """The card holds 160 characters; with the frame rate in it, the English reason was cut off mid-sentence."""
    db = FakeDB()
    db.meds[40]["med_name"], db.meds[41]["med_name"] = "Metformin 500mg", "Amlodipine 5mg"
    create(db, source="degraded", evidence={"decision": "uncertain", "degraded": True, "landmark_fps": 10.1})
    short = buttons(db.outbox[0])["template"]["text"]
    assert "Metformin 500mg、Amlodipine 5mg" in short and "…" not in short
    assert short.endswith("鏡頭畫面不夠流暢，無法確認\nthe camera stream was not smooth enough to verify")
    assert "10.1 frames per second" in db.outbox[0]["payload"]["messages"][0]["text"]   # the text message has it


@pytest.mark.parametrize("fps", [None, "10", True, float("nan"), -1, 12.0, 15.2])
def test_degraded_without_a_usable_low_rate_names_no_numbers(fps):
    db = FakeDB()
    create(db, source="degraded", evidence={"degraded": True, "landmark_fps": fps})
    text = message_text(db.outbox[0])
    assert "not smooth enough to verify" in text and "frames per second" not in text and "每秒" not in text


def test_only_the_camera_reason_quotes_the_frame_rate():
    db = FakeDB()
    create(db, source="patient_claim", evidence={"said_done": True, "degraded": True, "landmark_fps": 10.2})
    assert "10.2" not in message_text(db.outbox[0])


def test_quoted_frame_rate_is_the_servers_recording_rate():
    from app.services import monitor_service

    assert dose_confirmation.FPS_NEEDED == monitor_service.FPS_MIN


def test_create_without_eligible_intake_raises_and_changes_nothing():
    db = FakeDB()
    with pytest.raises(ValueError):
        create(db, intk_ids=(102, 103))
    assert db.confirmations == {}
    assert db.outbox == []


def test_create_rejects_unknown_source():
    db = FakeDB()
    with pytest.raises(ValueError):
        create(db, source="guess")
    assert db.intakes[100]["intake_stats"] == "pending"


def _from_now(**delta):
    return datetime.datetime.now(UTC) + datetime.timedelta(**delta)


@pytest.mark.parametrize("source", dose_confirmation.SOURCES)
def test_create_refuses_a_dose_not_due_yet_and_changes_nothing(source):
    """Whatever the robot reports (the patient's 「我吃完了」 included), family is never asked hours early."""
    db = FakeDB()
    db.intakes[100]["intake_time_stamp"] = _from_now(minutes=schedule.DOSE_EARLY_MINUTES + 5)
    with pytest.raises(schedule.DoseNotDueYet) as refused:
        create(db, intk_ids=(100, 101), source=source)
    assert refused.value.intk_id == 100
    assert not isinstance(refused.value, ValueError)   # no router's generic 409 may swallow its time
    assert db.intakes[100]["intake_stats"] == "pending" and db.intakes[101]["intake_stats"] == "missed"
    assert db.confirmations == {} and db.outbox == []


def test_create_accepts_a_dose_within_the_early_window():
    db = FakeDB()
    db.intakes[100]["intake_time_stamp"] = _from_now(minutes=schedule.DOSE_EARLY_MINUTES - 5)
    confirmation_id = create(db, intk_ids=(100,))
    assert db.intakes[100]["intake_stats"] == "pending_confirmation" and confirmation_id in db.confirmations


# ── resolve ──

def test_resolve_taken_decrements_stock_and_marks_caregiver_confirmed():
    db = FakeDB()
    confirmation_id = create(db)
    result = run(dose_confirmation.resolve(db, confirmation_id, 1, "taken"))

    assert result["status"] == "resolved"
    assert result["resolution"] == "confirmed"
    assert result["stock_empty"] == [101]
    assert db.intakes[100]["intake_stats"] == "taken"
    assert db.intakes[100]["detection_method"] == "caregiver_confirmed"
    assert db.intakes[101]["intake_stats"] == "taken"      # still recorded when stock is already 0
    assert db.meds[40]["pills_remaining"] == 4
    assert db.meds[41]["pills_remaining"] == 0
    assert db.confirmations[confirmation_id]["resolution"] == "confirmed"
    assert db.confirmations[confirmation_id]["resolved_by"] == 1


def test_resolve_not_taken_restores_exact_previous_status():
    db = FakeDB()
    confirmation_id = create(db)
    result = run(dose_confirmation.resolve(db, confirmation_id, 2, "not_taken"))
    assert result["resolution"] == "denied"
    assert db.intakes[100]["intake_stats"] == "pending"
    assert db.intakes[101]["intake_stats"] == "missed"
    assert db.meds[40]["pills_remaining"] == 5
    assert db.confirmations[confirmation_id]["resolved_by"] == 2


def test_first_answer_wins():
    db = FakeDB()
    confirmation_id = create(db)
    run(dose_confirmation.resolve(db, confirmation_id, 1, "taken"))
    second = run(dose_confirmation.resolve(db, confirmation_id, 2, "not_taken"))
    assert second == {"status": "already_resolved", "resolution": "confirmed", "resolved_by": 1}
    assert db.intakes[100]["intake_stats"] == "taken"
    assert db.meds[40]["pills_remaining"] == 4


def test_resolve_skips_intakes_changed_meanwhile():
    db = FakeDB()
    confirmation_id = create(db)
    db.intakes[100]["intake_stats"] = "skipped"
    run(dose_confirmation.resolve(db, confirmation_id, 1, "taken"))
    assert db.intakes[100]["intake_stats"] == "skipped"
    assert db.meds[40]["pills_remaining"] == 5


def test_resolve_never_records_a_dose_that_was_not_due_when_asked():
    """A request made before the rule (3 Oct 2026, 00:17, for the 20:00 dose) answered 'taken' later."""
    db = FakeDB()
    confirmation_id = create(db)
    asked = datetime.datetime(2026, 10, 2, 16, 17, tzinfo=UTC)            # 00:17 in Taipei
    db.confirmations[confirmation_id]["created_at"] = asked
    db.intakes[100]["intake_time_stamp"] = datetime.datetime(2026, 10, 3, 12, 0, tzinfo=UTC)   # 20:00
    result = run(dose_confirmation.resolve(db, confirmation_id, 1, "taken"))
    assert result["resolution"] == "confirmed"
    assert result["not_due"] == [100] and result["intk_ids"] == [101]
    assert db.intakes[100]["intake_stats"] == "pending"                    # back to how it was, not taken
    assert db.intakes[100]["detection_method"] is None
    assert db.meds[40]["pills_remaining"] == 5                              # no pill counted
    assert db.intakes[101]["intake_stats"] == "taken"                       # its slot-mate was due


def test_resolve_records_a_dose_that_was_due_when_asked_even_if_answered_later():
    db = FakeDB()
    confirmation_id = create(db, intk_ids=(100,))
    asked = datetime.datetime(2026, 10, 2, 22, 30, tzinfo=UTC)             # 06:30 in Taipei
    db.confirmations[confirmation_id]["created_at"] = asked
    db.intakes[100]["intake_time_stamp"] = asked + schedule.DOSE_EARLY     # 08:30: due from 06:30
    result = run(dose_confirmation.resolve(db, confirmation_id, 1, "taken"))
    assert result["not_due"] == [] and result["intk_ids"] == [100]
    assert db.intakes[100]["intake_stats"] == "taken" and db.meds[40]["pills_remaining"] == 4


def test_resolve_that_records_nothing_closes_the_request_as_expired():
    """Nothing was recorded, so the request must not read 'confirmed': taken_confirmation_job would credit this
    contact with a confirmation, and later answerers would be told it was answered 'taken'."""
    db = FakeDB()
    confirmation_id = create(db, intk_ids=(100,))
    db.confirmations[confirmation_id]["created_at"] = datetime.datetime(2026, 10, 2, 16, 17, tzinfo=UTC)   # 00:17
    db.intakes[100]["intake_time_stamp"] = datetime.datetime(2026, 10, 3, 12, 0, tzinfo=UTC)              # 20:00
    result = run(dose_confirmation.resolve(db, confirmation_id, 1, "taken"))
    assert result["resolution"] == "expired" and result["intk_ids"] == [] and result["not_due"] == [100]
    assert db.confirmations[confirmation_id]["resolution"] == "expired"
    assert db.intakes[100]["intake_stats"] == "pending" and db.meds[40]["pills_remaining"] == 5
    late = run(dose_confirmation.resolve(db, confirmation_id, 2, "taken"))
    assert late["status"] == "already_resolved" and late["resolution"] == "expired"


def test_create_refuses_the_next_dose_of_the_same_medicine_until_halfway():
    """20:00 and 22:00 (the user's allegra): just after 20:00 the 22:00 dose is within DOSE_EARLY, yet not due
    until 21:00, so family is not asked to confirm two doses of one medicine minutes apart."""
    db = FakeDB()
    night = _from_now(minutes=-5)
    db.intakes[102]["intake_time_stamp"] = night                                    # the previous dose, just taken
    db.intakes[100]["intake_time_stamp"] = night + datetime.timedelta(hours=2)      # its next one
    with pytest.raises(schedule.DoseNotDueYet) as refused:
        create(db, intk_ids=(100,))
    assert refused.value.due_from == night + datetime.timedelta(hours=1)
    assert db.intakes[100]["intake_stats"] == "pending" and db.confirmations == {} and db.outbox == []
    night = _from_now(minutes=-65)                                                  # an hour later: past halfway
    db.intakes[102]["intake_time_stamp"] = night
    db.intakes[100]["intake_time_stamp"] = night + datetime.timedelta(hours=2)
    assert create(db, intk_ids=(100,)) in db.confirmations


def test_resolve_rejects_bad_answer_and_unknown_id():
    db = FakeDB()
    confirmation_id = create(db)
    with pytest.raises(ValueError):
        run(dose_confirmation.resolve(db, confirmation_id, 1, "maybe"))
    assert run(dose_confirmation.resolve(db, str(uuid.uuid4()), 1, "taken")) == {"status": "not_found"}


# ── maintenance ──

def _age(db, confirmation_id, minutes):
    db.confirmations[confirmation_id]["created_at"] = datetime.datetime.now(UTC) - datetime.timedelta(minutes=minutes)


def test_maintenance_reminds_once_after_60_minutes(monkeypatch):
    db = FakeDB()
    db.settings[7] = {"remind_after_minutes": 30, "remind_after_retries": 3}   # window 120 min
    monkeypatch.setattr(dose_confirmation, "get_pool", lambda: FakePool(db))
    confirmation_id = create(db)
    initial = len(db.outbox)

    _age(db, confirmation_id, 59)
    run(dose_confirmation.maintenance())
    assert len(db.outbox) == initial

    _age(db, confirmation_id, 61)
    run(dose_confirmation.maintenance())
    reminders = db.outbox[initial:]
    assert [r["dedupe_key"] for r in reminders] == [
        f"dose_confirm_reminder:{confirmation_id}:1", f"dose_confirm_reminder:{confirmation_id}:2"]
    assert all(buttons(r)["template"]["actions"] for r in reminders)
    assert db.confirmations[confirmation_id]["reminded_at"] is not None

    run(dose_confirmation.maintenance())
    assert len(db.outbox) == initial + 2
    assert db.intakes[100]["intake_stats"] == "pending_confirmation"


def test_reminder_repeats_the_measured_frame_rate(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(dose_confirmation, "get_pool", lambda: FakePool(db))
    confirmation_id = create(db, source="degraded", evidence={"degraded": True, "landmark_fps": 10.1})
    # asyncpg hands JSONB back as text
    db.confirmations[confirmation_id]["evidence"] = json.dumps(db.confirmations[confirmation_id]["evidence"])
    initial = len(db.outbox)
    _age(db, confirmation_id, 61)
    run(dose_confirmation.maintenance())
    reminders = db.outbox[initial:]
    assert reminders and all("10.1 frames per second, 12 needed" in message_text(r) for r in reminders)


def test_maintenance_expires_after_missed_window_and_restores(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(dose_confirmation, "get_pool", lambda: FakePool(db))
    confirmation_id = create(db)          # default missed window 10 * (3 + 1) = 40 min < 2 h floor
    _age(db, confirmation_id, 41)
    run(dose_confirmation.maintenance())
    assert db.confirmations[confirmation_id]["resolution"] is None   # caregivers get at least 2 h

    _age(db, confirmation_id, 61)
    run(dose_confirmation.maintenance())
    # With default settings the 60-minute reminder now fires before expiry.
    assert any(r["dedupe_key"].startswith("dose_confirm_reminder") for r in db.outbox)
    assert db.confirmations[confirmation_id]["resolution"] is None

    _age(db, confirmation_id, 121)
    run(dose_confirmation.maintenance())
    row = db.confirmations[confirmation_id]
    assert row["resolution"] == "expired"
    assert row["resolved_by"] is None
    assert db.intakes[100]["intake_stats"] == "pending"
    assert db.intakes[101]["intake_stats"] == "missed"
    assert db.meds[40]["pills_remaining"] == 5


def test_maintenance_uses_longer_missed_window_when_configured(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(dose_confirmation, "get_pool", lambda: FakePool(db))
    confirmation_id = create(db)
    db.settings[7] = {"remind_after_minutes": 60, "remind_after_retries": 3}   # 240 min window
    _age(db, confirmation_id, 200)
    run(dose_confirmation.maintenance())
    assert db.confirmations[confirmation_id]["resolution"] is None
    _age(db, confirmation_id, 241)
    run(dose_confirmation.maintenance())
    assert db.confirmations[confirmation_id]["resolution"] == "expired"


def test_maintenance_ignores_resolved(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(dose_confirmation, "get_pool", lambda: FakePool(db))
    confirmation_id = create(db)
    run(dose_confirmation.resolve(db, confirmation_id, 1, "taken"))
    _age(db, confirmation_id, 500)
    run(dose_confirmation.maintenance())
    assert db.confirmations[confirmation_id]["resolution"] == "confirmed"
    assert db.intakes[100]["intake_stats"] == "taken"


def test_job_wrapper_calls_maintenance(monkeypatch):
    from app.jobs import dose_confirmation_job
    calls = []

    async def fake_maintenance(now=None):
        calls.append(now)

    monkeypatch.setattr(dose_confirmation_job.dose_confirmation, "maintenance", fake_maintenance)
    run(dose_confirmation_job.run_dose_confirmation_maintenance())
    assert calls == [None]


def test_request_gives_the_ai_estimate_even_when_unsure():
    evidence = {"decision": "uncertain", "degraded": True, "confidence": 0.48, "landmark_fps": 10.1,
                "said_done": False}
    text = dose_confirmation._request_messages({"patient": "Aisha", "meds": "allegra", "slot": "10/02 22:00"},
                                               "degraded", str(uuid.uuid4()), 34, evidence=evidence)[0]["text"]
    assert "AI 判斷已服藥的可能性：48%（不確定）" in text and "AI estimate that it was taken: 48% (uncertain)" in text
    assert "看不到藥丸本身" in text and text.count("10.1") == 2   # the frame rate once per language, in the reason
    claim = {"camera": "no_event", "said_done": True, "degraded": True, "landmark_fps": 10.1}
    text = dose_confirmation._request_messages({"patient": "Aisha", "meds": "allegra", "slot": "10/02 22:00"},
                                               "patient_claim", str(uuid.uuid4()), 34, evidence=claim)[0]["text"]
    assert "無法判斷（鏡頭沒有看到服藥動作）" in text and "本人有說「吃完了」" in text and "看不到藥丸本身" not in text
