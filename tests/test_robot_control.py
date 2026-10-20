"""Quick fix 9 (CONTRACT9.md): the robot app's control address, and the server's direct call that applies the Reachy
card's microphone button on the robot at once (robot app 0.17.2, POST {url}/api/microphone)."""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.services import robot_control

KEY = "k" * 21 + "-_" + "Z9" * 10   # 43 URL-safe characters, the shape of secrets.token_urlsafe(32)


# ── The control address a heartbeat may carry ────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://192.168.137.104:8042", "http://192.168.0.1:8042", "http://10.0.0.7:8042", "http://10.255.255.254:8042",
    "http://172.16.0.1:8042", "http://172.31.255.254:8042",
])
def test_a_private_ipv4_address_on_port_8042_and_a_url_safe_key_are_kept(url):
    assert robot_control.check({"url": url, "key": KEY}) == ({"url": url, "key": KEY}, None)


@pytest.mark.parametrize("key", ["a" * 20, "A-_9" * 50, "x" * 200])
def test_a_key_of_20_to_200_url_safe_characters_is_kept(key):
    assert robot_control.check({"url": "http://10.0.0.7:8042", "key": key})[0] == {"url": "http://10.0.0.7:8042",
                                                                                   "key": key}


def test_only_the_url_and_the_key_are_kept():
    control = {"url": "http://10.0.0.7:8042", "key": KEY, "port": 1, "note": "x"}
    assert robot_control.check(control)[0] == {"url": "http://10.0.0.7:8042", "key": KEY}


def test_no_control_is_nothing_to_keep_and_nothing_to_warn_about():
    assert robot_control.check(None) == (None, None)


@pytest.mark.parametrize("url", [
    "https://192.168.1.2:8042",          # only plain http, as the robot app serves it
    "http://192.168.1.2",                # its port, and only that one
    "http://192.168.1.2:8041", "http://192.168.1.2:80", "http://192.168.1.2:08042",
    "http://192.168.1.2:8042/", "http://192.168.1.2:8042/api", "http://192.168.1.2:8042?x=1",
    "http://192.168.1.2:8042#a", "http://user@192.168.1.2:8042", "http://user:pass@192.168.1.2:8042",
    "http://192.168.1.2:8042\n", " http://192.168.1.2:8042", "HTTP://192.168.1.2:8042",
    "http://reachy-mini.local:8042",     # a name could resolve anywhere: only an address literal
    "http://192.168.001.2:8042", "http://192.168.1.256:8042", "http://192.168.1:8042", "http://3232235778:8042",
    "http://[fd00::1]:8042", "", 42, None, ["http://192.168.1.2:8042"],
])
def test_any_other_address_is_dropped(url):
    control, reason = robot_control.check({"url": url, "key": KEY})
    assert control is None and reason == "the address is not http://<IPv4>:8042"


@pytest.mark.parametrize("host", [
    "8.8.8.8", "127.0.0.1", "0.0.0.0", "169.254.1.1", "100.64.0.1", "172.15.255.255", "172.32.0.1", "192.169.0.1",
    "224.0.0.1", "255.255.255.255",
])
def test_an_address_outside_the_three_private_networks_is_dropped(host):
    control, reason = robot_control.check({"url": f"http://{host}:8042", "key": KEY})
    assert control is None and reason == "the address is not on a private network"


@pytest.mark.parametrize("key", ["a" * 19, "a" * 201, "a" * 30 + "+", "a" * 30 + "/", "a" * 30 + "=", "a" * 30 + " ",
                                 "a" * 30 + "\n", "ä" * 30, "", 12345678901234567890123, None])
def test_a_key_that_is_not_20_to_200_url_safe_characters_is_dropped(key):
    control, reason = robot_control.check({"url": "http://10.0.0.7:8042", "key": key})
    assert control is None and reason == "the key is not 20-200 URL-safe characters"


@pytest.mark.parametrize("control", ["http://10.0.0.7:8042", ["http://10.0.0.7:8042", KEY], 1, True, {"url": "x"},
                                     {"key": KEY}, {}])
def test_a_control_that_is_not_an_object_with_both_fields_is_dropped(control):
    assert robot_control.check(control)[0] is None
    assert robot_control.check(control)[1] is not None


