"""The Reachy card's settings and status (spec 2026-10-12-live-voice-2a-design.md, D, E1 and section 4): the call
name, the live voice switch, a field left out keeps its value; the schema and LIVE_VOICE_TEST."""

import json
import os
import re
import subprocess
import sys
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.config import PUBLIC_PORT
from app.dependencies import get_current_user
from app.routers import api_reachy
from app.services import consent_service, reachy_tasks, robot_control

REPO = Path(__file__).resolve().parent.parent
DEVICE_ID = uuid.uuid4()
CORE = {"core": {"granted": True, "terms_version": config.TERMS_VERSION}}


class _Transaction:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.depth += 1
        return self

    async def __aexit__(self, *args):
        self.conn.depth -= 1
        return False


class FakeConn:
    """Patient 7's "user" row and paired robot (device None: not paired). The two UPDATEs act as PostgreSQL does:
    COALESCE keeps the stored value when its argument is NULL (checked on PostgreSQL 16 when this was written)."""

    def __init__(self):
        self.user = {"reachy_call_name": None, "name": "Pearl", "reachy_meal_retry": False, "reachy_mic_muted": False}
        self.device = {"device_id": DEVICE_ID, "auto_record": False, "live_voice": False}
        self.status_detail = None   # the robot's last heartbeat report (asyncpg hands JSONB back as text)
        self.last_seen_at = None    # the robot's last heartbeat (None: never, so offline)
        self.depth = 0
        self.updates = []   # (query, args) of every UPDATE, in order
        self.control_reads = []   # (args, transaction depth) of each read of the robot's control address
        self.volume_writes = []   # (args, transaction depth) of each write of the volume the robot answered
        self.volume_error = False

    def transaction(self):
        return _Transaction(self)

    async def fetchrow(self, query, *args):
        if "FROM reachy_device WHERE u_id = $1 AND revoked_at IS NULL" in query:
            if self.device is None or args[0] != 7:
                return None
            return {**self.device, "label": "Reachy Mini", "last_seen_at": self.last_seen_at, "robot_reachable": None,
                    "landmark_fps": None, "status_detail": self.status_detail}
        if "FROM reachy_task WHERE u_id = $1" in query:
            return None
        if query == 'SELECT reachy_call_name, name, reachy_meal_retry, reachy_mic_muted FROM "user" WHERE u_id = $1':
            return dict(self.user) if args[0] == 7 else None
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        if query.startswith("UPDATE reachy_device SET"):
            assert "auto_record = COALESCE($2, auto_record)" in query
            assert "live_voice = COALESCE($3, live_voice)" in query
            assert self.depth > 0   # one transaction with the call name
            self.updates.append((query, args))
            if self.device is None or args[0] != 7:
                return None
            for key, value in zip(("auto_record", "live_voice"), args[1:]):
                if value is not None:
                    self.device[key] = value
            return self.device["device_id"]
        if "FROM family_contacts" in query:
            return 0
        if query == api_reachy.CONTROL_SQL:   # quick fix 9: the control address the robot's heartbeat kept
            self.control_reads.append((args, self.depth))
            return self.status_detail if self.device is not None and args[0] == self.device["device_id"] else None
        raise AssertionError(query)

    async def execute(self, query, *args):
        if query == 'UPDATE "user" SET reachy_call_name = $2 WHERE u_id = $1':
            assert self.depth > 0
            self.updates.append((query, args))
            if args[0] == 7:
                self.user["reachy_call_name"] = args[1]
            return "UPDATE 1"
        if query == 'UPDATE "user" SET reachy_meal_retry = $2 WHERE u_id = $1':
            assert self.depth > 0
            self.updates.append((query, args))
            if args[0] == 7:
                self.user["reachy_meal_retry"] = args[1]
            return "UPDATE 1"
        if query == 'UPDATE "user" SET reachy_mic_muted = $2 WHERE u_id = $1':
            assert self.depth > 0
            self.updates.append((query, args))
            if args[0] == 7:
                self.user["reachy_mic_muted"] = args[1]
            return "UPDATE 1"
        if query == api_reachy.MIC_VOLUME_SQL:   # quick fix 9: the volume the robot answered, as jsonb || does
            self.volume_writes.append((args, self.depth))
            if self.volume_error:
                raise RuntimeError("database went away")
            if self.device is not None and args[0] == self.device["device_id"]:
                detail = json.loads(self.status_detail) if self.status_detail else {}
                self.status_detail = json.dumps({**detail, "microphone_volume": args[1]})
            return "UPDATE 1"
        raise AssertionError(query)


