"""The GPT-Live client (spec 2026-10-12-live-voice-2a-design.md A1) against an in-memory GPT-Live: no network.

The fake answers with the event names and payloads measured in the Phase 2 spike (11 Oct 2026): session.started
{session: {id}}, session.output_audio.delta {delta: base64}, transcript deltas {delta, start_ms, end_ms}, mute acks
{client_event_id}, error {error: {type, code, message}}, session.closed {reason, usage: {seconds}}."""

import asyncio
import base64
import gc
import json
import logging

import pytest
from websockets.asyncio.client import connect as websocket_connect
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConcurrencyError, ConnectionClosedError, ConnectionClosedOK, InvalidStatus
from websockets.http11 import Response

from app.services import live_client
from app.services.live_client import LiveClient, LiveError, LiveEvent

KEY = "sk-proj-live-test-key-1234"
STARTED = {"type": "session.started", "session": {"id": "sess_test", "expires_at": 1760000000}}
CLOSED = {"type": "session.closed", "reason": "close_requested", "usage": {"seconds": 7.0}}


class FakeSocket:
    """An in-memory GPT-Live WebSocket. Keeps every event the client sends (parsed JSON); script(event) gives the
    events that answer it; push() sends one at any time; hang_up() drops the connection without session.closed."""

    def __init__(self, script=None):
        self.sent, self.closed = [], False
        self.script = script or (lambda event: [])
        self.inbox = asyncio.Queue()

    def push(self, event):
        self.inbox.put_nowait(json.dumps(event, ensure_ascii=False))

    def hang_up(self):
        self.inbox.put_nowait(None)

    def sent_types(self):
        return [event["type"] for event in self.sent]

    async def send(self, text):
        if self.closed:
            raise ConnectionClosedOK(None, None)
        event = json.loads(text)
        self.sent.append(event)
        for answer in self.script(event):
            self.push(answer)

    async def recv(self):
        raw = await self.inbox.get()
        if raw is None:
            self.closed = True
            raise ConnectionClosedError(None, None)
        return raw

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.recv()

    async def close(self):
        self.closed = True


class BlockingSocket(FakeSocket):
    """A FakeSocket whose send of microphone audio stops until `release` is set, as a socket with a full buffer does.
    `entered` is set once such a send is stuck, so a test knows the client's send lock is held."""

    def __init__(self, script=None):
        super().__init__(script)
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def send(self, text):
        if json.loads(text)["type"] == "session.input_audio.append" and not self.release.is_set():
            self.entered.set()
            await self.release.wait()
        await super().send(text)


def gpt_live(event):
    """Answers as the measured GPT-Live does: session.started, the mute acks, the appended audio echoed back as
    output audio, and session.closed with the billed seconds."""
    kind = event["type"]
    if kind == "session.start":
        return [STARTED]
    if kind in ("session.input_audio.mute", "session.input_audio.unmute"):
        return [{"type": kind + "d", "client_event_id": event["event_id"]}]
    if kind == "session.input_audio.append":
        return [{"type": "session.output_audio.delta", "delta": event["audio"]}]
    if kind == "session.close":
        return [CLOSED]
    return []


def starts_only(event):
    return [STARTED] if event["type"] == "session.start" else []


def connector(socket, calls=None):
    async def connect(url, **options):
        if calls is not None:
            calls.append((url, options))
        return socket
    return connect


def refusing(error):
    async def connect(url, **options):
        raise error
    return connect


def refused(status):
    return InvalidStatus(Response(status, "refused", Headers()))


async def take(events, count, limit=1.0):
    """The next `count` events of one events() iterator."""
    async with asyncio.timeout(limit):
        return [await anext(events) for _ in range(count)]


def test_start_opens_the_session_with_the_key_only_in_the_header():
    async def run():
        socket, calls = FakeSocket(gpt_live), []
        client = LiveClient(KEY, "gpt-live-1", "marin", "你是 Reachy。", connect=connector(socket, calls))
        await client.start()
        first = await take(client.events(), 1)
        await client.close()
        return socket, calls, client, first

    socket, calls, client, first = asyncio.run(run())
    url, options = calls[0]
    assert url == "wss://api.openai.com/v1/live/sessions"
    assert options["additional_headers"] == {"Authorization": "Bearer " + KEY}
    assert options["open_timeout"] == 8.0 and options["close_timeout"] == 1.0
    assert options["compression"] is None and options["proxy"] is None
    assert socket.sent[0] == {"type": "session.start", "session": {
        "model": "gpt-live-1", "store": False, "instructions": "你是 Reachy。",
        "audio": {"format": {"type": "audio/pcm", "rate": 16000}, "output": {"voice": "marin"}}}}
    assert first == [LiveEvent("started", text="sess_test")]
    assert client.session_id == "sess_test"
    assert KEY not in json.dumps(socket.sent) and KEY not in repr(client)


