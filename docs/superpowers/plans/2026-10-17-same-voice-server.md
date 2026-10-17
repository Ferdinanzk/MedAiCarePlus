# Same Voice (server) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The server renders Reachy's own lines around a live chat once in the live voice, keeps them, serves them to the robot with a key, and names the copy to play beside each line it sends, so a robot that keeps the copies says every line in GPT-Live's voice.

**Architecture:** One new service, `app/services/voice_lines.py`, owns the line texts, the spoken form and the instructions for the speech API, the key, the robot's WAV format, the storage under `MED_CLIP_DIR`, the consent gate and the render-ahead. It reuses `med_clips.render` (which gains an `instructions` argument) and the live voice target gate. The device router gains `GET /api/device/clips/line/{line_id}` (200 with ETag, 304, 404) and the missing 304 on `/clips/med`. The live relay adds a `clip` field (`{"id", "key"}`) to the companion `ready` and `say` messages, nothing else. Render-ahead runs at startup (one `[STARTUP]` stage and a 30-minute retry job) and, for the two per-patient lines, when a task is queued and when the call name's inputs change.

**Tech Stack:** Python 3.11, FastAPI, asyncpg (faked in tests), `requests` for the speech API, numpy + scipy `resample_poly` (both in the venv), APScheduler, pytest.

**Spec:** `docs/superpowers/specs/2026-10-17-same-voice-design.md` (Task 1 copies it into the worktree from `C:\Users\AIDL-14\AppData\Local\Temp\claude\C--Reachy-Mini\1ff25281-c5da-4818-bbf1-9fd9035013bc\scratchpad\samevoice\2026-10-17-same-voice-design.md`). The read-only code maps the plan was written from are `server.md` and `robot.md` in that same scratchpad folder.

**Branch:** all server work goes on a new branch `same-voice`, made from `agent-tools-fixes` in its own git worktree (Task 1). Never push or merge. Another job may still be committing to `agent-tools-fixes` in the shared working tree `C:\Reachy_Mini\repo\MedAiCarePlus`: this plan never switches, stashes or cleans that tree.

**Where the work happens:** the worktree `C:\Reachy_Mini\worktrees\MedAiCarePlus-same-voice` (made in Task 1, Step 2). Every step after that runs from the worktree root. The worktree has no `.venv`, so every test command uses the main repo's interpreter by absolute path, run from the worktree root: pytest's rootdir is the worktree, so its `app/` is what is tested, never the main repo's.

