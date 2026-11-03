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


def test_admin_emails_parsing(monkeypatch):
    import importlib
    from app import config
    monkeypatch.setenv("ADMIN_EMAILS", " A@x.com, ,b@Y.com ")
    importlib.reload(config)
    assert config.ADMIN_EMAILS == frozenset({"a@x.com", "b@y.com"})
    monkeypatch.delenv("ADMIN_EMAILS")
    importlib.reload(config)
    assert config.ADMIN_EMAILS == frozenset()
