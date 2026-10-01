import asyncio
import datetime
import json
import sys
import types
import uuid

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.services import line_service, outbox, outbox_dispatcher

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 30, 8, 0, tzinfo=UTC)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class OutboxDB:
    """In-memory notification_outbox that understands the dispatcher's statements."""

    def __init__(self):
        self.rows = {}
        self.next_id = 1

    def add(self, *, priority=1, status="queued", attempts=0, next_attempt_at=NOW, line_id="U-family",
            messages=None):
        outbox_id = self.next_id
        self.next_id += 1
        self.rows[outbox_id] = {
            "outbox_id": outbox_id, "recipient_line_id": line_id, "priority": priority,
            "payload": json.dumps({"messages": messages or [{"type": "text", "text": f"m{outbox_id}"}]}),
            "status": status, "attempts": attempts, "next_attempt_at": next_attempt_at,
            "line_request_id": None, "last_error": None, "accepted_at": None,
        }
        return outbox_id

    def transaction(self):
        return _Transaction()

    async def fetch(self, query, *args):
        assert "FOR UPDATE SKIP LOCKED" in query
        now, limit = args
        due = [dict(r) for r in self.rows.values()
               if r["status"] in ("queued", "failed") and r["next_attempt_at"] <= now]
        due.sort(key=lambda r: (r["priority"], r["next_attempt_at"]))
        return due[:limit]

    async def execute(self, query, *args):
        if "status='sending'" in query and query.lstrip().startswith("UPDATE") and "SET status='sending'" in query:
            for outbox_id in args[0]:
                self.rows[outbox_id]["status"] = "sending"
            return "UPDATE"
        if "SET status='accepted'" in query:
            outbox_id, request_id, accepted_at = args
            row = self.rows[outbox_id]
            row.update(status="accepted", line_request_id=request_id, accepted_at=accepted_at,
                       attempts=row["attempts"] + 1, last_error=None)
            return "UPDATE"
        if "SET status='failed'" in query:
            outbox_id, attempts, next_attempt_at, error = args
            self.rows[outbox_id].update(status="failed", attempts=attempts, next_attempt_at=next_attempt_at,
                                        last_error=error)
            return "UPDATE"
        if "SET status='queued'" in query:
            ids = args[0] if args else None
            count = 0
            for row in self.rows.values():
                if row["status"] == "sending" and (ids is None or row["outbox_id"] in ids):
                    row["status"] = "queued"
                    count += 1
            return f"UPDATE {count}"
        raise AssertionError(f"unexpected query: {query}")


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class FakeLine:
    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    def push_messages(self, to, messages, retry_key):
        self.calls.append((to, messages, retry_key))
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return {"status": "accepted", "http_status": 200, "request_id": f"req-{len(self.calls)}", "error": None}


@pytest.fixture
def db(monkeypatch):
    database = OutboxDB()
    monkeypatch.setattr(outbox_dispatcher, "get_pool", lambda: _Pool(database))
    return database


def _use_line(monkeypatch, fake):
    monkeypatch.setattr(outbox_dispatcher.LineService, "get_instance", classmethod(lambda cls: fake))


def _key(outbox_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"outbox:{outbox_id}"))


def test_retry_key_is_uuid5_of_outbox_id():
    assert outbox_dispatcher.retry_key(42) == _key(42)


def test_backoff_schedule_by_priority():
    assert [outbox_dispatcher.backoff_seconds(n, 0) for n in range(1, 7)] == [5, 15, 30, 60, 60, 60]
    assert [outbox_dispatcher.backoff_seconds(n, 1) for n in range(1, 7)] == [5, 15, 30, 600, 600, 600]