def test_the_session_rate_is_the_one_asked_for():
    async def run():
        socket = FakeSocket(gpt_live)
        client = LiveClient(KEY, "gpt-live-1", "cedar", "x", rate=24000, connect=connector(socket))
        await client.start()
        await client.close()
        return socket

    audio = asyncio.run(run()).sent[0]["session"]["audio"]
    assert audio == {"format": {"type": "audio/pcm", "rate": 24000}, "output": {"voice": "cedar"}}


def test_the_default_connect_is_websockets_client_connect():
    assert LiveClient(KEY, "gpt-live-1", "marin", "x")._connect is websocket_connect


@pytest.mark.parametrize("connect, code", [
    (refusing(refused(401)), "live_auth"),
    (refusing(refused(403)), "live_auth"),
    (refusing(refused(429)), "rate_limit_exceeded"),
    (refusing(refused(503)), "live_unavailable"),
    (refusing(OSError("network is unreachable")), "live_unavailable"),
])
def test_a_refused_connection_is_a_machine_code(connect, code):
    async def run():
        client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connect)
        with pytest.raises(LiveError) as error:
            await client.start()
        return error.value, [event async for event in client.events()], await client.close()

    error, events, usage = asyncio.run(run())
    assert error.code == code and str(error) == code
    assert events == [] and usage is None


@pytest.mark.parametrize("answer, code", [
    ({"type": "error", "error": {"type": "invalid_request_error", "code": "invalid_api_key",
                                 "message": "Incorrect API key provided: " + KEY}}, "live_auth"),
    ({"type": "error", "error": {"type": "rate_limit_error", "code": "rate_limit_exceeded",
                                 "message": "Rate limit reached"}}, "rate_limit_exceeded"),
    ({"type": "error", "error": {"type": "content_filter", "code": "content_filter",
                                 "message": "The generation was stopped due to moderation.", "param": None}},
     "content_filter"),
    ({"type": "error", "error": {"type": "invalid_request_error", "code": "model_not_found",
                                 "message": "The model does not exist"}}, "live_unavailable"),
    ({"type": "session.closed", "reason": "remote_hangup", "usage": {"seconds": 0.0}}, "live_closed"),
])
def test_an_answer_other_than_session_started_is_a_machine_code(answer, code, caplog):
    caplog.set_level(logging.DEBUG, logger="app.services.live_client")

    async def run():
        socket = FakeSocket(lambda event: [answer] if event["type"] == "session.start" else [])
        client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connector(socket))
        with pytest.raises(LiveError) as error:
            await client.start()
        return socket, error.value, [event async for event in client.events()]

    socket, error, events = asyncio.run(run())
    assert error.code == code and str(error) == code
    assert socket.closed and events == []
    assert KEY not in str(error) and KEY not in caplog.text


def test_no_session_started_within_the_timeout_is_live_unavailable():
    async def run():
        socket = FakeSocket()
        client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connector(socket))
        with pytest.raises(LiveError) as error:
            await client.start(timeout=0.05)
        return socket, error.value

    socket, error = asyncio.run(run())
    assert error.code == "live_unavailable" and socket.closed
    assert socket.sent_types() == ["session.start"]


def test_a_connection_dropped_before_session_started_is_live_unavailable():
    async def run():
        socket = FakeSocket()
        socket.hang_up()
        client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connector(socket))
        with pytest.raises(LiveError) as error:
            await client.start()
        return error.value

    assert asyncio.run(run()).code == "live_unavailable"


def test_audio_goes_out_as_base64_and_comes_back_as_pcm():
    pcm = bytes(range(256)) * 5   # 1280 bytes: 40 ms of PCM16 at 16 kHz

    async def run():
        socket = FakeSocket(gpt_live)
        client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connector(socket))
        await client.start()
        events = client.events()
        await client.send_audio(pcm)
        got = await take(events, 2)
        await client.close()
        return socket, got

    socket, got = asyncio.run(run())
    assert socket.sent[1] == {"type": "session.input_audio.append", "audio": base64.b64encode(pcm).decode("ascii")}
    assert got == [LiveEvent("started", text="sess_test"), LiveEvent("audio", data=pcm)]


