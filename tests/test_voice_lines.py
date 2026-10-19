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
from pathlib import Path

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
    # the robot's lines.LINE_IDS lists exactly these (quick fixes of 19 Oct 2026: the contract between both sides)
    assert voice_lines.LINE_IDS == (
        "checkin_greeting", "repeat", "helpline", "rest", "medicine", "wind_down",
        "wake_greeting", "reminder", "searching", "waiting_for_patient", "waiting_for_tablet", "say_when_done",
        "med_prompt_generic", "already_taken", "confirm_with_caregiver", "help", "thanks", "ack", "thinking_1",
        "thinking_2", "thinking_3", "family_no_answer", "retry_limit",
        "opening", "reminder_lead", "dose_search", "family_not_taken", "family_taken")


MANIFEST = Path(__file__).resolve().parents[2] / "medcare-reachy-bridge" / "medcare_reachy" / "bridge" / "clips" / \
    "manifest.json"
MANIFEST_IDS = ("wake_greeting", "reminder", "searching", "waiting_for_patient", "waiting_for_tablet", "say_when_done",
                "med_prompt_generic", "already_taken", "confirm_with_caregiver", "help", "wind_down", "thanks", "ack",
                "thinking_1", "thinking_2", "thinking_3")


def test_the_robots_clip_lines_have_the_manifests_words():
    """Every clip of the robot's manifest is a fixed line with the manifest's words, so the GPT copy says what the
    Matcha clip says. Skipped where the robot repo is not beside this one."""
    if not MANIFEST.is_file():
        pytest.skip("the robot repo (medcare-reachy-bridge) is not beside this repo")
    import json
    clips = json.loads(MANIFEST.read_text(encoding="utf-8"))["clips"]
    assert set(clips) == set(MANIFEST_IDS)
    for line_id in MANIFEST_IDS:
        assert line_id in voice_lines.FIXED_LINES
        for language in ("zh-TW", "en"):
            assert voice_lines.line_text(line_id, language) == clips[line_id][language], (line_id, language)


def test_the_new_fixed_lines_have_their_words_and_the_fillers_their_own_instructions():
    assert voice_lines.line_text("wake_greeting", "zh-TW") == "您好！吃藥的時間到了。"
    assert voice_lines.line_text("thinking_3", "en") == "Let me think it over."
    assert voice_lines.line_text("family_no_answer", "zh-TW") == "家人還沒有回覆，請先不要再吃一次喔。"
    assert voice_lines.line_text("family_no_answer", "en") == (
        "Your family hasn't answered yet. Please don't take it again for now.")
    assert voice_lines.line_text("retry_limit", "zh-TW") == "我請家人來幫忙您吃藥喔。"
    assert voice_lines.line_text("retry_limit", "en") == "I'll ask your family to help you with your medicine."
    ack, thinking = voice_lines.instructions_for("ack"), voice_lines.instructions_for("thinking_1")
    assert "short, soft acknowledgement" in ack and "thinking aloud" in thinking
    assert voice_lines.instructions_for("thinking_2") == voice_lines.instructions_for("thinking_3") == thinking
    companion = voice_lines.instructions_for("wake_greeting")
    assert len({ack, thinking, companion, voice_lines.instructions_for("helpline")}) == 4
    assert voice_lines.instructions_for("family_taken") == companion