def test_dispatch_sends_due_rows_by_priority_and_marks_accepted(db, monkeypatch):
    normal = db.add(priority=1, next_attempt_at=NOW - datetime.timedelta(minutes=5))
    urgent = db.add(priority=0, next_attempt_at=NOW)
    future = db.add(priority=0, next_attempt_at=NOW + datetime.timedelta(seconds=1))
    done = db.add(status="accepted")
    fake = FakeLine()
    _use_line(monkeypatch, fake)

    assert asyncio.run(outbox_dispatcher.dispatch_once(now=NOW)) == 2

    assert [call[2] for call in fake.calls] == [_key(urgent), _key(normal)]
    assert fake.calls[0][0] == "U-family"
    assert fake.calls[0][1] == [{"type": "text", "text": f"m{urgent}"}]
    for outbox_id in (urgent, normal):
        assert db.rows[outbox_id]["status"] == "accepted"
        assert db.rows[outbox_id]["accepted_at"] is not None
        assert db.rows[outbox_id]["line_request_id"].startswith("req-")
    assert db.rows[future]["status"] == "queued"
    assert db.rows[done]["status"] == "accepted"


def test_duplicate_counts_as_accepted(db, monkeypatch):
    row = db.add()
    _use_line(monkeypatch, FakeLine([{"status": "duplicate", "http_status": 409, "request_id": "orig",
                                      "error": None}]))
    asyncio.run(outbox_dispatcher.dispatch_once(now=NOW))
    assert db.rows[row]["status"] == "accepted"
    assert db.rows[row]["line_request_id"] == "orig"


@pytest.mark.parametrize("priority, attempts, delay", [(1, 0, 5), (1, 1, 15), (1, 2, 30), (1, 3, 600), (0, 3, 60),
                                                        (0, 9, 60)])
def test_failure_backs_off(db, monkeypatch, priority, attempts, delay):
    row = db.add(priority=priority, attempts=attempts)
    _use_line(monkeypatch, FakeLine([{"status": "failed", "http_status": 500, "request_id": None,
                                      "error": "LINE API 500: boom"}]))
    asyncio.run(outbox_dispatcher.dispatch_once(now=NOW))
    stored = db.rows[row]
    assert stored["status"] == "failed"
    assert stored["attempts"] == attempts + 1
    assert stored["next_attempt_at"] == NOW + datetime.timedelta(seconds=delay)
    assert stored["last_error"] == "LINE API 500: boom"


def test_disabled_line_leaves_row_failed_with_backoff(db, monkeypatch):
    row = db.add()
    monkeypatch.setattr(line_service.LineService, "_available", False)
    monkeypatch.setattr(line_service.LineService, "_instance", line_service.LineService.__new__(
        line_service.LineService))
    asyncio.run(outbox_dispatcher.dispatch_once(now=NOW))
    stored = db.rows[row]
    assert stored["status"] == "failed"
    assert stored["attempts"] == 1
    assert stored["next_attempt_at"] == NOW + datetime.timedelta(seconds=5)
    assert stored["last_error"] == "LINE not configured"


def test_send_exception_is_recorded_as_failure(db, monkeypatch):
    row = db.add()
    _use_line(monkeypatch, FakeLine([RuntimeError("socket closed")]))
    asyncio.run(outbox_dispatcher.dispatch_once(now=NOW))
    assert db.rows[row]["status"] == "failed"
    assert "socket closed" in db.rows[row]["last_error"]


def test_crash_between_commit_and_send_resends_once_with_same_retry_key(db, monkeypatch):
    """Review focus 5: the row was committed as 'sending' but the process died before the
    result was recorded. On restart it goes back to 'queued' and is sent exactly once more
    with the same X-Line-Retry-Key, so LINE can de-duplicate."""
    row = db.add(priority=0)

    class Crash(BaseException):
        pass

    first = FakeLine([Crash()])
    _use_line(monkeypatch, first)
    with pytest.raises(Crash):
        asyncio.run(outbox_dispatcher.dispatch_once(now=NOW))
    assert db.rows[row]["status"] == "sending"
    assert [call[2] for call in first.calls] == [_key(row)]

    # Nothing is picked up while the row is stuck in 'sending' …
    second = FakeLine([{"status": "duplicate", "http_status": 409, "request_id": "orig", "error": None}])
    _use_line(monkeypatch, second)
    assert asyncio.run(outbox_dispatcher.dispatch_once(now=NOW)) == 0
    # … until the restart recovery runs.
    assert asyncio.run(outbox_dispatcher.recover_stuck()) == 1
    assert db.rows[row]["status"] == "queued"
    assert asyncio.run(outbox_dispatcher.dispatch_once(now=NOW)) == 1
    assert asyncio.run(outbox_dispatcher.dispatch_once(now=NOW)) == 0
    assert [call[2] for call in second.calls] == [_key(row)]
    assert db.rows[row]["status"] == "accepted"