def test_transcripts_become_text_events_and_bad_audio_is_dropped():
    good = base64.b64encode(b"\x10\x00\x20\x00").decode("ascii")

    async def run():
        socket = FakeSocket(gpt_live)
        client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connector(socket))
        await client.start()
        events = client.events()
        for event in (
                {"type": "session.input_transcript.delta", "delta": "我今天", "start_ms": 2200, "end_ms": 2400},
                {"type": "session.input_transcript.delta", "delta": "", "start_ms": 2400, "end_ms": 2600},
                {"type": "session.delegation.created", "delegation": {"id": "item_1", "target": "client"},
                 "offset_ms": 6600},
                {"type": "session.output_audio.delta", "delta": base64.b64encode(b"\x01").decode("ascii")},
                {"type": "session.output_audio.delta", "delta": "not base64!"},
                {"type": "session.output_transcript.delta", "delta": "聽起來", "start_ms": 4800, "end_ms": 5000},
                {"type": "session.instructions.appended", "client_event_id": "x"},
                {"type": "session.output_audio.delta", "delta": good}):
            socket.push(event)
        got = await take(events, 4)
        await client.close()
        return got

    assert asyncio.run(run()) == [LiveEvent("started", text="sess_test"), LiveEvent("input_text", text="我今天"),
                                  LiveEvent("output_text", text="聽起來"),
                                  LiveEvent("audio", data=b"\x10\x00\x20\x00")]


def test_a_bad_audio_delta_is_dropped_with_a_warning(caplog):
    caplog.set_level(logging.WARNING, logger="app.services.live_client")
    good = base64.b64encode(b"\x10\x00\x20\x00").decode("ascii")

    async def run():
        socket = FakeSocket(gpt_live)
        client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connector(socket))
        await client.start()
        events = client.events()
        for delta in (base64.b64encode(b"\x01").decode("ascii"), "not base64!", 12345, good):
            socket.push({"type": "session.output_audio.delta", "delta": delta})
        got = await take(events, 2)
        await client.close()
        return got

    got = asyncio.run(run())
    assert got[1] == LiveEvent("audio", data=b"\x10\x00\x20\x00")
    warnings = [record for record in caplog.records
                if record.name == "app.services.live_client" and record.levelno == logging.WARNING]
    assert len(warnings) == 3 and all("not PCM16" in record.getMessage() for record in warnings)


def test_mute_and_unmute_wait_for_their_acknowledgements():
    async def run():
        socket = FakeSocket(gpt_live)
        client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connector(socket))
        await client.start()
        await client.mute()
        muted = client.muted
        await client.mute()   # already muted: nothing is sent
        await client.unmute()
        await client.close()
        return socket, muted, client.muted

    socket, muted, after = asyncio.run(run())
    assert socket.sent_types() == ["session.start", "session.input_audio.mute", "session.input_audio.unmute",
                                   "session.close"]
    assert socket.sent[1]["event_id"] != socket.sent[2]["event_id"]
    assert muted is True and after is False


def test_an_unmute_asked_while_a_mute_waits_takes_its_turn():
    """The relay may not wait for mute() before it asks for unmute(). The unmute must then wait until the mute is
    answered and decide after that, not read the state the mute has not reached yet and return doing nothing."""
    async def run():
        socket = FakeSocket(starts_only)
        client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connector(socket))
        await client.start()
        mute = asyncio.create_task(client.mute())
        await asyncio.sleep(0.01)
        unmute = asyncio.create_task(client.unmute())
        await asyncio.sleep(0.01)
        waiting = socket.sent_types()
        socket.push({"type": "session.input_audio.muted", "client_event_id": socket.sent[-1]["event_id"]})
        await asyncio.wait_for(mute, 1)
        await asyncio.sleep(0.01)
        socket.push({"type": "session.input_audio.unmuted", "client_event_id": socket.sent[-1]["event_id"]})
        await asyncio.wait_for(unmute, 1)
        muted = client.muted
        await client.close(wait=0.05)
        return socket, waiting, muted

    socket, waiting, muted = asyncio.run(run())
    assert waiting == ["session.start", "session.input_audio.mute"]
    assert socket.sent_types() == ["session.start", "session.input_audio.mute", "session.input_audio.unmute",
                                   "session.close"]
    assert muted is False


