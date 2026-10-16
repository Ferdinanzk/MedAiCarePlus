"""The [STARTUP] report (app/startup_report.py) and the database retry (app/database.py): what the server says as it
comes up after a reboot, with the real reason for each stage that is not fine, and never a secret value."""

import asyncio
import json
import types
from datetime import datetime, timedelta, timezone

import pytest

from app import config, database, startup_report
from app.services import ai_settings


def _snapshot(*, live=None, connections=None):
    return types.SimpleNamespace(live=live, connections=connections or {}, models={}, jobs={})


def _live(**kw):
    base = {"connection_id": 2, "model_id": "gpt-live-1", "voice": "marin", "tested_at": None, "test_ms": None,
            "last_error": None}
    base.update(kw)
    return types.SimpleNamespace(**base)


def _connection(**kw):
    base = {"kind": "openai", "key_readable": True, "api_key": "sk-x"}
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def fresh():
    startup_report.begin()
    yield
    startup_report._report.clear()


def test_the_environment_line_names_switches_and_whether_secrets_are_set_never_their_values(monkeypatch):
    monkeypatch.setattr(config, "LINE_CHANNEL_ACCESS_TOKEN", "LINE-SECRET-VALUE")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "SECRET_KEY", startup_report.DEFAULT_SECRET)
    line = startup_report.environment(["LIVE_VOICE_TEST must be off"])
    assert "LINE-SECRET-VALUE" not in line and "LINE_CHANNEL_ACCESS_TOKEN=set" in line and "GEMINI_API_KEY=EMPTY" in line
    assert "LIVE_VOICE_TEST=" in line and "SECRET_KEY is the default" in line and "LIVE_VOICE_TEST must be off" in line


@pytest.mark.parametrize("live_test, live, connections, status, words", [
    (False, None, {}, "off", "LIVE_VOICE_TEST is off"),
    (True, None, {}, "no_model", "no live model saved"),
    (True, _live(), {}, "no_connection", "connection 2 is gone"),
    (True, _live(), {2: _connection(kind="openrouter")}, "wrong_kind", "not OpenAI"),
    (True, _live(), {2: _connection(key_readable=False, api_key=None)}, "key_unreadable", "SECRET_KEY changed"),
    (True, _live(), {2: _connection(api_key="")}, "no_key", "has no key"),
    (True, _live(last_error="live_auth"), {2: _connection()}, "check_failed", "last Check failed (live_auth)"),
    (True, _live(), {2: _connection()}, "no_check", "no passing Check saved"),
    (True, _live(tested_at=datetime(2026, 10, 14, 5, 49, tzinfo=timezone.utc), test_ms=2918), {2: _connection()},
     "ok", "Check passed at"),
])
def test_gpt_live_says_exactly_why_live_voice_is_off(monkeypatch, live_test, live, connections, status, words):
    monkeypatch.setattr(config, "LIVE_VOICE_TEST", live_test)
    got, detail = startup_report.gpt_live(_snapshot(live=live, connections=connections))
    assert got == status and words in detail and "sk-x" not in detail


def test_marks_build_the_health_summary_with_degraded_and_pending_stages():
    startup_report.mark("database", True, "postgres answered on attempt 1")
    startup_report.mark("line", False, "no public link", warn=True)
    startup_report.mark("gpt_live", False, "no passing Check saved", pending=True)
    report = startup_report.summary()
    assert report["ready"] and report["degraded"] == ["line", "gpt_live"] and report["pending"] == ["gpt_live"]
    assert report["stages"]["database"] == {"ok": True, "detail": "postgres answered on attempt 1", "pending": False}
    assert json.dumps(report)   # what /health returns