**Scope:** this plan is the SERVER part only. It produces working software on its own: robot app 0.15.0 keeps working with it, because 0.15.0 ignores the new `clip` field and never asks for `/clips/line/...`. The robot part (medcare-reachy-bridge 0.16.0: the line cache, playing the copies) is a separate plan. (For the record, the robot repo's tests run from its root with `pip install -e ".[test]"` then `python -m pytest tests medcare_reachy/bridge/tests -q`.)

**Test commands** (from the worktree root `C:\Reachy_Mini\worktrees\MedAiCarePlus-same-voice`):
- whole suite: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider`
- one test: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_voice_lines.py::test_name -q -p no:cacheprovider`
- in Git Bash the same commands work with `/c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe` and forward slashes.

**Commit trailer** for every commit in this plan: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` (given as a second `-m`).

## Global Constraints

- Server: FastAPI + asyncpg, tests `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider` (from the worktree root) must stay green.
- `CLAUDE.md`'s live voice section is checked by `tests/test_live_voice_docs.py` (every backticked name must exist in the server code; `NAME = value` spans must match; documented `/api/device/...` routes must exist; robot-only names from `medcare-reachy-bridge` must not be backticked there). The same test checks `HANDOFF.md`'s section 6.
- `Player.add_say` (robot) takes PCM16 mono 16 kHz; `robot.play_clip` takes 16-bit PCM WAV at any rate or channel count. So every line is kept as a PCM16 mono 16 kHz WAV.
- The speech API: `POST /v1/audio/speech`, `gpt-4o-mini-tts`, `response_format: wav` (24 kHz mono in practice), `instructions` supported. The existing `med_clips.render` is the one place that calls it.
- The existing keys and files must not change meaning: `med_clips.clip_key(text, voice)` and the medicine clip file names stay as they are (the robot has them cached).
- Marin only while a live voice target exists (`LIVE_VOICE_TEST=1` and a passing Check): `ai_settings.live_target()` is the gate, as for medicine prompts and reminders. Without it nothing changes.
- Per-patient lines (`opening`, `reminder_lead`) are rendered and served only while check-in consent is current (`reachy_tasks.checkin_allowed`); otherwise 404.
- Keys, tokens or the patient's name never go in logs. A render failure logs one WARNING with the line id, the language and the HTTP status or error type.
- `ready` / `say` never wait for a render.
- Simple English in docs and messages; plain Traditional Chinese for the zh-TW texts.
- Never push or merge; commits on the feature branch only.

## Review Focus

Inputs the spec implies but does not spell out; each has its test in the task named.

1. `If-None-Match` with a weak tag (`W/"k"`) or several tags (`"k9", "k10"`): the robot's key among them must still give a 304, never a full download. (Task 3's test, used by both clip routes.)
2. A render that holds no sound (an empty or all-silent WAV from the API): it must be refused, so the robot falls back to Matcha instead of keeping silence where Reachy spoke before. (Task 5's test: `to_robot_wav` raises `ValueError`.)
3. A line id or language outside the known set (`../rest`, `rest.wav`, `REST`, `en-GB`, `""`): unknown ids are a 404 with no path lookup; the language is folded so the key and the path are the same whatever spelling the robot sends. (Task 6's test.)
4. A database error while locating a per-patient line (consent or name read fails): the route must answer 500, never 404, because a 404 makes the robot delete its copy. (Task 7's test.)
5. The live voice target disappearing between the key and the render (the AI card's live section saved again), and two requests for the same key at once: no crash, no double render. (Task 6's tests.)

## File map

| File | Responsibility |
|---|---|
| `app/services/voice_lines.py` (new) | Line texts, spoken form, instructions, key, robot WAV format, storage, consent gate, render-ahead. |
| `app/services/med_clips.py` | `render` gains `instructions` (default: the medicine one). |
| `app/routers/api_device.py` | 304 on `/clips/med`; new `/clips/line/{line_id}` route; two small helpers. |
| `app/services/live_session.py` | `clip` on the companion `ready` and `say` (`_with_clip`). |
| `app/startup_report.py` | The `voice_lines` stage after the live Check. |
| `app/jobs/scheduler.py` | The 30-minute retry job `voice_lines`. |
| `app/services/reachy_tasks.py` | Render-ahead hook when a task is queued. |
| `app/routers/api_reachy.py`, `app/routers/api_memory.py`, `app/routers/api_consent.py` | Render-ahead hooks when the call name's inputs change. |
| `tests/test_voice_lines.py` (new) | Keys, texts, WAV format, storage, the line route, render-ahead, the `[STARTUP]` stage, the job. |
| `tests/test_med_clips.py`, `tests/test_medication_events.py`, `tests/test_live_companion.py`, `tests/test_live_safety.py`, `tests/test_live_speak.py`, `tests/test_reachy_tasks.py`, `tests/test_reachy_settings.py`, `tests/test_api_memory.py`, `tests/test_consent.py` | One or two tests each for the part they own. |
| `CLAUDE.md`, `HANDOFF.md` | Docs (checked by `tests/test_live_voice_docs.py`). |

---

### Task 1: Worktree, branch and spec

**Files:**
- Create: `docs/superpowers/specs/2026-10-17-same-voice-design.md` (a copy of the spec)
- Create: `docs/superpowers/plans/2026-10-17-same-voice-server.md` (a copy of this plan)

The spec and this plan are copied into the worktree's `docs/superpowers/specs/` and `docs/superpowers/plans/` from `C:\Users\AIDL-14\AppData\Local\Temp\claude\C--Reachy-Mini\1ff25281-c5da-4818-bbf1-9fd9035013bc\scratchpad\samevoice\2026-10-17-same-voice-design.md` and `C:\Users\AIDL-14\AppData\Local\Temp\claude\C--Reachy-Mini\1ff25281-c5da-4818-bbf1-9fd9035013bc\scratchpad\samevoice\plan-server.md`, and committed as the branch's first commit (Steps 3 and 5).

**Interfaces:**
- Consumes: branch `agent-tools-fixes` of `C:\Reachy_Mini\repo\MedAiCarePlus`.
- Produces: the worktree `C:\Reachy_Mini\worktrees\MedAiCarePlus-same-voice` on branch `same-voice`, with the spec at the path every later task and the docs cite.

- [ ] **Step 1: Check the starting point**

Run (PowerShell): `cd C:\Reachy_Mini\repo\MedAiCarePlus; git log -1 --oneline agent-tools-fixes; git branch --list same-voice; Test-Path C:\Reachy_Mini\worktrees\MedAiCarePlus-same-voice`
Expected: one commit line (the base of the new branch), nothing from `git branch --list same-voice`, and `False`. If the branch or the folder already exists, stop and ask; do not delete either. Leave the shared working tree as it is (its branch, its uncommitted changes): the worktree never touches it.

- [ ] **Step 2: Make the worktree and the branch from agent-tools-fixes**

Run (PowerShell, from `C:\Reachy_Mini\repo\MedAiCarePlus`): `git worktree add C:\Reachy_Mini\worktrees\MedAiCarePlus-same-voice -b same-voice agent-tools-fixes`
Then: `cd C:\Reachy_Mini\worktrees\MedAiCarePlus-same-voice; git branch --show-current`
Expected: `Preparing worktree (new branch 'same-voice')` from the first command (git creates the folder `C:\Reachy_Mini\worktrees`), then `same-voice`. Every later step in this plan runs from this worktree root, `C:\Reachy_Mini\worktrees\MedAiCarePlus-same-voice`.

- [ ] **Step 3: Copy the spec and the plan into the worktree**

Run (PowerShell, from the worktree root):
```powershell
Copy-Item "C:\Users\AIDL-14\AppData\Local\Temp\claude\C--Reachy-Mini\1ff25281-c5da-4818-bbf1-9fd9035013bc\scratchpad\samevoice\2026-10-17-same-voice-design.md" "docs\superpowers\specs\2026-10-17-same-voice-design.md"
Copy-Item "C:\Users\AIDL-14\AppData\Local\Temp\claude\C--Reachy-Mini\1ff25281-c5da-4818-bbf1-9fd9035013bc\scratchpad\samevoice\plan-server.md" "docs\superpowers\plans\2026-10-17-same-voice-server.md"
Test-Path docs\superpowers\specs\2026-10-17-same-voice-design.md; Test-Path docs\superpowers\plans\2026-10-17-same-voice-server.md
```
Expected: `True` twice.

- [ ] **Step 4: Run the docs test as the baseline**

Run (from the worktree root): `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_live_voice_docs.py -q -p no:cacheprovider`
Expected: all passed (the docs are unchanged so far).

- [ ] **Step 5: Commit (in the worktree: the branch's first commit)**

```bash
git add docs/superpowers/specs/2026-10-17-same-voice-design.md docs/superpowers/plans/2026-10-17-same-voice-server.md
git commit -m "Same voice: the design and the server plan" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `med_clips.render` takes the voice's instructions

**Files:**
- Modify: `app/services/med_clips.py:104-121`
- Test: `tests/test_med_clips.py`

**Interfaces:**
- Consumes: `med_clips.INSTRUCTIONS` (the medicine prompt's instructions, unchanged), `med_clips.SPEECH_URL`, `normalize_wav`.
- Produces: `render(text: str, voice: str, api_key: str, instructions: str = INSTRUCTIONS) -> bytes`. The three-argument calls in `med_clips.clip`, `reminders.audio` and every existing test fake keep working.

- [ ] **Step 1: Write the failing test**

Add to the end of `tests/test_med_clips.py`:

```python
def test_render_sends_the_instructions_it_is_given_and_the_medicine_ones_by_default(monkeypatch):
    posted = []

    class Answer:
        status_code = 200
        content = _streamed_wav(b"\0\0" * 24)

    def post(url, timeout, headers, json):
        posted.append((url, headers["Authorization"], json))
        return Answer()

    monkeypatch.setattr(med_clips.requests, "post", post)
    med_clips.render("請現在吃第一種藥，Adapalene。", "marin", "sk-x")
    med_clips.render("您好！我來找您聊聊天。", "marin", "sk-x", "A warm, calm companion robot.")
    assert [p[2]["instructions"] for p in posted] == [med_clips.INSTRUCTIONS, "A warm, calm companion robot."]
    assert [p[2]["input"] for p in posted] == ["請現在吃第一種藥，Adapalene。", "您好！我來找您聊聊天。"]
    assert all(p[0] == med_clips.SPEECH_URL and p[1] == "Bearer sk-x" and p[2]["response_format"] == "wav"
               for p in posted)
```

- [ ] **Step 2: Run it to see it fail**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_med_clips.py::test_render_sends_the_instructions_it_is_given_and_the_medicine_ones_by_default -q -p no:cacheprovider`
Expected: FAIL with `TypeError: render() takes 3 positional arguments but 4 were given`

- [ ] **Step 3: Give `render` the argument**

In `app/services/med_clips.py` replace the `render` function (lines 104-121) with:

```python
def render(text: str, voice: str, api_key: str, instructions: str = INSTRUCTIONS) -> bytes:
    """The speech API, tried twice (a read timed out at 30 s on 14 Oct 2026 for a 4 s clip); the WAV, normalized.
    `instructions` tells the voice how to speak: the medicine prompt's by default (the reminders use it too); Reachy's
    own lines bring their own (services/voice_lines.py). Raises on failure (the key never gets logged)."""
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            response = requests.post(SPEECH_URL, timeout=TIMEOUT, headers={"Authorization": f"Bearer {api_key}"},
                                     json={"model": MODEL, "voice": voice, "input": text,
                                           "instructions": instructions, "response_format": "wav"})
        except requests.RequestException as error:
            last = error
            continue
        if response.status_code == 200:
            return normalize_wav(response.content)
        last = RuntimeError(f"speech API answered {response.status_code}")
        if response.status_code < 500 and response.status_code != 429:
            break
    raise last if last is not None else RuntimeError("speech API failed")
```

- [ ] **Step 4: Run the medicine clip and reminder tests**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_med_clips.py tests\test_reminders.py -q -p no:cacheprovider`
Expected: all passed (the new test and every three-argument fake).

- [ ] **Step 5: Commit**

```bash
git add app/services/med_clips.py tests/test_med_clips.py
git commit -m "Same voice: med_clips.render takes the voice's instructions" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: The 304 on `GET /api/device/clips/med/{med_id}`

**Files:**
- Modify: `app/routers/api_device.py:16` (imports), `:247-259` (the route)
- Test: `tests/test_medication_events.py`

**Interfaces:**
- Consumes: `med_clips.clip(u_id, med_id, position, language) -> (bytes, key) | None` (unchanged).
- Produces, in `api_device.py`, for Task 7 too: `_unchanged(request: Request, key: str) -> bool` and `_wav_response(data: bytes | None, key: str) -> Response` (304 when `data` is None, else 200 `audio/wav`; both with `ETag: "<key>"` and `Cache-Control: private`).
- Note: the spec lists this 304 test under `tests/test_med_clips.py`; it goes in `tests/test_medication_events.py` instead, because that file already holds the clip route's test and the device-API harness (`env.robot`), and `test_med_clips.py` has no HTTP client.

- [ ] **Step 1: Write the failing test**

Add to the end of `tests/test_medication_events.py`:

```python
def test_a_robot_whose_medicine_clip_is_current_gets_a_304(env, monkeypatch):
    asked = []

    async def clip(u_id, med_id, position, language):
        asked.append(med_id)
        return b"RIFFwav", "k10"
    monkeypatch.setattr(med_clips, "clip", clip)
    response = env.robot.get("/api/device/clips/med/10", headers={"If-None-Match": '"k10"'})
    assert response.status_code == 304 and response.content == b"" and response.headers["etag"] == '"k10"'
    assert response.headers["cache-control"] == "private"
    # another key (the medicine renamed, the voice changed): the clip itself
    response = env.robot.get("/api/device/clips/med/10", headers={"If-None-Match": '"k9"'})
    assert response.status_code == 200 and response.content == b"RIFFwav"
    # a weak tag, or several tags: the key among them is enough (RFC 9110)
    assert env.robot.get("/api/device/clips/med/10", headers={"If-None-Match": 'W/"k10"'}).status_code == 304
    assert env.robot.get("/api/device/clips/med/10", headers={"If-None-Match": '"k9", "k10"'}).status_code == 304
    assert asked == [10, 10, 10, 10]   # the kept clip is read each time (disk, not the API); the body is skipped
```

- [ ] **Step 2: Run it to see it fail**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_medication_events.py::test_a_robot_whose_medicine_clip_is_current_gets_a_304 -q -p no:cacheprovider`
Expected: FAIL with `assert 200 == 304`

- [ ] **Step 3: Add the helpers and the 304**

In `app/routers/api_device.py` line 16, add `Request` to the FastAPI import:

```python
from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile,
                     WebSocket)
```

Then replace the `medication_clip` route (lines 247-259) with:

```python
def _unchanged(request: Request, key: str) -> bool:
    """If-None-Match names this key: the ETag as it was sent (quoted), a bare key or a weak W/ tag, one or several
    separated by commas (RFC 9110). Then the robot's copy is current and only the headers go back."""
    header = request.headers.get("if-none-match", "")
    tags = [tag.strip().removeprefix("W/").strip('"') for tag in header.split(",")]
    return key in tags


def _wav_response(data: bytes | None, key: str) -> Response:
    """A clip with its key in ETag (Cache-Control private: it is one patient's); 304 with the same headers and no
    body when `data` is None."""
    headers = {"ETag": f'"{key}"', "Cache-Control": "private"}
    if data is None:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type="audio/wav", headers=headers)


@router.get("/clips/med/{med_id}")
async def medication_clip(med_id: int, request: Request, position: int = Query(1, ge=1, le=8),
                          language: str = Query("zh-TW"), device: dict = Depends(get_device)):
    """The spoken prompt for this patient's medicine in `position` of its slot (services/med_clips.py), as a WAV
    with its key in ETag; 304 for a matching If-None-Match (the kept clip is read, never rendered again, when the
    robot's copy is current); 404 when there is none (the robot then says its generic prompt)."""
    from app.services import med_clips

    found = await med_clips.clip(device["u_id"], med_id, position, language)
    if found is None:
        raise HTTPException(404, "No prompt clip for this medicine")
    data, key = found
    return _wav_response(None if _unchanged(request, key) else data, key)
```

- [ ] **Step 4: Run the device tests**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_medication_events.py tests\test_device_api.py -q -p no:cacheprovider`
Expected: all passed (the old route test `test_the_robot_fetches_a_spoken_prompt_or_a_404` still pins the 200, the ETag, the 404 and the 422).

- [ ] **Step 5: Commit**

```bash
git add app/routers/api_device.py tests/test_medication_events.py
git commit -m "Same voice: the medicine clip route answers 304 for a current copy" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `voice_lines`: the words, the spoken form, the instructions and the key

**Files:**
- Create: `app/services/voice_lines.py`
- Create: `tests/test_voice_lines.py`

**Interfaces:**
- Consumes: `med_clips.MODEL`, `med_clips.language_of`, `med_clips.voice_for`, `ai_settings.live_target`, `conversation.HELPLINE`, `conversation.OPENING`, `memory.opening_line`, `live_session.REPEAT_LINES`, `live_session.REMINDER_OPENING`, `live_session.REMINDER_OPENING_NONAME` (imported, not copied; `live_session` is imported inside `line_text`, because `live_session` will import this module in Task 8).
- Produces (all pure, no I/O):
  - `FIXED_LINES = ("checkin_greeting", "repeat", "helpline", "rest", "medicine", "wind_down")`, `PATIENT_LINES = ("opening", "reminder_lead")`, `LINE_IDS = FIXED_LINES + PATIENT_LINES`, `LANGUAGES = ("zh-TW", "en")`, `LINE_TEXTS: dict[str, dict[str, str]]`
  - `line_text(line_id: str, language: str, name: str | None = None) -> str | None`
  - `instructions_for(line_id: str) -> str`
  - `speech_input(line_id: str, text: str, language: str) -> str` (the help line's phone numbers digit by digit; every other line unchanged)
  - `line_key(text: str, voice: str, instructions: str) -> str` (20 hex characters)
  - `key_for(line_id: str, text: str, language: str, voice: str) -> str` (= `line_key(speech_input(...), voice, instructions_for(line_id))`)
  - `expected_clip(line_id: str, text: str, language: str) -> dict | None` (`{"id": line_id, "key": ...}` with the live target's voice; None without a live target, for an unknown id or empty text). A deviation from the spec, kept on purpose: the spec wrote `expected_clip(u_id, line_id, language)`, but the relay already holds the line's words when it sends `ready`/`say`, so it passes them instead of re-reading the call name. Task 6 pins that this key equals the one the route serves.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_voice_lines.py`:

```python
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
```

- [ ] **Step 2: Run them to see them fail**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_voice_lines.py -q -p no:cacheprovider`
Expected: ERROR at collection with `ImportError: cannot import name 'voice_lines' from 'app.services'`

- [ ] **Step 3: Create the module**

Create `app/services/voice_lines.py`:

```python
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
import logging
import re

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
```

- [ ] **Step 4: Run the tests**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_voice_lines.py -q -p no:cacheprovider`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/voice_lines.py tests/test_voice_lines.py
git commit -m "Same voice: the line texts, their spoken form, instructions and key" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: `to_robot_wav` and `render_line`

**Files:**
- Modify: `app/services/voice_lines.py` (imports; new functions after `expected_clip`)
- Test: `tests/test_voice_lines.py`

**Interfaces:**
- Consumes: `med_clips.normalize_wav`, `med_clips.render(text, voice, api_key, instructions)` (Task 2), `scipy.signal.resample_poly`, numpy.
- Produces:
  - `RATE = 16000`, `TRIM_LEVEL = 0.003`, `TRIM_KEEP_SECONDS = 0.01`
  - `to_robot_wav(wav_bytes: bytes) -> bytes`: PCM16 mono 16 kHz WAV, leading silence trimmed to about 10 ms below about -50 dBFS (the deploy tool's values: `np.abs(samples) > 0.003`, keep `rate // 100` samples); `ValueError` for anything but 16-bit PCM or a WAV with no sound.
  - `render_line(text: str, voice: str, api_key: str, instructions: str) -> bytes` (blocking; raises on failure).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_voice_lines.py`:

```python
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
```

- [ ] **Step 2: Run them to see them fail**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_voice_lines.py -q -p no:cacheprovider`
Expected: 2 failed with `AttributeError: module 'app.services.voice_lines' has no attribute 'to_robot_wav'` (and `render_line`), 4 passed

- [ ] **Step 3: Add the conversion**

In `app/services/voice_lines.py`, extend the imports:

```python
import hashlib
import io
import logging
import math
import re
import wave

from app.services import ai_settings, conversation, med_clips, memory
```

Add after `_NUMBER`:

```python
RATE = 16000                 # the robot's live player takes PCM16 mono 16 kHz; robot.play_clip takes it too
TRIM_LEVEL = 0.003           # about -50 dBFS of full scale: the deploy tool's threshold for Matcha's clips ...
TRIM_KEEP_SECONDS = 0.01     # ... which keeps 10 ms of the silence before the first sound
```

Add after `expected_clip`:

```python
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
```

- [ ] **Step 4: Run the tests**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_voice_lines.py -q -p no:cacheprovider`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/voice_lines.py tests/test_voice_lines.py
git commit -m "Same voice: a line is kept as PCM16 mono 16 kHz with its leading silence trimmed" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Storage, the consent gate and `line()`

**Files:**
- Modify: `app/services/voice_lines.py` (imports; new code after `render_line`)
- Test: `tests/test_voice_lines.py`

**Interfaces:**
- Consumes: `config.MED_CLIP_DIR`, `app.database.get_pool`, `consent_service.get_state(u_id)`, `reachy_tasks.checkin_allowed(state)`, `memory.consent_current(state)`, `memory.call_name(conn, u_id, memory_on)`, `med_clips.TIMEOUT` (through `render`).
- Produces:
  - `fixed_path(line_id, language, key) -> Path` = `MED_CLIP_DIR/lines/<language>/<line_id>_<key>.wav`; `patient_path(u_id, line_id, key) -> Path` = `MED_CLIP_DIR/<u_id>/line_<line_id>_<key>.wav`
  - `Located` (frozen dataclass): `line_id, language, text, voice, key, path`
  - `patient_name(u_id: int) -> tuple[bool, str | None]` (check-in consent current?, the call name)
  - `locate(u_id: int | None, line_id: str, language: str) -> Located | None` (None without a live target, for an unknown id, or for a per-patient line without check-in consent; never renders)
  - `fetch(found: Located) -> tuple[bytes, str] | None` (the kept file, else rendered now and kept; one lock per key; a failure logs one WARNING and gives None)
  - `line(u_id, line_id, language) -> tuple[bytes, str] | None` = `locate` then `fetch`
  - `_renders: dict[str, asyncio.Lock]`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_voice_lines.py`:

```python
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
```

- [ ] **Step 2: Run them to see them fail**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_voice_lines.py -q -p no:cacheprovider`
Expected: 6 errors at setup with `AttributeError: <module 'app.services.voice_lines' ...> has no attribute 'get_pool'` (the `voice` fixture), 6 passed

- [ ] **Step 3: Add the storage**

In `app/services/voice_lines.py`, extend the imports:

```python
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
```

Add after the `TRIM_KEEP_SECONDS` line:

```python
_renders: dict[str, asyncio.Lock] = {}   # one lock per key, as med_clips: two requests never render the same line twice
```

Add after `render_line`:

```python
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
    then rename, under the key's lock, like med_clips.clip. None when the live target is gone meanwhile or the
    render failed: one WARNING with the line id, the language and the HTTP status or error type, never the key or
    the words (the opening carries the patient's name). No negative cache: the next request tries again."""
    lock = _renders.setdefault(found.key, asyncio.Lock())
    async with lock:
        if found.path.is_file():
            return found.path.read_bytes(), found.key
        target = ai_settings.live_target()
        if target is None or not target.api_key:
            return None
        try:
            data = await asyncio.to_thread(render_line, speech_input(found.line_id, found.text, found.language),
                                           found.voice, target.api_key, instructions_for(found.line_id))
        except Exception as error:
            log.warning("voice line %s (%s) could not be rendered (%s)", found.line_id, found.language, _reason(error))
            return None
        found.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = found.path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(found.path)
        log.info("voice line %s (%s) rendered in the live voice (%d bytes)", found.line_id, found.language, len(data))
        return data, found.key


async def line(u_id: int | None, line_id: str, language: str) -> tuple[bytes, str] | None:
    """The line in the live voice, (WAV bytes, key), from disk or rendered now; None when it cannot be made (see
    locate and fetch)."""
    found = await locate(u_id, line_id, language)
    return None if found is None else await fetch(found)
```

- [ ] **Step 4: Run the tests**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_voice_lines.py -q -p no:cacheprovider`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/voice_lines.py tests/test_voice_lines.py
git commit -m "Same voice: lines are kept under MED_CLIP_DIR, per-patient ones behind check-in consent" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: `GET /api/device/clips/line/{line_id}`

**Files:**
- Modify: `app/routers/api_device.py` (after `medication_clip`)
- Test: `tests/test_voice_lines.py`

**Interfaces:**
- Consumes: `voice_lines.locate`, `voice_lines.fetch`, `voice_lines.Located` (Task 6); `_unchanged`, `_wav_response`, `get_device` (Task 3).
- Produces: the route `GET /api/device/clips/line/{line_id}?language=` on the device port, device token, the same consent as the medicine clip (`get_device`): 200 `audio/wav` with `ETag: "<key>"` and `Cache-Control: private`; 304 when `If-None-Match` names the key (before any render); 404 when `locate` gives None or the render failed. A database error is a 500, never a 404.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_voice_lines.py`:

```python
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
```

- [ ] **Step 2: Run them to see them fail**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_voice_lines.py -q -p no:cacheprovider -k "304_before_any_render or 404_but"`
Expected: 2 failed with `assert 404 == 200` (the first; on the public app a missing device route answers 404) and `AssertionError` on the detail text (the second)

- [ ] **Step 3: Add the route**

In `app/routers/api_device.py`, right after the `medication_clip` route (before the `# ── Check-in conversations` comment):

```python
@router.get("/clips/line/{line_id}")
async def line_clip(line_id: str, request: Request, language: str = Query("zh-TW"),
                    device: dict = Depends(get_device)):
    """One of Reachy's own lines around a live chat, in the live voice (services/voice_lines.py): a PCM16 mono 16 kHz
    WAV with its key in ETag; 304 for a matching If-None-Match, checked before any render; 404 when there is none
    (no live voice target, an unknown line, a per-patient line without current check-in consent, or a render that
    failed): the robot then drops its copy and says the line with its own voice. A failing lookup is a 500 on
    purpose: the robot keeps its copy through a database error."""
    from app.services import voice_lines

    found = await voice_lines.locate(device["u_id"], line_id, language)
    if found is None:
        raise HTTPException(404, "No clip for this line")
    if _unchanged(request, found.key):
        return _wav_response(None, found.key)
    kept = await voice_lines.fetch(found)
    if kept is None:
        raise HTTPException(404, "No clip for this line")
    data, key = kept
    return _wav_response(data, key)
```

- [ ] **Step 4: Run the tests**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_voice_lines.py tests\test_device_api.py tests\test_medication_events.py -q -p no:cacheprovider`
Expected: all passed (14 in test_voice_lines)

- [ ] **Step 5: Commit**

```bash
git add app/routers/api_device.py tests/test_voice_lines.py
git commit -m "Same voice: the robot fetches each line with its key (200, 304, 404)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: `clip` on the companion `ready` and `say`

**Files:**
- Modify: `app/services/live_session.py:81-83` (imports), `:780-781` (`_open_companion`'s `ready`), `:1321-1323` (`_ask_again`), `:2122-2124` (`_end_blocked`), plus one new method `_with_clip` next to `_to_robot` (`:2290`)
- Test: `tests/test_live_companion.py`, `tests/test_live_safety.py`, `tests/test_live_speak.py`

**Interfaces:**
- Consumes: `voice_lines.expected_clip(line_id, text, language)` (Task 4), `voice_lines.key_for` (tests).
- Produces: for a companion robot (`self.companion`), `ready` gains `"clip": {"id": "opening" | "reminder_lead", "key"}` and `say` gains `"clip": {"id": "repeat" | "helpline", "key"}` when `expected_clip` gives one; `speech_text` and every other field stay. Older robots (not companion) get nothing new. `LiveRelay._with_clip(message: dict, line_id: str, text: str) -> dict`.

- [ ] **Step 1: Update the pinned messages and add the tests**

In `tests/test_live_safety.py`, replace the `_blocked` helper (lines 83-98) with one that takes the clip the companion's help line carries:

```python
def _blocked(world, *, flagged, quote, source, kind="self_harm", dedupe=None, clip=None) -> None:
    """Spec B3, once: flush, the help line in the robot's own voice (for a companion robot with the id and key of
    its copy in the live voice, `clip`), `end` with risk; the turns flagged and the help line stored as the last
    turn; family alerted once; GPT-Live muted and closed; the conversation ended with reason risk and its after-chat
    work started once."""
    chat = world.db.chat()
    say = {**SAY, "clip": clip} if clip is not None else SAY
    # Body language (`express`, services/affect.py) is not part of the safety protocol: an earlier sad turn may
    # have set a posture
    assert [m for m in world.robot.json()[1:] if m["type"] != "express"] == [FLUSH, say, END_RISK]
    assert world.robot.closed == 1000
    assert _flagged(world) == flagged
    assert world.db.rows()[-1] == ("reachy", HELPLINE)
    assert world.db.turns[-1]["metrics"] == {"live": {"say": "risk", "risk_source": source}}
    assert (chat["end_reason"], chat.get("risk_flag")) == ("risk", True)
    assert world.alerts == [_alert(world, quote, dedupe or flagged[-1], kind)]
    assert (world.clients[0].mutes, world.clients[0].closes) == (1, 1)
    assert world.after_chat == [(chat["conversation_id"], 7)]
```

In `tests/test_live_companion.py`:

1. Add `voice_lines` to the services import (line 28):
```python
from app.services import ai_settings, conversation, live_session, memory, monitor_service, voice_lines  # noqa: E402
```

2. After `NOT_SEEN = {"type": "not_seen"}` (line 40) add:
```python
# Same voice: the copies a companion robot plays for Reachy's lines (keys as the line route serves them for marin)
OPENING_CLIP = {"id": "opening", "key": voice_lines.key_for("opening", OPENING, "zh-TW", "marin")}
REPEAT_CLIP = {"id": "repeat", "key": voice_lines.key_for("repeat", live_session.REPEAT_LINES["zh-TW"], "zh-TW", "marin")}
HELP_CLIP = {"id": "helpline", "key": voice_lines.key_for("helpline", HELPLINE, "zh-TW", "marin")}
SAY_CLIP = {**SAY, "clip": HELP_CLIP}
```

3. In `test_a_companion_start_is_paused_and_the_robot_says_the_opening` (line 154-155) make the `ready` assertion:
```python
        assert companion.robot.json() == [{"type": "ready", "conversation_id": cid,
                                           "speech_text": conversation.speech_text(OPENING, "zh-TW"),
                                           "clip": OPENING_CLIP}]
```

4. Replace `SAY` with `SAY_CLIP` in the four companion assertions; only the `SAY` element changes. The four lines become:

Line 554 (the test whose scenario reaches `STRETCH_HARD_MAX_SECONDS` mid-reply and then blocks on 「我想自殺」):
```python
        assert companion.robot.json()[1:] == [FLUSH, FLUSH, PAUSED, SAY_CLIP, END_RISK]
```
Line 573 (in `test_openais_cut_off_still_queued_at_a_quiet_pause_is_judged_and_blocks`):
```python
        assert companion.robot.json()[1:] == [FLUSH, SAY_CLIP, END_RISK]   # blocked: never `paused`
```
Line 669 (in `test_a_robot_end_mid_stretch_still_screens_the_patients_last_words`):
```python
        assert companion.robot.json()[1:] == [FLUSH, SAY_CLIP, END_RISK]   # the help line, if the robot still listens
```
Line 706 (in `test_nothing_restarts_after_a_risk_found_while_paused`):
```python
        assert companion.robot.json()[1:] == [PAUSED, FLUSH, SAY_CLIP, END_RISK]
```
(Find each by the test's name if the line numbers have moved; `grep -n "SAY, END_RISK" tests/test_live_companion.py` lists exactly these four.)

5. In `test_a_stretch_that_cannot_start_asks_the_patient_to_say_it_again` (line 641) make the repeat dict:
```python
        repeat = {"type": "say", "speech_text": "不好意思，我刚刚没听清楚，可以再说一次吗？", "reason": "repeat",
                  "clip": REPEAT_CLIP}
```

6. In `test_the_patients_first_words_in_a_stretch_are_screened` (line 682) pass the clip:
```python
        _blocked(companion, flagged=[2], quote="我想自殺", source="keyword", clip=HELP_CLIP)
```

7. Add a new test after `test_the_patients_first_words_in_a_stretch_are_screened`:
```python
def test_a_say_carries_no_clip_once_the_live_voice_target_is_gone(companion, monkeypatch):
    async def scenario():
        relay, running = await _start(companion)
        assert companion.robot.json("ready")[0]["clip"] == OPENING_CLIP
        monkeypatch.setattr(ai_settings, "live_target", lambda snapshot=None: None)   # the card's model saved again
        await _speak(companion)                               # the stretch still starts: its target was kept at start
        await _live(companion, _said("我想自殺"), _sound())
        await _step(companion, relay, 0.5)
        await _over(companion, running)
        _blocked(companion, flagged=[2], quote="我想自殺", source="keyword")   # the help line, with no clip to name

    asyncio.run(scenario())
```

In `tests/test_live_speak.py`, in `test_a_checkin_task_made_for_a_reminder_opens_with_a_lead_in_then_says_it_after_the_opening`, right after line 146 (`assert ready["speech_text"] == "王奶奶，我来提醒您一件事。"`) add:
```python
        from app.services import voice_lines
        assert ready["clip"] == {"id": "reminder_lead",
                                 "key": voice_lines.key_for("reminder_lead", "王奶奶，我來提醒您一件事。", "zh-TW", "marin")}
```

- [ ] **Step 2: Run them to see them fail**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_live_companion.py tests\test_live_speak.py -q -p no:cacheprovider`
Expected: 9 failed (the `ready` without `clip`, the four `SAY_CLIP` lists, the repeat dict, the `_blocked` call with `clip=HELP_CLIP`, `KeyError: 'clip'` in the new test and in test_live_speak), the rest passed

- [ ] **Step 3: Send the clip beside the words**

In `app/services/live_session.py`:

1. Add `voice_lines` to the services import (lines 81-83):
```python
from app.services import (affect, after_chat, agent_bridge, ai_settings, consent_service, conversation,
                          device_auth, dose_status, gpt_live_config, memory, monitor_service, reachy_tasks, reminders,
                          tool_intents, voice_lines)
```

2. In `_open_companion`, replace the `ready` send (lines 780-781):
```python
        ready = {"type": "ready", "conversation_id": conversation_id,
                 "speech_text": conversation.speech_text(self.opening, self.language)}
        await self.robot.send_json(
            self._with_clip(ready, "reminder_lead" if self._reminder is not None else "opening", self.opening))
```

3. In `_ask_again`, replace the send (lines 1321-1323):
```python
        line = REPEAT_LINES[self.language]
        await self._to_robot(self._with_clip({"type": "say", "speech_text": conversation.speech_text(line, self.language),
                                              "reason": "repeat"}, "repeat", line))
```

4. In `_end_blocked`, replace the send (lines 2122-2124):
```python
        helpline = conversation.HELPLINE[self.language]
        await self._to_robot(self._with_clip({"type": "say",
                                              "speech_text": conversation.speech_text(helpline, self.language),
                                              "reason": "risk"}, "helpline", helpline))
```

5. Add the method just above `_to_robot` (line 2290):
```python
    def _with_clip(self, message: dict, line_id: str, text: str) -> dict:
        """Same voice (services/voice_lines.py): beside the words a companion robot says with its own voice, the id
        and key of that line's copy in the live voice (voice_lines.expected_clip, the key the line route serves).
        Robot app 0.16.0 plays its kept copy when the key matches, else Matcha now and it fetches the copy for next
        time; 0.15.0 ignores the field. Nothing is added without a live voice target, and never for an older robot.
        No render, no database: `ready` and `say` never wait."""
        if self.companion:
            clip = voice_lines.expected_clip(line_id, text, self.language)
            if clip is not None:
                message["clip"] = clip
        return message
```

- [ ] **Step 4: Run every live test**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_live_companion.py tests\test_live_safety.py tests\test_live_speak.py tests\test_live_access.py tests\test_live_relay_chat.py tests\test_live_start.py tests\test_live_tool_backstop.py tests\test_live_agent_tools.py tests\test_live_expression.py tests\test_voice_lines.py -q -p no:cacheprovider`
Expected: all passed (the older-robot tests still compare the plain `SAY` and `ready`)

- [ ] **Step 5: Commit**

```bash
git add app/services/live_session.py tests/test_live_companion.py tests/test_live_safety.py tests/test_live_speak.py
git commit -m "Same voice: ready and say name the copy the robot should play" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: The fixed lines rendered ahead: the `[STARTUP]` stage and the 30-minute retry

**Files:**
- Modify: `app/services/voice_lines.py` (new code after `line`)
- Modify: `app/startup_report.py:29-30` (`STAGES`), `:218-233` (`finish`), new `voice_lines_stage` and `_render_voice_lines`
- Modify: `app/jobs/scheduler.py` (one import, one job before `scheduler.start()`)
- Test: `tests/test_voice_lines.py`

**Interfaces:**
- Consumes: `voice_lines.line`, `FIXED_LINES`, `LANGUAGES`; `startup_report.mark` and `startup_report._tasks` (the set `finish_in_background` keeps its task in); `apscheduler.triggers.interval.IntervalTrigger`.
- Produces:
  - `voice_lines.LINES_RETRY_MINUTES = 30`
  - `voice_lines.render_fixed_ahead() -> list[str]`: every fixed line in both languages, rendered when missing; the ids that failed as `"<line_id>/<language>"`; `[]` without a live target.
  - `voice_lines.retry_missing() -> None` (the scheduler job `voice_lines`, every 30 min).
  - `startup_report.voice_lines_stage() -> asyncio.Task | None`, called by `finish` after the automatic Check and before `log_summary`; stage name `voice_lines` in `STAGES`. Without a target: OK at once, no task. With one: marked pending at once (like `gpt_live` while its automatic Check runs: `ok` False, `pending` True, `warn`), and the renders run as a background task kept in `_tasks` (`_render_voice_lines(total: int) -> None`), so the 12 renders one after another, each up to two tries of `med_clips.TIMEOUT` (40 s), never hold up the summary line. When they end, that task marks the stage OK when every line is kept, or DEGRADED (warn) naming the failed ids. (The spec writes the stage as `voice lines`; it is spelled `voice_lines` like every other stage name in `STAGES`, `gpt_live`, `device_api`, so `GET /health` and the log line follow the same pattern.)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_voice_lines.py`:

```python
# ── rendered ahead: the fixed lines at startup, and again every 30 min ──

def test_the_fixed_lines_are_rendered_ahead_once_and_the_failures_are_named(voice, tmp_path):
    assert asyncio.run(voice_lines.render_fixed_ahead()) == [] and len(voice.renders) == 12   # 6 lines x 2 languages
    for language in ("zh-TW", "en"):
        kept = {p.name.rsplit("_", 1)[0] for p in (tmp_path / "lines" / language).iterdir()}
        assert kept == set(voice_lines.FIXED_LINES)
    assert asyncio.run(voice_lines.render_fixed_ahead()) == [] and len(voice.renders) == 12   # nothing missing now
    found = asyncio.run(voice_lines.locate(None, "helpline", "en"))
    found.path.unlink()                                               # one copy lost (a wiped volume)
    voice.fail = RuntimeError("speech API answered 503")
    assert asyncio.run(voice_lines.render_fixed_ahead()) == ["helpline/en"] and len(voice.renders) == 13
    voice.fail = None
    assert asyncio.run(voice_lines.render_fixed_ahead()) == [] and len(voice.renders) == 14
    voice.target = None
    assert asyncio.run(voice_lines.render_fixed_ahead()) == [] and len(voice.renders) == 14   # nothing to do


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
        assert logged[-1]["pending"] and not logged[-1]["ok"] and "12 fixed lines" in logged[-1]["detail"]
        assert startup_report._tasks
        gate["release"].set()
        await asyncio.gather(*startup_report._tasks)
        return startup_report.summary()["stages"]["voice_lines"]   # the stage's final mark, after the renders

    stage = asyncio.run(scenario())
    assert stage["ok"] and not stage["pending"] and "12 fixed lines kept" in stage["detail"]
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
    assert "12 fixed lines are still missing" in caplog.text and "checkin_greeting/zh-TW" in caplog.text
```

- [ ] **Step 2: Run them to see them fail**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_voice_lines.py -q -p no:cacheprovider`
Expected: 4 failed (`AttributeError: ... no attribute 'render_fixed_ahead'` and `... 'retry_missing'`, `assert 'voice_lines' in STAGES`, `KeyError: 'voice_lines'`), 14 passed

- [ ] **Step 3: Add the render-ahead, the stage and the job**

In `app/services/voice_lines.py`, after the `TRIM_KEEP_SECONDS` line add:

```python
LINES_RETRY_MINUTES = 30     # the scheduler tries the fixed lines still missing again this often (voice_lines job)
```

Append after `line`:

```python
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
```

In `app/startup_report.py`:

1. Add the stage to `STAGES` (lines 29-30):
```python
STAGES = ("environment", "database", "ai_settings", "gpt_live", "models", "scheduler", "outbox", "line", "reminders",
          "device_api", "network", "clock", "voice_lines")
```

2. Add before `finish` (after `auto_check_live`):
```python
def voice_lines_stage() -> asyncio.Task | None:
    """Reachy's own lines around a live chat, rendered ahead in the live voice (services/voice_lines.py), after the
    live Check whatever its stored status, so a fresh start whose Check had been cleared still gets them. OK at once
    without a live target (nothing to render). Otherwise pending at once, as gpt_live while its Check runs, and the
    renders run in the background (kept in _tasks, as finish_in_background does): 12 renders one after another, each
    up to two tries of med_clips.TIMEOUT, must never hold up the summary line. _render_voice_lines settles the stage."""
    from app.services import ai_settings, voice_lines
    if ai_settings.live_target() is None:
        mark("voice_lines", True, "no live voice target: Reachy's own lines stay in the robot's own voice")
        return None
    total = len(voice_lines.FIXED_LINES) * len(voice_lines.LANGUAGES)
    mark("voice_lines", False, f"rendering the {total} fixed lines ahead in the live voice, in the background; the "
                               f"robot says them with its own voice until they are kept", pending=True, warn=True)
    task = asyncio.get_running_loop().create_task(_render_voice_lines(total), name="startup-voice-lines")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def _render_voice_lines(total: int) -> None:
    """The stage's background task: every fixed line rendered when missing, then the final mark: OK with every
    fixed line kept, DEGRADED naming the lines that failed (the voice_lines scheduler job tries them again; the robot
    says them with its own voice meanwhile)."""
    from app.services import voice_lines
    try:
        failed = await voice_lines.render_fixed_ahead()
    except Exception as error:
        mark("voice_lines", False, f"the render ahead failed: {type(error).__name__}", warn=True)
        return
    if failed:
        mark("voice_lines", False, f"{len(failed)} of {total} fixed lines could not be rendered in the live voice "
                                   f"({', '.join(failed)}); the robot says them with its own voice meanwhile; tried "
                                   f"again every {voice_lines.LINES_RETRY_MINUTES} min", warn=True)
    else:
        mark("voice_lines", True, f"{total} fixed lines kept in the live voice (both languages)")
```

3. In `finish`, between the automatic Check block and `log_summary()`:
```python
    if live_status in ("no_check", "check_failed"):
        try:
            await auto_check_live(snapshot)
        except Exception as error:
            mark("gpt_live", False, f"the automatic Check failed: {type(error).__name__}: {error}", warn=True)
    try:
        voice_lines_stage()   # pending at once; its renders run in the background and never hold up the summary line
    except Exception as error:
        mark("voice_lines", False, f"the render ahead could not start: {type(error).__name__}", warn=True)
    log_summary()
```
Also add to the module docstring's last paragraph (after "...until someone presses the button."): ` Then the fixed lines Reachy says around a live chat are rendered ahead in the live voice, in the background (services/voice_lines.py): the voice_lines stage stays pending until they end, so the summary line never waits for them.`

In `app/jobs/scheduler.py`:

1. Add after `from app.services.reminders import sweep as sweep_reminders`:
```python
    from app.services.voice_lines import LINES_RETRY_MINUTES, retry_missing as retry_voice_lines
```

2. Add before `scheduler.start()`:
```python
    scheduler.add_job(   # Reachy's own lines in the live voice: the fixed ones still missing are rendered again
        retry_voice_lines,
        IntervalTrigger(minutes=LINES_RETRY_MINUTES),
        id="voice_lines",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=None,
        coalesce=True,
    )
```

- [ ] **Step 4: Run the tests**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_voice_lines.py tests\test_startup_report.py tests\test_context_info.py tests\test_schedule_adherence.py -q -p no:cacheprovider`
Expected: all passed (18 in test_voice_lines; `test_every_scheduled_job_can_be_imported` and the weather job test are unchanged)

- [ ] **Step 5: Commit**

```bash
git add app/services/voice_lines.py app/startup_report.py app/jobs/scheduler.py tests/test_voice_lines.py
git commit -m "Same voice: the fixed lines are rendered ahead at startup and retried every 30 min" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: The per-patient lines rendered ahead when a task is queued

**Files:**
- Modify: `app/services/voice_lines.py` (new code after `retry_missing`)
- Modify: `app/services/reachy_tasks.py:103-149` (`enqueue_reachy_task`)
- Test: `tests/test_voice_lines.py`, `tests/test_reachy_tasks.py` (a new test, and the `live_ready` fixture at lines 905-915)

**Interfaces:**
- Consumes: `voice_lines.line`, `PATIENT_LINES`, `LANGUAGES`, `ai_settings.live_target`.
- Produces: `voice_lines.render_ahead_for_patient(u_id: int) -> asyncio.Task | None`: a background task rendering `opening` and `reminder_lead` in both languages (nothing when they are kept); None at once without a live target (no task, no database). `reachy_tasks.enqueue_reachy_task` keeps its signature and calls it for every task it opens or re-arms (a dose task's slot ends with a check-in too).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_voice_lines.py`:

```python
# ── rendered ahead: the patient's lines ──

def test_the_patients_lines_are_rendered_ahead_in_the_background(voice):
    async def scenario():
        task = voice_lines.render_ahead_for_patient(7)
        assert task is not None
        await task
        assert voice_lines.render_ahead_for_patient(7) is not None   # kept already: no new render
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert sorted(text for text, *_ in voice.renders) == sorted([
        OPENING_ZH, OPENING_EN, "王奶奶，我來提醒您一件事。", "王奶奶, I'm here to remind you of something."])
    voice.target = None
    assert voice_lines.render_ahead_for_patient(7) is None          # no live target: nothing, and no loop needed


def test_a_render_ahead_that_fails_is_logged_and_never_raises(voice, caplog, monkeypatch):
    async def broken(u_id):
        raise RuntimeError("database down")   # the consent or name read failed (locate raises through line)
    monkeypatch.setattr(voice_lines, "patient_name", broken)

    async def scenario():
        await voice_lines.render_ahead_for_patient(7)   # the task finishes without raising

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert "voice lines for patient 7: the render ahead failed (RuntimeError)" in caplog.text
    assert voice.renders == [] and "database down" not in caplog.text
```

Add to `tests/test_reachy_tasks.py` after `test_checkin_task_has_no_dose_to_check`:

```python
def test_a_queued_task_renders_the_patients_lines_ahead(db, monkeypatch):
    from app.services import voice_lines
    ahead = []
    monkeypatch.setattr(voice_lines, "render_ahead_for_patient", lambda u_id: ahead.append(u_id))
    assert enqueue(db, reason="checkin", intk_ids=()) and ahead == [7]
    assert enqueue(db) and ahead == [7, 7]                   # a dose task's slot ends with a check-in too
    db.devices[7]["revoked"] = True
    assert enqueue(db, slot=T0 + timedelta(hours=8)) is None and ahead == [7, 7]   # no task: nothing to render for
```

In the same file, the `live_ready` fixture (lines 905-915) makes `ai_settings.live_target()` give a real target, so from this task on each `enqueue` there would start a background render. Fake it: right after `monkeypatch.setattr(config, "LIVE_VOICE_TEST", True)` add:

```python
    from app.services import voice_lines
    # the live flag's tests are not about the render-ahead: nothing is rendered here
    monkeypatch.setattr(voice_lines, "render_ahead_for_patient", lambda u_id: None)
```

(Step 2 does not run the live-flag tests: until Step 3 adds `render_ahead_for_patient`, this `setattr` raises.)

- [ ] **Step 2: Run them to see them fail**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_voice_lines.py tests\test_reachy_tasks.py::test_a_queued_task_renders_the_patients_lines_ahead -q -p no:cacheprovider`
Expected: 3 failed (`AttributeError: ... no attribute 'render_ahead_for_patient'`), the rest passed

- [ ] **Step 3: Add the render-ahead and the hook**

In `app/services/voice_lines.py`, after `_renders` add:

```python
_ahead: set = set()   # the background renders of the per-patient lines (render_ahead_for_patient), kept until done
```

Append after `retry_missing`:

```python
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
```

In `app/services/reachy_tasks.py`, rename the existing `enqueue_reachy_task` to `_enqueue` (keep its body and docstring as they are; only the `def` line changes) and add above it:

```python
async def enqueue_reachy_task(conn, u_id: int, slot_time, intk_ids: list[int], reason: str, expires_at,
                              at=None) -> str | None:
    """_enqueue (the task), then the patient's own lines in the live voice rendered ahead for the check-in the task
    ends with (services/voice_lines.py; nothing without a live voice target)."""
    task_id = await _enqueue(conn, u_id, slot_time, intk_ids, reason, expires_at, at)
    if task_id is not None:
        from app.services import voice_lines   # here, not at the top: voice_lines uses this module's consent gate
        voice_lines.render_ahead_for_patient(u_id)
    return task_id
```

(`_enqueue` keeps the original docstring, which starts "Open (or re-arm a queued) task for this slot inside the caller's transaction.")

- [ ] **Step 4: Run the tests**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_voice_lines.py tests\test_reachy_tasks.py tests\test_reminders.py tests\test_device_api.py -q -p no:cacheprovider`
Expected: all passed (20 in test_voice_lines; the payload key set at `test_lease_next_leases_oldest_queued_and_returns_payload` is unchanged), and the live-flag tests (the `live_ready` fixture) log no voice-line WARNING

- [ ] **Step 5: Commit**

```bash
git add app/services/voice_lines.py app/services/reachy_tasks.py tests/test_voice_lines.py tests/test_reachy_tasks.py
git commit -m "Same voice: a queued task renders the patient's opening and lead-in ahead" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Render ahead when the call name's inputs change

**Files:**
- Modify: `app/routers/api_reachy.py:14` (imports), `:112-130` (`settings`)
- Modify: `app/routers/api_memory.py:14` (imports), `:52-62` (`_save`), `:83-88` (`_delete`)
- Modify: `app/routers/api_consent.py:10` (imports), `:29-57` (`record_consent`)
- Test: `tests/test_reachy_settings.py`, `tests/test_api_memory.py`, `tests/test_consent.py`

**Interfaces:**
- Consumes: `voice_lines.render_ahead_for_patient(u_id)` (Task 10), called after each route's transaction has committed (the background task reads the committed name and consent through its own connection).
- Produces: the hook in the three routes. No new fields or status codes.

- [ ] **Step 1: Write the failing tests**

Add to the end of `tests/test_reachy_settings.py`:

```python
def test_a_new_call_name_renders_the_opening_ahead(env, monkeypatch):
    from app.services import voice_lines
    ahead = []
    monkeypatch.setattr(voice_lines, "render_ahead_for_patient", lambda u_id: ahead.append(u_id))
    assert _settings(env, {"auto_record": True}).status_code == 200
    assert ahead == []                                        # no name in the request: the opening is unchanged
    assert _settings(env, {"call_name": "阿嬤"}).status_code == 200
    assert ahead == [7]
    assert _settings(env, {"call_name": ""}).status_code == 200    # back to the registered name: a new opening too
    assert ahead == [7, 7]
    assert _settings(env, {"call_name": "我不想活了"}).status_code == 422
    assert ahead == [7, 7]                                    # nothing saved, nothing rendered
```

Add to `tests/test_api_memory.py` after `test_add_validates_and_upserts_a_patient_row`:

```python
def test_a_name_entry_written_or_deleted_renders_the_opening_ahead(client_factory, monkeypatch):
    from app.services import voice_lines
    ahead = []
    monkeypatch.setattr(voice_lines, "render_ahead_for_patient", lambda u_id: ahead.append(u_id))
    conn = Conn(rows=[{"memory_id": uuid.uuid4(), "kind": "name", "subject": "preferred_name"}])
    client = client_factory(dict(MEMORY_ON), conn)
    assert client.post("/api/memory", json={"kind": "like", "text": "喜歡散步"}).status_code == 200
    assert ahead == []                                        # another kind leaves the opening as it is
    assert client.post("/api/memory", json={"kind": "name", "text": "王奶奶"}).status_code == 200
    assert ahead == [7]
    assert client.delete("/api/memory/fact?kind=name&subject=preferred_name").status_code == 200
    assert ahead == [7, 7]
    assert client.delete("/api/memory?confirm=all").status_code == 200
    assert ahead == [7, 7, 7]                                 # everything deleted: the name entry may be among it
```

Add to the end of `tests/test_consent.py`:

```python
def test_a_consent_change_renders_the_patients_voice_lines_ahead(monkeypatch):
    from app.services import voice_lines
    ahead = []
    monkeypatch.setattr(voice_lines, "render_ahead_for_patient", lambda u_id: ahead.append(u_id))
    conn = _Connection()
    client = _client(monkeypatch, conn)
    assert client.post("/api/consent", json=_payload()).status_code == 200
    assert ahead == [7]                                       # memory consent decides whose name the opening uses
    assert client.post("/api/consent", json=_payload(terms_version="old")).status_code == 409
    assert ahead == [7]                                       # refused: nothing changed, nothing rendered
```

- [ ] **Step 2: Run them to see them fail**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_reachy_settings.py::test_a_new_call_name_renders_the_opening_ahead tests\test_api_memory.py::test_a_name_entry_written_or_deleted_renders_the_opening_ahead tests\test_consent.py::test_a_consent_change_renders_the_patients_voice_lines_ahead -q -p no:cacheprovider`
Expected: 3 failed with `assert [] == [7]`

- [ ] **Step 3: Add the hooks**

In `app/routers/api_reachy.py`:

1. Line 14:
```python
from app.services import consent_service, dose_safety, memory, reachy_tasks, schedule, voice_lines
```

2. Replace the `settings` route (lines 112-130):
```python
@router.patch("/settings")
async def settings(payload: SettingsPayload, user: dict = Depends(get_consented_user)):
    """A call name follows the rules of the patient's memory 'name' entry (422 invalid_call_name); an empty one goes
    back to the registered name (NULL). A changed name renders the opening ahead in the live voice (voice_lines)."""
    call_name = None
    if payload.call_name is not None and payload.call_name.strip():
        call_name = memory.clean_call_name(payload.call_name)
        if call_name is None:
            raise HTTPException(422, "invalid_call_name")
    async with get_pool().acquire() as conn, conn.transaction():
        updated = await conn.fetchval(
            "UPDATE reachy_device SET auto_record = COALESCE($2, auto_record), live_voice = COALESCE($3, live_voice) "
            "WHERE u_id = $1 AND revoked_at IS NULL RETURNING device_id",
            user["u_id"], payload.auto_record, payload.live_voice)
        if updated is None:
            raise HTTPException(409, "robot_not_paired")
        if payload.call_name is not None:
            await conn.execute('UPDATE "user" SET reachy_call_name = $2 WHERE u_id = $1', user["u_id"], call_name)
        status = await _status(conn, user["u_id"])
    if payload.call_name is not None:   # after the commit: the render reads the new name on its own connection
        voice_lines.render_ahead_for_patient(user["u_id"])
    return status
```

In `app/routers/api_memory.py`:

1. Line 14:
```python
from app.services import consent_service, deletion_ledger, memory, voice_lines
```

2. In `_save`, after the transaction block (before `if saved is None:`), the function becomes:
```python
async def _save(u_id: int, raw: dict) -> dict:
    if not memory.consent_current(await consent_service.get_state(u_id)):
        raise HTTPException(403, "memory_consent_required")
    fact = memory.validate_fact(raw, today=memory.local_today(), source="patient")
    if fact is None:
        raise HTTPException(422, "invalid_fact")
    async with get_pool().acquire() as conn, conn.transaction():
        saved = await memory.save_patient_fact(conn, u_id, fact)
    if saved is None:
        raise HTTPException(403, "memory_consent_required")
    if fact["kind"] == "name":   # the opening line uses it: rendered ahead in the live voice (services/voice_lines.py)
        voice_lines.render_ahead_for_patient(u_id)
    return _item(saved, {})
```

3. `_delete` becomes:
```python
async def _delete(u_id: int, kind: str | None, subject: str | None) -> list[str]:
    async with get_pool().acquire() as conn, conn.transaction():
        ids = await memory.delete_facts(conn, u_id, kind, subject)
    for memory_id in ids:
        deletion_ledger.append_host_file("memory", u_id, memory_id)
    if kind in (None, "name"):   # the name entry may be gone: the opening goes back to the card's or registered name
        voice_lines.render_ahead_for_patient(u_id)
    return ids
```

In `app/routers/api_consent.py`:

1. Line 10:
```python
from app.services import consent_service, deletion_ledger, dose_video, reachy_tasks, voice_lines
```

2. In `record_consent`, after `consent_service.invalidate(u_id)` (line 53) add:
```python
    # Memory consent decides whose name the opening line uses, and check-in consent whether the patient's lines may
    # be made at all: the live voice copies are rendered ahead (or, without consent, nothing is)
    voice_lines.render_ahead_for_patient(u_id)
```

- [ ] **Step 4: Run the tests**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_reachy_settings.py tests\test_api_memory.py tests\test_consent.py tests\test_consent_withdrawal_robot.py tests\test_memory_ledger.py tests\test_consent_enforcement.py -q -p no:cacheprovider`
Expected: all passed (the other tests in these files never set a live target, so the hook returns None at once)

- [ ] **Step 5: Commit**

```bash
git add app/routers/api_reachy.py app/routers/api_memory.py app/routers/api_consent.py tests/test_reachy_settings.py tests/test_api_memory.py tests/test_consent.py
git commit -m "Same voice: a changed call name, name entry or consent renders the opening ahead" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: Docs, and the whole suite green

**Files:**
- Modify: `CLAUDE.md:232` (a new bullet before "- **Not in 2a (Phase 2b, before any real patient):**")
- Modify: `HANDOFF.md:299` (a new paragraph before "**Real patients need Phase 2b first.**")
- Test: `tests/test_live_voice_docs.py` (unchanged; it checks every backticked name below against the code)

**Interfaces:**
- Consumes: every name in Tasks 2-11. Robot-only names (the robot's line cache, its client method) are written in plain words, never backticked, because the docs test refuses names that only exist in `medcare-reachy-bridge`.
- Produces: the docs the spec asks for: the `clip` field, the line route, the lines table, the robot app 0.16.0 requirement.

- [ ] **Step 1: Run the docs test before editing (it must pass now, and still after)**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_live_voice_docs.py -q -p no:cacheprovider`
Expected: all passed

- [ ] **Step 2: Add the CLAUDE.md bullet**

In `CLAUDE.md`, insert this bullet as a new line directly above the line that starts `- **Not in 2a (Phase 2b, before any real patient):**` (line 232), inside the "Live voice check-ins (Phase 2a, test)" section:

```markdown
- **Same voice (17 Oct 2026; spec `docs/superpowers/specs/2026-10-17-same-voice-design.md`).** In a live chat GPT-Live speaks in the live voice (marin) while Reachy's own lines came from the robot's Matcha voice, so the patient heard the voice jump at every one of them. Now the server renders those lines once in the live voice (`services/voice_lines.py`, through `med_clips.render`, which takes an instructions argument per line kind: a warm, calm one for the companion lines, a calm, clear one for the help line that reads the phone numbers digit by digit, `voice_lines.speech_input`) as PCM16 mono 16 kHz WAVs with the leading silence trimmed (`voice_lines.to_robot_wav`), keyed by the model, the voice, the instructions and the words (`voice_lines.line_key`, a different formula from `med_clips.clip_key`, so the medicine clips' keys are unchanged), and keeps them under `MED_CLIP_DIR` (the fixed lines in a lines folder per language, the per-patient lines beside the patient's medicine clips). The lines (`voice_lines.LINE_TEXTS` and `voice_lines.line_text`): `checkin_greeting`, `rest`, `medicine`, `wind_down` (the robot's own lines, in Traditional and English), `repeat` (`live_session.REPEAT_LINES`), `helpline` (`conversation.HELPLINE`), and per patient `opening` (`memory.opening_line`) and `reminder_lead` (`REMINDER_OPENING`). Only while a live voice target exists (`ai_settings.live_target`, the medicine clips' gate); the per-patient lines also need current check-in consent (`reachy_tasks.checkin_allowed`). The robot fetches each one with `GET /api/device/clips/line/{line_id}?language=` (device token, the medicine clip's consent): 200 with the key in ETag, 304 for a matching If-None-Match before any render, 404 without a live target, for an unknown line, for a per-patient line without check-in consent, or when the render failed (one WARNING with the line and the language, never the key or the words), and the robot then drops its copy and says the line with its own voice; `GET /api/device/clips/med/{med_id}` answers the same 304 now. The relay names the copy to play beside the words: `ready` gains `clip` (the id, `opening` or `reminder_lead`, and the key) and `say` gains `clip` (`repeat` or `helpline`), for companion robots only (`voice_lines.expected_clip`, `LiveRelay._with_clip`); `speech_text` stays, so a robot without the copy, or app 0.15.0, which ignores `clip`, says it with Matcha as before, and the help line's rules are unchanged. Rendered ahead, never inside a robot's wait: the fixed lines in both languages after the live Check at startup (`startup_report.finish`, the `[STARTUP]` stage `voice_lines`: pending while they render in the background, so the summary line never waits for them; DEGRADED names the lines that failed) and again every `LINES_RETRY_MINUTES = 30` minutes while any is missing (`voice_lines.retry_missing`, the `voice_lines` scheduler job); the per-patient lines in the background when a task is queued (`reachy_tasks.enqueue_reachy_task`) and when the call name's inputs change (`PATCH /api/reachy/settings`, a memory `name` entry written or deleted, a consent change: `voice_lines.render_ahead_for_patient`). Hearing them needs robot app 0.16.0 (medcare-reachy-bridge, its own plan), which keeps the copies by key and plays them; the server sends the fields to every companion robot.
```

- [ ] **Step 3: Add the HANDOFF.md paragraph**

In `HANDOFF.md`, insert this paragraph (with a blank line before and after) directly above the line that starts `**Real patients need Phase 2b first.**` (line 299), inside section 6:

```markdown
**Same voice (17 Oct 2026).** Reachy's own lines around a live chat (the greeting, the opening with the name, the reminder lead-in, the repeat line, the help line, the rest and medicine lines, the wind-down) are rendered once by the server in the live voice and kept (`services/voice_lines.py`, `GET /api/device/clips/line/{line_id}`); the relay names the copy to play beside the words (`clip` on `ready` and `say`). Design: `docs/superpowers/specs/2026-10-17-same-voice-design.md`; how it works: CLAUDE.md, "Same voice" in the live voice section. **Hearing them needs robot app 0.16.0** (medcare-reachy-bridge, its own plan): it keeps the copies and plays them, and falls back to its Matcha voice for any line it has no copy of. Robot app 0.15.0 keeps working with this server and ignores the new fields; nothing changes without `LIVE_VOICE_TEST=1` and a passing Check.
```

- [ ] **Step 4: Run the docs test**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests\test_live_voice_docs.py -q -p no:cacheprovider`
Expected: all passed. If `test_every_code_name_in_the_section_is_in_the_code` fails, its message names the span (`name ...`, `route ...`, `file ...` or `constant ...`); fix the code name in the doc, never by adding a stray name to the code.

- [ ] **Step 5: Run the whole suite**

Run: `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider`
Expected: all passed, no failures, no errors.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md HANDOFF.md
git commit -m "Same voice: CLAUDE.md and HANDOFF.md describe the lines, the route and the clip field" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 7: Hand over**

Run (in the worktree, `C:\Reachy_Mini\worktrees\MedAiCarePlus-same-voice`): `git log --oneline agent-tools-fixes..same-voice`
Expected: 12 commits, one per task. Do not push or merge. The robot app 0.16.0 plan can now be built against this branch; until it is deployed, robot app 0.15.0 keeps working and hears Matcha for Reachy's own lines, as before.
