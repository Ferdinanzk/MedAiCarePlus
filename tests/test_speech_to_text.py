"""Speech-to-text for the robot's 「我吃完了」 listener in the cloud (services/speech_to_text.py, POST /api/device/stt,
quick fixes 2 of 19 Oct 2026): the gate (a live voice target with a key, the robot's microphone consent), the audio
limits, the call to OpenAI's transcription endpoint (faked here at the HTTP layer), the answer, the failures, and logs
that never carry the words, the audio or the key."""

import io
import logging
import sys
import time
import types
import wave

import pytest
import requests

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config   # noqa: E402
from app.services import ai_settings, consent_service, speech_to_text   # noqa: E402
from app.services.device_auth import get_device   # noqa: E402
from tests import test_device_api   # noqa: E402

env = test_device_api.env   # the device API harness: env.robot is the robot's client on the device port
TARGET = ai_settings.LiveTarget("sk-x", "gpt-live-1", "marin")
MICROPHONE = {**test_device_api.CONSENT,
              "robot_microphone": {"granted": True, "terms_version": config.TERMS_VERSION}}
WORDS = "我吃完了"
ONE_SECOND = b"\x10\x27" * 16000   # PCM16 mono 16 kHz
RAW = {"Content-Type": "application/octet-stream"}


class Answer:
    def __init__(self, status_code=200, payload=None, raises=None):
        self.status_code = status_code
        self._payload = {"text": WORDS} if payload is None else payload
        self._raises = raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._payload


@pytest.fixture
def stt(env, monkeypatch):
    """A live target with a key, the robot's microphone consent, and OpenAI's transcription endpoint faked: `posts`
    holds each call's (url, timeout, headers, data, files); `answer` is what the next call gets (an Answer, or an
    exception it raises); `delay` holds the call that long first."""
    w = types.SimpleNamespace(posts=[], answer=Answer(), target=TARGET, consent=MICROPHONE, delay=0.0)

    def post(url, timeout, headers, data, files):
        w.posts.append((url, timeout, headers, data, files))
        if w.delay:
            time.sleep(w.delay)
        if isinstance(w.answer, Exception):
            raise w.answer
        return w.answer

    async def get_state(u_id):
        assert u_id == 7
        return w.consent

    monkeypatch.setattr(speech_to_text.requests, "post", post)
    monkeypatch.setattr(ai_settings, "live_target", lambda snapshot=None: w.target)
    monkeypatch.setattr(consent_service, "get_state", get_state)
    w.robot = env.robot
    w.browser = env.browser
    return w


def _dependency_calls(dependant):
    calls = {dependant.call}
    for child in dependant.dependencies:
        calls.update(_dependency_calls(child))
    return calls


def _stt(w, audio=ONE_SECOND, language="zh-TW"):
    return w.robot.post(f"/api/device/stt?language={language}", content=audio, headers=RAW)


# ── the call ──

def test_the_settings_are_the_contracts():
    assert speech_to_text.STT_MODEL == "gpt-4o-mini-transcribe"   # OpenAI's docs, checked 19 Oct 2026
    assert speech_to_text.STT_TIMEOUT_SECONDS == 8
    assert speech_to_text.TRANSCRIBE_URL == "https://api.openai.com/v1/audio/transcriptions"
    assert speech_to_text.MAX_BYTES == 480_000 and speech_to_text.MIN_BYTES == 6_400   # 15 s and 0.2 s


def test_a_segment_goes_to_openai_as_a_wav_and_the_transcript_comes_back(stt):
    response = _stt(stt)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"text", "model", "ms"}
    assert body["text"] == WORDS and body["model"] == "gpt-4o-mini-transcribe"
    assert isinstance(body["ms"], int) and body["ms"] >= 0
    ((url, timeout, headers, data, files),) = stt.posts                      # one try
    assert url == speech_to_text.TRANSCRIBE_URL and timeout == 8
    assert headers == {"Authorization": "Bearer sk-x"}                       # the live connection's key
    assert data["model"] == "gpt-4o-mini-transcribe" and data["language"] == "zh"
    assert data["response_format"] == "json"
    assert "台灣" in data["prompt"] and "機器人" in data["prompt"] and "藥" in data["prompt"]
    assert WORDS not in data["prompt"]   # the prompt never suggests the claim itself
    name, wav_bytes, content_type = files["file"]
    assert name.endswith(".wav") and content_type == "audio/wav"
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert wav.readframes(wav.getnframes()) == ONE_SECOND


def test_english_is_asked_for_in_english(stt):
    stt.answer = Answer(payload={"text": " I'm finished. "})
    response = _stt(stt, language="en")
    assert response.status_code == 200 and response.json()["text"] == "I'm finished."
    (_, _, _, data, _), = stt.posts
    assert data["language"] == "en" and "Taiwan" in data["prompt"] and "robot" in data["prompt"]
    assert data["prompt"] != speech_to_text.PROMPTS["zh-TW"]


def test_any_other_language_is_refused_before_any_call(stt):
    assert _stt(stt, language="fr").status_code == 422
    assert stt.posts == []