class FakePool:
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


@pytest.fixture
def env(monkeypatch):
    from app.main import app

    conn = FakeConn()

    async def get_state(u_id):
        return CORE

    async def cancel_meal_retries(c, u_id):   # what it cancels: tests/test_reachy_tasks.py
        assert c is conn and conn.depth > 0   # in the settings' own transaction
        conn.updates.append(("cancel_meal_retries", (u_id,)))

    monkeypatch.setattr(app, "dependency_overrides", {get_current_user: lambda: {"u_id": 7, "name": "Pearl"}})
    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(api_reachy, "get_pool", lambda: FakePool(conn))
    # Quick fix 9: the direct call to the robot app (tests/test_robot_control.py tests the real one).
    robot = types.SimpleNamespace(calls=[], answer=(0, None))

    async def set_microphone(control, muted):
        robot.calls.append({"control": control, "muted": muted, "depth": conn.depth,
                            "saved": conn.user["reachy_mic_muted"]})
        return robot.answer

    monkeypatch.setattr(reachy_tasks, "cancel_meal_retries", cancel_meal_retries)
    monkeypatch.setattr(robot_control, "set_microphone", set_microphone)
    monkeypatch.setattr(config, "LIVE_VOICE_TEST", False)
    return types.SimpleNamespace(conn=conn, robot=robot,
                                 client=TestClient(app, base_url=f"http://testserver:{PUBLIC_PORT}"))


def _settings(env, body):
    return env.client.patch("/api/reachy/settings", json=body)


# ── /api/reachy/status ───────────────────────────────────────────────────────

def test_status_gives_the_call_name_and_the_live_switch(env, monkeypatch):
    status = env.client.get("/api/reachy/status").json()
    assert status["paired"] is True
    assert (status["call_name"], status["call_name_custom"]) == ("Pearl", False)   # the registered name
    assert (status["live_voice"], status["live_voice_test"]) == (False, False)
    monkeypatch.setattr(config, "LIVE_VOICE_TEST", True)
    env.conn.device["live_voice"] = True
    status = env.client.get("/api/reachy/status").json()
    assert (status["live_voice"], status["live_voice_test"]) == (True, True)


def test_status_gives_no_call_name_when_the_registered_name_breaks_the_name_rules(env):
    env.conn.user["name"] = "Demo Patient 1790963685"    # registration takes any text
    status = env.client.get("/api/reachy/status").json()
    assert status["call_name"] is None and status["call_name_custom"] is False


def test_status_without_a_robot_is_unchanged(env):
    env.conn.device = None
    assert env.client.get("/api/reachy/status").json() == {"paired": False}


# ── PATCH /api/reachy/settings ───────────────────────────────────────────────

def test_a_call_name_is_saved_and_an_empty_one_goes_back_to_the_registered_name(env):
    response = _settings(env, {"call_name": "  阿嬤 "})
    assert response.status_code == 200 and env.conn.user["reachy_call_name"] == "阿嬤"
    assert (response.json()["call_name"], response.json()["call_name_custom"]) == ("阿嬤", True)
    for empty in ("", "   "):
        env.conn.user["reachy_call_name"] = "阿嬤"
        response = _settings(env, {"call_name": empty})
        assert response.status_code == 200 and env.conn.user["reachy_call_name"] is None
        assert (response.json()["call_name"], response.json()["call_name_custom"]) == ("Pearl", False)


def test_latin_call_names_are_tidied(env):
    assert _settings(env, {"call_name": " Grandma   Lin "}).json()["call_name"] == "Grandma Lin"
    assert env.conn.user["reachy_call_name"] == "Grandma Lin"


