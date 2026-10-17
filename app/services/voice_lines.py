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

import asyncio
import hashlib
import io
import logging
import math
import re
import wave
from dataclasses import dataclass
from pathlib import Path

from app import config
from app.database import get_pool
from app.services import ai_settings, consent_service, conversation, med_clips, memory, reachy_tasks

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
LINES_RETRY_MINUTES = 30     # the scheduler tries the fixed lines still missing again this often (voice_lines job)

_renders: dict[str, asyncio.Lock] = {}   # one lock per key, as med_clips: two requests never render the same line twice
_ahead: set = set()   # the background renders of the per-patient lines (render_ahead_for_patient), kept until done


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


def fixed_path(line_id: str, language: str, key: str) -> Path:
    return config.MED_CLIP_DIR / "lines" / language / f"{line_id}_{key}.wav"


def patient_path(u_id: int, line_id: str, key: str) -> Path:
    """Beside the patient's medicine clips (med_clips.clip_path), so they go with the patient's folder."""
    return config.MED_CLIP_DIR / str(int(u_id)) / f"line_{line_id}_{key}.wav"


@dataclass(frozen=True)
class Located:
    """One line as it is keyed and kept: the route compares If-None-Match with `key` before fetch() renders."""
    line_id: str
    language: str   # folded: zh-TW or en
    text: str       # the written words (the robot's speech_text); the spoken form is derived when rendering
    voice: str
    key: str
    path: Path


async def patient_name(u_id: int) -> tuple[bool, str | None]:
    """Whether the per-patient lines may be made (the patient's check-in consent is current: the live chat's own
    gate), and the name Reachy calls the patient (memory.call_name: their memory 'name' entry while memory is on,
    else the Reachy card's call name, else the registered name)."""
    state = await consent_service.get_state(u_id)
    if not reachy_tasks.checkin_allowed(state):
        return False, None
    async with get_pool().acquire() as conn:
        return True, await memory.call_name(conn, u_id, memory.consent_current(state))


async def locate(u_id: int | None, line_id: str, language: str) -> Located | None:
    """The line's words, key and place, without rendering: None without a live voice target, for an unknown id
    (never a path lookup), or for a per-patient line without current check-in consent. `u_id` is read only for
    the per-patient lines."""
    target = ai_settings.live_target()
    if target is None or not target.api_key or line_id not in LINE_IDS:
        return None
    lang = med_clips.language_of(language)
    name = None
    if line_id in PATIENT_LINES:
        allowed, name = await patient_name(u_id)
        if not allowed:
            return None
    text = line_text(line_id, lang, name)
    voice = med_clips.voice_for(target)
    key = key_for(line_id, text, lang, voice)
    path = patient_path(u_id, line_id, key) if line_id in PATIENT_LINES else fixed_path(line_id, lang, key)
    return Located(line_id, lang, text, voice, key, path)


def _reason(error: Exception) -> str:
    """What the log says went wrong: the speech API's status when it answered (med_clips.render's RuntimeError),
    else the error's type. Never the words or the key."""
    text = str(error)
    return text if isinstance(error, RuntimeError) and text.startswith("speech API answered") else type(error).__name__


async def fetch(found: Located) -> tuple[bytes, str] | None:
    """(WAV bytes, key): the kept file, else rendered now (med_clips.TIMEOUT per try, one retry) and kept: `.part`
    then rename, under the key's lock, like med_clips.clip. None when the live target is gone meanwhile, the render
    failed or the line could not be kept (an OSError under MED_CLIP_DIR): one WARNING with the line id, the language
    and the HTTP status or error type, never the key or the words (the opening carries the patient's name; the
    error's own text holds the path, and so the key). No negative cache: the next request tries again."""
    lock = _renders.setdefault(found.key, asyncio.Lock())
    async with lock:
        if found.path.is_file():
            return found.path.read_bytes(), found.key
        target = ai_settings.live_target()
        if target is None or not target.api_key:
            return None
        try:
            found.path.parent.mkdir(parents=True, exist_ok=True)   # first: a folder that cannot be made costs no render
        except OSError as error:
            log.warning("voice line %s (%s) could not be kept (%s)", found.line_id, found.language,
                        type(error).__name__)
            return None
        try:
            data = await asyncio.to_thread(render_line, speech_input(found.line_id, found.text, found.language),
                                           found.voice, target.api_key, instructions_for(found.line_id))
        except Exception as error:
            log.warning("voice line %s (%s) could not be rendered (%s)", found.line_id, found.language, _reason(error))
            return None
        tmp = found.path.with_suffix(".part")
        try:
            tmp.write_bytes(data)
            tmp.replace(found.path)
        except OSError as error:
            log.warning("voice line %s (%s) could not be kept (%s)", found.line_id, found.language,
                        type(error).__name__)
            return None
        log.info("voice line %s (%s) rendered in the live voice (%d bytes)", found.line_id, found.language, len(data))
        return data, found.key


async def line(u_id: int | None, line_id: str, language: str) -> tuple[bytes, str] | None:
    """The line in the live voice, (WAV bytes, key), from disk or rendered now; None when it cannot be made (see
    locate and fetch)."""
    found = await locate(u_id, line_id, language)
    return None if found is None else await fetch(found)


# ── rendered ahead, never inside a robot's wait ──

async def render_fixed_ahead() -> list[str]:
    """Every fixed line in both languages, rendered when it is missing (startup_report's voice_lines stage, in the
    background after the live Check; the voice_lines scheduler job every LINES_RETRY_MINUTES). Each render runs in a
    worker thread (fetch). Returns the ids that could not be made, as "<line_id>/<language>"; [] without a live voice
    target (nothing to render)."""
    if ai_settings.live_target() is None:
        return []
    failed = []
    for line_id in FIXED_LINES:
        for language in LANGUAGES:
            if await line(None, line_id, language) is None:
                failed.append(f"{line_id}/{language}")
    return failed


async def retry_missing() -> None:
    """The scheduler's job: the fixed lines still missing (a render that failed at startup, the API down) are tried
    again while a live target exists; a line already kept costs one file check."""
    failed = await render_fixed_ahead()
    if failed:
        log.warning("voice lines: %d fixed lines are still missing in the live voice (%s); tried again in %d min",
                    len(failed), ", ".join(failed), LINES_RETRY_MINUTES)


def render_ahead_for_patient(u_id: int) -> asyncio.Task | None:
    """The per-patient lines (the opening and the reminder lead-in, both languages) rendered now, in the background,
    when a task is queued for the patient (reachy_tasks.enqueue_reachy_task) and when the call name's inputs change
    (the Reachy card's call name, a memory 'name' entry, a consent change), so the next check-in finds them kept.
    Nothing without a live voice target (None, at once); lines already kept cost one file check each."""
    if ai_settings.live_target() is None:
        return None
    task = asyncio.get_running_loop().create_task(_render_patient(u_id))
    _ahead.add(task)
    task.add_done_callback(_ahead.discard)
    return task


async def _render_patient(u_id: int) -> None:
    try:
        for line_id in PATIENT_LINES:
            for language in LANGUAGES:
                await line(u_id, line_id, language)
    except Exception as error:   # a render failure is already logged by fetch; this is the consent or name read
        log.warning("voice lines for patient %s: the render ahead failed (%s)", u_id, type(error).__name__)
