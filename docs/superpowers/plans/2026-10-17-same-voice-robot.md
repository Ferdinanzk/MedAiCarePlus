# Same Voice (robot side) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reachy's own lines around a live chat (greeting, opening, reminder lead-in, repeat, help line, rest, medicine, wind-down) play from copies the server rendered in GPT-Live's voice, with Matcha and the manifest WAV as the fallback, in robot app 0.16.0.

**Architecture:** A new `bridge/lines.py` `LineCache` keeps one WAV plus a key sidecar per line under `~/.medcare_reachy/lines/<language>/`, fetched from the server's `GET /api/device/clips/line/{id}` route through a new `AppClient.line_clip` (304 keeps, 404 drops, a bad body is refused), and refreshed in the background at service start and at each task lease. Inside a live chat `LiveChat._start_say` plays the kept copy through the same playback queue in one-second pieces when the server's `clip` key matches (else Matcha now and a fetch for next time); around the chat `SlotSession` plays the kept greeting and wind-down through `robot.play_clip`, gated on the task's `live` flag and on a live chat having run.

**Tech Stack:** Python 3.12 on the Reachy Mini (`requires-python >= 3.10`), asyncio, numpy, httpx (`MockTransport` in tests), the standard `wave` module, pytest.

**Spec:** `docs/superpowers/specs/2026-10-17-same-voice-design.md` in the server repo (`C:\Reachy_Mini\repo\MedAiCarePlus`). The read-only code maps the spec cites (`samevoice/robot.md`, `samevoice/server.md`) sit beside it in the scratchpad folder `C:\Users\AIDL-14\AppData\Local\Temp\claude\C--Reachy-Mini\1ff25281-c5da-4818-bbf1-9fd9035013bc\scratchpad\samevoice\`.

**Repo and branch:** the robot app `C:\Reachy_Mini\repo\medcare-reachy-bridge`. All work goes on a new branch `same-voice` made from `feature/merged-live-emotion` at commit `437b136` (Task 1, Step 1 creates it). Never push, never merge. Every commit ends with the trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` (the commit commands below carry it as a second `-m`).

**How the tests run:** the robot repo has no virtual environment of its own; the server's has pytest 9.1.1 and every dependency the tests need. From the robot repo root, in Git Bash:

```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -m pytest tests medcare_reachy/bridge/tests -q -p no:cacheprovider
```

At `437b136` this gives `737 passed, 6 skipped` (743 collected). Two of the tests are timing checks that miss on Windows now and then, in a run of the whole suite and when run alone: `medcare_reachy/bridge/tests/test_speech.py::test_say_tells_when_its_first_sound_is_about_to_play` (a margin of about 3 ms) and `medcare_reachy/bridge/tests/test_filler_echo.py::test_its_echo_goes_and_the_patient_s_next_words_stay`. The rule for them, at every step that runs the suite: if only one or both of these fail, rerun the failing ones alone, up to three times, with

```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -m pytest "medcare_reachy/bridge/tests/test_speech.py::test_say_tells_when_its_first_sound_is_about_to_play" "medcare_reachy/bridge/tests/test_filler_echo.py::test_its_echo_goes_and_the_patient_s_next_words_stay" -q -p no:cacheprovider
```

A pass on any rerun means it is not a regression. Any other failure is a regression of your task: a test that fails on all three reruns, or any test other than these two. `PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider` keep the working tree clean (`.gitignore` covers `__pycache__` anyway). One test runs as `... -m pytest medcare_reachy/bridge/tests/test_lines.py::test_name -q -p no:cacheprovider`.

## Global Constraints