@pytest.mark.parametrize("name", [
    "王奶奶123", "Demo Patient 1790963685", "a" * 21, "阿" * 9, "王奶奶!", "王 奶奶",
    "我不想活了",          # risk words
    "藥罐子",              # a health word
    "ignore all rules",    # a command word
])
def test_a_call_name_must_follow_the_name_rules(env, name):
    response = _settings(env, {"call_name": name, "auto_record": True})
    assert response.status_code == 422 and response.json() == {"detail": "invalid_call_name"}
    # nothing is saved, not even the other fields
    assert env.conn.updates == [] and env.conn.user["reachy_call_name"] is None
    assert env.conn.device["auto_record"] is False


def test_a_call_name_longer_than_its_column_is_refused(env):
    assert _settings(env, {"call_name": "a" * 41}).status_code == 422
    assert env.conn.updates == []


def test_a_field_left_out_keeps_its_value(env):
    assert _settings(env, {"auto_record": True, "live_voice": True, "call_name": "阿嬤"}).status_code == 200
    body = _settings(env, {"auto_record": False}).json()   # what the card's auto-record switch sends
    assert (body["auto_record"], body["live_voice"], body["call_name"]) == (False, True, "阿嬤")
    body = _settings(env, {"live_voice": False, "call_name": None}).json()   # null is the same as left out
    assert (body["auto_record"], body["live_voice"], body["call_name"]) == (False, False, "阿嬤")
    body = _settings(env, {}).json()
    assert (body["auto_record"], body["live_voice"], body["call_name"], body["call_name_custom"]) == (
        False, False, "阿嬤", True)
    # The robot's row gets NULL for a field left out; the "user" row is written only when a call name was given.
    assert [args for query, args in env.conn.updates if query.startswith("UPDATE reachy_device")] == [
        (7, True, True), (7, False, None), (7, None, False), (7, None, None)]
    assert [args for query, args in env.conn.updates if query.startswith('UPDATE "user"')] == [(7, "阿嬤")]


def test_settings_need_a_paired_robot(env):
    env.conn.device = None
    response = _settings(env, {"call_name": "阿嬤"})
    assert response.status_code == 409 and response.json() == {"detail": "robot_not_paired"}
    assert env.conn.user["reachy_call_name"] is None


# ── Quick fix 5 (20 Oct 2026): the switch for "come back 20 minutes after 'not eaten'" ──

def test_status_gives_the_meal_retry_switch_off_by_default(env):
    assert env.client.get("/api/reachy/status").json()["meal_retry"] is False
    env.conn.user["reachy_meal_retry"] = True
    assert env.client.get("/api/reachy/status").json()["meal_retry"] is True


def test_the_meal_retry_switch_is_saved_and_a_field_left_out_keeps_it(env):
    body = _settings(env, {"meal_retry": True}).json()   # what the card's switch sends
    assert body["meal_retry"] is True and env.conn.user["reachy_meal_retry"] is True
    for kept in ({}, {"auto_record": True}, {"call_name": "阿嬤"}, {"meal_retry": None}):
        response = _settings(env, kept)
        assert response.status_code == 200 and response.json()["meal_retry"] is True
    assert [args for query, args in env.conn.updates if "reachy_meal_retry" in query] == [(7, True)]
    assert ("cancel_meal_retries", (7,)) not in env.conn.updates   # switching on cancels nothing


@pytest.mark.parametrize("value", ["true", "false", 1, 0, "yes", "on", [], {}])
def test_the_meal_retry_switch_takes_only_a_boolean(env, value):
    response = _settings(env, {"meal_retry": value, "auto_record": True})
    assert response.status_code == 422
    assert env.conn.updates == []   # nothing is saved, not even the other fields, and nothing is cancelled
    assert env.conn.user["reachy_meal_retry"] is False and env.conn.device["auto_record"] is False