def test_the_heartbeat_warns_once_per_change_and_never_with_the_key(caplog):
    device = "dev-warn-once"
    bad = {"url": "http://8.8.8.8:8042", "key": KEY}
    with caplog.at_level("WARNING"):
        assert robot_control.from_heartbeat(device, 7, bad) is None
        assert robot_control.from_heartbeat(device, 7, bad) is None             # the same: no second line
        assert robot_control.from_heartbeat(device, 7, {**bad, "key": "b" * 43}) is None   # a new key: a change
        assert robot_control.from_heartbeat(device, 7, {**bad, "key": "b" * 43}) is None
        good = {"url": "http://10.0.0.7:8042", "key": KEY}
        assert robot_control.from_heartbeat(device, 7, good) == good                # kept: nothing to warn about
        assert robot_control.from_heartbeat(device, 7, None) is None                # an older robot: no warning
        assert robot_control.from_heartbeat(device, 7, bad) is None                 # dropped again after that: warns
    lines = [record for record in caplog.records if record.name == robot_control.log.name]
    assert [record.levelname for record in lines] == ["WARNING"] * 3
    assert all("patient 7" in record.getMessage() and "not on a private network" in record.getMessage()
               for record in lines)
    assert not any(KEY in record.getMessage() or "b" * 43 in record.getMessage() or "8.8.8.8" in record.getMessage()
                   for record in caplog.records)


# ── The direct call: POST {url}/api/microphone ──────────────────────────────

class _Robot:
    """The robot app's web API on 127.0.0.1 (the address check is the heartbeat's; the call takes what was kept)."""

    def __init__(self):
        self.requests = []   # (path, headers, body) of each request
        self.status = 200
        self.answer = None   # the JSON answer; None: {"muted": <as asked>, "volume": 0 or 100}
        self.raw = None      # bytes sent instead of JSON
        self.delay = 0.0
        self.location = None
        robot = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                robot.requests.append((self.path, dict(self.headers), body))
                time.sleep(robot.delay)
                asked = json.loads(body or b"{}").get("muted")
                payload = robot.raw if robot.raw is not None else json.dumps(
                    robot.answer if robot.answer is not None else {"muted": asked, "volume": 0 if asked else 100}
                ).encode()
                self.send_response(robot.status)
                if robot.location:
                    self.send_header("Location", robot.location)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.control = {"url": self.url, "key": KEY}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def robot():
    found = _Robot()
    yield found
    found.close()


def _set(control, muted):
    return asyncio.run(robot_control.set_microphone(control, muted))


def test_the_call_posts_the_switch_with_the_key_and_reads_the_volume(robot):
    assert _set(robot.control, True) == (0, None)
    assert _set(robot.control, False) == (100, None)
    assert [(path, json.loads(body)) for path, _, body in robot.requests] == [
        ("/api/microphone", {"muted": True}), ("/api/microphone", {"muted": False})]
    headers = robot.requests[0][1]
    assert headers["Authorization"] == f"Bearer {KEY}"
    assert headers["Content-Type"] == "application/json"


def test_a_null_volume_is_still_applied(robot):
    robot.answer = {"muted": True, "volume": None}
    assert _set(robot.control, True) == (None, None)


@pytest.mark.parametrize("status", [401, 403, 404, 409, 500, 503])
def test_a_refusal_is_not_applied(robot, status):
    robot.status = status
    robot.answer = {"detail": "daemon_unavailable"}
    assert _set(robot.control, True) == (None, f"HTTP {status}")


def test_a_redirect_is_never_followed(robot):
    robot.status = 307
    robot.location = "http://127.0.0.1:9/api/microphone"
    assert _set(robot.control, True) == (None, "HTTP 307")
    assert len(robot.requests) == 1


@pytest.mark.parametrize("answer", [
    {"muted": False, "volume": 0},      # not what was asked: not confirmed
    {"volume": 0}, {"muted": "true", "volume": 0}, {"muted": True, "volume": -1}, {"muted": True, "volume": 101},
    {"muted": True, "volume": 50.0}, {"muted": True, "volume": "0"}, {"muted": True, "volume": True}, [True, 0],
])
def test_an_answer_that_does_not_confirm_the_switch_is_not_applied(robot, answer):
    robot.answer = answer
    assert _set(robot.control, True) == (None, "bad answer")


def test_an_answer_that_is_not_json_is_not_applied(robot):
    robot.raw = b"<html>ok</html>"
    assert _set(robot.control, True) == (None, "bad answer")


def test_a_slow_robot_times_out_within_the_limit(robot, monkeypatch):
    monkeypatch.setattr(robot_control, "MIC_DIRECT_TIMEOUT_SECONDS", 0.3)
    robot.delay = 1.5
    started = time.monotonic()
    assert _set(robot.control, True) == (None, "timeout")
    assert time.monotonic() - started < 1.0   # one try, never longer than the limit


def test_a_robot_that_is_not_there_is_not_applied():
    with ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as closed:
        port = closed.server_address[1]   # bound, then closed: nothing listens there
    assert _set({"url": f"http://127.0.0.1:{port}", "key": KEY}, True) == (None, "no connection")


def test_the_call_never_goes_through_a_proxy(robot, monkeypatch):
    """The key goes to the robot alone: a proxy (or a .netrc) from the environment is never used."""
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")   # nothing listens there: through it, the call would fail
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    assert _set(robot.control, True) == (0, None)
    assert len(robot.requests) == 1


def test_the_limit_is_three_seconds():
    assert robot_control.MIC_DIRECT_TIMEOUT_SECONDS == 3