def test_the_dose_lines_carry_the_call_name_or_none():
    assert voice_lines.line_text("dose_search", "zh-TW", "王奶奶") == "吃藥的時間到了，我來找王奶奶吃藥。"
    assert voice_lines.line_text("dose_search", "zh-TW", None) == "吃藥的時間到了，我來找您吃藥。"
    assert voice_lines.line_text("dose_search", "en", "Grandma Wang") == (
        "It's time for your medicine. I've come to find Grandma Wang.")
    assert voice_lines.line_text("dose_search", "en", None) == "It's time for your medicine. I've come to find you."
    assert voice_lines.line_text("family_not_taken", "zh-TW", "王奶奶") == (
        "王奶奶，家人說您這次的藥還沒有吃完喔，我們現在把它吃完吧。")
    assert voice_lines.line_text("family_not_taken", "zh-TW", None) == "家人說您這次的藥還沒有吃完喔，我們現在把它吃完吧。"
    assert voice_lines.line_text("family_not_taken", "en", "Grandma Wang") == (
        "Grandma Wang, your family says you haven't finished this medicine yet. Let's finish it now.")
    assert voice_lines.line_text("family_not_taken", "en", None) == (
        "Your family says you haven't finished this medicine yet. Let's finish it now.")
    assert voice_lines.line_text("family_taken", "zh-TW", "王奶奶") == "王奶奶，家人確認您已經吃藥了，拍拍手！您越來越健康了！"
    assert voice_lines.line_text("family_taken", "zh-TW", None) == "家人確認您已經吃藥了，拍拍手！您越來越健康了！"
    assert voice_lines.line_text("family_taken", "en", "Grandma Wang") == (
        "Grandma Wang, your family confirmed you've taken your medicine. Well done! You're getting healthier every "
        "day!")
    assert voice_lines.line_text("family_taken", "en", None) == (
        "Your family confirmed you've taken your medicine. Well done! You're getting healthier every day!")
    assert set(voice_lines.DOSE_LINES) == {"dose_search", "family_not_taken", "family_taken"}
    assert set(voice_lines.DOSE_LINES) < set(voice_lines.PATIENT_LINES)


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


def test_no_pause_in_a_kept_line_is_long_enough_for_the_robot_to_report_idle():
    """The robot's live player (bridge/live.py) reports `idle` after 0.6 s of quiet 20 ms blocks (no sample above
    120). A build before the whole-branch review of 17 Oct 2026 did so even inside its own line, and the server then
    sent what waits for that idle (the reminder after its lead-in) while the robot still dropped audio for the line;
    app 0.16.0 waits for its own line. As a second guard, and so a long pause never holds the reminder back, every
    quiet stretch after the first sound, the one at the end included, is cut to PAUSE_MAX_SECONDS; a shorter one
    stays."""
    sound, silence, hum = b"\x10\x27", b"\x00\x00", b"\x64\x00"   # level 10000, 0, and 100: still quiet to the robot
    # at 16 kHz: 0.3 s of sound, a 0.9 s pause, 0.3 s, a 0.2 s pause, 0.3 s, then 1.2 s of a faint hum at the end
    pcm = sound * 4800 + silence * 14400 + sound * 4800 + silence * 3200 + sound * 4800 + hum * 19200
    samples, rate, channels = _samples(voice_lines.to_robot_wav(_streamed_wav(pcm, rate=16000)))
    keep = int(16000 * voice_lines.PAUSE_MAX_SECONDS)
    assert voice_lines.PAUSE_MAX_SECONDS == 0.4 and (rate, channels) == (16000, 1)
    assert len(samples) == 3 * 4800 + keep + 3200 + keep          # the long ones cut, the 0.2 s pause kept whole
    assert samples[:4800] == (10000,) * 4800 and samples[-keep:] == (100,) * keep
    longest = run = 0   # the robot's rule: 320-sample blocks, quiet when no sample is above 120, idle after 30
    for start in range(0, len(samples), 320):
        run = run + 1 if max(abs(s) for s in samples[start:start + 320]) <= 120 else 0
        longest = max(longest, run)
    assert longest < 30


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