def test_switching_the_meal_retry_off_cancels_a_waiting_one_in_the_same_transaction(env):
    """Off: the meal retry still waiting is cancelled (reachy_tasks.cancel_meal_retries), after the switch is written
    and in the same transaction, so the missed-dose job's hold ends with it. Off again cancels again: nothing to do."""
    env.conn.user["reachy_meal_retry"] = True
    for _ in range(2):
        body = _settings(env, {"meal_retry": False}).json()
        assert body["meal_retry"] is False and env.conn.user["reachy_meal_retry"] is False
    assert [query.split(" WHERE")[0] for query, args in env.conn.updates] == [
        "UPDATE reachy_device SET auto_record = COALESCE($2, auto_record), live_voice = COALESCE($3, live_voice)",
        'UPDATE "user" SET reachy_meal_retry = $2', "cancel_meal_retries"] * 2


def test_the_meal_retry_switch_needs_a_paired_robot(env):
    env.conn.device = None
    response = _settings(env, {"meal_retry": True})
    assert response.status_code == 409 and response.json() == {"detail": "robot_not_paired"}
    assert env.conn.user["reachy_meal_retry"] is False


def test_the_schema_adds_the_meal_retry_switch_off_by_default():
    """Per patient, on "user" like the call name: re-pairing the robot keeps it, as it keeps the patient's tasks."""
    sql = (REPO / "sql" / "init.sql").read_text(encoding="utf-8")
    statement = 'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS reachy_meal_retry BOOLEAN NOT NULL DEFAULT FALSE;'
    assert sql.count(statement) == 1 and sql.index(statement) > sql.index('CREATE TABLE IF NOT EXISTS "user"')


# ── Quick fix 8 (20 Oct 2026): the robot's microphone switch ──

def test_status_gives_the_microphone_switch_on_by_default_and_the_reported_volume(env):
    status = env.client.get("/api/reachy/status").json()
    assert (status["mic_muted"], status["mic_volume"]) == (False, None)   # not muted; no volume reported yet
    env.conn.user["reachy_mic_muted"] = True
    env.conn.status_detail = json.dumps({"vision_fps": None, "microphone_volume": 0})   # JSONB comes back as text
    status = env.client.get("/api/reachy/status").json()
    assert (status["mic_muted"], status["mic_volume"]) == (True, 0)
    env.conn.status_detail = {"microphone_volume": 85}
    assert env.client.get("/api/reachy/status").json()["mic_volume"] == 85


@pytest.mark.parametrize("stored", [{}, {"camera": None}, {"microphone_volume": None}, {"microphone_volume": True},
                                    {"microphone_volume": "85"}, "[]"])
def test_status_gives_no_microphone_volume_when_none_was_reported(env, stored):
    """An older robot (before 0.17.1) or one whose daemon did not answer reports none; anything else is no volume."""
    env.conn.status_detail = stored if isinstance(stored, str) else json.dumps(stored)
    assert env.client.get("/api/reachy/status").json()["mic_volume"] is None


def test_the_microphone_switch_is_saved_and_a_field_left_out_keeps_it(env):
    body = _settings(env, {"mic_muted": True}).json()   # what the card's switch sends to turn the microphone off
    assert body["mic_muted"] is True and env.conn.user["reachy_mic_muted"] is True
    for kept in ({}, {"auto_record": True}, {"meal_retry": True}, {"call_name": "阿嬤"}, {"mic_muted": None}):
        response = _settings(env, kept)
        assert response.status_code == 200 and response.json()["mic_muted"] is True
    body = _settings(env, {"mic_muted": False}).json()   # on again
    assert body["mic_muted"] is False and env.conn.user["reachy_mic_muted"] is False
    assert [args for query, args in env.conn.updates if "reachy_mic_muted" in query] == [(7, True), (7, False)]
    assert ("cancel_meal_retries", (7,)) not in env.conn.updates   # the microphone has nothing to cancel


@pytest.mark.parametrize("value", ["true", "false", 1, 0, "yes", "off", [], {}])
def test_the_microphone_switch_takes_only_a_boolean(env, value):
    response = _settings(env, {"mic_muted": value, "auto_record": True})
    assert response.status_code == 422
    assert env.conn.updates == []   # nothing is saved, not even the other fields
    assert env.conn.user["reachy_mic_muted"] is False and env.conn.device["auto_record"] is False