def test_the_clock_skew_reads_the_date_header():
    now = datetime.now(timezone.utc)
    header = (now - timedelta(seconds=90)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert 85 <= startup_report.clock_skew(header) <= 95
    assert startup_report.clock_skew(None) is None and startup_report.clock_skew("garbage") is None


def test_network_and_clock_probes_report_each_host_and_the_skew(monkeypatch):
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")

    def probe(url):
        if "openai" in url:
            return {"ok": True, "status": 401, "ms": 120, "date": now}
        return {"ok": False, "error": "ConnectionError: name resolution failed"}

    monkeypatch.setattr(startup_report, "probe", probe)
    asyncio.run(startup_report.network_and_clock())
    report = startup_report.summary()["stages"]
    assert not report["network"]["ok"] and "line UNREACHABLE" in report["network"]["detail"]
    assert "openai reachable in 120 ms" in report["network"]["detail"]
    assert report["clock"]["ok"] and "against the internet" in report["clock"]["detail"]


def test_the_automatic_check_runs_only_without_a_passing_check_and_saves_its_result(monkeypatch):
    live = _live()
    snapshot = _snapshot(live=live, connections={2: _connection(api_key="sk-live")})
    checks, saved = [], []

    async def check(api_key, model, voice):
        checks.append((api_key, model, voice))
        return (False, None, "live_unavailable") if len(checks) == 1 else (True, 1500, None)

    async def save_live_check(conn, ok, ms, error):
        saved.append((ok, ms, error))

    class Change:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Pool:
        def acquire(self):
            return Change()

    monkeypatch.setattr(startup_report, "LIVE_CHECK_RETRY_SECONDS", 0)
    monkeypatch.setattr(ai_settings, "current", lambda: snapshot)
    from app.services import ai_store, live_client
    monkeypatch.setattr(live_client, "check", check)
    monkeypatch.setattr(ai_store, "save_live_check", save_live_check)
    monkeypatch.setattr(ai_store, "change", lambda conn: Change())
    monkeypatch.setattr(database, "_pool", Pool())
    asyncio.run(startup_report.auto_check_live(snapshot))
    assert checks == [("sk-live", "gpt-live-1", "marin")] * 2 and saved == [(True, 1500, None)]
    stage = startup_report.summary()["stages"]["gpt_live"]
    assert stage["ok"] and "Check passed now (1500 ms)" in stage["detail"]


def test_a_refused_key_is_not_retried_and_says_so(monkeypatch):
    live = _live()
    snapshot = _snapshot(live=live, connections={2: _connection()})
    calls = []

    async def check(api_key, model, voice):
        calls.append(1)
        return False, None, "live_auth"

    async def save_live_check(conn, ok, ms, error):
        pass

    class Change:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Pool:
        def acquire(self):
            return Change()

    monkeypatch.setattr(ai_settings, "current", lambda: snapshot)
    from app.services import ai_store, live_client
    monkeypatch.setattr(live_client, "check", check)
    monkeypatch.setattr(ai_store, "save_live_check", save_live_check)
    monkeypatch.setattr(ai_store, "change", lambda conn: Change())
    monkeypatch.setattr(database, "_pool", Pool())
    asyncio.run(startup_report.auto_check_live(snapshot))
    assert calls == [1]
    assert "OpenAI key was refused" in startup_report.summary()["stages"]["gpt_live"]["detail"]


# ── the database retry ──

def test_init_pool_retries_until_postgres_answers(monkeypatch):
    attempts, slept = [], []

    async def create_pool(url, **kw):
        attempts.append(url)
        if len(attempts) < 3:
            raise ConnectionRefusedError("connection refused")
        return Pool()

    class Conn:
        async def execute(self, sql):
            pass

    class Acquire:
        async def __aenter__(self):
            return Conn()

        async def __aexit__(self, *exc):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(database.asyncpg, "create_pool", create_pool, raising=False)
    assert asyncio.run(database.init_pool(attempts=5, retry_seconds=2.0, sleep=sleep)) == 3
    assert len(attempts) == 3 and slept == [2.0, 2.0]
    database._pool = None


def test_init_pool_gives_up_with_the_real_reason(monkeypatch):
    async def create_pool(url, **kw):
        raise ConnectionRefusedError("connection refused")

    async def sleep(seconds):
        pass

    monkeypatch.setattr(database.asyncpg, "create_pool", create_pool, raising=False)
    with pytest.raises(RuntimeError, match="postgres did not answer in 2 attempts: ConnectionRefusedError"):
        asyncio.run(database.init_pool(attempts=2, retry_seconds=0, sleep=sleep))
    database._pool = None