def test_the_dose_lines_need_only_the_robots_consent_and_follow_the_call_name(voice, tmp_path):
    """The dose flow's per-patient lines (dose_search, family_not_taken, family_taken) are said in a dose task, so
    the robot's consent (core and robot_camera: the device route's own gate) is enough; check-in consent is not
    needed."""
    voice.consent = {"core": CHECKIN["core"], "robot_camera": CHECKIN["robot_camera"]}   # no check-in consent
    kept = asyncio.run(voice_lines.line(7, "dose_search", "zh-TW"))
    assert kept is not None and voice.renders[-1][0] == "吃藥的時間到了，我來找王奶奶吃藥。"
    assert [p.name for p in (tmp_path / "7").iterdir()] == [f"line_dose_search_{kept[1]}.wav"]
    assert asyncio.run(voice_lines.line(7, "family_taken", "en")) is not None
    assert voice.renders[-1][0].startswith("王奶奶, your family confirmed")
    assert asyncio.run(voice_lines.line(7, "opening", "zh-TW")) is None    # the chat's lines still need check-in consent
    voice.name = None
    assert asyncio.run(voice_lines.line(7, "family_not_taken", "zh-TW")) is not None
    assert voice.renders[-1][0] == "家人說您這次的藥還沒有吃完喔，我們現在把它吃完吧。"
    voice.consent = {"core": CHECKIN["core"]}                              # the robot's consent withdrawn
    renders = len(voice.renders)
    assert asyncio.run(voice_lines.locate(7, "dose_search", "en")) is None
    assert asyncio.run(voice_lines.line(7, "family_taken", "zh-TW")) is None and len(voice.renders) == renders


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


def test_a_line_that_cannot_be_kept_is_a_warning_and_the_other_lines_still_render(voice, tmp_path, caplog):
    """A storage error under MED_CLIP_DIR (whole-branch review of 17 Oct 2026) is one WARNING and None, like a
    render that failed: it never stops the render-ahead pass, and the retry job ends with its one summary line."""
    (tmp_path / "lines").mkdir()
    (tmp_path / "lines" / "zh-TW").write_bytes(b"")   # a file where the zh-TW folder should be
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(voice_lines.line(None, "rest", "zh-TW")) is None
        failed = asyncio.run(voice_lines.render_fixed_ahead())
    assert failed == [f"{line_id}/zh-TW" for line_id in voice_lines.FIXED_LINES]   # every line was tried
    assert sorted(p.name.rsplit("_", 1)[0] for p in (tmp_path / "lines" / "en").iterdir()) == sorted(
        voice_lines.FIXED_LINES)                                                     # the English ones are kept
    assert len(voice.renders) == len(voice_lines.FIXED_LINES)   # a folder that cannot be made costs no render
    assert "voice line rest (zh-TW) could not be kept (FileExistsError)" in caplog.text
    keys = [voice_lines.key_for(line_id, voice_lines.line_text(line_id, "zh-TW"), "zh-TW", "marin")
            for line_id in voice_lines.FIXED_LINES]
    assert not any(key in caplog.text for key in keys) and "sk-x" not in caplog.text
    # the folder is there, but the file cannot be written (a directory where the .part file goes)
    found = asyncio.run(voice_lines.locate(None, "medicine", "en"))
    found.path.unlink()
    found.path.with_suffix(".part").mkdir()
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(voice_lines.fetch(found)) is None and len(voice.renders) == len(voice_lines.FIXED_LINES) + 1
        asyncio.run(voice_lines.retry_missing())      # the scheduler's job: no exception, its one summary line
    assert "voice line medicine (en) could not be kept" in caplog.text and found.key not in caplog.text
    missing = len(voice_lines.FIXED_LINES) + 1        # every zh-TW line, and medicine/en
    assert f"{missing} fixed lines are still missing" in caplog.text and "medicine/en" in caplog.text


def test_a_kept_line_that_cannot_be_read_is_a_warning_and_the_other_lines_still_render(voice, caplog, monkeypatch):
    """A kept line that cannot be read (a permission or disk fault under MED_CLIP_DIR: is_file passes those on, and
    read_bytes raises them) is one WARNING and None, like one that cannot be kept: never rendered over, and the
    render-ahead pass goes on with the other lines."""
    assert asyncio.run(voice_lines.render_fixed_ahead()) == []   # every fixed line kept
    renders = len(voice.renders)
    read_bytes = Path.read_bytes

    def refused(self):
        if self.name.startswith("rest_"):
            raise PermissionError(13, "refused by the test", str(self))   # its text holds the path, and so the key
        return read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", refused)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(voice_lines.line(None, "rest", "zh-TW")) is None
        assert asyncio.run(voice_lines.render_fixed_ahead()) == ["rest/zh-TW", "rest/en"]
    assert len(voice.renders) == renders
    assert "voice line rest (zh-TW) could not be read (PermissionError)" in caplog.text
    keys = [voice_lines.key_for("rest", voice_lines.line_text("rest", lang), lang, "marin") for lang in ("zh-TW", "en")]
    assert not any(key in caplog.text for key in keys)