- Scope v1 (spec "Claude's calls"): only `checkin_greeting`, `opening`, `reminder_lead`, `repeat`, `helpline`, `rest`, `medicine`, `wind_down`. The dose-flow manifest clips, the text chat's replies and openings, 「嗯」 and the thinking phrases stay Matcha. No line's words change; no new lines.
- "The robot never guesses a line's text": for `opening`, `reminder_lead`, `repeat` and `helpline` the kept copy plays only when the server's `clip.key` matches the sidecar; otherwise Matcha now and a fetch for next time. The robot-owned `checkin_greeting`, `rest`, `medicine` and `wind_down` play the kept copy for that id (no key check) and are refreshed by key (If-None-Match) at service start and at each task lease.
- "Marin only while a live voice target exists": a 404 from the line route drops the kept copy and its sidecar (anti-goal: a line in the wrong voice once the live voice is switched off).
- The kept greeting plays only when the leased task says `live`; the kept wind-down only when `outcomes["live"]` is set; no wind-down at all after `outcomes["live"] == "ended"` and `outcomes["checkin"] == "goodbye"`; the wind-down after `risk` is unchanged (it plays, from the kept copy when there is one).
- `_may_go_live` (`session.py:1040-1048`) is unchanged: it still requires Matcha (`speaker.available`). The clip branch in `_start_say` comes before the `speaker.available` check.
- Every SAY rule of the live chat is unchanged: GPT-Live audio dropped while a line is said (`_say_active`), the microphone muted for the line plus `SAY_TAIL_SECONDS = 0.6`, still posture for `reason == "risk"`, `playing`/`idle` reported, the final drain (`DRAIN_LIMIT_SECONDS = 30.0`).
- `Player.add_say` takes PCM16 mono at `RATE = 16000`; `robot.play_clip` takes a 16-bit PCM WAV at any rate or channel count (`media.read_wav` raises `ValueError` otherwise). A kept copy is checked with `media.read_wav` before it is kept.
- A bad or missing copy never ends a task or a chat: it falls back to Matcha (in the chat) or the manifest WAV (the wind-down) and logs once; a `play_clip` failure on a kept line must never reach `tick()`'s catch-all (`session.py:278-280`, which shuts the slot down as `bridge_error`).
- No render or fetch inside a robot's wait: `refresh` runs in the background, one fetch at a time, at most 20 s per fetch (the medicine clip's `MED_CLIP_FETCH_SECONDS = 20.0`); `ready` and `say` never wait for it.
- `ClipPlayer._file` raises `KeyError` for ids that are neither in the manifest nor `med_*` (`clips.py:45-46`); the kept lines have their own folder and lookup and never go through `ClipPlayer`.
- Files: `<LINES_DIR>/<language>/<line_id>.wav` plus `<line_id>.json` `{"key": ...}`; `LINES_DIR` defaults to `~/.medcare_reachy/lines` (the app's own folder, like the deploy's `~/.medcare_reachy/clips`, so a reinstall keeps them; not the package's `clips` folder).
- Never a key, a token or the patient's name in a log line: the cache logs the line id, the language and the failure's type or status only.
- Version `0.16.0` in `medcare_reachy/bridge/__init__.py` and `pyproject.toml` (and `tests/test_app.py:393`, which pins both). The heartbeat is unchanged. Robot app 0.15.0 keeps working with the new server (it ignores the new fields); the server's side is a separate plan.
- Plain simple English in docstrings, log lines, the README and commit messages; the zh-TW texts in the robot stay as they are (Simplified, for Matcha).
- The full suite must stay green at every commit (see "How the tests run").

## Review Focus

1. A `clip` id the robot does not know, or one shaped like a path (`../zh-TW/rest`), must never name a file or start a fetch: `kept()`, `fetch()` and `refresh()` take only the ids in `LINE_IDS` (pinned in Task 2 and Task 3).
2. A `clip` field of another shape (a bare string such as `"helpline"`, a number, a list, a dict without an id, a key that is not a string) must leave the line on Matcha, as a 0.15.0 robot would, with nothing fetched and nothing raised: the `ready` and `say` handlers pass the server's field on only when it is a dict, and a bare id reaches `_start_say` only from `stop()` (pinned in Task 4).
3. A kept copy in a format other than the server's design (an older server copy at 32 kHz stereo, say) must still play at the chat's 16 kHz mono, at its right length (pinned in Task 5; around the chat `robot.play_clip` resamples by itself).
4. A server `flush` while a kept line plays must cut only what is queued and let the rest of the line play, as it does for Matcha's chunks (pinned in Task 4).
5. A kept copy that disappears between `kept()` and `play_clip` (a 404 refresh dropping it in the background during ANNOUNCE) raises `FileNotFoundError` inside `play_clip`; the greeting must still be said by Matcha and the slot must go on (pinned in Task 6).

---

## File structure

| File | Responsibility |
|---|---|
| Create `medcare_reachy/bridge/lines.py` | `LINE_IDS` and `LineCache`: the kept copies, their keys, `fetch`, `refresh`, `kept`. Never raises into callers. |
| Create `medcare_reachy/bridge/tests/test_lines.py` | The cache on a temporary folder with a fake line route. |
| Modify `medcare_reachy/bridge/app_client.py:180-194` | `line_clip` beside `med_clip`: the HTTP fetch, 304 → `"unchanged"`, 404 → `"missing"`, else `None`. |
| Modify `medcare_reachy/bridge/live.py` | `clip_pcm`, `CLIP_PIECE_SECONDS`, `Player.queued_own`, `LiveChat(lines=)`, `stop(clip=)`, `_start_say(clip=)`, `_kept_clip`, `_say_clip`, the `ready`/`say` passthrough, docstrings. |
| Modify `medcare_reachy/bridge/session.py` | `SlotSession(lines=)`, `_refresh_lines` at the lease, `_play_line`, the ANNOUNCE gate, `chat.lines`, the wind-down gates, the stop clip ids. |
| Modify `medcare_reachy/bridge/runner.py:506-526, 613-618` | `Runner(lines=)` handed to every `SlotSession`. |
| Modify `medcare_reachy/service.py:21, 49-114` | `LINES_DIR`; builds the `LineCache` beside the `AppClient`, starts the first refresh, hands it to the `Runner`. |
| Modify `medcare_reachy/bridge/tests/fakes.py` | `FakeLines`, `FakeRobot.play_clip`, `Harness(lines=)`. |
| Modify tests `test_medication_prompts.py`, `test_live.py`, `test_session_live.py`, `test_session_checkin.py`, `test_runner.py`, `tests/test_app.py` | The new behaviour, each beside the test that pins today's. |
| Modify `medcare_reachy/bridge/__init__.py:3`, `pyproject.toml:8`, `README.md:136-160, 191-193` | 0.16.0 and its docs. |

---

### Task 1: `AppClient.line_clip` and the branch

**Files:**
- Modify: `medcare_reachy/bridge/app_client.py:180-194` (add `line_clip` right after `med_clip`)
- Test: `medcare_reachy/bridge/tests/test_medication_prompts.py` (append two tests)

**Interfaces:**
- Consumes: `AppClient._http` (an `httpx.AsyncClient` with the device token header), the server route `GET /api/device/clips/line/{line_id}?language=` answering `200 audio/wav` with `ETag: "<key>"`, `304` for a matching `If-None-Match`, `404` when there is no live voice, the id is unknown, or the render failed.
- Produces: `async AppClient.line_clip(line_id: str, language: str, etag: str | None = None) -> tuple[bytes, str] | Literal["unchanged", "missing"] | None`. Never raises, never starts an outage (`unreachable_since` untouched). Task 2's `LineCache.fetch` calls it.

- [ ] **Step 1: Make the branch**

```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge
git status --short
git checkout -b same-voice feature/merged-live-emotion
git log --oneline -1
```

Expected: `git status --short` prints nothing (clean tree); the last line prints `437b136 Add Reachy live emotion and medication interactions`.

- [ ] **Step 2: Write the failing tests**

Append to `medcare_reachy/bridge/tests/test_medication_prompts.py` (it already imports `asyncio`, `httpx`, `AppClient` and defines `WAV`):

```python
def test_the_app_client_fetches_a_line_with_its_key_and_tells_a_missing_one_from_a_failure():
    seen = []

    def handler(request):
        seen.append((request.url.path, dict(request.url.params), request.headers.get("If-None-Match")))
        if request.url.path.endswith("/line/helpline"):
            return httpx.Response(500)
        if request.url.path.endswith("/line/opening"):
            return httpx.Response(404, json={"detail": "No line clip"})
        if request.headers.get("If-None-Match") == '"k1"':
            return httpx.Response(304)
        return httpx.Response(200, content=WAV, headers={"ETag": '"k1"', "Content-Type": "audio/wav"})

    async def run():
        async with AppClient("http://app:8001", "rdv1.token", transport=httpx.MockTransport(handler)) as client:
            fresh = await client.line_clip("rest", "zh-TW")
            same = await client.line_clip("rest", "zh-TW", etag="k1")
            missing = await client.line_clip("opening", "zh-TW", etag="k0")
            failed = await client.line_clip("helpline", "en")
            return fresh, same, missing, failed, client.unreachable_since

    fresh, same, missing, failed, outage = asyncio.run(run())
    assert fresh == (WAV, "k1") and same == "unchanged" and missing == "missing" and failed is None
    assert outage is None                                                  # a 500 here starts no outage
    assert seen[0] == ("/api/device/clips/line/rest", {"language": "zh-TW"}, None)
    assert seen[1][2] == '"k1"' and seen[2][2] == '"k0"'
    assert seen[3] == ("/api/device/clips/line/helpline", {"language": "en"}, None)


def test_a_line_fetch_that_cannot_reach_the_server_is_none_and_starts_no_outage():
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    async def run():
        async with AppClient("http://app:8001", "rdv1.token", transport=httpx.MockTransport(handler)) as client:
            return await client.line_clip("rest", "zh-TW"), client.unreachable_since

    assert asyncio.run(run()) == (None, None)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run:
```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_medication_prompts.py -q -p no:cacheprovider
```
Expected: 2 failed, 5 passed; both failures end with `AttributeError: 'AppClient' object has no attribute 'line_clip'`.

- [ ] **Step 4: Write the implementation**

In `medcare_reachy/bridge/app_client.py`, right after `med_clip` (after line 194, before `heartbeat`), add:

```python
    async def line_clip(self, line_id: str, language: str, etag: str | None = None):
        """One of Reachy's own lines in the server's live voice (services/voice_lines.py, bridge/lines.py): (WAV
        bytes, key), "unchanged" when `etag` still names the server's copy, "missing" when the server has none to
        serve now (404: no live voice, an unknown id, or a per-patient line without check-in consent; the robot drops
        its copy), or None when the server cannot be reached or fails (the robot keeps what it has). Never raises and
        never starts an outage."""
        headers = {"If-None-Match": f'"{etag}"'} if etag else {}
        try:
            response = await self._http.get(f"/api/device/clips/line/{line_id}", headers=headers,
                                            params={"language": language}, timeout=40.0)
        except httpx.HTTPError:
            return None
        if response.status_code == 304:
            return "unchanged"
        if response.status_code == 404:
            return "missing"
        if response.status_code != 200 or not response.content:
            return None
        return response.content, response.headers.get("ETag", "").strip('"')
```

- [ ] **Step 5: Run the tests to verify they pass**

Run:
```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_medication_prompts.py medcare_reachy/bridge/tests/test_app_client.py -q -p no:cacheprovider
```
Expected: all passed (7 in `test_medication_prompts.py` plus the client's own file), 0 failed.

- [ ] **Step 6: Commit**

```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge
git add medcare_reachy/bridge/app_client.py medcare_reachy/bridge/tests/test_medication_prompts.py
git commit -m "Fetch Reachy's lines in the live voice: AppClient.line_clip" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `LineCache`: files, keys, `kept`, `fetch`

**Files:**
- Create: `medcare_reachy/bridge/lines.py`
- Create: `medcare_reachy/bridge/tests/test_lines.py`

**Interfaces:**
- Consumes: `AppClient.line_clip` (Task 1, faked here by `LineServer`), `media.read_wav(path) -> (float32 mono samples, rate)` (`media.py:121-130`, raises `ValueError` for non-16-bit PCM, `wave.Error`/`EOFError` for a body that is not a WAV).
- Produces, in `medcare_reachy/bridge/lines.py`:
  - `FETCH_SECONDS = 20.0`
  - `LINE_IDS = ("checkin_greeting", "opening", "reminder_lead", "repeat", "helpline", "rest", "medicine", "wind_down")`
  - `class LineCache(lines_dir, app, language: str, *, run_blocking=asyncio.to_thread)` with `dir: Path` (= `lines_dir / language`), `path(line_id) -> Path`, `key(line_id) -> str | None`, `kept(line_id, key=None) -> Path | None`, `async fetch(line_id) -> str` (one of `"kept"`, `"unchanged"`, `"dropped"`, `"failed"`; never raises; `"failed"` at once, with no request and no file touched, for an id outside `LINE_IDS`). Task 3 adds `refresh`.

- [ ] **Step 1: Write the failing tests**

Create `medcare_reachy/bridge/tests/test_lines.py`:

```python
"""Reachy's own lines in the live voice (spec 2026-10-17 same voice): the copies the server rendered, kept beside the
app (bridge/lines.py LineCache): fetched by key, checked before they are kept, dropped when the server has none,
refreshed in the background one at a time, and never a reason for a failure."""

import asyncio
import io
import json
import logging
import wave

import numpy as np
import pytest

from medcare_reachy.bridge import lines as lines_module
from medcare_reachy.bridge.lines import LINE_IDS, LineCache
from medcare_reachy.bridge.media import read_wav


def wav_bytes(seconds=0.5, rate=16000, level=16000, channels=1, width=2) -> bytes:
    """A PCM WAV at a steady level: what the server's line route answers (16 kHz mono 16-bit by design)."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        frames = int(round(seconds * rate)) * channels
        out.writeframes(np.full(frames, level, "<i2").tobytes() if width == 2 else bytes([128]) * frames)
    return buffer.getvalue()


async def inline(fn, *args):
    return fn(*args)


def run(scenario):
    return asyncio.run(scenario())


class LineServer:
    """The server's line route as AppClient.line_clip answers it: `clips[line_id]` = (bytes, key), "unchanged" when
    the key matches, "missing" without one; `answers` (popped first) force an answer, or raise an exception; `delay`
    makes every answer take that long. `max_active` says how many fetches ever ran at once."""

    def __init__(self):
        self.clips = {}
        self.calls = []
        self.answers = []
        self.delay = 0.0
        self.active = self.max_active = 0

    async def line_clip(self, line_id, language, etag=None):
        self.calls.append((line_id, language, etag))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            if self.answers:
                answer = self.answers.pop(0)
                if isinstance(answer, BaseException):
                    raise answer
                return answer
            found = self.clips.get(line_id)
            if found is None:
                return "missing"
            return "unchanged" if etag and etag == found[1] else found
        finally:
            self.active -= 1


def test_the_line_ids_are_the_v1_lines_in_the_order_they_are_heard():
    assert LINE_IDS == ("checkin_greeting", "opening", "reminder_lead", "repeat", "helpline", "rest", "medicine",
                        "wind_down")


def test_a_fetch_keeps_the_servers_copy_with_its_key_and_asks_with_that_key_next_time(tmp_path):
    server = LineServer()
    server.clips["rest"] = (wav_bytes(0.5), "k1")

    async def scenario():
        cache = LineCache(tmp_path, server, "zh-TW", run_blocking=inline)
        first = await cache.fetch("rest")
        second = await cache.fetch("rest")
        return cache, first, second

    cache, first, second = run(scenario)
    assert (first, second) == ("kept", "unchanged")
    assert server.calls == [("rest", "zh-TW", None), ("rest", "zh-TW", "k1")]
    path = tmp_path / "zh-TW" / "rest.wav"
    assert cache.dir == tmp_path / "zh-TW" and cache.path("rest") == path
    assert cache.kept("rest") == path and cache.kept("rest", "k1") == path and cache.kept("rest", "k2") is None
    assert path.read_bytes() == wav_bytes(0.5) and cache.key("rest") == "k1"
    assert json.loads(path.with_suffix(".json").read_text(encoding="utf-8")) == {"key": "k1"}
    assert not path.with_suffix(".part").exists()
    samples, rate = read_wav(path)
    assert rate == 16000 and len(samples) == 8000


def test_kept_wants_the_file_the_key_when_given_and_a_known_id(tmp_path):
    server = LineServer()
    cache = LineCache(tmp_path, server, "zh-TW", run_blocking=inline)
    assert cache.kept("rest") is None and cache.key("rest") is None
    folder = tmp_path / "zh-TW"
    folder.mkdir()
    (folder / "rest.wav").write_bytes(wav_bytes())
    assert cache.kept("rest") == folder / "rest.wav" and cache.kept("rest", "k1") is None
    (folder / "rest.json").write_text("not json", encoding="utf-8")
    assert cache.key("rest") is None and cache.kept("rest", "k1") is None
    (folder / "rest.json").write_text('{"key": "k1"}', encoding="utf-8")
    assert cache.kept("rest", "k1") == folder / "rest.wav"
    # Only the ids the robot knows: the id comes from the server's message, and a path-shaped one names a real file
    (folder / "stray.wav").write_bytes(wav_bytes())
    assert cache.kept("stray") is None and cache.kept("../zh-TW/rest") is None and cache.kept("") is None
    # fetch() is public too: such an id is "failed" at once, nothing asked of the server (a 404 would unlink the file
    # the id names) and nothing on disk touched
    assert asyncio.run(cache.fetch("../zh-TW/rest")) == "failed" and asyncio.run(cache.fetch("stray")) == "failed"
    assert server.calls == [] and (folder / "stray.wav").is_file() and (folder / "rest.wav").is_file()


def test_a_404_drops_the_kept_copy_and_its_key(tmp_path):
    server = LineServer()
    server.clips["wind_down"] = (wav_bytes(), "k1")

    async def scenario():
        cache = LineCache(tmp_path, server, "zh-TW", run_blocking=inline)
        kept = await cache.fetch("wind_down")
        del server.clips["wind_down"]                     # the live voice was switched off
        dropped = await cache.fetch("wind_down")
        again = await cache.fetch("wind_down")            # nothing to drop: still no error
        return cache, kept, dropped, again

    cache, kept, dropped, again = run(scenario)
    assert (kept, dropped, again) == ("kept", "dropped", "dropped")
    assert server.calls[1] == ("wind_down", "zh-TW", "k1") and server.calls[2] == ("wind_down", "zh-TW", None)
    assert cache.kept("wind_down") is None and cache.key("wind_down") is None
    assert list((tmp_path / "zh-TW").iterdir()) == []


@pytest.mark.parametrize("body", [b"not a wav at all", wav_bytes(width=1), wav_bytes(0)])
def test_a_body_the_robot_cannot_play_is_refused_and_the_old_copy_stays(tmp_path, body, caplog):
    server = LineServer()
    server.clips["medicine"] = (wav_bytes(0.3), "k1")
    path = tmp_path / "zh-TW" / "medicine.wav"

    async def scenario():
        cache = LineCache(tmp_path, server, "zh-TW", run_blocking=inline)
        outcomes = [await cache.fetch("medicine")]
        server.clips["medicine"] = (body, "k2")
        outcomes += [await cache.fetch("medicine"), await cache.fetch("medicine")]
        old = (path.read_bytes(), cache.key("medicine"), sorted(p.name for p in path.parent.iterdir()))
        server.clips["medicine"] = (wav_bytes(0.4), "k3")
        outcomes.append(await cache.fetch("medicine"))
        server.clips["medicine"] = (body, "k4")
        outcomes.append(await cache.fetch("medicine"))
        return cache, outcomes, old

    with caplog.at_level(logging.WARNING, logger="medcare_reachy.bridge.lines"):
        cache, outcomes, old = run(scenario)
    assert outcomes == ["kept", "failed", "failed", "kept", "failed"]
    assert old == (wav_bytes(0.3), "k1", ["medicine.json", "medicine.wav"])     # no .part left behind either
    assert cache.key("medicine") == "k3" and read_wav(path)[0].shape == (6400,)
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2 and all("medicine" in w and "could not be fetched" in w for w in warnings)
    assert "k1" not in caplog.text and "k2" not in caplog.text and "k4" not in caplog.text   # never the key


def test_no_answer_an_error_or_a_timeout_keeps_what_is_there_and_never_raises(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(lines_module, "FETCH_SECONDS", 0.01)
    server = LineServer()
    server.clips["rest"] = (wav_bytes(), "k1")

    async def scenario():
        cache = LineCache(tmp_path, server, "zh-TW", run_blocking=inline)
        await cache.fetch("rest")
        server.answers = [None, RuntimeError("down")]
        outcomes = [await cache.fetch("rest"), await cache.fetch("rest")]
        server.delay = 0.05                                 # longer than FETCH_SECONDS
        outcomes.append(await cache.fetch("rest"))
        return cache, outcomes

    with caplog.at_level(logging.WARNING, logger="medcare_reachy.bridge.lines"):
        cache, outcomes = run(scenario)
    assert outcomes == ["failed"] * 3
    assert cache.kept("rest", "k1") == tmp_path / "zh-TW" / "rest.wav"
    assert [r.levelname for r in caplog.records] == ["WARNING"]                 # logged once, not three times
    assert "the kept copy stays" in caplog.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_lines.py -q -p no:cacheprovider
```
Expected: an error at collection, `ModuleNotFoundError: No module named 'medcare_reachy.bridge.lines'`.

- [ ] **Step 3: Write the implementation**

Create `medcare_reachy/bridge/lines.py`:

```python
"""Reachy's own lines in the live voice (spec 2026-10-17 same voice): the copies the server rendered with GPT-Live's
voice (GET /api/device/clips/line/{id}, the server's services/voice_lines.py), kept beside the app so a reinstall
keeps them.

Files: <lines_dir>/<language>/<line_id>.wav and <line_id>.json {"key": ...}, the server's key (its ETag), which the
next fetch sends as If-None-Match. A copy is read back (media.read_wav) before it is kept, so a body the player
cannot play is never on disk; one the server no longer serves (404: no live voice now, or a per-patient line without
check-in consent) is dropped, so an old voice is never played. Nothing here raises into the session or the chat, and
nothing here starts an outage: Matcha and the manifest WAV stay the fallback for every line (bridge/live.py
_start_say, bridge/session.py _play_line).
"""

import asyncio
import json
import logging
from pathlib import Path

from medcare_reachy.bridge.media import read_wav

log = logging.getLogger(__name__)

FETCH_SECONDS = 20.0   # one fetch at most, as a medicine clip's (session.MED_CLIP_FETCH_SECONDS)
# The lines the server renders in the live voice (v1), in the order they are heard. checkin_greeting, rest, medicine
# and wind_down are the robot's own: played by id. opening, reminder_lead, repeat and helpline come with the live
# chat's `ready` and `say`, whose `clip` field names the id and the key the copy must have.
LINE_IDS = ("checkin_greeting", "opening", "reminder_lead", "repeat", "helpline", "rest", "medicine", "wind_down")


class LineCache:
    """The kept copies for one language. `app` is the AppClient (line_clip); `run_blocking` runs the disk work off
    the loop (asyncio.to_thread; inline in tests)."""

    def __init__(self, lines_dir, app, language: str, *, run_blocking=asyncio.to_thread):
        self.dir = Path(lines_dir) / language
        self.app, self.language = app, language
        self._run_blocking = run_blocking
        self._warned: set[str] = set()      # lines whose last fetch failed and was logged

    def path(self, line_id: str) -> Path:
        return self.dir / f"{line_id}.wav"

    def key(self, line_id: str) -> str | None:
        """The server's key of the kept copy (its sidecar), or None."""
        try:
            return json.loads(self.path(line_id).with_suffix(".json").read_text(encoding="utf-8")).get("key") or None
        except (OSError, ValueError, AttributeError):
            return None

    def kept(self, line_id: str, key: str | None = None) -> Path | None:
        """The kept copy's path when there is one and, with `key`, its key is that one; else None. Only the ids in
        LINE_IDS: the id comes from the server's message, and must never name another file."""
        if line_id not in LINE_IDS:
            return None
        path = self.path(line_id)
        if not path.is_file() or (key is not None and self.key(line_id) != key):
            return None
        return path

    async def fetch(self, line_id: str) -> str:
        """Bring the kept copy of one line up to date with the server: "kept" (a new copy, checked and written),
        "unchanged" (304: the key still matches), "dropped" (404: the server has none now; the copy and its key are
        deleted) or "failed" (no answer, a timeout, an error, or a body the player cannot read: logged once per line
        until a fetch succeeds, and what is there stays). An id outside LINE_IDS is "failed" at once, with no request
        and nothing on disk touched (a 404 would delete whatever file it names). Never raises."""
        if line_id not in LINE_IDS:
            return "failed"
        key = self.key(line_id)
        try:
            found = await asyncio.wait_for(self.app.line_clip(line_id, self.language, key), FETCH_SECONDS)
        except Exception as error:   # asyncio.TimeoutError included
            return self._failed(line_id, type(error).__name__)
        if found is None:
            return self._failed(line_id, "no answer")
        if found == "unchanged":
            self._warned.discard(line_id)
            return "unchanged"
        if found == "missing":
            await self._run_blocking(self._delete, line_id)
            self._warned.discard(line_id)
            if key is not None:
                log.info("line %s (%s): the server has no copy in the live voice now; the kept one is dropped",
                         line_id, self.language)
            return "dropped"
        data, new_key = found
        try:
            await self._run_blocking(self._keep, line_id, data, new_key)
        except Exception as error:   # a body that is not a WAV the player reads, or the disk
            return self._failed(line_id, type(error).__name__)
        self._warned.discard(line_id)
        log.info("line %s (%s): the live voice's copy kept (%d bytes)", line_id, self.language, len(data))
        return "kept"

    def _failed(self, line_id: str, why: str) -> str:
        if line_id not in self._warned:
            self._warned.add(line_id)
            log.warning("line %s (%s): the live voice's copy could not be fetched (%s); %s", line_id, self.language,
                        why, "the kept copy stays" if self.path(line_id).is_file() else "Reachy says it itself")
        return "failed"

    def _keep(self, line_id: str, data: bytes, key: str) -> None:
        """Write the copy once it is checked: `.part`, read back as the player will read it (read_wav: 16-bit PCM
        with sound in it), then into place, then the key. A copy that fails the check is deleted and the error
        raised; the old copy, if any, stays."""
        path = self.path(line_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(".part")
        part.write_bytes(data)
        try:
            samples, rate = read_wav(part)
            if not len(samples) or rate <= 0:
                raise ValueError(f"{part}: no sound in it")
        except Exception:
            part.unlink(missing_ok=True)
            raise
        part.replace(path)
        path.with_suffix(".json").write_text(json.dumps({"key": key}), encoding="utf-8")

    def _delete(self, line_id: str) -> None:
        for suffix in (".wav", ".json"):
            self.path(line_id).with_suffix(suffix).unlink(missing_ok=True)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_lines.py -q -p no:cacheprovider
```
Expected: `8 passed` (the parametrized test counts three).

- [ ] **Step 5: Commit**

```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge
git add medcare_reachy/bridge/lines.py medcare_reachy/bridge/tests/test_lines.py
git commit -m "Keep the live voice's copies of Reachy's lines: LineCache fetch and kept" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `LineCache.refresh`: in the background, one fetch at a time

**Files:**
- Modify: `medcare_reachy/bridge/lines.py` (the `LineCache` class from Task 2)
- Test: `medcare_reachy/bridge/tests/test_lines.py` (append)

**Interfaces:**
- Consumes: `LineCache.fetch` (Task 2).
- Produces: `LineCache.refresh(ids) -> asyncio.Task | None`: fetches the known ids among `ids` in the background, one at a time (an `asyncio.Lock` shared by every refresh), never twice at once for the same id; returns the task (tests await it) or `None` when nothing is to do. Must be called on the running loop (the bridge's). Task 4's `_kept_clip`, Task 6's `_refresh_lines` and Task 8's `default_bridge` call it.

- [ ] **Step 1: Write the failing tests**

Append to `medcare_reachy/bridge/tests/test_lines.py`:

```python
def test_refresh_fetches_in_the_background_one_at_a_time_and_queues_an_id_once(tmp_path):
    server = LineServer()
    for line_id in LINE_IDS:
        server.clips[line_id] = (wav_bytes(), f"key-{line_id}")

    async def scenario():
        cache = LineCache(tmp_path, server, "zh-TW", run_blocking=inline)
        first = cache.refresh(LINE_IDS)
        again = cache.refresh(["rest", "bogus", "../zh-TW/rest"])   # queued already, and not lines
        assert first is not None and again is None and server.calls == []   # nothing fetched yet: the background
        await first
        later = cache.refresh(["rest", "rest"])                       # a new refresh, the id once
        assert later is not None
        await later
        return cache

    cache = run(scenario)
    assert [call[0] for call in server.calls] == [*LINE_IDS, "rest"]
    assert server.max_active == 1
    assert all(cache.kept(line_id, f"key-{line_id}") is not None for line_id in LINE_IDS)


def test_two_refreshes_still_fetch_one_at_a_time(tmp_path):
    server = LineServer()
    server.delay = 0.001
    for line_id in ("rest", "medicine", "wind_down"):
        server.clips[line_id] = (wav_bytes(), "k")

    async def scenario():
        cache = LineCache(tmp_path, server, "zh-TW", run_blocking=inline)
        await asyncio.gather(cache.refresh(["rest", "medicine"]), cache.refresh(["wind_down"]))
        return cache

    cache = run(scenario)
    assert server.max_active == 1 and sorted(call[0] for call in server.calls) == ["medicine", "rest", "wind_down"]
    assert cache.kept("wind_down", "k") is not None


def test_a_fetch_that_fails_never_ends_the_refresh_and_the_others_are_kept(tmp_path):
    server = LineServer()
    for line_id in ("rest", "medicine"):
        server.clips[line_id] = (wav_bytes(), f"key-{line_id}")
    server.answers = [RuntimeError("down")]                 # the first fetch fails

    async def scenario():
        cache = LineCache(tmp_path, server, "zh-TW", run_blocking=inline)
        await cache.refresh(["rest", "medicine"])
        return cache

    cache = run(scenario)
    assert cache.kept("rest") is None and cache.kept("medicine", "key-medicine") is not None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_lines.py -q -p no:cacheprovider
```
Expected: 3 failed, 8 passed; the failures end with `AttributeError: 'LineCache' object has no attribute 'refresh'`.

- [ ] **Step 3: Write the implementation**

In `medcare_reachy/bridge/lines.py`, add two fields to `LineCache.__init__` after `self._warned`:

```python
        self._queued: set[str] = set()      # lines a refresh still has to fetch
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()         # one fetch at a time, whichever refresh asked
```

and add these two methods after `fetch` (before `_failed`):

```python
    def refresh(self, ids) -> asyncio.Task | None:
        """Bring the copies of `ids` up to date in the background, one fetch at a time (never a wait before a
        line): the task (to await in tests), or None when nothing is to do (ids that are not lines, or that a
        refresh still has to fetch). Call it on the running loop (the bridge's)."""
        wanted = []
        for line_id in ids:
            if line_id in LINE_IDS and line_id not in self._queued:
                self._queued.add(line_id)
                wanted.append(line_id)
        if not wanted:
            return None
        task = asyncio.create_task(self._refresh(wanted), name="line-refresh")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _refresh(self, ids: list[str]) -> None:
        for line_id in ids:
            try:
                async with self._lock:
                    await self.fetch(line_id)
            finally:
                self._queued.discard(line_id)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_lines.py -q -p no:cacheprovider
```
Expected: `11 passed`.

- [ ] **Step 5: Commit**

```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge
git add medcare_reachy/bridge/lines.py medcare_reachy/bridge/tests/test_lines.py
git commit -m "Refresh the kept lines in the background, one fetch at a time" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: The live chat says a line from its kept copy (`ready` and `say` with `clip`)

**Files:**
- Modify: `medcare_reachy/bridge/live.py:13-15` (docstring), `:50` (import), `:56-86` (a constant), `:115-128` (a helper), `:131-213` (`Player.queued_own`), `:308-343` (`LiveChat.__init__`), `:475-488` (`ready`/`say`), `:656-682` (`_start_say` and the new methods)
- Modify: `medcare_reachy/bridge/tests/fakes.py` (add `FakeLines` after `FakeClips`, `fakes.py:146-176`)
- Test: `medcare_reachy/bridge/tests/test_live.py:55-90` (the `Chat` helper) and new tests appended after `test_stop_with_a_last_line_lets_reachy_finish_its_sentence_then_says_the_line` (`:762-781`)

**Interfaces:**
- Consumes: `LineCache.kept(line_id, key=None) -> Path | None` and `LineCache.refresh(ids)` (Tasks 2-3; `FakeLines` here); `media.read_wav`, `media.resample`, `to_pcm`, `Player.add_say(samples, still)`, `chunks(text)`.
- Produces:
  - `live.CLIP_PIECE_SECONDS = 1.0`; `live.clip_pcm(path) -> np.ndarray` (PCM16 mono at `RATE`); `Player.queued_own() -> int`.
  - `LiveChat(..., lines=None)` with the attribute `lines` (a `LineCache` or `None`; `session.py` sets it after construction, Task 6).
  - `LiveChat._start_say(text, reason, clip=None)`: `clip` is the server's `{"id": ..., "key": ...}` (the kept copy plays only under that key), a bare id string (any key; it comes only from `stop()`, Task 5), or anything else (Matcha as before). The `ready` and `say` handlers pass the server's field on only when it is a dict, so a bare string from the server never names a copy.
  - `fakes.FakeLines` with `files: dict[str, tuple[Path, str]]`, `refreshed: list[list[str]]`, `kept(line_id, key=None)`, `refresh(ids) -> None`.
  - `test_live.Chat(..., lines=None)` and the helper `kept_wav(path, seconds, level=16000, rate=RATE, channels=1) -> Path`.

- [ ] **Step 1: Add `FakeLines` to the fakes**

In `medcare_reachy/bridge/tests/fakes.py`, after the `FakeClips` class (after line 176), add:

```python
class FakeLines:
    """LineCache stand-in (bridge/lines.py): `files` maps a line id to (the WAV path kept() answers, its key);
    refresh() only records what it was asked for, one list per call."""

    def __init__(self):
        self.files = {}
        self.refreshed = []

    def kept(self, line_id, key=None):
        found = self.files.get(line_id)
        if found is None or (key is not None and found[1] != key):
            return None
        return found[0]

    def refresh(self, ids):
        self.refreshed.append(list(ids))
        return None
```

- [ ] **Step 2: Write the failing tests**

In `medcare_reachy/bridge/tests/test_live.py`:

Add to the imports (`:6-22`): `import logging` and `import wave` among the standard imports, and `FakeLines` to the fakes import:

```python
from medcare_reachy.bridge.tests.fakes import FakeApp, FakeClock, FakeLines, FakeRobot, FakeSocket, FakeSpeaker, make_task
```

Add a constant after `LAST_LINE` (`:26`):

```python
REST = "我先休息一下，有需要再叫我哦。"                 # the rest line for stop(say=...)
REPEAT = "不好意思，我刚刚没听清楚，可以再说一次吗？"   # a 'say' line with reason "repeat"
```

Change the `Chat` helper's constructor (`:58-69`) so it takes and passes `lines`:

```python
    def __init__(self, connect_error=None, live_audio=True, speaker=True, speech_start=None, lines=None):
        self.clock = FakeClock()
        self.app = FakeApp(make_task([], reason="checkin", checkin=True), self.clock)
        self.robot = FakeRobot()
        self.robot.live_audio = live_audio
        self.speaker = FakeSpeaker(available=speaker)
        self.socket = FakeSocket(stamp=lambda: self.robot.blocks)
        self.connected = []
        self.connect_error = connect_error
        self.detector = speech_start
        self.live = LiveChat(self.app, self.robot, self.speaker, connect=self._connect, clock=self.clock.monotonic,
                             sleep=no_wait, run_blocking=inline, speech_start=speech_start, lines=lines)
```

Add a helper after `said(text)` (`:583-585`):

```python
def kept_wav(path, seconds, level=16000, rate=RATE, channels=1):
    """A kept line in the live voice (bridge/lines.py): PCM16 at `rate`, `seconds` long, every sample at `level`
    (16000 at 16 kHz comes back as 16000 exactly through read_wav, to_pcm and the speaker)."""
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(np.full(int(round(seconds * rate)) * channels, level, "<i2").tobytes())
    return path
```

Append these tests after `test_stop_with_a_last_line_lets_reachy_finish_its_sentence_then_says_the_line` (before the `# ── the real WebSocket client` section):

```python
# ── Reachy's lines from their kept copies in the live voice (bridge/lines.py) ───
def test_a_say_with_a_kept_clip_plays_the_live_voices_copy_in_pieces_under_the_say_rules(tmp_path):
    lines = FakeLines()
    lines.files["helpline"] = (kept_wav(tmp_path / "helpline.wav", 2.5), "k1")
    pieces = []

    async def scenario():
        chat = Chat(lines=lines)
        add_say = chat.live.player.add_say
        chat.live.player.add_say = lambda samples, still: pieces.append((len(samples), still)) or add_say(samples, still)
        task = chat.start()
        await chat.ready()
        chat.socket.serve(pcm(0.4))                                         # GPT-Live's sentence first
        chat.socket.serve({"type": "say", "speech_text": HELP, "reason": "risk",
                           "clip": {"id": "helpline", "key": "k1"}})
        chat.socket.serve(pcm(0.4, level=3000))                             # GPT-Live after the line: never played
        await until(lambda: chat.live.dropped_audio == 1)                   # the line is being said
        chat.robot.mic.extend([mic_chunk()] * 5)                            # the microphone meanwhile
        await until(lambda: len(chat.socket.frames_sent()) == 2)
        chat.socket.serve({"type": "end", "reason": "risk", "risk": True})
        return chat, await task

    chat, outcome = run(scenario)
    assert outcome == LiveOutcome("ended", "risk", "c9", True)
    assert chat.speaker.synthesised == [] and lines.refreshed == []        # not Matcha, nothing to fetch
    assert pieces == [(16000, True), (16000, True), (8000, True)]          # about a second each, said still
    heard = chat.robot.heard()
    assert list(heard[heard != 0]) == [8000] * 6400 + [16000] * 40000      # after GPT-Live, whole, before run() returned
    assert all(not np.any(np.frombuffer(frame, "<i2")) for frame in chat.socket.frames_sent())   # the microphone muted
    gestures = [call for call in chat.robot.calls if call.startswith("gesture")]
    assert gestures[:2] == ["gesture:speak", "gesture:None"] and "gesture:speak" not in gestures[1:]   # still


def test_the_opening_comes_from_the_kept_copy_when_the_servers_key_matches(tmp_path):
    lines = FakeLines()
    lines.files["opening"] = (kept_wav(tmp_path / "opening.wav", 1.2), "k1")

    async def scenario():
        chat = Chat(speech_start=FakeSpeechStart(), lines=lines)
        task = chat.start()
        await chat.ready(speech_text=OPENING, clip={"id": "opening", "key": "k1"})
        for _ in range(3):
            await hear(chat, 0.75)                                          # the patient talks over the greeting
        await until(lambda: len(chat.robot.heard()) >= 19200)
        chat.socket.serve({"type": "end", "reason": "goodbye", "risk": False})
        return chat, await task

    chat, outcome = run(scenario)
    assert outcome == LiveOutcome("ended", "goodbye", "c9", False)
    assert chat.speaker.synthesised == [] and lines.refreshed == []
    heard = chat.robot.heard()
    assert list(heard[heard != 0]) == [16000] * 19200                        # the copy, and nothing else
    assert all(not np.any(np.frombuffer(frame, "<i2")) for frame in chat.socket.frames_sent())
    assert chat.detector.fed == [0.0] * 3
    assert {"type": "speech"} not in chat.sent() and {"type": "playing"} in chat.sent()
    assert [call for call in chat.robot.calls if call.startswith("gesture")][:1] == ["gesture:speak"]


@pytest.mark.parametrize("kept_key", [None, "old"])
def test_a_missing_or_outdated_copy_means_matcha_now_and_a_fetch_for_next_time(tmp_path, kept_key):
    lines = FakeLines()
    if kept_key:
        lines.files["opening"] = (kept_wav(tmp_path / "opening.wav", 1.0), kept_key)

    async def scenario():
        chat = Chat(lines=lines)
        task = chat.start()
        await chat.ready(speech_text=OPENING, clip={"id": "opening", "key": "new"})
        await until(lambda: len(chat.robot.heard()) >= said(OPENING))
        chat.socket.serve({"type": "end", "reason": "goodbye", "risk": False})
        return chat, await task

    chat, outcome = run(scenario)
    assert chat.speaker.synthesised == chunks(OPENING) and lines.refreshed == [["opening"]]
    heard = chat.robot.heard()
    assert list(heard[heard != 0]) == [8192] * said(OPENING)


def test_a_kept_copy_plays_even_when_matcha_is_off(tmp_path):
    lines = FakeLines()
    lines.files["repeat"] = (kept_wav(tmp_path / "repeat.wav", 0.5), "k1")

    async def scenario():
        chat = Chat(speaker=False, lines=lines)
        task = chat.start()
        await chat.ready()
        chat.socket.serve({"type": "say", "speech_text": REPEAT, "reason": "repeat",
                           "clip": {"id": "repeat", "key": "k1"}})
        await until(lambda: len(chat.robot.heard()) >= 8000)
        chat.socket.serve({"type": "end", "reason": "goodbye", "risk": False})
        return chat, await task

    chat, outcome = run(scenario)
    heard = chat.robot.heard()
    assert list(heard[heard != 0]) == [16000] * 8000 and chat.speaker.synthesised == []


@pytest.mark.parametrize("matcha", [True, False])
def test_a_kept_copy_that_cannot_be_read_is_logged_once_then_matcha_says_the_line_or_it_is_lost(tmp_path, caplog,
                                                                                                 matcha):
    lines = FakeLines()
    (tmp_path / "repeat.wav").write_bytes(b"not a wav")
    lines.files["repeat"] = (tmp_path / "repeat.wav", "k1")

    def warned():
        return any("could not be read" in r.getMessage() for r in caplog.records)

    async def scenario():
        chat = Chat(speaker=matcha, lines=lines)
        task = chat.start()
        await chat.ready()
        chat.socket.serve({"type": "say", "speech_text": REPEAT, "reason": "repeat",
                           "clip": {"id": "repeat", "key": "k1"}})
        await until(warned)
        if matcha:
            await until(lambda: len(chat.robot.heard()) >= said(REPEAT))
        chat.socket.serve({"type": "end", "reason": "goodbye", "risk": False})
        return chat, await task

    with caplog.at_level(logging.WARNING, logger="medcare_reachy.bridge.live"):
        chat, outcome = run(scenario)
    assert outcome == LiveOutcome("ended", "goodbye", "c9", False)
    assert chat.speaker.synthesised == (chunks(REPEAT) if matcha else []) and lines.refreshed == []
    assert [r.levelname for r in caplog.records if "could not be read" in r.getMessage()] == ["WARNING"]
    assert not [r for r in caplog.records if r.levelname == "ERROR"]        # one warning, never a traceback
    assert chat.live._saying == 0                                            # the say window closed once


# "helpline" as a bare string: the server's field must be a dict; a bare id is stop()'s own form (Task 5), and the
# handlers never pass one on, so the kept copy must not play under just any key
@pytest.mark.parametrize("clip", ["", "helpline", 7, ["helpline"], {"key": "k1"}, {"id": "", "key": "k1"},
                                  {"id": "helpline", "key": 3}, {"id": "helpline"}])
def test_a_clip_field_of_another_shape_leaves_the_line_on_matcha(tmp_path, clip):
    lines = FakeLines()
    lines.files["helpline"] = (kept_wav(tmp_path / "helpline.wav", 0.5), "k1")

    async def scenario():
        chat = Chat(lines=lines)
        task = chat.start()
        await chat.ready()
        chat.socket.serve({"type": "say", "speech_text": HELP, "reason": "risk", "clip": clip})
        await until(lambda: len(chat.robot.heard()) >= said(HELP))
        chat.socket.serve({"type": "end", "reason": "risk", "risk": True})
        return chat, await task

    chat, outcome = run(scenario)
    assert outcome == LiveOutcome("ended", "risk", "c9", True)
    assert chat.speaker.synthesised == chunks(HELP) and lines.refreshed == []
    heard = chat.robot.heard()
    assert list(heard[heard != 0]) == [8192] * said(HELP)


def test_a_server_flush_during_a_kept_line_cuts_what_is_queued_and_the_rest_still_plays(tmp_path):
    lines = FakeLines()
    lines.files["opening"] = (kept_wav(tmp_path / "opening.wav", 3.0), "k1")

    async def scenario():
        chat = Chat(lines=lines)
        task = chat.start()
        await chat.ready(speech_text=OPENING, clip={"id": "opening", "key": "k1"})
        await until(lambda: len(chat.robot.pushed) >= 5)
        chat.socket.serve({"type": "flush"})
        await until(lambda: "clear_live_audio" in chat.robot.calls)
        await until(lambda: chat.live._saying == 0 and not chat.live.player.has(live.SAY))   # the rest played
        chat.socket.serve({"type": "end", "reason": "goodbye", "risk": False})
        return chat, await task

    chat, outcome = run(scenario)
    heard = chat.robot.heard()
    assert 16000 <= len(heard) < 48000 and np.all(heard == 16000)          # a piece or two cut, the rest heard
    assert chat.robot.pushed_at[-1] > chat.robot.cleared_at               # ... after the flush
    assert chat.sent().index({"type": "playing"}) < chat.sent().index({"type": "idle"})
```

- [ ] **Step 3: Run the tests to verify they fail**

Run:
```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_live.py -q -p no:cacheprovider
```
Expected: every test that builds a `Chat` fails with `TypeError: LiveChat.__init__() got an unexpected keyword argument 'lines'` (the helper now passes it); the `Player` tests at the top of the file, the `SpeechStart` test and the real-WebSocket test still pass.

- [ ] **Step 4: Write the implementation**

In `medcare_reachy/bridge/live.py`:

(a) Module docstring, lines 13-15: replace

```
- server -> robot: ready {conversation_id, speech_text} (speech_text only from the companion relay: Reachy's opening,
  which it says itself); GPT-Live's voice; flush (drop what is queued, now); say {speech_text, reason} (Reachy says
  it with its own Matcha voice, through the same queue); paused (GPT-Live was closed until the patient speaks again);
```

with

```
- server -> robot: ready {conversation_id, speech_text, clip} (speech_text only from the companion relay: Reachy's
  opening, which it says itself); GPT-Live's voice; flush (drop what is queued, now); say {speech_text, reason, clip}
  (Reachy says it itself through the same queue: from its kept copy in the live voice when `clip` {id, key} names
  one the robot has under that key, bridge/lines.py, else with its own Matcha voice); paused (GPT-Live was closed
  until the patient speaks again);
```

(b) The import at line 50:

```python
from medcare_reachy.bridge.media import read_wav, resample
```

(c) A constant after `SPEECH_ECHO_GUARD_SECONDS = 0.75` (line 86):

```python
# A kept line in the live voice (bridge/lines.py) is queued in pieces this long, the next one once the last is
# playing, as Matcha's chunks come: a server flush cuts the pieces queued, and the rest of the line still plays.
CLIP_PIECE_SECONDS = 1.0
```

(d) A helper after `mic_pcm` (after line 128):

```python
def clip_pcm(path: Path) -> np.ndarray:
    """A kept line (bridge/lines.py: a 16-bit PCM WAV, any rate or channel count) as PCM16 mono at RATE."""
    samples, rate = read_wav(Path(path))
    return to_pcm(resample(np.asarray(samples, dtype=np.float32), int(rate), RATE))
```

(e) In `Player`, after `has` (after line 173):

```python
    def queued_own(self) -> int:
        """Samples of Reachy's own lines (SAY, STILL) still queued."""
        return sum(len(samples) - played for samples, played, kind in self._items if kind != LIVE)
```

(f) `LiveChat.__init__` (line 308-309): add the keyword and the attribute:

```python
    def __init__(self, app, robot, speaker, *, connect=None, clock=time.monotonic, sleep=asyncio.sleep,
                 run_blocking=asyncio.to_thread, gestures=True, speech_start=None, hardware=None, lines=None):
        self.app, self.robot, self.speaker = app, robot, speaker
        self.lines = lines            # bridge/lines.py LineCache: Reachy's lines in the live voice (session.py sets
                                      # it too); None: Matcha only
```

and add to the class docstring (line 293-306), after the `closing` sentence: `` `lines` is the kept copies of Reachy's own lines in the live voice (bridge/lines.py), played instead of Matcha when the server's `clip` names one (_start_say); None: Matcha only.``

(g) The `ready` handler (line 484) and the `say` handler (line 488) pass the server's field on, and only in its designed shape, a dict. A bare string there (`"clip": "helpline"`) is not a stop line and must not name a copy (Review Focus 2): the bare-id form of `_start_say` is for `stop()` alone (Task 5). The `ready` handler's `if` block becomes:

```python
            if isinstance(opening, str) and opening.strip():
                # The companion relay: Reachy greets in its own voice first; GPT-Live starts when the patient answers
                self._companion = True
                clip = message.get("clip")
                self._start_say(opening, "opening", clip if isinstance(clip, dict) else None)
```

and the body of the `elif kind == "say":` branch (its one line at 488) becomes:

```python
            clip = message.get("clip")
            self._start_say(message.get("speech_text"), message.get("reason"),
                            clip if isinstance(clip, dict) else None)
```

(h) Replace `_start_say` (lines 656-665) and add the two methods between it and `_say`:

```python
    def _start_say(self, text, reason, clip=None) -> None:
        """Reachy's own line: from its kept copy in the live voice when `clip` names one the cache has (the server's
        `clip` {id, key}: only under that key; a bare id: any key, and a bare id comes only from stop(), since the
        ready and say handlers pass the server's field on only as a dict), else synthesised by Matcha one chunk at a
        time. The copy plays even when Matcha is off; without a copy and without Matcha the line is lost."""
        path = self._kept_clip(clip)
        pieces = chunks(text if isinstance(text, str) else None)
        if path is None and (not pieces or self.speaker is None or not self.speaker.available):
            log.warning("live chat %s: Reachy cannot say its %s line (no voice or no text)",
                        self.conversation_id, reason)
            return
        self._saying += 1
        still = reason == "risk"
        say = self._say(pieces, still) if path is None else self._say_clip(path, pieces, still)
        task = asyncio.create_task(say, name="live-say")
        self._says.add(task)
        task.add_done_callback(self._says.discard)

    def _kept_clip(self, clip) -> Path | None:
        """The kept copy `clip` names, when the cache has it: the server's {"id", "key"} only under that key; a bare
        id under any key (a bare id comes only from stop(): the server's field must be a dict, and the ready and say
        handlers pass nothing else on). None otherwise, and the cache fetches the copy for next time."""
        if self.lines is None:
            return None
        if isinstance(clip, str):
            line_id, key = clip, None
        elif isinstance(clip, dict):
            line_id, key = clip.get("id"), clip.get("key")
            if not isinstance(key, str) or not key:
                return None
        else:
            return None
        if not isinstance(line_id, str) or not line_id:
            return None
        path = self.lines.kept(line_id, key)
        if path is None:
            self.lines.refresh([line_id])
        return path

    async def _say_clip(self, path: Path, pieces: list[str], still: bool) -> None:
        """The kept copy, read off the loop and queued in CLIP_PIECE_SECONDS pieces, the next one once the last is
        playing (as Matcha's chunks come, so a server flush behaves the same). A copy that cannot be read is logged
        once, and Matcha says the line instead (`pieces`) when it is on; when it is off the line is lost, as it is
        today without a voice (one WARNING, never _say's traceback)."""
        try:
            pcm = await self._run_blocking(clip_pcm, path)
        except Exception as error:
            matcha = self.speaker is not None and self.speaker.available
            log.warning("live chat %s: the kept %s line could not be read (%s); %s", self.conversation_id,
                        Path(path).stem, type(error).__name__,
                        "Reachy says it itself" if matcha else "Matcha is off, so the line is lost")
            if matcha:
                await self._say(pieces, still)   # it ends the _saying window
            else:
                self._saying -= 1
            return
        try:
            step = round(CLIP_PIECE_SECONDS * RATE)
            for start in range(0, len(pcm), step):
                while self.player.queued_own() >= step:
                    await self._sleep(BLOCK_SECONDS)
                self.player.add_say(pcm[start:start + step], still)
        finally:
            self._saying -= 1
```

- [ ] **Step 5: Run the tests to verify they pass**

Run:
```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_live.py medcare_reachy/bridge/tests/test_session_live.py -q -p no:cacheprovider
```
Expected: all passed (the 7 new tests count 16 cases: the unreadable copy with Matcha on and off, the eight shapes, and the two kept keys; every older test in both files still passes, since `clip` is absent from their messages).

- [ ] **Step 6: Commit**

```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge
git add medcare_reachy/bridge/live.py medcare_reachy/bridge/tests/fakes.py medcare_reachy/bridge/tests/test_live.py
git commit -m "Live chat: say a line from its kept copy when the server's clip key matches" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: `LiveChat.stop(clip=)`: the rest and medicine lines from their kept copies

**Files:**
- Modify: `medcare_reachy/bridge/live.py:330-331` (two attributes), `:345-358` (`stop`), `:426-434` (`_converse`'s robot ending)
- Test: `medcare_reachy/bridge/tests/test_live.py` (append)

**Interfaces:**
- Consumes: `_start_say(text, reason, clip)` with a bare id string (Task 4), `kept_wav`, `FakeLines`.
- Produces: `LiveChat.stop(reason: str, say: str | None = None, clip: str | None = None)`; the session passes `clip="rest"` or `clip="medicine"` (Task 7).

- [ ] **Step 1: Write the failing tests**

Append to `medcare_reachy/bridge/tests/test_live.py`:

```python
def test_stop_with_a_last_line_says_it_from_the_kept_copy_at_the_chats_rate(tmp_path):
    lines = FakeLines()
    lines.files["rest"] = (kept_wav(tmp_path / "rest.wav", 0.5, rate=32000, channels=2), "k1")   # an older copy

    async def scenario():
        chat = Chat(lines=lines)
        task = chat.start()
        await chat.ready()
        chat.socket.serve(pcm(0.4))                                         # GPT-Live's sentence
        await until(lambda: len(chat.robot.pushed) >= 5)
        chat.live.stop("patient_left", say=REST, clip="rest")
        chat.socket.serve(pcm(0.4, level=3000))                             # after the stop: never played
        return chat, await task

    chat, outcome = run(scenario)
    assert outcome == LiveOutcome("ended", "patient_left", "c9")
    assert chat.speaker.synthesised == [] and lines.refreshed == []
    heard = chat.robot.heard()
    assert list(heard[heard != 0]) == [8000] * 6400 + [16000] * 8000      # the sentence, then 0.5 s at 16 kHz mono
    assert [m for m in chat.sent() if m["type"] == "end"] == [{"type": "end", "reason": "patient_left"}]
    assert chat.socket.stamp_of({"type": "end", "reason": "patient_left"}) < chat.robot.pushed_at[20]   # sent at once
    assert "clear_live_audio" not in chat.robot.calls
    assert chat.robot.calls[-2:] == ["gesture:None", "stop_live_audio"]


def test_stop_with_a_last_line_and_no_kept_copy_says_it_with_matcha_and_fetches_the_copy():
    lines = FakeLines()

    async def scenario():
        chat = Chat(lines=lines)
        task = chat.start()
        await chat.ready()
        chat.live.stop("medicine", say=LAST_LINE, clip="medicine")
        return chat, await task

    chat, outcome = run(scenario)
    assert outcome == LiveOutcome("ended", "medicine", "c9")
    assert chat.speaker.synthesised == chunks(LAST_LINE) and lines.refreshed == [["medicine"]]
    heard = chat.robot.heard()
    assert list(heard[heard != 0]) == [8192] * said(LAST_LINE)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_live.py -q -p no:cacheprovider -k "stop_with_a_last_line"
```
Expected: 2 failed (`TypeError: LiveChat.stop() got an unexpected keyword argument 'clip'`), 1 passed (the older stop test).

- [ ] **Step 3: Write the implementation**

In `medcare_reachy/bridge/live.py`:

(a) After `self._last_line: str | None = None   # stop(say=...): Reachy's last line` (line 330) add:

```python
        self._last_clip: str | None = None   # stop(clip=...): the kept copy of that line, if the cache has one
```

(b) `stop` (lines 345-358): the new signature, docstring sentence and body:

```python
    def stop(self, reason: str, say: str | None = None, clip: str | None = None) -> None:
        """End the chat now: run() tells the server `end {reason}` and returns an "ended" outcome. Call it on the
        chat's event loop (the bridge's stop paths run there); only the first call counts.

        Without `say`, what is queued is dropped at once. With `say` (Reachy's last line, in the voice's Simplified
        characters), what is queued still plays (Reachy finishes its sentence), then Reachy says the line through the
        same queue; `end` goes to the server at once, so GPT-Live says nothing more, and run() returns once the line
        has played (DRAIN_LIMIT_SECONDS at most). `clip` names the line's kept copy in the live voice ("rest",
        "medicine"; bridge/lines.py): it plays instead of Matcha when the cache has one. Once `closing` is set (the
        server ended the chat or fell back: its final drain plays its help line) the session asks for no companion
        ending (medicine, rest, a lost voice detector or camera); a stop that still comes then (stop_all, the app
        shutting down) cuts that drain, with or without a line, and its line is not said."""
        if self._stop_reason is None:
            self._stop_reason, self._last_line, self._last_clip = reason, say, clip
            self._inbox.put_nowait(_STOP)
```

(c) In `_converse` (line 429):

```python
                    self._start_say(self._last_line, self._stop_reason, self._last_clip)   # after what is queued
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_live.py -q -p no:cacheprovider
```
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge
git add medcare_reachy/bridge/live.py medcare_reachy/bridge/tests/test_live.py
git commit -m "Live chat: the rest and medicine lines from their kept copies" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: The session: the refresh at the lease, the kept greeting before a live chat, the cache handed to the chat

**Files:**
- Modify: `medcare_reachy/bridge/session.py:44-47` (import), `:73` (comment), `:154-166` (`__init__`), `:284-300` (`_wake`, `_announce`), `:633-638` (`_live_checkin`), `:1014-1025` (two helpers beside `_play`/`_speak`)
- Modify: `medcare_reachy/bridge/tests/fakes.py:1-13` (import), `:29-53` and `:111-114` (`FakeRobot.play_clip`), `:525-536` (`Harness`)
- Test: `medcare_reachy/bridge/tests/test_session_live.py` (append), `medcare_reachy/bridge/tests/test_session_checkin.py` (append)

**Interfaces:**
- Consumes: `LineCache.kept(line_id)`, `LineCache.refresh(ids)`, `LINE_IDS` (Tasks 2-3; `FakeLines` here), `LiveChat.lines` (Task 4), `robot.play_clip(path, wait=True) -> bool` (`media.py:280-306`).
- Produces:
  - `SlotSession(..., lines=None)` with the attribute `lines`.
  - `SlotSession._refresh_lines() -> None` (called in `_wake`), `async SlotSession._play_line(line_id: str, text: str | None = None) -> bool` (False without a kept copy or when `play_clip` failed; Task 7 uses it for the wind-down).
  - `FakeRobot.play_clip(path, wait=True) -> bool`, recording `"play_clip:<stem>"` in `calls` and the path in `clips_played`; `FakeRobot.play_clip_error` raised when set.
  - `Harness(task, robot=None, voice=False, speaker=False, lines=None, **session)` with `h.lines` (a `FakeLines` unless one is given).

- [ ] **Step 1: Extend the fakes**

In `medcare_reachy/bridge/tests/fakes.py`:

(a) Add `from pathlib import Path` to the imports (after `from datetime import ...`, line 8).

(b) In `FakeRobot.__init__`, after `self.affects = []` (line 52), add:

```python
        # Reachy's own lines from their kept copies in the live voice (bridge/session.py _play_line):
        self.clips_played = []      # the paths play_clip() got
        self.play_clip_error = None  # raised by play_clip() when set
```

and after `gesture` (after line 114) add:

```python
    def play_clip(self, path, wait=True):
        if self.play_clip_error is not None:
            raise self.play_clip_error
        self.clips_played.append(Path(path))
        self.calls.append(f"play_clip:{Path(path).stem}")
        return True
```

(c) `Harness.__init__` (lines 526-536):

```python
    def __init__(self, task, robot=None, voice=False, speaker=False, lines=None, **session):
        self.clock = FakeClock()
        self.app = FakeApp(task, self.clock)
        self.robot = robot or FakeRobot()
        self.clips = FakeClips()
        self.stream = FakeStream()
        self.voice = FakeVoice(self.clock) if voice else None
        self.speaker = FakeSpeaker(robot=self.robot) if speaker else None
        self.lines = lines or FakeLines()
        self.slot = SlotSession(copy.deepcopy(task), app=self.app, robot=self.robot, clips=self.clips,
                                stream=self.stream, voice=self.voice, speaker=self.speaker, clock=self.clock,
                                lines=self.lines, **session)
```

- [ ] **Step 2: Write the failing tests**

In `medcare_reachy/bridge/tests/test_session_live.py`, add to the imports: `from medcare_reachy.bridge.lines import LINE_IDS` (after the `live` import, line 14). Append:

```python
# ── Reachy's lines from their kept copies in the live voice (bridge/lines.py) ───
def test_the_greeting_comes_from_the_kept_copy_when_the_task_says_live(tmp_path):
    chat = FakeLiveChat()
    h = Harness(live_task(reason="checkin"), voice=True, speaker=True, live_chat=chat.factory)
    h.lines.files["checkin_greeting"] = (tmp_path / "checkin_greeting.wav", "k1")
    h.find_patient()
    assert h.lines.refreshed == [list(LINE_IDS)]                            # at the lease, in the background
    assert h.robot.calls.index("wake") < h.robot.calls.index("play_clip:checkin_greeting")
    assert h.robot.calls.count("play_clip:checkin_greeting") == 1 and h.speaker.said == []
    assert h.robot.clips_played == [tmp_path / "checkin_greeting.wav"]
    assert h.voice.spoken == [CHECKIN_GREETING["zh-TW"]]                      # the listener is told the words ...
    assert h.voice.holds == 0 and h.voice.max_holds == 1                     # ... and was held while they played
    h.tick()
    assert chat.lines is h.lines                                             # the chat plays the kept lines too
    assert h.slot.outcomes == {"live": "ended", "checkin": "goodbye"}


def test_a_check_in_without_live_keeps_its_matcha_greeting_even_with_a_kept_copy(tmp_path):
    h = Harness(live_task(reason="checkin", live=False), voice=True, speaker=True)
    h.lines.files["checkin_greeting"] = (tmp_path / "checkin_greeting.wav", "k1")
    h.find_patient()
    assert h.speaker.said == [CHECKIN_GREETING["zh-TW"]] and "play_clip:checkin_greeting" not in h.robot.calls
    assert h.lines.refreshed == [list(LINE_IDS)]                            # still refreshed: a 404 drops old copies


@pytest.mark.parametrize("error", [ValueError("only 16-bit PCM clips are supported"), FileNotFoundError("gone")])
def test_a_kept_greeting_that_cannot_be_played_is_said_by_matcha_and_logged_once(tmp_path, caplog, error):
    chat = FakeLiveChat()
    h = Harness(live_task(reason="checkin"), voice=True, speaker=True, live_chat=chat.factory)
    h.lines.files["checkin_greeting"] = (tmp_path / "checkin_greeting.wav", "k1")
    h.robot.play_clip_error = error                        # a bad copy, or one a 404 refresh dropped meanwhile
    with caplog.at_level(logging.WARNING, logger="medcare_reachy.bridge.session"):
        h.find_patient()
    assert h.speaker.said == [CHECKIN_GREETING["zh-TW"]] and h.slot.state == "CHECKIN" and not h.slot.done
    assert h.voice.holds == 0
    assert [r.levelname for r in caplog.records if "could not be played" in r.getMessage()] == ["WARNING"]
    assert not [r for r in caplog.records if r.levelname == "ERROR"]         # never tick()'s bridge_error


def test_without_a_line_cache_the_session_speaks_as_before():
    h = Harness(live_task(reason="checkin"), voice=True, speaker=True, live_chat=FakeLiveChat().factory)
    h.slot.lines = None
    h.find_patient()
    assert h.speaker.said == [CHECKIN_GREETING["zh-TW"]]
    assert not any(call.startswith("play_clip:") for call in h.robot.calls)
```

In `medcare_reachy/bridge/tests/test_session_checkin.py`, add `from medcare_reachy.bridge.lines import LINE_IDS` to the imports (after the `session_module` import, line 10) and append:

```python
def test_todays_chat_keeps_matcha_for_its_lines_even_with_kept_copies(tmp_path):
    """v1: only the lines around a live chat play from the live voice; a text-chat check-in stays Matcha throughout."""
    h = Harness(make_task([], reason="checkin", checkin=True), voice=True, speaker=True)
    for line_id in ("checkin_greeting", "rest", "medicine", "wind_down"):
        h.lines.files[line_id] = (tmp_path / f"{line_id}.wav", "k1")
    h.find_patient()
    h.tick()                                             # opens today's chat and says its opening
    h.see(identity_status="searching")
    h.tick(REST_AFTER_SECONDS + 1)                       # nobody heard or seen: the rest line
    h.run_until(lambda: h.slot.done)
    assert h.speaker.said == [CHECKIN_GREETING["zh-TW"], "今天感觉怎么样？", REST_LINE["zh-TW"]]
    assert not any(call.startswith("play_clip:") for call in h.robot.calls)
    assert h.lines.refreshed == [list(LINE_IDS)]
```

- [ ] **Step 3: Run the tests to verify they fail**

Run:
```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_session_live.py medcare_reachy/bridge/tests/test_session_checkin.py -q -p no:cacheprovider
```
Expected: every test that builds a `Harness` fails with `TypeError: SlotSession.__init__() got an unexpected keyword argument 'lines'` (the `Harness` now passes it); the few that do not (`open_live_chat` tests, `AppClient` tests) still pass.

- [ ] **Step 4: Write the implementation**

In `medcare_reachy/bridge/session.py`:

(a) Add the import after the `app_client` import block (after line 46):

```python
from medcare_reachy.bridge.lines import LINE_IDS
```

(b) Replace the comment at line 73 with:

```python
# Spoken (not prerecorded) lines, already in the voice's Simplified characters (喔 written 哦, as in the clips). Around
# a live chat their kept copies in the live voice (bridge/lines.py, `lines`) play instead; these are the fallback.
```

(c) `__init__` (line 154-156): add `lines=None` and keep the attribute:

```python
    def __init__(self, task: dict, *, app, robot, clips, stream, voice=None, speaker=None, language="zh-TW",
                 clock=None, run_blocking=_inline, ack=True, gestures=True, mood=None, follow=None,
                 live_chat=open_live_chat, lines=None):
        self.task = task
        self.lines = lines   # bridge/lines.py LineCache: Reachy's lines in the live voice; None: Matcha and the clips only
```

(d) `_wake` (lines 284-293): start the refresh after the robot is awake, before ANNOUNCE:

```python
    async def _wake(self):
        if not await self._robot("is_reachable"):
            await self._shutdown("robot_offline", status="aborted")
            return
        # A camera recovery the daemon refused may have left the speaker and the microphone off: the greeting needs
        # them (ReachyRobot.restore_media, which never raises).
        await self._robot("restore_media")
        await self._robot("wake")
        self._refresh_lines()
        await self._advance("searching")
        self._go(ANNOUNCE)
```

(e) `_announce` (lines 295-300):

```python
    async def _announce(self):
        if self._is_checkin_task():
            text = self._line(CHECKIN_GREETING)
            # The live voice's greeting only before a live chat (the task says live): a text-chat check-in stays
            # Matcha throughout (v1), and a task leased after the live voice was switched off never plays an old copy
            if not (self.task.get("live") and await self._play_line("checkin_greeting", text)):
                await self._say(text)
        else:
            await self._play("reminder" if self.task.get("reason") == "missed_retry" else "wake_greeting")
        self._go(SEARCHING)
```

(f) In `_live_checkin`, after `chat.mood = self.mood ...` (line 638), add:

```python
        chat.lines = self.lines         # the kept copies of Reachy's lines in the live voice (LiveChat._start_say)
```

(g) After `_speak` (after line 1025), add:

```python
    def _refresh_lines(self) -> None:
        """Bring the kept copies of Reachy's lines in the live voice up to date, in the background: never a wait
        before the greeting, and a copy the server no longer serves is dropped (bridge/lines.py)."""
        if self.lines is not None:
            self.lines.refresh(LINE_IDS)

    async def _play_line(self, line_id: str, text: str | None = None) -> bool:
        """Reachy's own line from its kept copy in the live voice (bridge/lines.py), through robot.play_clip with the
        listener held and told the words (`text`), as _say does. False without a kept copy, or when playing it failed
        (logged; never the slot's end): the caller then says the line the usual way."""
        path = self.lines.kept(line_id) if self.lines is not None else None
        if path is None:
            return False
        if self.voice is not None and text:
            self.voice.note_spoken(text)   # hearing it back through the microphone isn't the patient answering
        try:
            return bool(await self._speak(self.robot.play_clip, path))
        except Exception as error:
            log.warning("slot %s: the kept %s line could not be played (%s); Reachy says it itself", self.task_id,
                        line_id, type(error).__name__)
            return False
```

- [ ] **Step 5: Run the tests to verify they pass**

Run:
```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests -q -p no:cacheprovider
```
Expected: all passed (every older session test keeps its Matcha greeting, because the default `FakeLines` keeps nothing). If only one or both of the two timing checks named in the header fail, rerun them alone as the header says, up to three times; anything else is a regression.

- [ ] **Step 6: Commit**

```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge
git add medcare_reachy/bridge/session.py medcare_reachy/bridge/tests/fakes.py medcare_reachy/bridge/tests/test_session_live.py medcare_reachy/bridge/tests/test_session_checkin.py
git commit -m "Session: the check-in greeting from the kept copy before a live chat" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: The wind-down gates and the stop clip ids

**Files:**
- Modify: `medcare_reachy/bridge/session.py:714-720` (`_run_live`'s stop), `:920-928` (`_wind_down`)
- Test: `medcare_reachy/bridge/tests/test_session_live.py:40-83` (`FakeLiveChat.stop`), `:271-297` (two asserts), `:299-317` (a `stop` stand-in), and new tests appended

**Interfaces:**
- Consumes: `LiveChat.stop(reason, say, clip)` (Task 5; `FakeLiveChat` here), `_play_line("wind_down")` (Task 6), `outcomes["live"]` / `outcomes["checkin"]` as `_live_checkin` sets them (`session.py:652-663`).
- Produces: `SlotSession._wants_wind_down() -> bool`; `FakeLiveChat.stop(reason, say=None, clip=None)` recording `clips`.

- [ ] **Step 1: Write the failing tests**

In `medcare_reachy/bridge/tests/test_session_live.py`:

(a) `FakeLiveChat` (lines 46-65): record the clip id. In `__init__` change the list line to

```python
        self.made, self.models_dirs, self.runs, self.stops, self.cancelled = [], [], [], [], False
        self.clips = []                   # stop()'s `clip` per call: the kept copy the session names
```

and `stop` to

```python
    def stop(self, reason, say=None, clip=None):
        self.stops.append((reason, say))
        self.clips.append(clip)
        self.outcome = LiveOutcome("ended", reason, "c9")
        if self.stopped is not None:
            self.stopped.set()
```

(b) In `test_a_live_companion_rests_when_nobody_is_heard_or_seen` (line 281) make the first assert

```python
    assert chat.stops == [("patient_left", REST_LINE["zh-TW"])] and chat.clips == ["rest"] and not chat.cancelled
```

and in `test_a_due_medicine_ends_the_live_chat_with_reachys_line` (line 293)

```python
    assert chat.stops == [("medicine", MEDICINE_LINE["zh-TW"])] and chat.clips == ["medicine"]
```

and in `test_a_hand_to_mouth_moment_during_reachys_closing_line_is_no_news_for_family` (lines 305-308) the stand-in for `stop` must take the new keyword and pass it on, or the session's call raises `TypeError` into `tick()`'s catch-all:

```python
    def closing_line(reason, say=None, clip=None):
        h.stream.frame_seq = 8
        h.see(identity_status="verified", extra_events=[sip])   # a sip while Reachy says its line
        asyncio.get_running_loop().call_later(0.05, stop, reason, say, clip)   # the line takes a moment
```

(c) Add `POST_SLOT_SECONDS` to the `session` import (lines 15-17):

```python
from medcare_reachy.bridge.session import (
    CAMERA_LOST_SECONDS, CHECKIN_GREETING, COMPANION_CAPTURE_FPS, MEDICINE_LINE, POST_SLOT_SECONDS,
    REST_AFTER_SECONDS, REST_LINE, open_live_chat)
```

(d) Append:

```python
def test_no_wind_down_after_a_live_chat_that_ended_with_the_patients_goodbye(tmp_path):
    h = checkin_live(FakeLiveChat())                                         # ends "goodbye"
    h.lines.files["wind_down"] = (tmp_path / "wind_down.wav", "k1")
    h.tick()
    assert h.slot.state == "WIND_DOWN"
    h.run_until(lambda: h.slot.done)
    assert "wind_down" not in h.clips.played and "play_clip:wind_down" not in h.robot.calls
    assert h.slot.result == "completed" and h.robot.calls[-1] == "sleep"


def test_the_wind_down_after_a_live_risk_ending_comes_from_the_kept_copy_else_the_manifest(tmp_path):
    h = checkin_live(FakeLiveChat(LiveOutcome("ended", "risk", "c9", risk=True)))
    h.lines.files["wind_down"] = (tmp_path / "wind_down.wav", "k1")
    h.tick()
    h.run_until(lambda: h.slot.done)
    assert "play_clip:wind_down" in h.robot.calls and "wind_down" not in h.clips.played
    assert h.voice.holds == 0

    h = checkin_live(FakeLiveChat(LiveOutcome("ended", "risk", "c9", risk=True)))   # no copy kept
    h.tick()
    h.run_until(lambda: h.slot.done)
    assert "wind_down" in h.clips.played and "play_clip:wind_down" not in h.robot.calls

    h = checkin_live(FakeLiveChat(LiveOutcome("ended", "risk", "c9", risk=True)))   # a copy that cannot play
    h.lines.files["wind_down"] = (tmp_path / "wind_down.wav", "k1")
    h.robot.play_clip_error = ValueError("only 16-bit PCM clips are supported")
    h.tick()
    h.run_until(lambda: h.slot.done)
    assert "wind_down" in h.clips.played and h.slot.result == "completed"


def test_a_text_chat_goodbye_after_a_live_fallback_still_winds_down_from_the_kept_copy(tmp_path):
    h = checkin_live(FakeLiveChat(LiveOutcome("fallback", "live_ended", "c7")))
    h.lines.files["wind_down"] = (tmp_path / "wind_down.wav", "k1")
    h.app.turn_ids = 4
    h.tick()
    h.app.replies = [{"reply": "再見", "speech_text": "再见", "end": True}]
    h.voice.speak("好，拜拜")
    h.tick(1)
    assert h.slot.outcomes == {"live": "live_ended", "checkin": "goodbye"}   # not a live goodbye: GPT-Live said no bye
    h.run_until(lambda: h.slot.done)
    assert "play_clip:wind_down" in h.robot.calls and "wind_down" not in h.clips.played


def test_a_dose_tasks_live_goodbye_skips_the_wind_down_two_minutes_later(tmp_path):
    h = Harness(live_task([dose(1)]), voice=True, speaker=True, live_chat=FakeLiveChat().factory)
    h.lines.files["wind_down"] = (tmp_path / "wind_down.wav", "k1")
    h.find_patient()
    h.verify_and_prompt()
    h.see(identity_status="verified", recorded=RECORDED)
    h.tick()
    h.tick()                                             # the live chat runs and ends with the goodbye
    assert h.slot.state == "POST_SLOT_OBSERVE" and h.slot.outcomes["checkin"] == "goodbye"
    h.tick(POST_SLOT_SECONDS + 1)
    assert h.slot.state == "WIND_DOWN"
    h.run_until(lambda: h.slot.done)
    assert "wind_down" not in h.clips.played and "play_clip:wind_down" not in h.robot.calls
    assert h.slot.result == "completed"


def test_a_dose_only_task_keeps_the_manifest_wind_down_even_with_a_kept_copy(tmp_path):
    h = Harness(make_task([dose(1)]))
    h.lines.files["wind_down"] = (tmp_path / "wind_down.wav", "k1")
    h.find_patient()
    h.verify_and_prompt()
    h.see(identity_status="verified", recorded=RECORDED)
    h.tick()
    assert h.slot.state == "POST_SLOT_OBSERVE" and "live" not in h.slot.outcomes
    h.tick(POST_SLOT_SECONDS + 1)
    h.run_until(lambda: h.slot.done)
    assert h.clips.played[-1] == "wind_down" and "play_clip:wind_down" not in h.robot.calls
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_session_live.py -q -p no:cacheprovider
```
Expected: 6 failed, the rest passed. `..._rests_when_nobody...` and `..._due_medicine_ends...` fail on `chat.clips == [...]` (they got `[None]`); `test_no_wind_down_after_a_live_chat...` and `..._dose_tasks_live_goodbye...` fail because `"wind_down"` is in `h.clips.played`; the risk test and the fallback test fail because `"play_clip:wind_down"` is not in `h.robot.calls`. `..._dose_only_task...` and `..._hand_to_mouth_moment...` pass already (the session does not pass `clip` yet).

- [ ] **Step 3: Write the implementation**

In `medcare_reachy/bridge/session.py`:

(a) In `_run_live` (line 720), the stop names the kept copy:

```python
                    chat.stop(ending, say=self._line(MEDICINE_LINE if ending == "medicine" else REST_LINE),
                              clip="medicine" if ending == "medicine" else "rest")
```

(b) Replace `_wind_down` (lines 920-928) and add `_wants_wind_down` after it:

```python
    async def _wind_down(self):
        if not self.entered:
            await self._end_session()
            if self._wants_wind_down():
                # After a live chat the kept copy in the live voice, else the manifest WAV; a task in which no live
                # chat ran keeps the manifest WAV even with a copy kept (v1: no live voice inside a Matcha flow)
                if self.outcomes.get("live") is None or not await self._play_line("wind_down"):
                    await self._play("wind_down")
            self.entered = True
        await self._advance("completed", {"outcomes": self._outcome_detail()})
        await self._robot("sleep")
        self._finish("completed")

    def _wants_wind_down(self) -> bool:
        """No wind-down line after Reachy has just said why it rests (COMPANION_ENDINGS), nor after a live chat the
        patient ended with their goodbye (GPT-Live answers it; a closing line on top would be one too many). After
        every other ending, a risk included, it plays."""
        ending = self.outcomes.get("checkin")
        if ending in COMPANION_ENDINGS:
            return False
        return not (self.outcomes.get("live") == "ended" and ending == "goodbye")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_session_live.py medcare_reachy/bridge/tests/test_session.py medcare_reachy/bridge/tests/test_session_checkin.py -q -p no:cacheprovider
```
Expected: all passed (`test_session.py`'s `test_happy_path_two_doses_recorded_then_post_slot_and_wind_down` still sees `played[-1] == "wind_down"`: no chat ran, so `outcomes["live"]` is unset).

- [ ] **Step 5: Commit**

```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge
git add medcare_reachy/bridge/session.py medcare_reachy/bridge/tests/test_session_live.py
git commit -m "Session: no wind-down after a live goodbye; the kept wind-down after other live endings" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Wiring: the runner, the service and `LINES_DIR`

**Files:**
- Modify: `medcare_reachy/bridge/runner.py:506-512` (`Runner.__init__`), `:613-618` (`run_slot`)
- Modify: `medcare_reachy/service.py:11` (imports), `:21` (`LINES_DIR` beside `CLIPS_DIR`), `:49-114` (`default_bridge`)
- Test: `medcare_reachy/bridge/tests/test_runner.py` (append), `tests/test_app.py` (append)

**Interfaces:**
- Consumes: `LineCache(lines_dir, app, language)`, `LineCache.refresh(ids)`, `LINE_IDS` (Tasks 2-3), `SlotSession(lines=)` (Task 6).
- Produces: `Runner(..., lines=None)` with the attribute `lines`, handed to every `SlotSession` in `run_slot`; `service.LINES_DIR: Path` = `Path.home() / ".medcare_reachy" / "lines"`; `default_bridge` builds the cache beside the `AppClient`, starts the first refresh, and hands it to the `Runner`.

- [ ] **Step 1: Write the failing tests**

Append to `medcare_reachy/bridge/tests/test_runner.py` (it already imports `asyncio`, `runner_module`, `Runner`, `FakeClips`, `FakeRobot`, `inline`, and defines `HeartbeatApp` and `StubStream`):

```python
def test_the_runner_hands_the_line_cache_to_every_slot(monkeypatch):
    made = []

    class StubSession:
        def __init__(self, task, **kwargs):
            made.append(kwargs)
            self.done, self.result = True, "completed"

        def stop(self, reason, abort=True):
            pass

        async def tick(self):
            pass

    monkeypatch.setattr(runner_module, "SlotSession", StubSession)
    lines = object()
    runner = Runner(app=HeartbeatApp(), robot=FakeRobot(), clips=FakeClips(), stream=StubStream(),
                    run_blocking=inline, lines=lines)
    asyncio.run(runner.run_slot({"task_id": "t1", "reason": "checkin", "doses": []}))
    assert made[0]["lines"] is lines and runner.slot is None
    assert heartbeat_runner(HeartbeatApp()).lines is None                     # the default: Matcha and the clips only
```

Append to `tests/test_app.py` (it already imports `Path`):

```python
def test_the_kept_lines_live_under_the_robot_users_home():
    """Reachy's lines in the live voice (bridge/lines.py) sit beside the deploy's clips (~/.medcare_reachy/clips), not
    in the package, so a reinstall of the app keeps them."""
    from medcare_reachy.service import LINES_DIR

    assert LINES_DIR == Path.home() / ".medcare_reachy" / "lines"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_runner.py::test_the_runner_hands_the_line_cache_to_every_slot tests/test_app.py::test_the_kept_lines_live_under_the_robot_users_home -q -p no:cacheprovider
```
Expected: 2 failed: `TypeError: Runner.__init__() got an unexpected keyword argument 'lines'` and `ImportError: cannot import name 'LINES_DIR' from 'medcare_reachy.service'`.

- [ ] **Step 3: Write the implementation**

In `medcare_reachy/bridge/runner.py`:

(a) `Runner.__init__` (lines 507-511):

```python
    def __init__(self, *, app, robot, clips, stream, voice=None, speaker=None, language="zh-TW",
                 heartbeat_seconds=HEARTBEAT_SECONDS, tick_seconds=TICK_SECONDS, run_blocking=asyncio.to_thread,
                 ack=True, gestures=True, mood=None, follow=None, clock=time.monotonic, server_url=None, lines=None):
        self.app, self.robot, self.clips, self.stream, self.voice = app, robot, clips, stream, voice
        self.speaker, self.language, self.mood, self.follow = speaker, language, mood, follow
        self.lines = lines   # bridge/lines.py LineCache: Reachy's lines in the live voice, for every slot
```

(b) `run_slot` (lines 615-618):

```python
        slot = SlotSession(task, app=self.app, robot=self.robot, clips=self.clips, stream=self.stream,
                           voice=self.voice, speaker=self.speaker, language=self.language,
                           run_blocking=self.run_blocking, ack=self.ack, gestures=self.gestures, mood=self.mood,
                           follow=self.follow, lines=self.lines)
```

In `medcare_reachy/service.py`:

(c) After `CLIPS_DIR = ...` (line 21):

```python
# Reachy's own lines in the server's live voice (bridge/lines.py), kept under the robot user's home like the deploy's
# clips (~/.medcare_reachy/clips), so a reinstall of the app keeps them.
LINES_DIR = Path.home() / ".medcare_reachy" / "lines"
```

(d) In `default_bridge`, add the import beside the other deferred ones (after `from medcare_reachy.bridge.clips import ClipPlayer`, line 52):

```python
    from medcare_reachy.bridge.lines import LINE_IDS, LineCache
```

and replace the `async with AppClient(...)` block (lines 105-114) with:

```python
        async with AppClient(settings["app_url"], settings["device_token"]) as app:
            lines = LineCache(LINES_DIR, app, settings["language"])
            lines.refresh(LINE_IDS)   # the live voice's copies of Reachy's lines, brought up to date in the background
            log.info("[STARTUP] lines: the live voice's copies of Reachy's lines are kept in %s", lines.dir)
            runner = Runner(app=app, robot=robot, clips=clips, voice=voice, speaker=speaker,
                            language=settings["language"],
                            stream=MonitorStream(app, robot, engine, fps=float(settings["capture_fps"]),
                                                 emotion=emotion, governor=governor),
                            ack=settings.get("checkin_ack", True), gestures=settings.get("checkin_gestures", True),
                            mood=emotion if hear is not None else None, follow=face_follower(robot, settings),
                            server_url=settings["app_url"], lines=lines)
            on_runner(runner)
            await runner.run()
```

- [ ] **Step 4: Run the tests to verify they pass, and that the service still imports**

Run:
```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_runner.py tests/test_app.py -q -p no:cacheprovider && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -c "from medcare_reachy.service import LINES_DIR, default_bridge; print(LINES_DIR)"
```
Expected: all passed, then one line ending in `\.medcare_reachy\lines` (the home folder of whoever runs it).

- [ ] **Step 5: Commit**

```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge
git add medcare_reachy/bridge/runner.py medcare_reachy/service.py medcare_reachy/bridge/tests/test_runner.py tests/test_app.py
git commit -m "Wire the line cache into the runner and the service (LINES_DIR)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: 0.16.0: the version, the README, the full suite

**Files:**
- Modify: `medcare_reachy/bridge/__init__.py:3`, `pyproject.toml:8`, `tests/test_app.py:393`
- Modify: `README.md` (a new section after "Live voice check-ins (0.8.0, test)", lines 136-160, and a Status entry before the `0.15.0` one at line 193)

**Interfaces:**
- Consumes: everything above.
- Produces: `__version__ == "0.16.0"`, which the live chat's `start` message and the heartbeat already report (`live.py:378-379`, `runner.py`), so the server can tell a 0.16.0 robot.

- [ ] **Step 1: Pin the new version in the test**

In `tests/test_app.py` line 393 change `"0.15.0"` to `"0.16.0"`:

```python
    assert tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"] == __version__ == "0.16.0"
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -m pytest tests/test_app.py::test_the_package_and_the_heartbeat_report_the_same_version -q -p no:cacheprovider
```
Expected: 1 failed, `assert '0.15.0' == '0.16.0'`.

- [ ] **Step 3: Bump the version and write the docs**

`medcare_reachy/bridge/__init__.py` line 3:

```python
__version__ = "0.16.0"
```

`pyproject.toml` line 8:

```toml
version = "0.16.0"
```

`README.md`: after the "Live voice check-ins (0.8.0, test)" section's last bullet (`- It uses the \`websockets\` package, ...`, line 159) and before `## Companion session (0.9.0)`, add:

```markdown
## Same voice (0.16.0, test)

From 0.16.0 Reachy's own lines around a live chat are heard in the live voice (GPT-Live's), not in Matcha: the
check-in greeting, the opening with the patient's name, the reminder lead-in, the repeat line, the help line, the rest
and medicine lines, and the wind-down. Update the MedAiCarePlus server at the same time; an older server has no lines
to give, and everything then sounds as in 0.15.0.

- The server renders each line once in the live voice (`GET /api/device/clips/line/{id}`) and the robot keeps a copy
  under `~/.medcare_reachy/lines/<language>/` (`LINES_DIR` in `service.py`, beside the deploy's clips, so a reinstall
  keeps it) with the server's key beside it. The copies are brought up to date in the background when the app starts
  and at each task, one at a time, never while Reachy waits to speak.
- Matcha stays as the fallback. A line whose copy is missing, out of date (the server's `clip` key differs), or
  cannot be read is said as before, and the copy is fetched for next time. The help line keeps every rule it has: it
  plays at once, whole and still, and a live chat still needs Matcha for it.
- The greeting plays from the kept copy only when the task is a live check-in, and the wind-down only after a live
  chat; a text-chat check-in and a medicine reminder without a chat sound as before. After a live chat that ended with
  the patient's goodbye there is no wind-down line at all; after any other ending (a risk included) it plays.
- When the server has no live voice any more, the copies are dropped (the route answers 404), so an old voice is
  never played.
- Not in this version: the medicine reminder's own clips, the text chat's replies and openings, 「嗯」 and the thinking
  phrases stay Matcha.
```

