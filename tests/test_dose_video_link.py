"""Dose-video links must reach this server (16 Oct 2026, plan 2026-10-16-agent-tool-fixes, Task 1).

Two computers share one LINE bot, and every video link was built from the bot's one webhook address. The old probe
accepted any answer below 500, and the LINE proxy answers 404 itself, so links went to the other computer's server,
which has no such clip. Now the probe asks for this server's own probe token, a clip kept unsent for lack of a link
says why in one WARNING, and the dose_videos job sends it once a link answers while its family request is open."""

import asyncio
import datetime
import logging
import re
import sys
import types
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config, startup_report   # noqa: E402
from app.routers import api_dose_video   # noqa: E402
from app.services import dose_video, outbox   # noqa: E402

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
BASE = "https://x.trycloudflare.com"
LINE_TOKEN = "line-channel-token"
ROOT = Path(__file__).resolve().parents[1]


class Clock:
    """dose_video's own monotonic clock (asyncio keeps the real one)."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def time(self):
        import time
        return time.time()


@pytest.fixture(autouse=True)
def fresh_link(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(dose_video, "time", clock)
    monkeypatch.setattr(dose_video, "_base_url", None)
    monkeypatch.setattr(dose_video, "_link_reason", None)
    monkeypatch.setattr(dose_video, "_link_checked", False)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(config, "LINE_CHANNEL_ACCESS_TOKEN", LINE_TOKEN)
    dose_video._buffers.clear()
    yield clock
    dose_video._buffers.clear()


class Answer:
    def __init__(self, status, body=b"", payload=None):
        self.status_code, self.content, self._payload = status, body, payload

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class Internet:
    """requests.get as dose_video sees it: LINE's webhook endpoint API, then the tunnel's answer to the probe."""

    def __init__(self, monkeypatch, tunnel=None, line=None):
        self.tunnel = tunnel if tunnel is not None else Answer(200, dose_video.PROBE_MARKER)
        self.line = line if line is not None else Answer(200, payload={"endpoint": f"{BASE}/api/notify/webhook/line"})
        self.calls = []
        monkeypatch.setattr(requests, "get", self.get)

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        answer = self.line if url == config.LINE_WEBHOOK_ENDPOINT_URL else self.tunnel
        if isinstance(answer, Exception):
            raise answer
        return answer

    def probes(self):
        return [url for url, _ in self.calls if url != config.LINE_WEBHOOK_ENDPOINT_URL]


def _check():
    return asyncio.run(dose_video.public_base_url()), dose_video.no_link_reason()


# ── the probe token and its route ──

def test_the_probe_token_has_the_shape_of_a_real_link_token_so_the_line_proxy_forwards_it():
    token = dose_video.PROBE_TOKEN
    assert len(token) == dose_video.TOKEN_LENGTH and api_dose_video._NAME.fullmatch(f"{token}.jpg")
    conf = (ROOT / "scripts" / "line-webhook" / "nginx.conf").read_text(encoding="utf-8")
    forwarded = re.search(r'location ~ "(\^/api/media/line/[^"]+)"', conf).group(1)
    assert re.match(forwarded, f"/api/media/line/{token}.jpg")


@pytest.fixture
def media(monkeypatch):
    from app.main import app

    opened = []

    async def open_link(token, kind):
        opened.append((token, kind))
        return None

    async def mark_fetched(link_id):
        raise AssertionError("the probe is no download")

    monkeypatch.setattr(dose_video, "open_link", open_link)
    monkeypatch.setattr(dose_video, "mark_fetched", mark_fetched)
    return types.SimpleNamespace(client=TestClient(app), app=app, opened=opened)


def test_the_probe_route_answers_only_this_servers_own_token(media):
    token = dose_video.PROBE_TOKEN
    response = media.client.get(f"/api/media/line/{token}.jpg")
    assert response.status_code == 200 and response.content == dose_video.PROBE_MARKER
    assert response.headers["cache-control"] == "no-store"
    assert media.opened == []                                   # never looked up as a clip
    other = ("B" if token[0] != "B" else "C") + token[1:]       # another server's probe, or a guess
    assert media.client.get(f"/api/media/line/{other}.jpg").status_code == 404
    assert media.client.get(f"/api/media/line/{token}.mp4").status_code == 404
    assert media.opened == [(other, "jpg"), (token, "mp4")]    # unknown tokens keep today's answer
    robot_port = TestClient(media.app, base_url=f"http://testserver:{config.DEVICE_PORT}")
    assert robot_port.get(f"/api/media/line/{token}.jpg").status_code == 404


# ── what counts as a public link ──

def test_the_probe_asks_the_tunnel_for_this_servers_token_and_accepts_the_marker(monkeypatch):
    internet = Internet(monkeypatch)
    assert _check() == (BASE, None)
    assert internet.probes() == [f"{BASE}/api/media/line/{dose_video.PROBE_TOKEN}.jpg"]
    assert internet.calls[1][1]["allow_redirects"] is False and internet.calls[1][1]["timeout"] <= 5


@pytest.mark.parametrize("tunnel, reason", [
    (Answer(404, b"Not Found"), dose_video.OTHER_SERVER),               # the LINE proxy, or another computer's server
    (Answer(200, b"\xff\xd8 a picture"), dose_video.OTHER_SERVER),       # something else answers 200
    (Answer(530, b"tunnel error"), dose_video.TUNNEL_DOWN),              # a quick tunnel that restarted
    (Answer(502, b"Bad Gateway"), dose_video.TUNNEL_DOWN),
    (requests.ConnectionError("no route"), dose_video.TUNNEL_DOWN),
    (requests.Timeout("slow"), dose_video.TUNNEL_DOWN),
])
def test_the_probe_rejects_anything_but_this_servers_marker(monkeypatch, tunnel, reason):
    Internet(monkeypatch, tunnel=tunnel)
    assert _check() == (None, reason)


@pytest.mark.parametrize("line, reason", [
    (Answer(401, b"unauthorized"), dose_video.WEBHOOK_ERROR),
    (Answer(200, b"<html>"), dose_video.WEBHOOK_ERROR),                                       # not JSON
    (Answer(200, payload={"endpoint": "http://x.trycloudflare.com/api/notify/webhook/line"}), dose_video.WEBHOOK_ERROR),
    (Answer(200, payload={}), dose_video.WEBHOOK_ERROR),
    (requests.ConnectionError("no route"), dose_video.WEBHOOK_ERROR),
])
def test_without_a_usable_webhook_address_there_is_no_link(monkeypatch, line, reason):
    internet = Internet(monkeypatch, line=line)
    assert _check() == (None, reason) and internet.probes() == []


def test_without_a_line_token_nothing_is_asked(monkeypatch):
    monkeypatch.setattr(config, "LINE_CHANNEL_ACCESS_TOKEN", "")
    internet = Internet(monkeypatch)
    assert _check() == (None, dose_video.NO_LINE_TOKEN) and internet.calls == []


def test_the_cache_times_are_kept(monkeypatch, fresh_link):
    internet = Internet(monkeypatch, tunnel=Answer(404))
    assert _check()[0] is None
    fresh_link.now += dose_video.BASE_URL_RETRY_SECONDS - 1
    assert _check()[0] is None and len(internet.probes()) == 1     # a failure is asked again after a minute
    internet.tunnel = Answer(200, dose_video.PROBE_MARKER)
    fresh_link.now += 2
    assert _check()[0] == BASE and len(internet.probes()) == 2
    fresh_link.now += dose_video.BASE_URL_CACHE_SECONDS - 1
    assert _check()[0] == BASE and len(internet.probes()) == 2     # a link is trusted for five minutes


def test_one_warning_per_reason_never_on_every_call_and_never_the_token(monkeypatch, caplog):
    internet = Internet(monkeypatch, tunnel=Answer(404))
    caplog.set_level(logging.INFO, logger=dose_video.log.name)

    def check(times):
        for _ in range(times):
            monkeypatch.setattr(dose_video, "_base_url", None)      # past the cache: asked every time
            _check()

    check(3)
    internet.tunnel = requests.ConnectionError(f"HTTPSConnectionPool: /api/media/line/{dose_video.PROBE_TOKEN}.jpg")
    check(2)
    internet.tunnel = Answer(200, dose_video.PROBE_MARKER)
    check(2)
    internet.tunnel = Answer(404)
    check(1)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 3 and len(internet.probes()) == 8
    assert dose_video.OTHER_SERVER in warnings[0] and dose_video.TUNNEL_DOWN in warnings[1]
    assert dose_video.OTHER_SERVER in warnings[2]
    assert [r.levelno for r in caplog.records].count(logging.INFO) == 1    # the link answers again
    assert dose_video.PROBE_TOKEN not in caplog.text and LINE_TOKEN not in caplog.text


def test_a_working_link_at_the_first_check_logs_nothing(monkeypatch, caplog):
    Internet(monkeypatch)
    caplog.set_level(logging.INFO, logger=dose_video.log.name)
    assert _check() == (BASE, None) and caplog.records == []


def test_public_base_url_is_still_taken_as_configured(monkeypatch):
    internet = Internet(monkeypatch)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://videos.example.org")
    assert _check() == ("https://videos.example.org", None) and internet.calls == []
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "http://videos.example.org")
    assert _check() == (None, dose_video.NOT_HTTPS)


def test_the_startup_report_names_the_reason(monkeypatch):
    Internet(monkeypatch, tunnel=Answer(404))
    startup_report.begin()
    try:
        startup_report.mark("line", True, "token set")
        asyncio.run(startup_report.public_link())
        stage = startup_report._report["line"]
        assert not stage["ok"] and dose_video.OTHER_SERVER in stage["detail"]
        assert dose_video.PROBE_TOKEN not in stage["detail"]
    finally:
        startup_report._report.clear()


# ── a clip kept unsent, then sent by the dose_videos job ──

class Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class VideoDB:
    """The dose_video rows, one family request for intake 4 and one contact, answering the queries of _store,
    cleanup, the retry and send_with_confirmation."""

    def __init__(self, resolution=None):
        self.videos = {}
        self.resolution = resolution
        self.links = []
        self.cutoffs = []

    def transaction(self):
        return Tx()

    async def execute(self, query, *args):
        if query.startswith("INSERT INTO dose_video ("):
            self.videos[args[0]] = {"video_id": args[0], "u_id": args[1], "intk_ids": list(args[2]),
                                    "created_at": datetime.datetime.now(UTC), "sent_at": None, "deleted_at": None}
        elif query.startswith("INSERT INTO dose_video_link"):
            self.links.append(args[1])
        elif "SET sent_at = NOW()" in query:
            self.videos[args[0]]["sent_at"] = datetime.datetime.now(UTC)
        elif "SET deleted_at = NOW()" in query:
            self.videos[args[0]]["deleted_at"] = datetime.datetime.now(UTC)
        elif "UPDATE notification_outbox" not in query:
            raise AssertionError(query)

    async def fetch(self, query, *args):
        live = [row for row in self.videos.values() if row["deleted_at"] is None]
        if "LEFT JOIN dose_video_link" in query:                 # cleanup
            return [{**row, "expires_at": None, "links": 0, "waiting": 0, "last_done": None} for row in live]
        if "FROM dose_video v" in query:                          # the retry's candidates
            assert "sent_at IS NULL" in query and "deleted_at IS NULL" in query
            assert "resolution IS NULL" in query and "intk_ids && v.intk_ids" in query
            self.cutoffs.append(args[0])
            return [{"video_id": row["video_id"], "u_id": row["u_id"], "intk_ids": row["intk_ids"]} for row in live
                    if row["sent_at"] is None and row["created_at"] > args[0] and self.resolution is None
                    and 4 in row["intk_ids"]]
        if "FROM family_contacts" in query:
            return [{"id": 34, "name": "aisha", "line_id": "U34"}]
        raise AssertionError(query)

    async def fetchrow(self, query, *args):                      # send_with_confirmation: the open request
        assert "resolution IS NULL" in query
        return {"created_at": NOW, "name": "Aisha"} if self.resolution is None else None

    async def fetchval(self, query, *args):                      # the retry locks the clip and checks it again
        assert "FOR UPDATE" in query
        row = self.videos.get(args[0])
        return args[0] if row and row["sent_at"] is None and row["deleted_at"] is None else None


class Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return Ctx()


@pytest.fixture
def world(monkeypatch, tmp_path):
    db = VideoDB()
    enqueued = []

    async def enqueue(conn, **kwargs):
        enqueued.append(kwargs)
        return True

    async def enabled(u_id):
        return True

    def encode(frames, mp4, preview):
        return {"frames": len(frames), "duration_ms": 1000, "size_bytes": 10}

    monkeypatch.setattr(config, "DOSE_VIDEO_DIR", tmp_path)
    monkeypatch.setattr(dose_video, "get_pool", lambda: Pool(db))
    monkeypatch.setattr(dose_video, "enabled", enabled)
    monkeypatch.setattr(dose_video, "encode", encode)
    monkeypatch.setattr(outbox, "enqueue", enqueue)
    return types.SimpleNamespace(db=db, enqueued=enqueued, internet=Internet(monkeypatch, tunnel=Answer(404)))


def _store():
    asyncio.run(dose_video._store(1, [4], "reachy", [(index * 0.2, b"jpeg") for index in range(6)]))


def test_a_clip_stored_without_a_link_says_why_in_one_warning(world, caplog):
    caplog.set_level(logging.WARNING, logger=dose_video.log.name)
    _store()
    (video_id,) = world.db.videos
    assert world.db.videos[video_id]["sent_at"] is None and world.enqueued == []
    kept = [r.getMessage() for r in caplog.records if "kept unsent" in r.getMessage()]
    assert kept == [f"dose video {video_id} for intakes [4] kept unsent: no public link ({dose_video.OTHER_SERVER})"]
    assert dose_video.PROBE_TOKEN not in caplog.text


def test_the_job_sends_a_kept_clip_once_the_link_returns_and_only_once(world, fresh_link):
    _store()
    (video_id,) = world.db.videos
    asyncio.run(dose_video.maintain())
    fresh_link.now += dose_video.BASE_URL_RETRY_SECONDS + 1
    asyncio.run(dose_video.maintain())                        # the link is still another server's
    assert world.enqueued == [] and world.db.videos[video_id]["sent_at"] is None
    world.internet.tunnel = Answer(200, dose_video.PROBE_MARKER)
    fresh_link.now += dose_video.BASE_URL_RETRY_SECONDS + 1
    asyncio.run(dose_video.maintain())
    assert len(world.enqueued) == 1 and world.db.links == [video_id]
    item = world.enqueued[0]
    assert item["kind"] == "dose_video" and item["dedupe_key"] == dose_video.outbox_key(video_id, 34)
    note, video = item["messages"]
    assert "請看完再回覆上面的確認" in note["text"] and video["originalContentUrl"].startswith(f"{BASE}/api/media/line/")
    assert world.db.videos[video_id]["sent_at"] is not None
    for _ in range(2):
        fresh_link.now += dose_video.BASE_URL_CACHE_SECONDS + 1
        asyncio.run(dose_video.maintain())
    assert len(world.enqueued) == 1 and world.db.links == [video_id]
    assert all(datetime.datetime.now(UTC) - dose_video.UNSENT_MAX_AGE - cutoff < datetime.timedelta(minutes=1)
               for cutoff in world.db.cutoffs)                # younger than the 4 h limit


@pytest.mark.parametrize("case", ["answered", "expired", "older_than_4_hours"])
def test_the_job_leaves_a_clip_whose_request_is_closed_or_too_old(world, fresh_link, case):
    _store()
    (video_id,) = world.db.videos
    if case == "older_than_4_hours":
        world.db.videos[video_id]["created_at"] -= dose_video.UNSENT_MAX_AGE + datetime.timedelta(minutes=1)
    else:
        world.db.resolution = "confirmed" if case == "answered" else "expired"
    world.internet.tunnel = Answer(200, dose_video.PROBE_MARKER)
    fresh_link.now += dose_video.BASE_URL_RETRY_SECONDS + 1
    asyncio.run(dose_video.maintain())
    assert world.enqueued == [] and world.db.links == []
    expected = "deleted" if case == "older_than_4_hours" else "kept"       # the existing deletion rules decide
    assert (world.db.videos[video_id]["deleted_at"] is not None) == (expected == "deleted")


def test_the_job_asks_for_no_link_when_nothing_waits(world, fresh_link):
    asyncio.run(dose_video.maintain())
    assert world.internet.calls == [] and world.enqueued == []


def test_the_dose_videos_job_runs_the_retry(monkeypatch):
    from app.jobs import scheduler as module

    jobs = {}

    class Scheduler:
        def add_job(self, func, trigger, id, **kwargs):
            jobs[id] = (func, trigger, kwargs)

        def start(self):
            pass

    monkeypatch.setattr(module, "scheduler", Scheduler())
    module.start_scheduler()
    func, trigger, kwargs = jobs["dose_videos"]
    assert func is dose_video.maintain and trigger.interval == datetime.timedelta(minutes=2)
    assert kwargs.get("max_instances") == 1