def test_stop_request_releases_unsent_claimed_rows(db, monkeypatch):
    first = db.add()
    second = db.add()

    class StoppingLine(FakeLine):
        def push_messages(self, to, messages, retry_key):
            outbox_dispatcher._stopping = True
            return super().push_messages(to, messages, retry_key)

    fake = StoppingLine()
    _use_line(monkeypatch, fake)
    monkeypatch.setattr(outbox_dispatcher, "_stopping", False)
    asyncio.run(outbox_dispatcher.dispatch_once(now=NOW))
    assert len(fake.calls) == 1
    assert db.rows[first]["status"] == "accepted"
    assert db.rows[second]["status"] == "queued"


def test_start_recovers_then_dispatches_and_stop_is_clean(db, monkeypatch):
    stuck = db.add(status="sending", next_attempt_at=datetime.datetime.now(UTC) - datetime.timedelta(seconds=1))
    fake = FakeLine()
    _use_line(monkeypatch, fake)

    async def scenario():
        outbox._wake = None
        await outbox_dispatcher.start_dispatcher()
        for _ in range(100):
            if db.rows[stuck]["status"] == "accepted":
                break
            await asyncio.sleep(0.01)
        fresh = db.add(priority=0, next_attempt_at=datetime.datetime.now(UTC) - datetime.timedelta(seconds=1))
        outbox.wake()
        for _ in range(100):
            if db.rows[fresh]["status"] == "accepted":
                break
            await asyncio.sleep(0.01)
        started = asyncio.get_running_loop().time()
        await outbox_dispatcher.stop_dispatcher()
        return fresh, asyncio.get_running_loop().time() - started

    fresh, stop_elapsed = asyncio.run(scenario())
    assert db.rows[stuck]["status"] == "accepted"
    assert db.rows[fresh]["status"] == "accepted"
    assert [call[2] for call in fake.calls] == [_key(stuck), _key(fresh)]
    assert stop_elapsed < 1.0
    assert outbox_dispatcher._task is None


def test_push_messages_sends_retry_key_and_maps_statuses(monkeypatch):
    sent = []

    class Response:
        def __init__(self, status, headers=None, text=""):
            self.status_code = status
            self.headers = headers or {}
            self.text = text

    responses = [Response(200, {"x-line-request-id": "r1"}),
                 Response(409, {"x-line-request-id": "r2", "x-line-accepted-request-id": "r1"}),
                 Response(429, {"x-line-request-id": "r3"}, "rate limited")]

    def post(url, json=None, headers=None, timeout=None):
        sent.append((url, json, headers))
        return responses.pop(0)

    monkeypatch.setattr(line_service.requests, "post", post)
    monkeypatch.setattr(line_service.LineService, "_available", True)
    monkeypatch.setattr(line_service, "LINE_CHANNEL_ACCESS_TOKEN", "token")
    service = line_service.LineService.__new__(line_service.LineService)
    messages = [{"type": "text", "text": "hi"}]

    first = service.push_messages("U1", messages, "key-1")
    assert first == {"status": "accepted", "http_status": 200, "request_id": "r1", "error": None}
    assert sent[0][0] == line_service.LINE_API_URL
    assert sent[0][1] == {"to": "U1", "messages": messages}
    assert sent[0][2]["X-Line-Retry-Key"] == "key-1"
    assert sent[0][2]["Authorization"] == "Bearer token"

    duplicate = service.push_messages("U1", messages, "key-1")
    assert duplicate["status"] == "duplicate"
    assert duplicate["request_id"] == "r1"

    failed = service.push_messages("U1", messages, "key-2")
    assert failed["status"] == "failed"
    assert failed["http_status"] == 429
    assert "rate limited" in failed["error"]


def test_push_messages_disabled_without_token(monkeypatch):
    monkeypatch.setattr(line_service.LineService, "_available", False)
    service = line_service.LineService.__new__(line_service.LineService)
    result = service.push_messages("U1", [], "key")
    assert result["status"] == "disabled"
