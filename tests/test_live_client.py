"""The GPT-Live client (spec 2026-10-12-live-voice-2a-design.md A1) against an in-memory GPT-Live: no network.

The fake answers with the event names and payloads measured in the Phase 2 spike (11 Oct 2026): session.started
{session: {id}}, session.output_audio.delta {delta: base64}, transcript deltas {delta, start_ms, end_ms}, mute acks
{client_event_id}, error {error: {type, code, message}}, session.closed {reason, usage: {seconds}}."""

import asyncio
import base64
import json
import logging

import pytest
from websockets.asyncio.client import connect as websocket_connect
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK, InvalidStatus
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
