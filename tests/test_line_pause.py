import asyncio
import datetime
import json
import sys
import types

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.services import line_pause

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
LATER = NOW + datetime.timedelta(hours=1)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class SwitchDB:
    """Records the ops_state / line_switch_log statements the switch runs."""

    def __init__(self, stored=None):
        self.ops_state = {} if stored is None else {line_pause.STATE_KEY: stored}
        self.log = []

    def transaction(self):
        return _Transaction()

    async def fetchval(self, query, *args):
        assert "FROM ops_state" in query
        return self.ops_state.get(args[0])

    async def execute(self, query, *args):
        if "INSERT INTO line_switch_log" in query:
            self.log.append(args)
        elif "INSERT INTO ops_state" in query:
            assert "ON CONFLICT (key) DO NOTHING" in query
            self.ops_state.setdefault(args[0], args[1])
        elif "DELETE FROM ops_state" in query:
            self.ops_state.pop(args[0], None)
        else:
            raise AssertionError(query)


@pytest.fixture(autouse=True)
def _reset():
    line_pause._state.update(paused=False, since=None, by=None)
    yield
    line_pause._state.update(paused=False, since=None, by=None)


def run(coro):
    return asyncio.run(coro)


def test_blocks_only_med_reminder_while_paused():
    conn = SwitchDB()
    assert line_pause.blocks("med_reminder") is False
    run(line_pause.set_paused(conn, True, 7, NOW))
    assert line_pause.blocks("med_reminder") is True
    assert line_pause.blocks("other") is False
    run(line_pause.set_paused(conn, False, 7, NOW))
    assert line_pause.blocks("med_reminder") is False


def test_set_paused_writes_log_and_state():
    conn = SwitchDB()
    result = run(line_pause.set_paused(conn, True, 7, NOW))
    assert conn.log == [(True, 7, NOW)]
    assert json.loads(conn.ops_state[line_pause.STATE_KEY]) == {"since": NOW.isoformat(), "by": 7}
    assert result == {"paused": True, "since": NOW, "by": 7}
    run(line_pause.set_paused(conn, False, 7, LATER))
    assert conn.log == [(True, 7, NOW), (False, 7, LATER)]
    assert line_pause.STATE_KEY not in conn.ops_state
    assert line_pause.state() == {"paused": False, "since": None, "by": None}


def test_load_restores_after_restart():
    stored = json.dumps({"since": NOW.isoformat(), "by": 7})
    run(line_pause.load(SwitchDB(stored)))
    assert line_pause.state() == {"paused": True, "since": NOW, "by": 7}
    run(line_pause.load(SwitchDB()))
    assert line_pause.state() == {"paused": False, "since": None, "by": None}


def test_pausing_twice_keeps_first_since():
    conn = SwitchDB()
    run(line_pause.set_paused(conn, True, 7, NOW))
    result = run(line_pause.set_paused(conn, True, 8, LATER))
    assert result == {"paused": True, "since": NOW, "by": 7}
    assert json.loads(conn.ops_state[line_pause.STATE_KEY]) == {"since": NOW.isoformat(), "by": 7}
    assert len(conn.log) == 2
    assert conn.log[1] == (True, 8, LATER)


def test_failed_write_leaves_memory_unchanged():
    class Broken(SwitchDB):
        async def execute(self, query, *args):
            raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        run(line_pause.set_paused(Broken(), True, 7, NOW))
    assert line_pause.blocks("med_reminder") is False


def test_admin_user_ids_parsing(monkeypatch, caplog):
    import importlib
    from app import config
    try:
        assert config.parse_admin_user_ids("7, 12") == frozenset({7, 12})
        assert config.parse_admin_user_ids("") == frozenset()
        with caplog.at_level("WARNING", logger="app.config"):
            assert config.parse_admin_user_ids("abc, 0, -3, 5, 1.5, ") == frozenset({5})
        warnings = [r.getMessage() for r in caplog.records if r.name == "app.config"]
        assert warnings == ["ADMIN_USER_IDS: ignored an entry that is not an account number"] * 4
        monkeypatch.setenv("ADMIN_USER_IDS", " 7 ,x, 12")
        importlib.reload(config)
        assert config.ADMIN_USER_IDS == frozenset({7, 12})
        monkeypatch.delenv("ADMIN_USER_IDS")
        importlib.reload(config)
        assert config.ADMIN_USER_IDS == frozenset()
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def _missed_alert_texts(**kwargs):
    from app.services.line_service import LineService
    service = LineService.__new__(LineService)
    sent = []
    service.send_text = lambda to, text, *, kind: sent.append((text, kind)) or {}
    service.send_missed_dose_alert("UF", "Pearl", "Med 1", "2026-10-16 18:05", **kwargs)
    return sent


def test_missed_alert_to_family_says_when_reminders_were_paused(monkeypatch):
    """Final review F2: family are told the patient got no LINE reminder; the alert itself is never paused."""
    note = "（管理員已暫停 LINE 用藥提醒，Pearl 沒有收到 LINE 提醒。）"
    ((text, kind),) = _missed_alert_texts()
    assert note not in text and kind == line_pause.OTHER
    monkeypatch.setitem(line_pause._state, "paused", True)
    ((text, kind),) = _missed_alert_texts()
    assert kind == line_pause.OTHER and note in text
    assert text.index("預定時間：") < text.index(note) < text.index("請關心確認狀況。")
    ((text, _),) = _missed_alert_texts(offline=True)
    assert text.index("系統離線") < text.index(note)
