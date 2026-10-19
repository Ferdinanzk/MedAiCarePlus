"""Any sentence the server sends the robot as text, in the live voice (quick fixes of 19 Oct 2026, robot app 0.17.0).

Reachy's fixed and named lines are kept by services/voice_lines.py. The rest is text the server writes at the moment:
the text chat's opening and replies (/api/device/conversations*) and the overdose refusal's sentence (the 409's
speech_text on the device port). The robot's own Matcha voice said those, so Reachy changed voice within one task.
Now each such answer carries `clip` ({"id": "speech", "key"}) while a live voice target exists, and the server starts
rendering the words at once in the background (one try of RENDER_TIMEOUT, through med_clips.render in a small pool of
its own, so speech never holds up the frame decoding on the default executor). The robot fetches the WAV with
GET /api/device/clips/speech/{key}, which waits at most WAIT_SECONDS for a render still running; on a 404 it says the
words with Matcha as before.

Only keys issued to the device's own patient are served, so the robot's token is no speech service for any text, and
the robot never uploads text. The clips are kept in memory only (CACHE_SIZE, TTL_SECONDS): a chat reply is personal and
never lands under MED_CLIP_DIR. The logs carry the language and the length, never the words or the key.
"""

import asyncio
import logging
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from app.services import ai_settings, conversation, med_clips, voice_lines

log = logging.getLogger(__name__)

CLIP_ID = "speech"
MAX_CHARS = 240        # conversation.clean_reply's cap: a longer text gets no clip
CACHE_SIZE = 64        # clips kept, the least recently used dropped first
TTL_SECONDS = 600      # a clip is kept this long after it was last issued
RENDER_TIMEOUT = 10    # one try; the robot waits at most 5 s for it anyway
WAIT_SECONDS = 8       # the route waits this long for a render still running

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="speech-clip")
_running: set = set()   # the renders in flight, kept until done (an evicted clip's render still finishes)


@dataclass
class _Clip:
    text: str          # the Traditional (or English) words the key was made from
    language: str      # folded: zh-TW or en
    voice: str
    issued_at: float
    owners: set = field(default_factory=set)   # the patients it was issued to
    render: asyncio.Task | None = None          # its result: the WAV, or None when the render failed


_clips: "OrderedDict[str, _Clip]" = OrderedDict()


def _now() -> float:
    return time.monotonic()


def key_of(text: str, voice: str) -> str:
    return voice_lines.line_key(text, voice, voice_lines.INSTRUCTIONS["companion"])


def _expire() -> None:
    now = _now()
    for key in [key for key, clip in _clips.items() if now - clip.issued_at > TTL_SECONDS]:
        del _clips[key]


def _failed(task: asyncio.Task | None) -> bool:
    return task is None or (task.done() and (task.cancelled() or task.result() is None))


def issue(u_id: int, text: str, language: str) -> dict | None:
    """The `clip` for `text` said to patient `u_id`, and its render started in the background (on the running loop);
    None without a live voice target with a key, for no words or for more than MAX_CHARS. The same words issued again
    share one render, and a render that failed is tried again."""
    target = ai_settings.live_target()
    if target is None or not target.api_key or not text or len(text) > MAX_CHARS:
        return None
    voice = med_clips.voice_for(target)
    key = key_of(text, voice)
    _expire()
    clip = _clips.get(key)
    if clip is None:
        clip = _clips[key] = _Clip(text, med_clips.language_of(language), voice, _now())
    clip.owners.add(u_id)
    clip.issued_at = _now()
    _clips.move_to_end(key)
    if _failed(clip.render):
        clip.render = asyncio.get_running_loop().create_task(_render(clip, target.api_key))
        _running.add(clip.render)
        clip.render.add_done_callback(_running.discard)
    while len(_clips) > CACHE_SIZE:
        _clips.popitem(last=False)
    return {"id": CLIP_ID, "key": key}


def _spoken(clip: _Clip) -> str:
    """What the speech API is given: the help line's phone numbers digit by digit (voice_lines.speech_input), so they
    are read as numbers to dial; any other text as written."""
    if clip.text == conversation.HELPLINE[clip.language]:
        return voice_lines.speech_input("helpline", clip.text, clip.language)
    return clip.text


def _render_wav(text: str, voice: str, api_key: str) -> bytes:
    return voice_lines.to_robot_wav(med_clips.render(text, voice, api_key, voice_lines.INSTRUCTIONS["companion"],
                                                     timeout=RENDER_TIMEOUT, retries=0))


async def _render(clip: _Clip, api_key: str) -> bytes | None:
    started = time.monotonic()
    try:
        data = await asyncio.get_running_loop().run_in_executor(_pool, _render_wav, _spoken(clip), clip.voice,
                                                                api_key)
    except Exception as error:
        log.warning("speech clip (%s, %d characters) could not be rendered (%s)", clip.language, len(clip.text),
                    voice_lines._reason(error))
        return None
    log.info("speech clip (%s, %d characters) rendered in the live voice in %.1f s", clip.language, len(clip.text),
             time.monotonic() - started)
    return data


async def get(u_id: int, key: str) -> bytes | None:
    """The WAV (PCM16 mono 16 kHz) of a clip issued to patient `u_id`, waiting at most WAIT_SECONDS for its render;
    None when it was never issued to them, has expired, failed or is not ready in time (the render goes on, so a
    later fetch may still get it)."""
    _expire()
    clip = _clips.get(key)
    if clip is None or u_id not in clip.owners or clip.render is None or clip.render.cancelled():
        return None
    _clips.move_to_end(key)
    try:
        return await asyncio.wait_for(asyncio.shield(clip.render), WAIT_SECONDS)
    except asyncio.TimeoutError:
        return None
