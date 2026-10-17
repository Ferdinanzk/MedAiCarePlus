"""Reachy's own lines around a live chat, rendered once in the live voice (17 Oct 2026, same-voice spec:
docs/superpowers/specs/2026-10-17-same-voice-design.md).

In a live check-in GPT-Live speaks in OpenAI's voice (marin) while Reachy's own lines (the check-in greeting, the
opening with the patient's name, the reminder lead-in, the repeat line, the help line, the rest and medicine lines,
the wind-down) came from the robot's Matcha voice, and the patient heard the jump at every one of them. Now the server
renders each of those lines once with the speech API (med_clips.render, the live voice connection's key, the live
voice itself), keeps it under MED_CLIP_DIR as the robot needs it (PCM16 mono 16 kHz WAV), serves it with its key
(GET /api/device/clips/line/{line_id}), and names the copy the robot should play beside the words it sends (`clip` on
the relay's `ready` and `say`). The robot (app 0.16.0) keeps a copy per line and plays it when its key matches; Matcha
stays the fallback for any line it has no copy of, and robot app 0.15.0 ignores all of this. Only while a live voice
target exists (ai_settings.live_target, the medicine prompts' gate); the two per-patient lines also need the
patient's check-in consent, because they carry the call name.
"""

import hashlib
import io
import logging
import math
import re
import wave

from app.services import ai_settings, conversation, med_clips, memory

log = logging.getLogger(__name__)

