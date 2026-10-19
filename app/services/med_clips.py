"""Spoken medication prompts for the robot: 「請現在吃第一種藥，Adapalene。」 (14 Oct 2026).

Reachy's own voice (Matcha, Chinese only) drops every word it cannot read: a prompt with "Adapalene" in it came out
without the name. So the server renders each prompt once with OpenAI's speech API (the live voice connection's
key, the live voice itself when the speech API knows it) and the robot fetches it (GET /api/device/clips/med/{id},
robot app 0.14.0) and keeps it as its per-medication clip (clips/manifest.json: med_<id>.wav). One clip per
medicine, position in the slot (first, second...) and language, named after the medicine's text so a renamed
medicine gets a new one. Without a usable key, or when the API fails, there is no clip and the robot says its
generic prompt, as before: a name is never guessed.
"""

import asyncio
import hashlib
import io
import logging
import struct
import time
import wave
from pathlib import Path

import requests

from app import config
from app.database import get_pool
from app.services import ai_settings

log = logging.getLogger(__name__)

SPEECH_URL = "https://api.openai.com/v1/audio/speech"
MODEL = "gpt-4o-mini-tts"
TIMEOUT = 40
RETRIES = 1
CHUNK_BYTES = 16384   # a streamed answer is read this much at a time (render's `total`)
VOICES = ("alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer", "verse", "marin",
          "cedar")
DEFAULT_VOICE = "coral"
INSTRUCTIONS = ("A warm, calm companion robot reminding an older adult at home to take a medicine. Speak slowly "
                "and clearly; read the medicine's name in the language it is written in.")
MAX_POSITION = 8
ORDINALS = {"zh-TW": ("一", "二", "三", "四", "五", "六", "七", "八"),
            "en": ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth")}
TEXTS = {"zh-TW": ("請現在吃第一種藥，{name}。", "接下來，請吃第{ordinal}種藥，{name}。"),
         "en": ("Please take your first medicine, {name}.", "Next, please take your {ordinal} medicine, {name}.")}
MAX_NAME = 60

_renders: dict[str, asyncio.Lock] = {}


def language_of(language: str | None) -> str:
    return "en" if str(language or "").lower().startswith("en") else "zh-TW"


def prompt_text(name: str, position: int, language: str) -> str:
    """The sentence the robot says for the medicine in `position` (1-based) of its slot."""
    lang = language_of(language)
    position = max(1, min(MAX_POSITION, int(position)))
    first, later = TEXTS[lang]
    ordinal = ORDINALS[lang][position - 1]
    return (first if position == 1 else later).format(name=name.strip()[:MAX_NAME], ordinal=ordinal)


def voice_for(target) -> str:
    voice = str(getattr(target, "voice", "") or "")
    return voice if voice in VOICES else DEFAULT_VOICE


def clip_key(text: str, voice: str) -> str:
    """What a clip depends on: its words, the voice and the model. Named after them, a changed name re-renders."""
    return hashlib.sha256(f"{MODEL}|{voice}|{text}".encode("utf-8")).hexdigest()[:20]


def clip_path(u_id: int, med_id: int, key: str) -> Path:
    return config.MED_CLIP_DIR / str(int(u_id)) / f"med_{int(med_id)}_{key}.wav"


def normalize_wav(data: bytes) -> bytes:
    """A WAV whose header tells the truth: the API streams its WAV with placeholder sizes (89478 s on 13 Oct 2026),
    which the robot's wave reader would take at face value. Raises ValueError when it is not 16-bit PCM."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    pos, fmt, pcm = 12, None, None
    while pos + 8 <= len(data):
        chunk, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size] if chunk != b"data" else data[pos + 8:]
        if chunk == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif chunk == b"data":
            pcm = body if size in (0, 0xFFFFFFFF) or size > len(body) else body[:size]
            break
        pos += 8 + size + (size & 1)
    if fmt is None or pcm is None:
        raise ValueError("WAV without fmt or data chunk")
    audio_format, channels, rate, _, _, bits = fmt
    if audio_format != 1 or bits != 16:
        raise ValueError(f"WAV is not 16-bit PCM (format {audio_format}, {bits} bits)")
    pcm = pcm[:len(pcm) - len(pcm) % (channels * 2)]
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return out.getvalue()


def _clock() -> float:
    return time.monotonic()


def _body_by(response, ends: float) -> bytes:
    """The streamed answer's body, read until `ends` (on _clock): TimeoutError once that has passed."""
    body = bytearray()
    for chunk in response.iter_content(CHUNK_BYTES):
        body += chunk
        if _clock() > ends:
            response.close()
            raise TimeoutError("the speech API's answer took too long")
    return bytes(body)


def render(text: str, voice: str, api_key: str, instructions: str = INSTRUCTIONS, timeout: float = TIMEOUT,
           retries: int = RETRIES, total: float | None = None) -> bytes:
    """The speech API, tried twice (a read timed out at 30 s on 14 Oct 2026 for a 4 s clip); the WAV, normalized.
    `instructions` tells the voice how to speak: the medicine prompt's by default (the reminders use it too); Reachy's
    own lines bring their own (services/voice_lines.py). A sentence the robot waits for (services/speech_clips.py)
    gets one try with a shorter `timeout`, and `total`: requests' timeout limits each connect and each read, not the
    whole call (a WAV streamed slowly took 19 s with timeout=10), so with `total` the body is streamed and the render
    gives up (TimeoutError) once that many seconds have passed. Raises on failure (the key never gets logged)."""
    ends = None if total is None else _clock() + total
    stream = {} if ends is None else {"stream": True}
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = requests.post(SPEECH_URL, timeout=timeout, headers={"Authorization": f"Bearer {api_key}"},
                                     json={"model": MODEL, "voice": voice, "input": text,
                                           "instructions": instructions, "response_format": "wav"}, **stream)
            if response.status_code == 200:
                return normalize_wav(response.content if ends is None else _body_by(response, ends))
        except requests.RequestException as error:
            last = error
            continue
        if ends is not None:
            response.close()
        last = RuntimeError(f"speech API answered {response.status_code}")
        if response.status_code < 500 and response.status_code != 429:
            break
    raise last if last is not None else RuntimeError("speech API failed")


async def medicine_name(u_id: int, med_id: int) -> str | None:
    async with get_pool().acquire() as conn:
        name = await conn.fetchval("SELECT med_name FROM medication WHERE u_id = $1 AND med_id = $2", u_id, med_id)
    return name.strip() if isinstance(name, str) and name.strip() else None


async def clip(u_id: int, med_id: int, position: int, language: str) -> tuple[bytes, str] | None:
    """The prompt clip for this patient's medicine, (WAV bytes, key): from disk, else rendered now and kept. None
    without a medicine of that id, a usable live voice connection, or when rendering failed (logged)."""
    name = await medicine_name(u_id, med_id)
    if name is None:
        return None
    target = ai_settings.live_target()
    if target is None or not target.api_key:
        return None
    text, voice = prompt_text(name, position, language), voice_for(target)
    key = clip_key(text, voice)
    path = clip_path(u_id, med_id, key)
    lock = _renders.setdefault(key, asyncio.Lock())
    async with lock:
        if path.is_file():
            return path.read_bytes(), key
        try:
            data = await asyncio.to_thread(render, text, voice, target.api_key)
        except Exception as error:
            log.warning("medication prompt for medicine %s could not be rendered (%s)", med_id, type(error).__name__)
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        log.info("medication prompt rendered for medicine %s, position %s (%d bytes)", med_id, position, len(data))
        return data, key
