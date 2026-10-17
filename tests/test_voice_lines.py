"""Reachy's own lines around a live chat in the live voice (services/voice_lines.py): the words, the spoken form of
the help line, the key, the robot's WAV format, the storage and its consent gate, the line route, the render-ahead at
startup and when a task is queued, the [STARTUP] stage and the retry job."""

import asyncio
import hashlib
import io
import logging
import struct
import sys
import types
import wave
from datetime import timedelta

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config, startup_report   # noqa: E402
from app.services import ai_settings, consent_service, conversation, live_session, med_clips, memory, voice_lines  # noqa: E402
from tests import test_device_api   # noqa: E402
from tests.test_med_clips import _streamed_wav   # noqa: E402

env = test_device_api.env   # the device API harness: env.robot is the robot's client on the device port
TARGET = ai_settings.LiveTarget("sk-x", "gpt-live-1", "marin")
CHECKIN = {scope: {"granted": True, "terms_version": config.TERMS_VERSION}
           for scope in ("core", "robot_camera", "robot_microphone", "cloud_voice", "conversation_analysis",
                         "safety_alerts")}
OPENING_ZH = "王奶奶，今天感覺怎麼樣？想跟我聊聊嗎？"
OPENING_EN = "王奶奶, how are you feeling today? Would you like to chat?"


# ── the words ──

def test_every_line_has_its_words_in_both_languages():
    assert voice_lines.line_text("checkin_greeting", "zh-TW") == "您好！我來找您聊聊天。"
    assert voice_lines.line_text("checkin_greeting", "en") == "Hello! I came to have a chat with you."
    assert voice_lines.line_text("rest", "zh-TW") == "我先休息一下，有需要再叫我喔。"
    assert voice_lines.line_text("rest", "en") == "I'll rest for a bit. Call me if you need anything."
    assert voice_lines.line_text("medicine", "zh-TW") == "該吃藥囉，我們等一下再聊。"
    assert voice_lines.line_text("medicine", "en") == "It's time for your medicine. Let's talk again in a little while."
    assert voice_lines.line_text("wind_down", "zh-TW") == "這次就到這裡，請多保重！"
    assert voice_lines.line_text("wind_down", "en") == "That's all for now. Take care!"
    assert voice_lines.line_text("repeat", "zh-TW") is live_session.REPEAT_LINES["zh-TW"]        # the same constant
    assert voice_lines.line_text("helpline", "en") is conversation.HELPLINE["en"]
    assert voice_lines.line_text("opening", "zh-TW", "王奶奶") == OPENING_ZH
    assert voice_lines.line_text("opening", "en-US", None) == conversation.OPENING["en"]      # the language folds
    assert voice_lines.line_text("reminder_lead", "zh-TW", "王奶奶") == "王奶奶，我來提醒您一件事。"
    assert voice_lines.line_text("reminder_lead", "en", None) == "I'm here to remind you of something."
    assert voice_lines.line_text("dance", "zh-TW") is None
    assert voice_lines.LINE_IDS == ("checkin_greeting", "repeat", "helpline", "rest", "medicine", "wind_down",
                                    "opening", "reminder_lead")


def test_the_help_lines_numbers_are_spelled_digit_by_digit_for_the_voice():
    zh = voice_lines.speech_input("helpline", conversation.HELPLINE["zh-TW"], "zh-TW")
    assert "一一九" in zh and "一九二五" in zh and "24 小時" in zh and "119" not in zh and "1925" not in zh
    en = voice_lines.speech_input("helpline", conversation.HELPLINE["en"], "en")
    assert "one one nine" in en and "one nine two five" in en and "24 hours" in en
    assert voice_lines.speech_input("rest", "我先休息 119 秒", "zh-TW") == "我先休息 119 秒"   # only the help line
    assert "digit" in voice_lines.instructions_for("helpline")
    assert voice_lines.instructions_for("helpline") != voice_lines.instructions_for("rest") == voice_lines.instructions_for("opening")


# ── the key ──

def test_the_key_follows_the_model_the_voice_the_instructions_and_the_words():
    text, instructions = "我先休息一下，有需要再叫我喔。", voice_lines.instructions_for("rest")
    key = voice_lines.line_key(text, "marin", instructions)
    assert key == hashlib.sha256(f"gpt-4o-mini-tts|marin|{instructions}|{text}".encode("utf-8")).hexdigest()[:20]
    assert len(key) == 20 and key != med_clips.clip_key(text, "marin")      # the medicine keys are untouched
    assert key != voice_lines.line_key(text, "coral", instructions) != voice_lines.line_key(text, "marin", "other")
    assert voice_lines.key_for("rest", text, "zh-TW", "marin") == key
    help_zh = conversation.HELPLINE["zh-TW"]
    assert voice_lines.key_for("helpline", help_zh, "zh-TW", "marin") == voice_lines.line_key(
        voice_lines.speech_input("helpline", help_zh, "zh-TW"), "marin", voice_lines.instructions_for("helpline"))


