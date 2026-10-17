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