# The robot-owned lines (the robot's own Simplified constants are its Matcha fallback; these are the Traditional and
# English words the live voice says), then the server-owned ones, which are read from their own constants.
FIXED_LINES = ("checkin_greeting", "repeat", "helpline", "rest", "medicine", "wind_down")
PATIENT_LINES = ("opening", "reminder_lead")   # they carry the call name: rendered per patient, with consent
LINE_IDS = FIXED_LINES + PATIENT_LINES
LANGUAGES = ("zh-TW", "en")
LINE_TEXTS = {
    "checkin_greeting": {"zh-TW": "您好！我來找您聊聊天。", "en": "Hello! I came to have a chat with you."},
    "rest": {"zh-TW": "我先休息一下，有需要再叫我喔。", "en": "I'll rest for a bit. Call me if you need anything."},
    "medicine": {"zh-TW": "該吃藥囉，我們等一下再聊。",
                 "en": "It's time for your medicine. Let's talk again in a little while."},
    "wind_down": {"zh-TW": "這次就到這裡，請多保重！", "en": "That's all for now. Take care!"},
}
# How the voice says a line (part of its key, so new wording re-renders): the help line calm and clear, the numbers
# one digit at a time; every other line warm, calm and slow, in the spirit of the medicine prompt's instructions.
INSTRUCTIONS = {
    "companion": ("A warm, calm companion robot talking with an older adult at home. Speak slowly, gently and "
                  "clearly, as to a friend, in the language the text is written in."),
    "helpline": ("A calm, steady companion robot telling an older adult at home where to get help right now. "
                 "Speak slowly and clearly, with warmth; read each phone number one digit at a time, with a short "
                 "pause between the digits."),
}
DIGITS = {"zh-TW": "零一二三四五六七八九",
          "en": ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")}
_NUMBER = re.compile(r"\d{3,}")   # 119 and 1925 are read digit by digit; "24 小時" / "24 hours" stays a number
RATE = 16000                 # the robot's live player takes PCM16 mono 16 kHz; robot.play_clip takes it too
TRIM_LEVEL = 0.003           # about -50 dBFS of full scale: the deploy tool's threshold for Matcha's clips ...
TRIM_KEEP_SECONDS = 0.01     # ... which keeps 10 ms of the silence before the first sound


def line_text(line_id: str, language: str, name: str | None = None) -> str | None:
    """The words of a line as the robot gets them (speech_text) and as the key is made: the robot-owned lines from
    LINE_TEXTS, the server-owned ones from their own constants (never copied), the per-patient ones with the call
    name. None for an unknown id."""
    lang = med_clips.language_of(language)
    if line_id in LINE_TEXTS:
        return LINE_TEXTS[line_id][lang]
    if line_id == "helpline":
        return conversation.HELPLINE[lang]
    from app.services import live_session   # here, not at the top: live_session imports this module (expected_clip)
    if line_id == "repeat":
        return live_session.REPEAT_LINES[lang]
    if line_id == "opening":
        return memory.opening_line(lang, name)
    if line_id == "reminder_lead":
        return (live_session.REMINDER_OPENING[lang].format(name=name) if name
                else live_session.REMINDER_OPENING_NONAME[lang])
    return None


def instructions_for(line_id: str) -> str:
    return INSTRUCTIONS["helpline" if line_id == "helpline" else "companion"]


def speech_input(line_id: str, text: str, language: str) -> str:
    """What the speech API is given: the help line with its phone numbers written digit by digit (119 → 一一九,
    1925 → 一九二五; "one one nine" in English), so they are read as numbers to dial; every other line as written."""
    if line_id != "helpline":
        return text
    lang = med_clips.language_of(language)

    def spell(match):
        digits = [DIGITS[lang][int(digit)] for digit in match.group()]
        return "".join(digits) if lang == "zh-TW" else " ".join(digits)
    return _NUMBER.sub(spell, text)


def line_key(text: str, voice: str, instructions: str) -> str:
    """What a kept line depends on: the model, the voice, the instructions and the spoken words. A different formula
    from med_clips.clip_key on purpose: the medicine clips' keys must not change (the robot has them cached)."""
    return hashlib.sha256(f"{med_clips.MODEL}|{voice}|{instructions}|{text}".encode("utf-8")).hexdigest()[:20]


def key_for(line_id: str, text: str, language: str, voice: str) -> str:
    """The key of `text` said as line `line_id` by `voice` (the spoken form and the line's instructions included)."""
    return line_key(speech_input(line_id, text, language), voice, instructions_for(line_id))


def expected_clip(line_id: str, text: str, language: str) -> dict | None:
    """For the relay's `ready` and `say`: {"id", "key"} of the copy the robot should play for `text`, the very key
    the line route serves for it, without rendering anything; None without a live voice target (then nothing is
    sent and the robot says the words with its own voice), for an unknown line or no words."""
    target = ai_settings.live_target()
    if target is None or line_id not in LINE_IDS or not text:
        return None
    return {"id": line_id, "key": key_for(line_id, text, language, med_clips.voice_for(target))}


def to_robot_wav(wav_bytes: bytes) -> bytes:
    """The speech API's WAV (24 kHz mono in practice; any PCM16 rate or channel count) as the robot keeps it: PCM16
    mono 16 kHz, the leading silence trimmed to about 10 ms below about -50 dBFS, as the deploy tool trims Matcha's
    clips, so a kept line starts as promptly as a Matcha one. Raises ValueError for anything but 16-bit PCM
    (normalize_wav) and for a WAV with no sound at all: silence where Reachy spoke before is never kept."""
    import numpy as np
    from scipy.signal import resample_poly
    with wave.open(io.BytesIO(med_clips.normalize_wav(wav_bytes)), "rb") as wav:
        rate, channels = wav.getframerate(), wav.getnchannels()
        frames = wav.readframes(wav.getnframes())
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != RATE:
        g = math.gcd(RATE, rate)
        samples = resample_poly(samples, RATE // g, rate // g)
    loud = np.flatnonzero(np.abs(samples) > TRIM_LEVEL)
    if not len(loud):
        raise ValueError("the WAV holds no sound")
    samples = samples[max(0, int(loud[0]) - int(RATE * TRIM_KEEP_SECONDS)):]
    pcm = np.clip(np.round(samples * 32768.0), -32768, 32767).astype("<i2").tobytes()
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(pcm)
    return out.getvalue()


def render_line(text: str, voice: str, api_key: str, instructions: str) -> bytes:
    """The speech API (med_clips.render: two tries of med_clips.TIMEOUT), then the robot's format. Blocking: callers
    run it with asyncio.to_thread. Raises on failure; the key is never logged."""
    return to_robot_wav(med_clips.render(text, voice, api_key, instructions))
