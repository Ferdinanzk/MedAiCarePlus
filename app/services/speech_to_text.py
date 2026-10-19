"""Speech-to-text in the cloud for the robot's 「我吃完了」 listener (quick fixes 2 of 19 Oct 2026, robot app 0.17.0).

The robot's done-mode listener (bridge voice.DoneListener) heard the patient's claim with its local Whisper, which
missed it. Now it sends each segment's audio (raw PCM16 mono 16 kHz) to POST /api/device/stt first; the server sends
it as a WAV to OpenAI's transcription endpoint (STT_MODEL, one try, STT_TIMEOUT_SECONDS for the whole call) with the
live voice connection's key, and answers the transcript. Any failure is a 502 (one WARNING), no cloud STT a 404, and
the robot then uses its local Whisper as before. The claim decision stays on the robot.

Only while a live voice target with a key exists (ai_settings.live_target, the gate of every line in the live voice)
and the patient's robot microphone consent is current (the robot's listener needs it already: the task's
`microphone`). Nothing is stored. The logs carry the audio's length, the language and the time, never the words, the
audio or the key.
"""

import asyncio
import io
import logging
import time
import wave
from concurrent.futures import ThreadPoolExecutor

import requests

from app.services import ai_settings

log = logging.getLogger(__name__)

TRANSCRIBE_URL = "https://api.openai.com/v1/audio/transcriptions"
# OpenAI's docs (checked 19 Oct 2026): gpt-4o-mini-transcribe is a current transcription model (snapshot
# 2025-12-15), takes `language` and `prompt`, and answers {"text"} with response_format json.
STT_MODEL = "gpt-4o-mini-transcribe"
STT_TIMEOUT_SECONDS = 8   # one try, the whole call; the robot waits at most 8 s too, then uses its own Whisper
RATE = 16000              # the robot's microphone audio: PCM16 mono 16 kHz
MAX_BYTES = 15 * RATE * 2           # 15 s (480 000 bytes); more is a 413
MIN_BYTES = RATE * 2 // 5           # 0.2 s (6 400 bytes); less is a 422
LANGUAGES = {"zh-TW": "zh", "en": "en"}   # the robot's language as ISO-639-1, as the endpoint takes it
# Who speaks and about what, in the language of the speech (the Traditional one nudges Traditional characters). Never
# the claim's own words: a prompt can be echoed back for a quiet segment.
PROMPTS = {"zh-TW": "一位住在台灣的長輩正在跟照護機器人說話，聊的是吃藥的事。",
           "en": "An older adult in Taiwan is talking to a care robot about their medicine."}
POOL_SIZE = 2   # calls at once, in a pool of their own: a slow answer never holds up the frame decoding
_pool = ThreadPoolExecutor(max_workers=POOL_SIZE, thread_name_prefix="stt")


class SttFailed(Exception):
    """The transcription failed (already logged once): the route answers 502 stt_failed."""


class _Answered(Exception):
    def __init__(self, status: int):
        super().__init__(f"the transcription API answered {status}")


def cloud_key() -> str | None:
    """The live voice connection's OpenAI key while a live voice target with a key exists, else None."""
    target = ai_settings.live_target()
    return target.api_key if target is not None and target.api_key else None


def to_wav(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(pcm)
    return out.getvalue()


def _post(wav: bytes, language: str, api_key: str) -> str:
    """Blocking: the endpoint's transcript, stripped. Raises on any failure; the key is never logged."""
    response = requests.post(TRANSCRIBE_URL, timeout=STT_TIMEOUT_SECONDS,
                             headers={"Authorization": f"Bearer {api_key}"},
                             data={"model": STT_MODEL, "language": LANGUAGES[language], "prompt": PROMPTS[language],
                                   "response_format": "json"},
                             files={"file": ("segment.wav", wav, "audio/wav")})
    if response.status_code != 200:
        raise _Answered(response.status_code)
    answer = response.json()
    text = answer.get("text") if isinstance(answer, dict) else None
    if not isinstance(text, str):
        raise ValueError("no text in the answer")
    return text.strip()


def _reason(error: Exception) -> str:
    """What the log says went wrong: the status the endpoint answered, "no text", else the error's type. Never the
    error's own text, which can hold the answer's body (the words)."""
    if isinstance(error, _Answered) or (isinstance(error, ValueError) and str(error) == "no text in the answer"):
        return str(error)
    return type(error).__name__


async def transcribe(pcm: bytes, language: str, api_key: str) -> dict:
    """{"text", "model", "ms"} for the segment (`language` zh-TW or en); SttFailed after one WARNING."""
    seconds = len(pcm) / (RATE * 2)
    started = time.monotonic()
    future = asyncio.get_running_loop().run_in_executor(_pool, _post, to_wav(pcm), language, api_key)
    try:
        text = await asyncio.wait_for(future, STT_TIMEOUT_SECONDS)
    except Exception as error:
        log.warning("speech-to-text failed (%s): %.1f s of %s audio, %d ms", _reason(error), seconds, language,
                    round((time.monotonic() - started) * 1000))
        raise SttFailed() from None
    ms = round((time.monotonic() - started) * 1000)
    log.info("speech-to-text: %.1f s of %s audio in %d ms (%s)", seconds, language, ms, STT_MODEL)
    return {"text": text, "model": STT_MODEL, "ms": ms}