def test_the_microphone_switch_needs_a_paired_robot(env):
    env.conn.device = None
    response = _settings(env, {"mic_muted": True})
    assert response.status_code == 409 and response.json() == {"detail": "robot_not_paired"}
    assert env.conn.user["reachy_mic_muted"] is False


def test_the_microphone_switch_needs_current_core_consent(env, monkeypatch):
    async def no_consent(u_id):
        return {}

    monkeypatch.setattr(consent_service, "get_state", no_consent)
    response = _settings(env, {"mic_muted": True})
    assert response.status_code == 403 and response.json() == {"detail": "consent_required"}
    assert env.conn.updates == [] and env.conn.user["reachy_mic_muted"] is False


def test_the_schema_adds_the_microphone_switch_unmuted_by_default():
    """Per patient, on "user" like reachy_meal_retry: re-pairing the robot (a new reachy_device row) never unmutes it."""
    sql = (REPO / "sql" / "init.sql").read_text(encoding="utf-8")
    statement = 'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS reachy_mic_muted BOOLEAN NOT NULL DEFAULT FALSE;'
    assert sql.count(statement) == 1 and sql.index(statement) > sql.index('CREATE TABLE IF NOT EXISTS "user"')


# ── Quick fix 9 (21 Oct 2026): the microphone button applies the switch on the robot at once ──

CONTROL_KEY = "Zk-_" * 11   # 44 URL-safe characters, the shape of the robot app's secrets.token_urlsafe(32)
CONTROL = {"url": "http://192.168.137.104:8042", "key": CONTROL_KEY}


def _robot_reports(env, control=CONTROL, volume=100, online=True):
    """The robot's last heartbeat, as api_device.heartbeat keeps it (status_detail, JSONB handed back as text)."""
    env.conn.last_seen_at = datetime.now(timezone.utc) if online else None
    detail = {"vision_fps": None, "bridge_version": "0.17.2", "missing_clips": None, "microphone_volume": volume}
    env.conn.status_detail = json.dumps({**detail, **({"control": control} if control is not None else {})})


def _mic_lines(caplog) -> list:
    return [record.getMessage() for record in caplog.records
            if record.name == api_reachy.log.name and record.getMessage().startswith("microphone switched")]


def test_the_button_applies_the_switch_on_the_robot_after_the_commit(env, caplog):
    _robot_reports(env)
    env.robot.answer = (0, None)   # the robot app's answer: muted, and the daemon's volume after the change
    with caplog.at_level("INFO"):
        response = _settings(env, {"mic_muted": True})
    assert response.status_code == 200
    body = response.json()
    assert (body["mic_muted"], body["mic_applied"], body["mic_volume"]) == (True, True, 0)
    # One call, after the transaction committed the switch: the heartbeat applies it too, whatever the call does.
    assert env.robot.calls == [{"control": CONTROL, "muted": True, "depth": 0, "saved": True}]
    assert env.conn.control_reads == [((DEVICE_ID,), 1)]   # read in the settings' own transaction
    # The volume the robot answered is kept as the heartbeat keeps it, so the card shows it before the next one.
    assert env.conn.volume_writes == [((DEVICE_ID, 0), 0)]
    assert env.client.get("/api/reachy/status").json()["mic_volume"] == 0
    (line,) = _mic_lines(caplog)
    assert re.fullmatch(r"microphone switched off for patient 7: applied on the robot in \d+ ms", line)

    env.robot.answer = (100, None)
    caplog.clear()
    with caplog.at_level("INFO"):
        body = _settings(env, {"mic_muted": False}).json()
    assert (body["mic_muted"], body["mic_applied"], body["mic_volume"]) == (False, True, 100)
    assert env.robot.calls[1] == {"control": CONTROL, "muted": False, "depth": 0, "saved": False}
    assert env.conn.volume_writes[1] == ((DEVICE_ID, 100), 0)
    (line,) = _mic_lines(caplog)
    assert re.fullmatch(r"microphone switched on for patient 7: applied on the robot in \d+ ms", line)


