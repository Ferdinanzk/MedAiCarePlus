"""Any sentence the server sends the robot as text, in the live voice (quick fixes of 19 Oct 2026, robot app 0.17.0).

Reachy's fixed and named lines are kept by services/voice_lines.py. The rest is text the server writes at the moment:
the text chat's opening and replies (/api/device/conversations*) and the overdose refusal's sentence (the 409's
speech_text on the device port). The robot's own Matcha voice said those, so Reachy changed voice within one task.
Now each such answer carries `clip` ({"id": "speech", "key"}) while a live voice target exists, and the server starts
rendering the words at once in the background (one try of RENDER_TIMEOUT, through med_clips.render in a small pool of
its own, so speech never holds up the frame decoding on the default executor). The robot fetches the WAV with
GET /api/device/clips/speech/{key}, which waits at most WAIT_SECONDS for a render still running; on a 404 it says the
words with Matcha as before.

The help line is no new render: it is voice_lines' kept `helpline` line (rendered ahead at startup with the calm
help-line instructions, the numbers digit by digit), read from its file at once, so a patient in crisis never waits.

The key the robot gets is a random token per issue (secrets.token_hex), mapped here to the words' own key, which
dedupes the renders. uvicorn logs the device port's paths, and the words' key is the same in every process: in a
logged path it would tell which sentence a patient's robot fetched (the help line: a risk turn). Only keys issued to
the device's own patient are served, so the robot's token is no speech service for any text, and the robot never
uploads text. The clips are kept in memory only (CACHE_SIZE, TTL_SECONDS): a chat reply is personal and never lands
under MED_CLIP_DIR. The logs carry the language and the length, never the words or either key.
"""

import asyncio
import logging
import secrets
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from app.services import ai_settings, conversation, med_clips, voice_lines

log = logging.getLogger(__name__)

CLIP_ID = "speech"
MAX_CHARS = 240        # conversation.clean_reply's cap: a longer text gets no clip
CACHE_SIZE = 64        # clips kept, the least recently used dropped first
TTL_SECONDS = 600      # a clip is kept this long after it was last issued
RENDER_TIMEOUT = 10    # one try; the robot waits at most 5 s for it anyway
WAIT_SECONDS = 8       # the route waits this long for a render still running
TOKEN_BYTES = 10       # the key in the URL: 20 hex characters, random per issue
TOKENS_MAX = 4 * CACHE_SIZE   # keys issued and still kept, the oldest dropped first (and each after TTL_SECONDS)

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="speech-clip")
_running: set = set()   # the renders in flight, kept until done (an evicted clip's render still finishes)


@dataclass
class _Clip:
    text: str          # the Traditional (or English) words the key was made from
    language: str      # folded: zh-TW or en
    voice: str
    issued_at: float
    helpline: bool = False                      # served from voice_lines' kept `helpline` line, never rendered here
    render: asyncio.Task | None = None          # its result: the WAV, or None when the render failed


@dataclass(frozen=True)
class _Token:
    key: str           # the clip's own key (key_of, or the kept help line's key)
    u_id: int          # the patient it was issued to
    issued_at: float


_clips: "OrderedDict[str, _Clip]" = OrderedDict()     # by the words' key
_tokens: "OrderedDict[str, _Token]" = OrderedDict()   # by the key the robot got, oldest first


def _now() -> float:
    return time.monotonic()


def key_of(text: str, voice: str) -> str:
    return voice_lines.line_key(text, voice, voice_lines.INSTRUCTIONS["companion"])


def _helpline_language(text: str) -> str | None:
    return next((lang for lang in voice_lines.LANGUAGES if text == conversation.HELPLINE[lang]), None)


def _expire() -> None:
    now = _now()
    for key in [key for key, clip in _clips.items() if now - clip.issued_at > TTL_SECONDS]:
        del _clips[key]
    while _tokens and now - next(iter(_tokens.values())).issued_at > TTL_SECONDS:
        _tokens.popitem(last=False)


def _failed(task: asyncio.Task | None) -> bool:
    return task is None or (task.done() and (task.cancelled() or task.result() is None))


def issue(u_id: int, text: str, language: str) -> dict | None:
    """The `clip` for `text` said to patient `u_id`, and its render started in the background (on the running loop);
    None without a live voice target with a key, for no words or for more than MAX_CHARS. The same words issued again
    share one render, and a render that failed is tried again; the help line is the kept `helpline` line (_kept).
    Its key is a new random token each time."""
    target = ai_settings.live_target()
    if target is None or not target.api_key or not text or len(text) > MAX_CHARS:
        return None
    voice = med_clips.voice_for(target)
    helpline = _helpline_language(text)
    key = voice_lines.key_for("helpline", text, helpline, voice) if helpline else key_of(text, voice)
    _expire()
    clip = _clips.get(key)
    if clip is None:
        clip = _clips[key] = _Clip(text, helpline or med_clips.language_of(language), voice, _now(),
                                   helpline=helpline is not None)
    clip.issued_at = _now()
    _clips.move_to_end(key)
    if _failed(clip.render):
        work = _kept(clip) if clip.helpline else _render(clip, target.api_key)
        clip.render = asyncio.get_running_loop().create_task(work)
        _running.add(clip.render)
        clip.render.add_done_callback(_running.discard)
    while len(_clips) > CACHE_SIZE:
        _clips.popitem(last=False)
    token = secrets.token_hex(TOKEN_BYTES)
    _tokens[token] = _Token(key, u_id, _now())
    while len(_tokens) > TOKENS_MAX:
        _tokens.popitem(last=False)
    return {"id": CLIP_ID, "key": token}


def _render_wav(text: str, voice: str, api_key: str) -> bytes:
    return voice_lines.to_robot_wav(med_clips.render(text, voice, api_key, voice_lines.INSTRUCTIONS["companion"],
                                                     timeout=RENDER_TIMEOUT, retries=0))


async def _render(clip: _Clip, api_key: str) -> bytes | None:
    started = time.monotonic()
    try:
        data = await asyncio.get_running_loop().run_in_executor(_pool, _render_wav, clip.text, clip.voice, api_key)
    except Exception as error:
        log.warning("speech clip (%s, %d characters) could not be rendered (%s)", clip.language, len(clip.text),
                    voice_lines._reason(error))
        return None
    log.info("speech clip (%s, %d characters) rendered in the live voice in %.1f s", clip.language, len(clip.text),
             time.monotonic() - started)
    return data


async def _kept(clip: _Clip) -> bytes | None:
    """The help line as voice_lines keeps it: its file, read at once; rendered (with the help-line instructions and
    the numbers digit by digit) and kept there only when the startup render has not made it yet."""
    kept = await voice_lines.line(None, "helpline", clip.language)
    return None if kept is None else kept[0]


async def get(u_id: int, key: str) -> bytes | None:
    """The WAV (PCM16 mono 16 kHz) of the clip whose key (the token) was issued to patient `u_id`, waiting at most
    WAIT_SECONDS for its render; None when it was never issued to them, has expired, failed or is not ready in time
    (the render goes on, so a later fetch may still get it)."""
    _expire()
    token = _tokens.get(key)
    clip = _clips.get(token.key) if token is not None and token.u_id == u_id else None
    if clip is None or clip.render is None or clip.render.cancelled():
        return None
    _clips.move_to_end(token.key)
    try:
        return await asyncio.wait_for(asyncio.shield(clip.render), WAIT_SECONDS)
    except asyncio.TimeoutError:
        return None