In the Status list, before the `- **0.15.0**:` entry (line 193), add:

```markdown
- **0.16.0** (test): Reachy's own lines around a live chat play in the live voice from copies the server rendered
  (above); Matcha remains the fallback.
```

- [ ] **Step 4: Run the whole suite to verify it passes**

Run:
```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -m pytest tests medcare_reachy/bridge/tests -q -p no:cacheprovider
```
Expected: `781 passed, 6 skipped` (the 737 at `437b136` plus the 44 test cases this plan adds: 2 in Task 1, 11 in Tasks 2-3, 18 in Tasks 4-5, 6 in Task 6, 5 in Task 7, 2 in Task 8), and 0 failed. If the only failures are one or both of the two timing checks named in the header, `test_speech.py::test_say_tells_when_its_first_sound_is_about_to_play` and `test_filler_echo.py::test_its_echo_goes_and_the_patient_s_next_words_stay`, rerun them alone, up to three times:

```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && PYTHONDONTWRITEBYTECODE=1 /c/Reachy_Mini/repo/MedAiCarePlus/.venv/Scripts/python.exe -m pytest "medcare_reachy/bridge/tests/test_speech.py::test_say_tells_when_its_first_sound_is_about_to_play" "medcare_reachy/bridge/tests/test_filler_echo.py::test_its_echo_goes_and_the_patient_s_next_words_stay" -q -p no:cacheprovider
```
Expected: `2 passed` on one of the reruns (they are the timing checks described in the header, not this work); a pass on any rerun means it is not a regression. A test that fails on all three reruns, or any failing test other than these two, is a regression of this plan's work: fix it before the commit. Then check the tree holds nothing but the intended changes:

```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge && git status --short
```
Expected: exactly `M README.md`, `M medcare_reachy/bridge/__init__.py`, `M pyproject.toml`, `M tests/test_app.py`.

- [ ] **Step 5: Commit**

```bash
cd /c/Reachy_Mini/repo/medcare-reachy-bridge
git add README.md medcare_reachy/bridge/__init__.py pyproject.toml tests/test_app.py
git commit -m "0.16.0: Reachy's lines around a live chat in the live voice" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git log --oneline feature/merged-live-emotion..same-voice
```
Expected: nine commits listed, newest first, from `0.16.0: ...` down to `Fetch Reachy's lines in the live voice: AppClient.line_clip`. Do not push.

---

## What this plan does not do (and where it is)

- The server side (the `voice_lines.py` service, the line route and its 304, the `clip` fields on `ready` and `say`, the render-ahead, the docs): the separate server plan. Until it lands, every robot fetch answers 404 or an HTTP error, the cache keeps nothing, and the robot sounds exactly as 0.15.0 does; the tests here fake the route.
- Listening to the lines on the real robot (spec "Before release"): with the user, after both plans are built and the server's `LIVE_VOICE_TEST=1` Check passes. What to listen for: the greeting at ANNOUNCE in the live voice on a live check-in, the opening and the help line from the copies, the wind-down absent after a goodbye and present after a risk, and the first chat after a name change saying that line in Matcha once.