def test_a_robot_that_could_not_read_its_volume_still_applied_the_switch(env):
    _robot_reports(env, volume=100)
    env.robot.answer = (None, None)
    body = _settings(env, {"mic_muted": True}).json()
    assert (body["mic_applied"], body["mic_volume"]) == (True, None)
    assert env.conn.volume_writes == [((DEVICE_ID, None), 0)]
    assert env.client.get("/api/reachy/status").json()["mic_volume"] is None


@pytest.mark.parametrize("reason", ["timeout", "HTTP 401", "HTTP 503", "no connection", "bad answer"])
def test_a_robot_that_did_not_confirm_leaves_it_to_the_heartbeat(env, caplog, reason):
    _robot_reports(env, volume=100)
    env.robot.answer = (None, reason)
    with caplog.at_level("INFO"):
        response = _settings(env, {"mic_muted": True})
    assert response.status_code == 200
    body = response.json()
    assert (body["mic_muted"], body["mic_applied"], body["mic_volume"]) == (True, False, 100)   # saved, not applied
    assert env.conn.user["reachy_mic_muted"] is True and len(env.robot.calls) == 1
    assert env.conn.volume_writes == []
    assert _mic_lines(caplog) == [
        f"microphone switched off for patient 7: not applied directly ({reason}), the heartbeat will"]


@pytest.mark.parametrize("control", [None, {"url": "http://8.8.8.8:8042", "key": CONTROL_KEY},
                                     {"url": "http://192.168.137.104:8042", "key": "short"}, "http://10.0.0.7:8042"])
def test_without_a_control_address_the_heartbeat_applies_it(env, caplog, control):
    """A robot app before 0.17.2 sends none; a stored one is checked again before any call."""
    _robot_reports(env, control=control)
    with caplog.at_level("INFO"):
        body = _settings(env, {"mic_muted": True}).json()
    assert (body["mic_muted"], body["mic_applied"]) == (True, False)
    assert env.robot.calls == [] and env.conn.volume_writes == []
    assert _mic_lines(caplog) == [
        "microphone switched off for patient 7: not applied directly (no control address), the heartbeat will"]


def test_an_offline_robot_is_not_called(env, caplog):
    """Its last address may be old (another machine may have it now): the key never goes to it."""
    _robot_reports(env, online=False)
    with caplog.at_level("INFO"):
        body = _settings(env, {"mic_muted": False}).json()
    assert (body["mic_muted"], body["mic_applied"]) == (False, False)
    assert env.robot.calls == []
    assert _mic_lines(caplog) == [
        "microphone switched on for patient 7: not applied directly (robot offline), the heartbeat will"]


def test_a_failed_volume_write_keeps_the_robots_answer(env, caplog):
    """The robot applied the switch: the answer says so, and the next heartbeat brings the volume to the card."""
    _robot_reports(env, volume=100)
    env.conn.volume_error = True
    with caplog.at_level("INFO"):
        body = _settings(env, {"mic_muted": True}).json()
    assert (body["mic_applied"], body["mic_volume"]) == (True, 0)
    assert [r.levelname for r in caplog.records if "volume the robot answered" in r.getMessage()] == ["WARNING"]
    assert len(_mic_lines(caplog)) == 1


@pytest.mark.parametrize("body", [{}, {"auto_record": True}, {"meal_retry": True}, {"call_name": "阿嬤"},
                                  {"mic_muted": None}])
def test_other_settings_never_call_the_robot(env, body):
    _robot_reports(env)
    answer = _settings(env, body).json()
    assert "mic_applied" not in answer
    assert env.robot.calls == [] and env.conn.control_reads == [] and env.conn.volume_writes == []


def test_a_refused_press_never_calls_the_robot(env, monkeypatch):
    _robot_reports(env)
    assert _settings(env, {"mic_muted": "true"}).status_code == 422
    env.conn.device = None
    assert _settings(env, {"mic_muted": True}).status_code == 409

    async def no_consent(u_id):
        return {}

    env.conn.device = {"device_id": DEVICE_ID, "auto_record": False, "live_voice": False}
    monkeypatch.setattr(consent_service, "get_state", no_consent)
    assert _settings(env, {"mic_muted": True}).status_code == 403
    assert env.robot.calls == []