def test_fetch_gives_nothing_once_the_live_voice_target_is_gone(voice, caplog):
    found = asyncio.run(voice_lines.locate(None, "rest", "zh-TW"))
    voice.target = None                       # the AI card's live section saved again: its Check cleared meanwhile
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(voice_lines.fetch(found)) is None and voice.renders == []
        voice.target = ai_settings.LiveTarget("", "gpt-live-1", "marin")   # or its key is gone
        assert asyncio.run(voice_lines.fetch(found)) is None and voice.renders == []
    assert "could not be rendered" not in caplog.text   # fetch's own re-check, never a render that failed


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


# ── rendered ahead: the fixed lines at startup, and again every 30 min ──

def test_the_fixed_lines_are_rendered_ahead_once_and_the_failures_are_named(voice, tmp_path):
    total = 2 * len(voice_lines.FIXED_LINES)                          # 23 lines x 2 languages
    assert len(voice_lines.FIXED_LINES) == 23
    assert asyncio.run(voice_lines.render_fixed_ahead()) == [] and len(voice.renders) == total
    for language in ("zh-TW", "en"):
        kept = {p.name.rsplit("_", 1)[0] for p in (tmp_path / "lines" / language).iterdir()}
        assert kept == set(voice_lines.FIXED_LINES)
    assert asyncio.run(voice_lines.render_fixed_ahead()) == [] and len(voice.renders) == total   # nothing missing now
    found = asyncio.run(voice_lines.locate(None, "helpline", "en"))
    found.path.unlink()                                               # one copy lost (a wiped volume)
    voice.fail = RuntimeError("speech API answered 503")
    assert asyncio.run(voice_lines.render_fixed_ahead()) == ["helpline/en"] and len(voice.renders) == total + 1
    voice.fail = None
    assert asyncio.run(voice_lines.render_fixed_ahead()) == [] and len(voice.renders) == total + 2
    voice.target = None
    assert asyncio.run(voice_lines.render_fixed_ahead()) == [] and len(voice.renders) == total + 2   # nothing to do
    # the fillers go to the API with their own instructions
    voice.target = TARGET
    ack = next(r for r in voice.renders if r[0] == "嗯")
    assert ack[3] == voice_lines.INSTRUCTIONS["ack"]


def test_the_startup_report_marks_the_voice_lines_stage_without_holding_up_the_summary(monkeypatch):
    async def nothing():
        pass
    monkeypatch.setattr(startup_report, "network_and_clock", nothing)
    monkeypatch.setattr(startup_report, "public_link", nothing)
    logged = []   # the voice_lines stage as it stood each time the summary line was logged
    monkeypatch.setattr(startup_report, "log_summary",
                        lambda: logged.append(dict(startup_report.summary()["stages"]["voice_lines"])))
    startup_report.begin()
    assert "voice_lines" in startup_report.STAGES
    monkeypatch.setattr(ai_settings, "live_target", lambda snapshot=None: None)
    asyncio.run(startup_report.finish(None, live_status="off"))
    assert logged[-1]["ok"] and "no live voice target" in logged[-1]["detail"] and not startup_report._tasks
    results = [[], ["helpline/en", "rest/zh-TW"]]
    gate = {}

    async def render_fixed_ahead():
        await gate["release"].wait()   # the real renders take a while: 12, each up to two tries of med_clips.TIMEOUT
        return results.pop(0)
    monkeypatch.setattr(ai_settings, "live_target", lambda snapshot=None: TARGET)
    monkeypatch.setattr(voice_lines, "render_fixed_ahead", render_fixed_ahead)

    async def scenario():
        gate["release"] = asyncio.Event()
        await startup_report.finish(None, live_status="ok")
        # the summary is logged before the renders finish: the stage is still pending, its renders in the background
        assert logged[-1]["pending"] and not logged[-1]["ok"] and "46 fixed lines" in logged[-1]["detail"]
        assert startup_report._tasks
        gate["release"].set()
        await asyncio.gather(*startup_report._tasks)
        return startup_report.summary()["stages"]["voice_lines"]   # the stage's final mark, after the renders

    stage = asyncio.run(scenario())
    assert stage["ok"] and not stage["pending"] and "46 fixed lines kept" in stage["detail"]
    stage = asyncio.run(scenario())
    assert not stage["ok"] and not stage["pending"] and "30 min" in stage["detail"]
    assert "helpline/en, rest/zh-TW" in stage["detail"] and "sk-x" not in stage["detail"]
    assert "voice_lines" in startup_report.summary()["degraded"] and len(logged) == 3   # one summary per finish
    startup_report._report.clear()


