"""LineService.start_loading and get_content: LINE's loading animation and the photo download (8 Nov 2026)."""

import logging

import pytest

from app.services import line_service
from app.services.line_service import LineService

CHAT_ID = "Uchat0123456789abcdef"
MESSAGE_ID = "msg-5551234567890"


class _Resp:
    def __init__(self, status, chunks=()):
        self.status_code = status
        self._chunks = list(chunks)
        self.closed = False

    def iter_content(self, chunk_size=1):
        yield from self._chunks

    def close(self):
        self.closed = True


def _service(monkeypatch):
    service = LineService()
    monkeypatch.setattr(LineService, "_available", True)
    monkeypatch.setattr(line_service, "LINE_CHANNEL_ACCESS_TOKEN", "TOKEN123")
    return service


def test_start_loading_posts_the_chat_and_seconds(monkeypatch):
    service = _service(monkeypatch)
    seen = {}

    def post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, body=json, headers=headers)
        return _Resp(202)

    monkeypatch.setattr(line_service.requests, "post", post)
    assert service.start_loading(CHAT_ID) is True
    assert seen["url"] == "https://api.line.me/v2/bot/chat/loading/start"
    assert seen["body"] == {"chatId": CHAT_ID, "loadingSeconds": 60}
    assert seen["headers"]["Authorization"] == "Bearer TOKEN123"
    assert service.start_loading(CHAT_ID, 30) is True and seen["body"]["loadingSeconds"] == 30

    monkeypatch.setattr(line_service.requests, "post", lambda *a, **k: _Resp(500))
    assert service.start_loading(CHAT_ID) is False

    def boom(*a, **k):
        raise line_service.requests.Timeout(CHAT_ID)

    monkeypatch.setattr(line_service.requests, "post", boom)
    assert service.start_loading(CHAT_ID) is False


def test_start_loading_without_a_token_is_false(monkeypatch):
    service = _service(monkeypatch)
    monkeypatch.setattr(LineService, "_available", False)
    monkeypatch.setattr(line_service.requests, "post", lambda *a, **k: pytest.fail("must not call LINE"))
    assert service.start_loading(CHAT_ID) is False


def test_get_content_streams_and_caps(monkeypatch):
    service = _service(monkeypatch)
    seen = {}

    def get(url, headers=None, stream=None, timeout=None):
        seen.update(url=url, headers=headers, stream=stream)
        return _Resp(200, [b"abcd", b"efgh", b"ij"])

    monkeypatch.setattr(line_service.requests, "get", get)
    assert service.get_content(MESSAGE_ID, 10) == ("ok", b"abcdefghij")
    assert seen["url"] == f"https://api-data.line.me/v2/bot/message/{MESSAGE_ID}/content"
    assert seen["headers"]["Authorization"] == "Bearer TOKEN123" and seen["stream"] is True
    assert service.get_content(MESSAGE_ID, 9) == ("too_large", None)

    monkeypatch.setattr(line_service.requests, "get", lambda *a, **k: _Resp(404))
    assert service.get_content(MESSAGE_ID, 10) == ("failed", None)

    def boom(*a, **k):
        raise line_service.requests.Timeout(MESSAGE_ID)

    monkeypatch.setattr(line_service.requests, "get", boom)
    assert service.get_content(MESSAGE_ID, 10) == ("failed", None)

    def breaks(*a, **k):
        class Broken(_Resp):
            def iter_content(self, chunk_size=1):
                yield b"ab"
                raise line_service.requests.ConnectionError(MESSAGE_ID)
        return Broken(200)

    monkeypatch.setattr(line_service.requests, "get", breaks)
    assert service.get_content(MESSAGE_ID, 10) == ("failed", None)


def test_get_content_stops_reading_at_the_cap(monkeypatch):
    service = _service(monkeypatch)
    resp = _Resp(200)

    def chunks(chunk_size=1):
        yield b"x" * 6
        yield b"x" * 6
        pytest.fail("read past the cap")

    resp.iter_content = chunks
    monkeypatch.setattr(line_service.requests, "get", lambda *a, **k: resp)
    assert service.get_content(MESSAGE_ID, 10) == ("too_large", None)
    assert resp.closed


def test_get_content_without_a_token_is_failed(monkeypatch):
    service = _service(monkeypatch)
    monkeypatch.setattr(LineService, "_available", False)
    monkeypatch.setattr(line_service.requests, "get", lambda *a, **k: pytest.fail("must not call LINE"))
    assert service.get_content(MESSAGE_ID, 10) == ("failed", None)


def test_content_calls_log_no_ids(monkeypatch, caplog):
    service = _service(monkeypatch)

    def boom(*a, **k):
        raise line_service.requests.ConnectionError(f"{CHAT_ID} {MESSAGE_ID} TOKEN123")

    monkeypatch.setattr(line_service.requests, "post", boom)
    monkeypatch.setattr(line_service.requests, "get", boom)
    with caplog.at_level(logging.DEBUG):
        service.start_loading(CHAT_ID)
        service.get_content(MESSAGE_ID, 10)
        monkeypatch.setattr(line_service.requests, "get", lambda *a, **k: _Resp(404))
        monkeypatch.setattr(line_service.requests, "post", lambda *a, **k: _Resp(500))
        service.start_loading(CHAT_ID)
        service.get_content(MESSAGE_ID, 10)
    text = caplog.text
    assert CHAT_ID not in text and MESSAGE_ID not in text and "TOKEN123" not in text