def test_the_control_key_never_reaches_an_answer_or_a_log(env, caplog):
    _robot_reports(env)
    with caplog.at_level("DEBUG"):
        for muted in (True, False):
            assert CONTROL_KEY not in _settings(env, {"mic_muted": muted}).text
        env.robot.answer = (None, "HTTP 401")
        assert CONTROL_KEY not in _settings(env, {"mic_muted": True}).text
        assert CONTROL_KEY not in env.client.get("/api/reachy/status").text
    assert not any(CONTROL_KEY in record.getMessage() or "192.168.137.104" in record.getMessage()
                   for record in caplog.records)


# ── The schema and LIVE_VOICE_TEST ───────────────────────────────────────────

def test_the_schema_adds_the_live_voice_columns_and_table():
    sql = (REPO / "sql" / "init.sql").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert 'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS reachy_call_name VARCHAR(40);' in sql
    assert "ALTER TABLE reachy_device ADD COLUMN IF NOT EXISTS live_voice BOOLEAN NOT NULL DEFAULT FALSE;" in sql
    assert ("ALTER TABLE conversation ADD COLUMN IF NOT EXISTS mode VARCHAR(10) NOT NULL DEFAULT 'text'\n"
            "    CHECK (mode IN ('text','live'));") in sql
    assert "ALTER TABLE conversation ADD COLUMN IF NOT EXISTS live JSONB;" in sql
    start = sql.index("CREATE TABLE IF NOT EXISTS ai_live (")
    assert sql.index("CREATE TABLE IF NOT EXISTS ai_connection (") < start   # its foreign key needs that table
    table = " ".join(re.sub(r"--[^\n]*", "", sql[start:sql.index(");", start)]).split())
    for column in ("id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),",
                   "connection_id BIGINT REFERENCES ai_connection(connection_id),",
                   "model_id VARCHAR(100) NOT NULL DEFAULT 'gpt-live-1',",
                   "voice VARCHAR(20) NOT NULL DEFAULT 'marin',",
                   "tested_at TIMESTAMPTZ,", "test_ms INTEGER,", "last_error TEXT,",
                   "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"):
        assert column in table
    assert "ON DELETE" not in table   # a connection the live row uses cannot be deleted


@pytest.mark.parametrize("value, expected", [
    (None, False), ("", False), ("0", False), ("off", False), ("1", True), ("true", True), (" Yes ", True),
])
def test_live_voice_test_is_off_unless_the_env_says_1_true_or_yes(value, expected):
    clean = {key: item for key, item in os.environ.items() if key != "LIVE_VOICE_TEST"}
    if value is not None:
        clean["LIVE_VOICE_TEST"] = value
    out = subprocess.run([sys.executable, "-c", "import json, app.config as c; print(json.dumps(c.LIVE_VOICE_TEST))"],
                         cwd=REPO, env=clean, capture_output=True, text=True, check=True)
    assert json.loads(out.stdout.strip().splitlines()[-1]) is expected


def test_env_example_documents_the_live_voice_test_switch_off():
    lines = (REPO / ".env.example").read_text(encoding="utf-8").splitlines()
    assert "LIVE_VOICE_TEST=0" in lines


def test_a_new_call_name_renders_the_opening_ahead(env, monkeypatch):
    from app.services import voice_lines
    ahead = []
    monkeypatch.setattr(voice_lines, "render_ahead_for_patient", lambda u_id: ahead.append(u_id))
    assert _settings(env, {"auto_record": True}).status_code == 200
    assert ahead == []                                        # no name in the request: the opening is unchanged
    assert _settings(env, {"call_name": "阿嬤"}).status_code == 200
    assert ahead == [7]
    assert _settings(env, {"call_name": ""}).status_code == 200    # back to the registered name: a new opening too
    assert ahead == [7, 7]
    assert _settings(env, {"call_name": "我不想活了"}).status_code == 422
    assert ahead == [7, 7]                                    # nothing saved, nothing rendered