@pytest.mark.parametrize("where", ["error", "event"])
def test_an_error_naming_the_mute_request_makes_mute_raise_its_code(where, monkeypatch):
    """OpenAI puts client_event_id inside `error` or on the event itself (its Live reference allows both)."""
    monkeypatch.setattr(live_client, "ACK_SECONDS", 0.5)

    def script(event):
        if event["type"] != "session.input_audio.mute":
            return starts_only(event)
        error = {"type": "rate_limit_error", "code": "rate_limit_exceeded", "message": "Rate limit reached"}
        answer = {"type": "error", "error": error}
        (error if where == "error" else answer)["client_event_id"] = event["event_id"]
        return [answer]

    async def run():
        socket = FakeSocket(script)
        client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connector(socket))
        await client.start()
        events = client.events()
        with pytest.raises(LiveError) as error:
            await client.mute()
        got = await take(events, 2)
        await client.close(wait=0.05)
        return error.value, client.muted, got

    error, muted, got = asyncio.run(run())
    assert error.code == "rate_limit_exceeded" and muted is False
    assert got[1] == LiveEvent("error", code="rate_limit_exceeded", text="rate_limit_exceeded")


def test_a_mute_never_acknowledged_is_live_unavailable(monkeypatch):
    monkeypatch.setattr(live_client, "ACK_SECONDS", 0.05)

    async def run():
        socket = FakeSocket(starts_only)
        client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connector(socket))
        await client.start()
        with pytest.raises(LiveError) as error:
            await client.mute()
        await client.close(wait=0.05)
        return error.value, client.muted

    error, muted = asyncio.run(run())
    assert error.code == "live_unavailable" and muted is False


def test_commentary_and_instructions_go_out_without_a_delegation():
    async def run():
        socket = FakeSocket(gpt_live)
        client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connector(socket))
        await client.start()
        await client.commentary("王奶奶早安，今天過得好嗎？")
        await client.instructions_append("每次回答最多 30 個字。")
        await client.close()
        return socket

    commentary, instructions = asyncio.run(run()).sent[1:3]
    assert commentary["type"] == "session.commentary.append" and commentary["delegation_id"] is None
    assert commentary["content"] == "王奶奶早安，今天過得好嗎？" and commentary["event_id"].startswith("commentary-")
    assert instructions["type"] == "session.instructions.append" and instructions["delegation_id"] is None
    assert instructions["content"] == "每次回答最多 30 個字。" and instructions["event_id"].startswith("instructions-")


def test_close_sends_session_close_and_returns_the_billed_seconds():
    async def run():
        socket = FakeSocket(gpt_live)
        client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connector(socket))
        await client.start()
        events = client.events()
        usage = await client.close()
        rest = [event async for event in events]
        again = await client.close()
        return socket, usage, rest, again

    socket, usage, rest, again = asyncio.run(run())
    assert usage == 7.0 and again == 7.0
    assert socket.sent_types() == ["session.start", "session.close"] and socket.closed
    assert rest == [LiveEvent("started", text="sess_test"),
                    LiveEvent("closed", reason="close_requested", usage_seconds=7.0)]


def test_close_without_an_answer_returns_the_last_usage_update():
    async def run():
        socket = FakeSocket(starts_only)
        client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connector(socket))
        await client.start()
        events = client.events()
        socket.push({"type": "session.usage.updated", "usage": {"seconds": 3.0}})
        await take(events, 1)
        await asyncio.sleep(0.01)
        usage = await client.close(wait=0.05)
        return usage, [event async for event in events]

    usage, rest = asyncio.run(run())
    assert usage == 3.0
    assert rest == [LiveEvent("closed", reason="close_requested", usage_seconds=3.0)]