def test_the_missing_lines_are_tried_again_every_30_minutes(monkeypatch):
    from app.jobs import scheduler as jobs
    added = {}

    class Scheduler:
        def add_job(self, function, trigger, id, **kwargs):
            added[id] = (function, trigger, kwargs)

        def start(self):
            pass

    monkeypatch.setattr(jobs, "scheduler", Scheduler())
    jobs.start_scheduler()
    function, trigger, kwargs = added["voice_lines"]
    assert function is voice_lines.retry_missing and trigger.interval == timedelta(minutes=30)
    assert voice_lines.LINES_RETRY_MINUTES == 30
    assert kwargs["misfire_grace_time"] is None and kwargs["coalesce"] is True and kwargs["max_instances"] == 1
    assert asyncio.iscoroutinefunction(function)   # it runs on the loop; each render is in a worker thread


def test_the_retry_logs_what_is_still_missing(voice, caplog):
    voice.fail = RuntimeError("speech API answered 503")
    with caplog.at_level(logging.WARNING):
        asyncio.run(voice_lines.retry_missing())
    assert "46 fixed lines are still missing" in caplog.text and "checkin_greeting/zh-TW" in caplog.text


# ── rendered ahead: the patient's lines ──

def test_the_patients_lines_are_rendered_ahead_in_the_background(voice):
    async def scenario():
        task = voice_lines.render_ahead_for_patient(7)
        assert task is not None
        await task
        assert voice_lines.render_ahead_for_patient(7) is not None   # kept already: no new render
        await asyncio.sleep(0)

    asyncio.run(scenario())
    named = {line_id: {lang: voice_lines.line_text(line_id, lang, "王奶奶") for lang in ("zh-TW", "en")}
             for line_id in voice_lines.DOSE_LINES}
    assert sorted(text for text, *_ in voice.renders) == sorted([
        OPENING_ZH, OPENING_EN, "王奶奶，我來提醒您一件事。", "王奶奶, I'm here to remind you of something.",
        *(text for texts in named.values() for text in texts.values())])          # 5 lines x 2 languages
    assert len(voice.renders) == 10
    voice.target = None
    assert voice_lines.render_ahead_for_patient(7) is None          # no live target: nothing, and no loop needed


def test_without_checkin_consent_the_render_ahead_still_makes_the_dose_lines(voice):
    voice.consent = {"core": CHECKIN["core"], "robot_camera": CHECKIN["robot_camera"]}

    async def scenario():
        await voice_lines.render_ahead_for_patient(7)

    asyncio.run(scenario())
    assert sorted(text for text, *_ in voice.renders) == sorted(
        voice_lines.line_text(line_id, lang, "王奶奶") for line_id in voice_lines.DOSE_LINES for lang in ("zh-TW", "en"))


def test_a_render_ahead_that_fails_is_logged_and_never_raises(voice, caplog, monkeypatch):
    async def broken(u_id, dose_line=False):
        raise RuntimeError("database down")   # the consent or name read failed (locate raises through line)
    monkeypatch.setattr(voice_lines, "patient_name", broken)

    async def scenario():
        await voice_lines.render_ahead_for_patient(7)   # the task finishes without raising

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert "voice lines for patient 7: the render ahead failed (RuntimeError)" in caplog.text
    assert voice.renders == [] and "database down" not in caplog.text