def test_expected_clip_names_the_copy_the_robot_should_play(monkeypatch):
    monkeypatch.setattr(ai_settings, "live_target", lambda snapshot=None: None)
    assert voice_lines.expected_clip("helpline", conversation.HELPLINE["zh-TW"], "zh-TW") is None
    monkeypatch.setattr(ai_settings, "live_target", lambda snapshot=None: TARGET)
    clip = voice_lines.expected_clip("helpline", conversation.HELPLINE["zh-TW"], "zh-TW")
    assert clip == {"id": "helpline", "key": voice_lines.key_for("helpline", conversation.HELPLINE["zh-TW"], "zh-TW", "marin")}
    assert voice_lines.expected_clip("opening", OPENING_EN, "en")["key"] == voice_lines.key_for("opening", OPENING_EN, "en", "marin")
    assert voice_lines.expected_clip("dance", "x", "zh-TW") is None
    assert voice_lines.expected_clip("repeat", "", "zh-TW") is None


# ── the robot's format ──

def _samples(wav_bytes: bytes) -> tuple[tuple, int, int]:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        assert w.getsampwidth() == 2
        frames = w.readframes(w.getnframes())
        return struct.unpack("<" + "h" * (len(frames) // 2), frames), w.getframerate(), w.getnchannels()


def test_to_robot_wav_gives_pcm16_mono_16_khz_and_trims_the_leading_silence():
    silence, tone = b"\x00\x00" * 4800, b"\x10\x27" * 2400   # 200 ms of silence, then 100 ms at level 10000, 24 kHz
    samples, rate, channels = _samples(voice_lines.to_robot_wav(_streamed_wav(silence + tone, rate=24000)))
    assert (rate, channels) == (16000, 1)
    # the tone (100 ms = 1600 frames at 16 kHz) with at most about 10 ms of what came before it (the resampler's
    # filter rings a little before the first sound)
    assert 1600 <= len(samples) <= 1600 + 160 + 64 and max(samples) > 9000
    stereo = _streamed_wav(b"\x10\x27\x10\x27" * 1600, rate=16000, channels=2)        # already 16 kHz, two channels
    samples, rate, channels = _samples(voice_lines.to_robot_wav(stereo))
    assert (rate, channels, len(samples)) == (16000, 1, 1600) and samples[0] == 10000  # mixed to mono, no trim needed
    with pytest.raises(ValueError):
        voice_lines.to_robot_wav(b"OggS" + b"\0" * 40)                                 # not a WAV
    with pytest.raises(ValueError):
        voice_lines.to_robot_wav(_streamed_wav(b"\x00\x00" * 1600, rate=16000))        # no sound at all: never kept


def test_render_line_renders_with_the_lines_instructions_then_converts(monkeypatch):
    renders = []

    def render(text, voice, api_key, instructions=med_clips.INSTRUCTIONS):
        renders.append((text, voice, api_key, instructions))
        if voice == "broken":
            raise RuntimeError("speech API answered 502")
        return _streamed_wav(b"\x10\x27" * 2400, rate=24000)

    monkeypatch.setattr(med_clips, "render", render)
    out = voice_lines.render_line("您好！", "marin", "sk-x", voice_lines.instructions_for("checkin_greeting"))
    assert renders == [("您好！", "marin", "sk-x", voice_lines.INSTRUCTIONS["companion"])]
    assert _samples(out)[1:] == (16000, 1)
    with pytest.raises(RuntimeError, match="speech API answered 502"):
        voice_lines.render_line("您好！", "broken", "sk-x", voice_lines.instructions_for("checkin_greeting"))


# ── storage ──

class _Pool:
    """A pool whose connection is never used: memory.call_name is faked."""

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def voice(monkeypatch, tmp_path):
    """A live target (marin), a patient with check-in consent and the call name 王奶奶, the speech API faked
    (`renders`: (text, voice, api_key, instructions) per call; `fail`: the error the next render raises)."""
    monkeypatch.setattr(config, "MED_CLIP_DIR", tmp_path)
    w = types.SimpleNamespace(renders=[], target=TARGET, fail=None, consent=dict(CHECKIN), name="王奶奶")

    def render(text, voice, api_key, instructions=med_clips.INSTRUCTIONS):
        w.renders.append((text, voice, api_key, instructions))
        if w.fail is not None:
            raise w.fail
        return _streamed_wav(b"\x10\x27" * 2400, rate=24000)

    async def get_state(u_id):
        return w.consent

    async def call_name(conn, u_id, memory_on):
        return w.name

    monkeypatch.setattr(med_clips, "render", render)
    monkeypatch.setattr(ai_settings, "live_target", lambda snapshot=None: w.target)
    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(memory, "call_name", call_name)
    monkeypatch.setattr(voice_lines, "get_pool", lambda: _Pool())
    voice_lines._renders.clear()
    return w


def test_a_fixed_line_is_rendered_once_per_key_and_kept_under_lines(voice, tmp_path):
    first = asyncio.run(voice_lines.line(None, "rest", "zh-TW"))
    again = asyncio.run(voice_lines.line(None, "rest", "zh-TW"))
    assert first is not None and again == first and len(voice.renders) == 1
    assert voice.renders[0] == ("我先休息一下，有需要再叫我喔。", "marin", "sk-x", voice_lines.instructions_for("rest"))
    assert [p.name for p in (tmp_path / "lines" / "zh-TW").iterdir()] == [f"rest_{first[1]}.wav"]
    assert _samples(first[0])[1:] == (16000, 1)
    voice.target = ai_settings.LiveTarget("sk-x", "gpt-live-1", "coral")   # another voice: another key and file
    other = asyncio.run(voice_lines.line(None, "rest", "zh-TW"))
    assert other[1] != first[1] and len(voice.renders) == 2
    assert len(list((tmp_path / "lines" / "zh-TW").iterdir())) == 2          # the old file stays (never deleted)
    voice.target = TARGET
    # the help line goes to the API in its spoken form, with its own instructions; the robot gets the written words
    kept = asyncio.run(voice_lines.line(None, "helpline", "zh-TW"))
    assert "一一九" in voice.renders[-1][0] and voice.renders[-1][3] == voice_lines.instructions_for("helpline")
    found = asyncio.run(voice_lines.locate(None, "helpline", "zh-TW"))
    assert found.text == conversation.HELPLINE["zh-TW"] and found.key == kept[1]
    # and the key is the one the relay sends beside the words (expected_clip): the robot's copy matches
    assert kept[1] == voice_lines.expected_clip("helpline", conversation.HELPLINE["zh-TW"], "zh-TW")["key"]


def test_a_patients_line_needs_checkin_consent_and_follows_the_call_name(voice, tmp_path):
    first = asyncio.run(voice_lines.line(7, "opening", "zh-TW"))
    assert first is not None and voice.renders[-1][0] == OPENING_ZH
    assert [p.name for p in (tmp_path / "7").iterdir()] == [f"line_opening_{first[1]}.wav"]
    assert first[1] == voice_lines.expected_clip("opening", OPENING_ZH, "zh-TW")["key"]   # what `ready` carries
    assert asyncio.run(voice_lines.line(7, "reminder_lead", "en")) is not None
    assert voice.renders[-1][0] == "王奶奶, I'm here to remind you of something."
    voice.name = "阿嬤"                                     # the call name changed: a new key, the old file stays
    renamed = asyncio.run(voice_lines.line(7, "opening", "zh-TW"))
    assert renamed[1] != first[1] and len(voice.renders) == 3
    voice.name = None
    assert asyncio.run(voice_lines.line(7, "reminder_lead", "zh-TW")) is not None
    assert voice.renders[-1][0] == "我來提醒您一件事。"
    voice.consent = {"core": CHECKIN["core"], "robot_camera": CHECKIN["robot_camera"]}   # check-in consent gone
    assert asyncio.run(voice_lines.line(7, "opening", "zh-TW")) is None and len(voice.renders) == 4
    assert asyncio.run(voice_lines.locate(7, "reminder_lead", "zh-TW")) is None
    assert asyncio.run(voice_lines.line(7, "rest", "zh-TW")) is not None     # a fixed line needs no check-in consent


def test_the_language_is_folded_and_only_the_known_lines_have_a_place(voice, tmp_path):
    found = asyncio.run(voice_lines.locate(None, "rest", "en-GB"))
    assert found == asyncio.run(voice_lines.locate(None, "rest", "en"))
    assert found.path == tmp_path / "lines" / "en" / f"rest_{found.key}.wav"
    assert asyncio.run(voice_lines.locate(None, "rest", "")).language == "zh-TW"
    for line_id in ("../rest", "rest.wav", "REST", "med_10", ""):
        assert asyncio.run(voice_lines.locate(None, line_id, "zh-TW")) is None
    assert asyncio.run(voice_lines.locate(7, "opening", "en")).path == (
        tmp_path / "7" / f"line_opening_{voice_lines.key_for('opening', OPENING_EN, 'en', 'marin')}.wav")


def test_no_line_without_a_target_or_when_the_api_fails_and_the_log_never_has_the_words_or_the_key(voice, caplog):
    voice.fail = RuntimeError("speech API answered 502")
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(voice_lines.line(7, "opening", "zh-TW")) is None
        voice.fail = OSError("disk")
        assert asyncio.run(voice_lines.line(None, "rest", "en")) is None
    assert "voice line opening (zh-TW) could not be rendered (speech API answered 502)" in caplog.text
    assert "voice line rest (en) could not be rendered (OSError)" in caplog.text
    key = voice_lines.expected_clip("opening", OPENING_ZH, "zh-TW")["key"]
    assert "王奶奶" not in caplog.text and key not in caplog.text and "sk-x" not in caplog.text
    voice.fail = None
    assert asyncio.run(voice_lines.line(7, "opening", "zh-TW")) is not None    # no negative cache: tried again
    voice.target = None
    assert asyncio.run(voice_lines.line(None, "rest", "zh-TW")) is None
    voice.target = ai_settings.LiveTarget("", "gpt-live-1", "marin")
    assert asyncio.run(voice_lines.line(None, "rest", "zh-TW")) is None


def test_fetch_gives_nothing_once_the_live_voice_target_is_gone(voice):
    found = asyncio.run(voice_lines.locate(None, "rest", "zh-TW"))
    voice.target = None                       # the AI card's live section saved again: its Check cleared meanwhile
    assert asyncio.run(voice_lines.fetch(found)) is None and voice.renders == []


def test_two_fetches_of_one_line_at_once_render_it_once(voice):
    async def scenario():
        return await asyncio.gather(voice_lines.line(None, "wind_down", "en"), voice_lines.line(None, "wind_down", "en"))

    first, second = asyncio.run(scenario())
    assert first == second and first is not None and len(voice.renders) == 1


# ── the route ──

def test_the_robot_fetches_a_line_with_its_key_and_gets_a_304_before_any_render(env, monkeypatch):
    from pathlib import Path
    located = voice_lines.Located("rest", "en", "I'll rest for a bit. Call me if you need anything.", "marin",
                                  "k-rest", Path("rest.wav"))
    asked, fetched = [], []

    async def locate(u_id, line_id, language):
        asked.append((u_id, line_id, language))
        return located if (line_id, language) == ("rest", "en") else None

    async def fetch(found):
        fetched.append(found.key)
        return b"RIFFwav", found.key

    monkeypatch.setattr(voice_lines, "locate", locate)
    monkeypatch.setattr(voice_lines, "fetch", fetch)
    response = env.robot.get("/api/device/clips/line/rest?language=en")
    assert response.status_code == 200 and response.content == b"RIFFwav"
    assert response.headers["etag"] == '"k-rest"' and response.headers["cache-control"] == "private"
    assert response.headers["content-type"].startswith("audio/wav")
    assert asked == [(7, "rest", "en")] and fetched == ["k-rest"]          # the device's own patient
    # the robot's copy is current: 304 without a body, and nothing is rendered or read
    response = env.robot.get("/api/device/clips/line/rest?language=en", headers={"If-None-Match": '"k-rest"'})
    assert response.status_code == 304 and response.content == b"" and response.headers["etag"] == '"k-rest"'
    assert fetched == ["k-rest"]
    # another key (the voice changed, the name changed): the new copy
    response = env.robot.get("/api/device/clips/line/rest?language=en", headers={"If-None-Match": '"k-old"'})
    assert response.status_code == 200 and fetched == ["k-rest", "k-rest"]
    assert env.robot.get("/api/device/clips/line/rest").status_code == 404   # zh-TW: not located in this fake


def test_no_line_is_a_404_but_a_failing_lookup_is_not(env, monkeypatch):
    from pathlib import Path

    async def nothing(u_id, line_id, language):
        return None
    monkeypatch.setattr(voice_lines, "locate", nothing)
    response = env.robot.get("/api/device/clips/line/opening")
    assert response.status_code == 404 and response.json() == {"detail": "No clip for this line"}

    located = voice_lines.Located("rest", "zh-TW", "我先休息一下，有需要再叫我喔。", "marin", "k", Path("rest.wav"))

    async def found(u_id, line_id, language):
        return located

    async def failed(found):
        return None
    monkeypatch.setattr(voice_lines, "locate", found)
    monkeypatch.setattr(voice_lines, "fetch", failed)
    assert env.robot.get("/api/device/clips/line/rest").status_code == 404   # the render failed: Matcha this time

    async def broken(u_id, line_id, language):
        raise RuntimeError("database down")
    monkeypatch.setattr(voice_lines, "locate", broken)
    with pytest.raises(RuntimeError):   # a 500 on the robot, never a 404: the robot keeps its copy
        env.robot.get("/api/device/clips/line/opening")