def test_a_moderation_cut_is_an_error_event_then_the_close_reason():
    async def run():
        socket = FakeSocket(gpt_live)
        client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connector(socket))
        await client.start()
        events = client.events()
        socket.push({"type": "error", "error": {"type": "content_filter", "code": "content_filter",
                                                "message": "The generation was stopped due to moderation.",
                                                "param": None}})
        socket.push({"type": "session.closed", "reason": "content", "usage": {"seconds": 9.0}})
        got = await take(events, 3)
        with pytest.raises(LiveError) as error:
            await client.send_audio(b"\x00\x00")
        usage = await client.close()
        return socket, got, error.value, usage

    socket, got, error, usage = asyncio.run(run())
    assert got[1:] == [LiveEvent("error", code="content_filter", text="content_filter"),
                       LiveEvent("closed", reason="content", usage_seconds=9.0)]
    assert error.code == "live_closed" and usage == 9.0
    assert socket.sent_types() == ["session.start"]   # no session.close after OpenAI closed it


@pytest.mark.parametrize("method", ["send_audio", "commentary", "instructions_append", "mute", "unmute"])
def test_a_send_after_close_has_begun_is_live_closed(method):
    async def run():
        # GPT-Live never answers session.close, so close() keeps waiting for session.closed while the send is tried.
        socket = FakeSocket(lambda event: [] if event["type"] == "session.close" else gpt_live(event))
        client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connector(socket))
        await client.start()
        if method == "unmute":
            await client.mute()   # unmute() does nothing unless the session is muted
        closing = asyncio.create_task(client.close(wait=0.1))
        await asyncio.sleep(0.01)
        sent_before = len(socket.sent)
        arguments = {"send_audio": (b"\x00\x00",), "commentary": ("hi",), "instructions_append": ("hi",)}
        with pytest.raises(LiveError) as error:
            await getattr(client, method)(*arguments.get(method, ()))
        await closing
        return socket, error.value, sent_before

    socket, error, sent_before = asyncio.run(run())
    assert error.code == "live_closed"
    assert socket.sent_types()[sent_before - 1:sent_before] == ["session.close"]
    assert len(socket.sent) == sent_before   # nothing went out after close() began


@pytest.mark.parametrize("second_call", ["send_audio", "mute"])
def test_a_send_waiting_for_the_lock_when_the_session_ends_is_live_closed(second_call, caplog):
    """Send B passed the first check and waits behind send A, which is stuck. The connection drops, close() runs and
    the socket is gone before B holds the lock: B must raise live_closed (not AttributeError) and send nothing.
    A mute as B has an acknowledgement future that the ending session already failed: asyncio must not complain
    that nobody read it."""
    async def run():
        socket = BlockingSocket(gpt_live)
        client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connector(socket))
        await client.start()
        events = client.events()
        first = asyncio.create_task(client.send_audio(b"\x01\x00"))
        await asyncio.wait_for(socket.entered.wait(), 1)
        second = asyncio.create_task(client.send_audio(b"\x02\x00") if second_call == "send_audio"
                                     else client.mute())
        await asyncio.sleep(0.01)
        socket.hang_up()
        await take(events, 2)   # started, closed (connection_lost)
        await client.close()
        socket.release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 1)
        return socket.sent_types(), [getattr(result, "code", repr(result)) for result in results]

    sent, codes = asyncio.run(run())
    gc.collect()   # asyncio reports an unread future exception when the future is freed
    assert codes == ["live_closed", "live_closed"] and sent == ["session.start"]
    assert [record.getMessage() for record in caplog.records if record.name == "asyncio"] == []


def test_a_websocket_error_from_a_send_is_live_closed():
    """websockets' send can fail with a WebSocketException other than ConnectionClosed (ConcurrencyError here)."""
    async def run():
        socket = FakeSocket(gpt_live)
        client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connector(socket))
        await client.start()

        async def refuse(text):
            raise ConcurrencyError("another send is in progress")

        socket.send = refuse
        with pytest.raises(LiveError) as error:
            await client.send_audio(b"\x00\x00")
        await client.close(wait=0.05)   # its session.close is refused the same way: close() still never raises
        return error.value

    assert asyncio.run(run()).code == "live_closed"


def test_a_send_waiting_behind_close_never_goes_out():
    """Send B was queued for the lock before close() began. close() then holds the line for session.close, so B,
    which reaches the lock first, must see that the session is closing and raise live_closed instead of sending."""
    async def run():
        socket = BlockingSocket(gpt_live)
        client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connector(socket))
        await client.start()
        first = asyncio.create_task(client.send_audio(b"\x01\x00"))
        await asyncio.wait_for(socket.entered.wait(), 1)
        second = asyncio.create_task(client.send_audio(b"\x02\x00"))
        await asyncio.sleep(0.01)
        closing = asyncio.create_task(client.close())
        await asyncio.sleep(0.01)
        socket.release.set()
        usage = await asyncio.wait_for(closing, 3)
        results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 1)
        return socket, results, usage

    socket, results, usage = asyncio.run(run())
    assert results[0] is None and getattr(results[1], "code", repr(results[1])) == "live_closed"
    assert socket.sent_types() == ["session.start", "session.input_audio.append", "session.close"]
    assert socket.sent[1]["audio"] == base64.b64encode(b"\x01\x00").decode("ascii")
    assert usage == 7.0