def test_only_on_the_device_port_and_only_with_a_device_token(stt, env):
    assert stt.browser.post("/api/device/stt", content=ONE_SECOND, headers=RAW).status_code == 404
    (route,) = [r for r in env.app.routes if getattr(r, "path", "") == "/api/device/stt"]
    assert route.methods == {"POST"} and get_device in _dependency_calls(route.dependant)
    assert stt.posts == []


# ── the gate ──

@pytest.mark.parametrize("missing", ["target", "key", "microphone"])
def test_without_cloud_stt_the_robot_gets_a_404_and_uses_its_own_whisper(stt, missing):
    if missing == "target":
        stt.target = None                                       # no live voice target (LIVE_VOICE_TEST off, no Check)
    elif missing == "key":
        stt.target = ai_settings.LiveTarget("", "gpt-live-1", "marin")
    else:
        stt.consent = test_device_api.CONSENT                   # the robot's camera consent, not its microphone's
    response = _stt(stt)
    assert response.status_code == 404 and response.json() == {"detail": "no_cloud_stt"}
    assert stt.posts == []


def test_a_withdrawn_microphone_consent_closes_the_gate(stt):
    stt.consent = {**MICROPHONE, "robot_microphone": {"granted": False, "terms_version": config.TERMS_VERSION}}
    assert _stt(stt).json() == {"detail": "no_cloud_stt"}
    stt.consent = {**MICROPHONE, "robot_microphone": {"granted": True, "terms_version": "old"}}
    assert _stt(stt).json() == {"detail": "no_cloud_stt"} and stt.posts == []


# ── the limits ──

def test_at_most_15_seconds_of_audio(stt):
    assert _stt(stt, b"\0\0" * 240_000).status_code == 200                # exactly 15 s
    assert len(stt.posts) == 1
    response = _stt(stt, b"\0\0" * 240_001)
    assert response.status_code == 413 and len(stt.posts) == 1          # never sent


def test_at_least_a_fifth_of_a_second_of_audio(stt):
    assert _stt(stt, b"\0\0" * 3_200).status_code == 200                  # exactly 0.2 s
    for short in (b"\0\0" * 3_199, b""):
        assert _stt(stt, short).status_code == 422
    assert len(stt.posts) == 1


def test_an_odd_number_of_bytes_is_no_pcm16_and_is_never_sent(stt):
    """PCM16 has 2 bytes a sample: an odd body would go out as a WAV whose odd data chunk lacks RIFF's pad byte."""
    for odd in (b"\0\0" * 3_200 + b"\0", b"\0\0" * 239_999 + b"\0"):   # 0.2 s and 15 s, one byte off
        response = _stt(stt, odd)
        assert response.status_code == 422 and response.json() == {"detail": "audio_not_pcm16"}
    assert stt.posts == []


# ── the failures ──

@pytest.mark.parametrize("answer, reason", [
    (Answer(status_code=500), "answered 500"),
    (Answer(status_code=401), "answered 401"),
    (Answer(status_code=429), "answered 429"),
    (requests.Timeout("read timed out"), "Timeout"),
    (requests.ConnectionError("no route to host"), "ConnectionError"),
    (Answer(raises=ValueError(f"Expecting value: {WORDS}")), "ValueError"),
    (Answer(payload=[WORDS]), "no text"),
    (Answer(payload={"text": None}), "no text"),
])
def test_any_failure_is_a_502_with_one_warning_and_no_words(stt, caplog, answer, reason):
    stt.answer = answer
    with caplog.at_level(logging.INFO, logger="app.services.speech_to_text"):
        response = _stt(stt)
    assert response.status_code == 502 and response.json() == {"detail": "stt_failed"}
    assert len(stt.posts) == 1                                          # one try, never a retry
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and reason in warnings[0].getMessage()
    assert WORDS not in caplog.text and "sk-x" not in caplog.text


def test_a_call_that_takes_too_long_is_given_up(stt, monkeypatch, caplog):
    """requests' timeout limits each connect and each read, not the whole call: the route waits STT_TIMEOUT_SECONDS
    in all, and the robot then uses its own Whisper."""
    monkeypatch.setattr(speech_to_text, "STT_TIMEOUT_SECONDS", 0.2)
    stt.delay = 1.0
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="app.services.speech_to_text"):
        response = _stt(stt)
    assert response.status_code == 502 and response.json() == {"detail": "stt_failed"}
    assert time.monotonic() - started < 0.9
    assert "TimeoutError" in caplog.text and WORDS not in caplog.text


# ── the logs ──

def test_a_transcript_is_never_logged_nor_the_audio_nor_the_key(stt, caplog):
    with caplog.at_level(logging.DEBUG):
        assert _stt(stt).status_code == 200
    assert WORDS not in caplog.text and "sk-x" not in caplog.text
    lines = [r.getMessage() for r in caplog.records if r.name == "app.services.speech_to_text"]
    assert len(lines) == 1 and "1.0 s" in lines[0] and "ms" in lines[0]   # the length and the time only