def test_an_error_event_carries_only_the_mapped_code_and_openais_code():
    async def run():
        socket = FakeSocket(gpt_live)
        client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connector(socket))
        await client.start()
        events = client.events()
        socket.push({"type": "error", "error": {"type": "server_error", "code": "context_injection_incomplete",
                                                "message": "Bearer " + KEY + " failed"}})
        socket.push({"type": "error", "error": {"type": "invalid_request_error", "code": "Bearer " + KEY}})
        got = await take(events, 3)
        await client.close()
        return got

    got = asyncio.run(run())
    assert got[1] == LiveEvent("error", code="live_unavailable", text="context_injection_incomplete")
    assert got[2].code == "live_unavailable" and KEY not in got[2].text


def test_a_dropped_connection_ends_the_events_and_fails_a_waiting_mute():
    async def run():
        socket = FakeSocket(starts_only)
        client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connector(socket))
        await client.start()
        events = client.events()
        mute = asyncio.create_task(client.mute())
        await asyncio.sleep(0.01)
        socket.hang_up()
        rest = await take(events, 2)
        with pytest.raises(LiveError) as mute_error:
            await mute
        with pytest.raises(LiveError) as send_error:
            await client.send_audio(b"\x00\x00")
        usage = await client.close()
        return rest, mute_error.value, send_error.value, usage

    rest, mute_error, send_error, usage = asyncio.run(run())
    assert rest == [LiveEvent("started", text="sess_test"), LiveEvent("closed", reason="connection_lost")]
    assert mute_error.code == "live_closed" and send_error.code == "live_closed" and usage is None


def test_a_real_local_websocket_takes_the_options_and_carries_the_events():
    """websockets' own client and server on 127.0.0.1 (nothing leaves this computer): the options start() passes
    are ones websockets accepts, the key arrives only as the Authorization header, and close() gets the usage."""
    async def run():
        seen = {}

        async def server(ws):
            seen["authorization"] = ws.request.headers.get("Authorization")
            async for raw in ws:
                event = json.loads(raw)
                for answer in gpt_live(event):
                    await ws.send(json.dumps(answer))

        async with serve(server, "127.0.0.1", 0) as local:
            port = local.sockets[0].getsockname()[1]

            async def connect(url, **options):
                seen["url"] = url
                return await websocket_connect(f"ws://127.0.0.1:{port}", **options)

            client = LiveClient(KEY, "gpt-live-1", "marin", "x", connect=connect)
            await client.start()
            events = client.events()
            await client.send_audio(b"\x01\x00\x02\x00")
            got = await take(events, 2, limit=3.0)
            await client.mute()
            usage = await client.close()
        return seen, got, usage, client.muted

    seen, got, usage, muted = asyncio.run(run())
    assert seen == {"url": "wss://api.openai.com/v1/live/sessions", "authorization": "Bearer " + KEY}
    assert got == [LiveEvent("started", text="sess_test"), LiveEvent("audio", data=b"\x01\x00\x02\x00")]
    assert usage == 7.0 and muted is True


def test_check_times_a_session_start_and_closes_it():
    socket = None

    async def run():
        nonlocal socket
        socket = FakeSocket(gpt_live)
        return await live_client.check(KEY, "gpt-live-1", "cedar", connect=connector(socket))

    ok, ms, error = asyncio.run(run())
    assert ok is True and isinstance(ms, int) and ms >= 0 and error is None
    assert socket.sent_types() == ["session.start", "session.close"]
    assert socket.sent[0]["session"]["model"] == "gpt-live-1"
    assert socket.sent[0]["session"]["audio"]["output"] == {"voice": "cedar"}


def test_check_reports_the_code_of_a_failure():
    result = asyncio.run(live_client.check(KEY, "gpt-live-1", "marin", connect=refusing(refused(401))))
    assert result == (False, None, "live_auth")
