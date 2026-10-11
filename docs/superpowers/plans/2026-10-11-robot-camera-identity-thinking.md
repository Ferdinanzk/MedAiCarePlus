# Robot camera, finding the patient, AI card fixes and thinking levels (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Reachy robot find the patient reliably before it talks (a camera watchdog and a fixed bridge race on the robot, an OpenVINO-only rule for the search on the server), fix the AI connections card's blind spots, and let each AI job think at a chosen level.

**Architecture:** Robot app 0.6.0 (`reachy_app/`) gets a pure-logic `CameraWatchdog` driven by its frame stream, recovery through the SDK's public `release_media()`/`acquire_media()`, and a camera state in the heartbeat. The FastAPI server stores and shows that state, relaxes the identity rule only for robot sessions that started as a search, and logs identity outcomes. The AI connections card gains per-job usability, a `.env` key fill for keyless OpenRouter connections, a voice-only model message, and a per-job thinking level whose accepted levels each model's Test finds.

**Tech Stack:** Python 3.11 (server, FastAPI, asyncpg, pytest with fake pools), Python 3.12 on the robot (reachy_mini SDK 1.11.0, pytest with fakes), React 19 + Vite + Tailwind + i18next (Playwright tests), PostgreSQL 16 (`sql/init.sql`, idempotent).

**Spec:** `docs/superpowers/specs/2026-10-11-robot-camera-identity-thinking-design.md` (commit `37ba84e`). The flaw list (outside the repo) is `C:\Reachy_Mini\repo\flaw.md`.

## Global Constraints

- Branch `robot-camera-thinking` in `C:/Reachy_Mini/repo/MedAiCarePlus`, based on `ai-connections` (`4808126`). Commit only there; never push, never merge.
- Backend tests (from the repo root): `.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider`. Baseline 1596 passed + 1 known failure (`test_a_broken_background_never_costs_the_reply`, BUG-37). Two timing tests flake now and then (BUG-39).
- Robot app tests (from `reachy_app/`): `../.venv/Scripts/python.exe -m pytest tests medcare_reachy/bridge/tests -q -p no:cacheprovider`. Baseline 494 passed, 6 skipped. Known flakes: `test_speech.py::test_say_tells_when_its_first_sound_is_about_to_play` and `test_gestures.py::test_a_clip_handed_over_without_waiting_still_plays_before_the_next_one`.
- Frontend (from `frontend_source/`): `npx tsc -b` and `npm run build`; Playwright after a build: `npx playwright test --config "C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/pw-frontend.config.mjs" tests/<file>.spec.ts`. Baseline 48 passed + the known `scan.spec.ts:148` failure (BUG-38).
- Existing files keep their line endings (many are CRLF); new files use LF. Match each file's style.
- Plain, simple user-facing wording. Every new web-app string in en and zh-TW (Traditional Chinese, Taiwan wording). The robot app's own settings page is English only today.
- Never put API keys, passwords or robot keys in code, tests, logs or commit messages.
- Commit messages end with a blank line, then `Co-Authored-By: <the model that wrote the commit> <noreply@anthropic.com>` (pass it as a separate `-m`).
- Thinking `off` is the default everywhere and must keep today's request bodies byte for byte.

## Review Focus

1. **SDK recovery on the real robot** (spec §7.1): a release/acquire cycle restarts the app's audio too. Task 2 pins it with fakes (recovery waits for a clip and holds the playback lock; the microphone pipeline restarts); Task 10 Part B Step 13 checks playback and listening after a real recovery.
2. **A slow camera is not a dead camera** (spec §7.2): frames at 5 fps must never count as a stall; only a 2 s gap does. Pinned in Task 2's watchdog tests.
3. **A dose task that ends with `camera_unavailable`** (spec §7.3) must not mark the dose taken or missed early; the server's normal `not_found` path decides. Pinned in Task 2 (status + detail posted through the existing path) and Task 10 Part B Step 13.
4. **Thinking probes cost time and money on Test** (spec §7.4): a level that times out must not block saving the base profile. Pinned in Task 7 (`detect_thinking` runs in its own `try` after the base test is saved).
5. **Main and backup with different accepted levels** (spec §7.5): each model gets the nearest lower level it accepts, never a refused one. Pinned in Task 8 (`job_targets` nearest-lower tests) and Task 9 (the card shows the level each model really gets).

## Execution order and controller rulings

- Tasks run in this order: **1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 10 (Part A only)**. Task 11 (the 「活著沒什麼意思」 keyword fix, flaw F21, added on the user's request) runs before Task 10, so Task 11's Step 7 ("make it live") does not apply: the stack is rebuilt once in Task 10 Part B.
- **R1:** Task 6 Step 6b updates the card's "copied from .env" note, which Task 5's key fill makes untrue.
- **R2:** Task 10 Step 4b adds `camera` to the docs that list the heartbeat fields.
- **R3:** Task 10 Parts B (rebuild, live checks, deploy 0.6.0, robot checks) and C (vault, `flaw.md` part 2) are run by the controller after the final whole-branch review, not by an implementer.
- Accepted deviations from the shared interface contract: `CameraWatchdog.detach()` keeps `unavailable` (spec A3 says it stays) instead of going to `idle`; the robot's `not_found` detail is `{"reason": "camera_unavailable", "outcomes": {...}}`, so the server reads `detail["reason"]`; `CameraWatchdog` takes one extra keyword-only `wall=time.time` for `since`.
- Out of scope (noted, not planned): pushing robot app 0.6.0 to the Hugging Face space `pearlyjam21/medcare_reachy`.

## Expected test counts after each task

| Suite | Before | After |
|---|---|---|
| Backend (passed, plus BUG-37) | 1596 | 1609 (T3), 1624 (T4), 1640 (T5), 1680 (T7), 1723 (T8), 1727 (T11) |
| Robot app (passed, 6 skipped) | 494 | 497 (T1), 533 (T2) |
| Playwright (passed, plus BUG-38) | 48 | 53 (T1/T3), 59 (T6), 70 (T9) |

---

### Task 1: Robot app: one bridge thread, even with overlapping restarts

Spec part: A1 (`docs/superpowers/specs/2026-10-11-robot-camera-identity-thinking-design.md`, section 3.A1). Flaw F9.

**What is wrong today (verified).** `BridgeService.restart()` is `stop()` then `start()` (`reachy_app/medcare_reachy/service.py:124-126`).
The settings page's `/api/config` and `/api/restart` are plain `def` endpoints (`reachy_app/medcare_reachy/web.py:23-41`), so
FastAPI runs two requests at once on its thread pool. Both `stop()` calls join the same old thread. Caller A's `start()`
creates thread T1. Caller B's `stop()` then runs `self._thread = None` unconditionally (`service.py:120`), which forgets
T1 while it runs. B's `start()` creates T2. Both bridges drive the same robot (camera, microphone, motors) until the
app process exits, and `stop()` can no longer reach T1. A double click on "Restart" or "Save and connect" is enough: the
page doesn't disable them while a request is pending (`reachy_app/medcare_reachy/static/main.js:60-89`).

There is a second way to the same result. `stop()` signals only a bridge that already has a runner
(`service.py:113-114`). A bridge that is still starting (robot, clips, voice, server client:
`service.py:48-63`) gets no signal. `stop()` then waits the whole 15 s timeout and forgets the thread anyway.

**The fix:**
- `restart()` runs under a new `threading.Lock` named `_restart_lock`.
- `stop()` never forgets a live thread. If the thread outlives the timeout, the service keeps tracking it and the
  state becomes `stopping`.
- `start()` refuses, with a WARNING, while a bridge thread is alive.
- A stop that comes while a bridge is still starting stops that bridge as soon as it has a runner (`_stop_requested`).
- The settings page disables Save and Restart while either request is pending.

**Files:**
- Modify: `reachy_app/medcare_reachy/service.py`:
  - `BridgeService.__init__`: lines 73-85 (attributes at 79-82).
  - `start()`: lines 95-108.
  - `stop()`: lines 110-122.
  - `restart()`: lines 124-126.
  - `_current()`: lines 128-131.
  - `_on_runner()`: lines 159-163.
- Modify: `reachy_app/medcare_reachy/static/main.js`:
  - `STATE_TEXT` and the constants: lines 1-11.
  - The submit handler: lines 60-84.
  - The restart handler: lines 86-89.
- Modify: `reachy_app/medcare_reachy/static/style.css`: line 18 (the state colours) and line 32 (`button.secondary`).
- Test, modify: `reachy_app/tests/test_app.py`:
  - Imports: lines 1-5.
  - The new tests go after `test_bridge_failure_is_reported_not_raised` (lines 158-168), before
    `# ── settings page API ──` (line 171).
  - The tests reuse `FakeRunner` (lines 107-117), `wait_for` (129-135) and `GOOD` (16).
- Test, create: `frontend_source/tests/robot-settings-page.spec.ts` (Playwright).
  - It loads the robot app's static page from disk and mocks its API.
  - Why in `frontend_source`: the robot app has no JavaScript test setup, and `frontend_source` has Playwright 1.60.
  - `consent.spec.ts:305` already reads repo files with `new URL(..., import.meta.url)`.
  - `tsc -b` does not include `tests/`: `tsconfig.app.json` includes only `src`, and `tsconfig.node.json` only
    `vite.config.ts`.

All existing files here have CRLF line endings in the working tree (git `core.autocrlf=true`; the index stores LF).
Edit them in place: don't rewrite whole files with LF. For the new spec file, either line ending is fine, because git
normalises it.

**Interfaces:**
- Consumes: nothing from other tasks. This is the first task and it is independent of the camera and server tasks.
- Produces, as the contract asks:
  - `BridgeService._restart_lock: threading.Lock`.
  - `BridgeService.restart()` is serialized.
  - There are never two bridge threads.
- Also produces:
  - A new private flag `BridgeService._stop_requested: bool`.
  - A new service state `"stopping"` in `BridgeService.status()["state"]`, with the detail "The previous connection is
    still closing. Press Restart in a moment.".
  - `STATE_TEXT.stopping = "Stopping…"` on the settings page.
- The public methods (`attach`, `start`, `stop(timeout=15.0)`, `restart`, `status`) keep their signatures. No later task
  depends on the new names.

---

- [ ] **Step 1: Write the failing robot-app tests**

In `reachy_app/tests/test_app.py`, replace the import lines 3-4:

```python
import json
import time
```

with:

```python
import json
import logging
import threading
import time
```

Then insert this block after `test_bridge_failure_is_reported_not_raised`, which ends with
`    assert "integrity" in service.status()["detail"]` (line 168). Keep two blank lines before it and two blank lines
before the existing `# ── settings page API ──` comment:

```python
# ── one bridge at a time ──
def tracked_bridge(alive, runs, start_seconds=0.1, stop_seconds=0.2):
    """Shaped like default_bridge: a moment to start before it has a runner (robot, clips, voice, server client),
    and a moment to shut down. `alive` holds the threads whose bridge is running right now."""
    async def bridge(reachy_mini, settings, models_dir, on_runner):
        me = threading.current_thread()
        alive.add(me)
        runs.append(settings)
        try:
            await asyncio.sleep(start_seconds)
            runner = FakeRunner()
            on_runner(runner)
            await runner.stopping.wait()
            await asyncio.sleep(stop_seconds)   # monitor_end, robot.sleep(), voice.stop()
        finally:
            alive.discard(me)
    return bridge


def test_two_restarts_at_once_leave_one_bridge_and_it_is_the_tracked_one(tmp_path):
    """A double click on Save or Restart sends two requests, and the settings server runs them at the same time
    (its endpoints are plain functions, run on a thread pool). Both bridges used to drive the same robot."""
    store = SettingsStore(tmp_path / "s.json")
    store.save(GOOD)
    alive, runs = set(), []
    service = BridgeService(object(), store, bridge=tracked_bridge(alive, runs))
    service.start()
    assert wait_for(lambda: service.status()["state"] == "running")
    began = time.monotonic()
    callers = [threading.Thread(target=service.restart) for _ in range(2)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(20)
    assert time.monotonic() - began < 5   # the second restart doesn't sit out stop()'s 15 s timeout
    assert wait_for(lambda: service.status()["state"] == "running")
    assert alive == {service._thread}     # exactly one bridge, and stop() can reach it
    assert len(runs) == 3                 # each restart started a fresh bridge with the saved settings
    service.stop()
    assert not alive and service.status()["state"] == "stopped"


def test_a_bridge_that_outlives_stop_stays_tracked_and_no_second_one_starts(tmp_path, caplog):
    store = SettingsStore(tmp_path / "s.json")
    store.save(GOOD)
    alive, runs = set(), []
    release = threading.Event()

    async def slow_to_stop(reachy_mini, settings, models_dir, on_runner):
        me = threading.current_thread()
        alive.add(me)
        runs.append(settings)
        try:
            runner = FakeRunner()
            on_runner(runner)
            await runner.stopping.wait()
            release.wait(5)   # shutdown work that takes longer than stop() waits (a blocking robot call)
        finally:
            alive.discard(me)

    service = BridgeService(object(), store, bridge=slow_to_stop)
    service.start()
    assert wait_for(lambda: service.status()["state"] == "running")
    first = service._thread
    with caplog.at_level(logging.WARNING, logger="medcare_reachy"):
        service.stop(timeout=0.2)
        assert service.status()["state"] == "stopping"
        service.start()
    assert service._thread is first and alive == {first} and len(runs) == 1
    messages = [record.getMessage() for record in caplog.records]
    assert any("did not stop" in m for m in messages) and any("not starting a second" in m for m in messages)
    release.set()
    first.join(2)
    assert service.status()["state"] == "stopped"   # its own exit reports it
    service.start()   # once it has ended, a new bridge may start
    assert wait_for(lambda: service.status()["state"] == "running") and service._thread is not first
    service.stop()
    assert not alive


def test_a_stop_while_the_bridge_is_still_starting_ends_it_once_it_has_a_runner(tmp_path):
    store = SettingsStore(tmp_path / "s.json")
    store.save(GOOD)
    alive, runs = set(), []
    service = BridgeService(object(), store, bridge=tracked_bridge(alive, runs, start_seconds=0.3))
    service.start()
    began = time.monotonic()
    service.stop(timeout=5)
    assert time.monotonic() - began < 2   # stopped as soon as it had a runner, not after the timeout
    assert not alive and service.status()["state"] == "stopped"
```

- [ ] **Step 2: Run the new tests and see them fail**

From `C:/Reachy_Mini/repo/MedAiCarePlus/reachy_app`:

```bash
../.venv/Scripts/python.exe -m pytest tests/test_app.py -q -p no:cacheprovider -k "restarts_at_once or outlives_stop or still_starting"
```

Expected: `3 failed`. These are the reasons seen on the current code:
- `test_two_restarts_at_once_leave_one_bridge_and_it_is_the_tracked_one`:
  - Output: `assert {<Thread(medc...)>, <Thread(medc...)>} == {<Thread(medc...)>}` with `Extra items in the left set`.
  - Two bridges were alive. This failed in 8 of 8 runs.
- `test_a_bridge_that_outlives_stop_stays_tracked_and_no_second_one_starts`:
  - Output: `AssertionError: assert 'stopped' == 'stopping'`.
- `test_a_stop_while_the_bridge_is_still_starting_ends_it_once_it_has_a_runner`:
  - Output: `assert (<t1> - <t0>) < 2`.
  - `stop()` sat out its whole 5 s timeout.

The failing runs leave daemon threads waiting. They end with the pytest process.

- [ ] **Step 3: Fix `BridgeService` in `reachy_app/medcare_reachy/service.py`**

Replace the class from `class BridgeService:` (line 72) down to the end of `_current()` (line 131) with the code below.
`_thread_main` (line 133 on) stays unchanged.

```python
class BridgeService:
    def __init__(self, reachy_mini, store: SettingsStore, models_dir: Path = models.DEFAULT_DIR,
                 bridge=default_bridge):
        self.reachy_mini = reachy_mini
        self.store = store
        self.models_dir = models_dir
        self.bridge = bridge
        self._lock = threading.Lock()
        # The settings page's requests run on a thread pool, so two restarts can overlap (a double click, or Save
        # right after Restart). Interleaved, they once left two bridges driving the same robot.
        self._restart_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_requested = False   # stop() was called for the tracked thread, maybe before it had a runner
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner = None
        self._state = "connecting_robot" if reachy_mini is None else "stopped"
        self._detail = "Connecting to the robot…" if reachy_mini is None else ""
        self._since = time.time()

    # ── lifecycle ──
    def attach(self, reachy_mini) -> None:
        """The runtime hands over the robot connection after the settings page is already up."""
        with self._lock:
            self.reachy_mini = reachy_mini
            if self._state == "connecting_robot":
                self._set("stopped", "")

    def start(self) -> None:
        with self._lock:
            if self.reachy_mini is None:
                return   # settings are saved; run() starts the bridge once the robot is attached
            if self._thread and self._thread.is_alive():
                # Never two bridges on one robot: the tracked one is still running, or still stopping.
                log.warning("a bridge thread is still running; not starting a second one")
                return
            settings = self.store.load()
            if not SettingsStore.configured(settings):
                self._set("not_configured", "Enter the server address and robot key on this page.")
                return
            self._set("starting", "Connecting to the server…")
            self._stop_requested = False
            self._thread = threading.Thread(target=self._thread_main, args=(settings,),
                                            name="medcare-bridge", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        with self._lock:
            thread, loop, runner = self._thread, self._loop, self._runner
            self._stop_requested = True   # a bridge still starting has no runner to signal yet: _on_runner stops it
        if loop and runner:
            loop.call_soon_threadsafe(runner.request_shutdown)
        if thread:
            thread.join(timeout)
        with self._lock:
            if thread is not None and thread.is_alive():
                # Keep tracking it: a forgotten live bridge once kept running beside its replacement. start()
                # refuses until it has ended, and its own exit then sets the state.
                log.warning("bridge thread did not stop within %.0f s; a new one starts only after it ends", timeout)
                self._set("stopping", "The previous connection is still closing. Press Restart in a moment.")
                return
            if self._thread is not thread:
                return   # start() ran meanwhile (the app starting up): the new bridge is the tracked one
            self._thread = None
            if self._state not in ("error", "not_configured", "connecting_robot"):
                self._set("stopped", "")

    def restart(self) -> None:
        with self._restart_lock:   # one restart at a time (see __init__)
            self.stop()
            self.start()

    def _current(self) -> bool:
        """Whether the calling bridge thread is the service's one. stop() keeps tracking a bridge thread until it
        has ended, so this guards against a thread the service no longer tracks."""
        return self._thread is threading.current_thread()
```

Then replace `_on_runner` (lines 159-163, now further down) with:

```python
    def _on_runner(self, runner) -> None:
        with self._lock:
            if not self._current():
                return
            self._runner = runner
            stop_now = self._stop_requested
            if not stop_now:
                self._set("running", "Waiting for a medication reminder from the server.")
        if stop_now:
            runner.request_shutdown()   # stop() came while this bridge was still starting: it had no runner then
```

Why `runner.request_shutdown()` is safe here:
- `default_bridge` calls `on_runner(runner)` on the bridge's own event-loop thread, right before `await runner.run()`
  (`service.py:63-64`).
- `Runner.request_shutdown()` only sets `self.stopping` (no slot exists yet) (`bridge/runner.py:381-385`).
- `Runner.run()` then returns at once: `_task_loop` ends because `stopping` is already set, and `asyncio.wait` returns
  (`runner.py:387-406, 412`).
- `default_bridge`'s `finally` then stops the voice listener and closes the robot wrapper as usual.

No new imports are needed: `logging`, `threading` and `log` are already there (`service.py:8-16`).

- [ ] **Step 4: Run the new tests and see them pass**

From `C:/Reachy_Mini/repo/MedAiCarePlus/reachy_app`:

```bash
../.venv/Scripts/python.exe -m pytest tests/test_app.py -q -p no:cacheprovider -k "restarts_at_once or outlives_stop or still_starting"
../.venv/Scripts/python.exe -m pytest tests/test_app.py -q -p no:cacheprovider
```

Expected: `3 passed, 24 deselected`, then `27 passed`. These tests depend on timing, so run the second command 3 times.
All 3 runs must pass. With this exact code it passed 5 of 5 runs, about 4.5 s each.

- [ ] **Step 5: Check against the investigation's reproduction**

The read-only script imports the repo's real `BridgeService`. Before the fix it printed `2 bridge thread(s)` (5 of 5
runs). From `C:/Reachy_Mini/repo/MedAiCarePlus`:

```bash
.venv/Scripts/python.exe -B "C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/inv_restart_race.py"
```

Expected output, last line: `after two concurrent restart() calls: 1 bridge thread(s) running; service tracks: medcare-bridge`.

- [ ] **Step 6: Run the whole robot-app suite**

From `C:/Reachy_Mini/repo/MedAiCarePlus/reachy_app`:

```bash
../.venv/Scripts/python.exe -m pytest tests medcare_reachy/bridge/tests -q -p no:cacheprovider
```

Expected: `497 passed, 6 skipped`, which is the baseline of 494 plus the 3 new tests.

Two timing tests fail now and then on this PC, before and after this change:
- `medcare_reachy/bridge/tests/test_speech.py::test_say_tells_when_its_first_sound_is_about_to_play` (known).
- `medcare_reachy/bridge/tests/test_gestures.py::test_a_clip_handed_over_without_waiting_still_plays_before_the_next_one`.
  It failed 2 of 6 runs on the unchanged code.

If one of these fails, rerun it alone. It must pass:

```bash
../.venv/Scripts/python.exe -m pytest "medcare_reachy/bridge/tests/test_speech.py::test_say_tells_when_its_first_sound_is_about_to_play" "medcare_reachy/bridge/tests/test_gestures.py::test_a_clip_handed_over_without_waiting_still_plays_before_the_next_one" -q -p no:cacheprovider
```

Any other failure is real.

- [ ] **Step 7: Write the failing settings-page test**

Create `frontend_source/tests/robot-settings-page.spec.ts`:

```ts
import { readFileSync } from 'node:fs';
import { expect, test, type Page, type Route } from '@playwright/test';

// The robot app's own settings page (reachy_app/medcare_reachy/static, served on port 8042 of the robot), loaded
// from disk under a made-up address with its API mocked. Two restarts at once used to leave two bridges driving the
// same robot, so the page sends one request at a time.
const STATIC = new URL('../../reachy_app/medcare_reachy/static/', import.meta.url);
const ROBOT = 'http://robot.test';
const TYPES: Record<string, string> = { html: 'text/html', js: 'text/javascript', css: 'text/css' };

const config = {
  app_url: 'http://192.168.1.20:8001', language: 'zh-TW', capture_fps: 15, vision_on_server: true,
  checkin_ack: true, checkin_gestures: true, device_token_set: true,
};
const running = { state: 'running', detail: 'Waiting for a medication reminder from the server.', since: 0 };
const stopping = { state: 'stopping', detail: 'The previous connection is still closing. Press Restart in a moment.', since: 0 };

function held() {
  let release!: () => void;
  const until = new Promise<void>(resolve => { release = resolve; });
  return { until, release };
}

async function openSettingsPage(page: Page, onPost: (path: string, route: Route) => Promise<void>,
                                status: () => object = () => running) {
  await page.route(`${ROBOT}/**`, async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'POST') return onPost(path, route);
    if (path === '/api/config') return route.fulfill({ json: config });
    if (path === '/api/status') return route.fulfill({ json: status() });
    const name = path === '/' ? 'index.html' : path.replace('/static/', '');
    return route.fulfill({ body: readFileSync(new URL(name, STATIC)), contentType: TYPES[name.split('.').pop() ?? ''] });
  });
  await page.goto(`${ROBOT}/`);
  await expect(page.locator('input[name="app_url"]')).toHaveValue(config.app_url);
  return {
    save: page.getByRole('button', { name: 'Save and connect' }),
    restart: page.getByRole('button', { name: 'Restart' }),
  };
}

test('a double click on Restart sends one request, and both buttons wait for its answer', async ({ page }) => {
  const answer = held();
  let restarts = 0;
  let current: object = running;
  const { save, restart } = await openSettingsPage(page, async (path, route) => {
    if (path === '/api/restart') restarts += 1;
    await answer.until;
    current = stopping;
    await route.fulfill({ json: stopping });
  }, () => current);
  await restart.dblclick();
  await expect(restart).toBeDisabled();
  await expect(save).toBeDisabled();
  answer.release();
  await expect(restart).toBeEnabled();
  await expect(save).toBeEnabled();
  await expect(page.locator('#state')).toHaveText('Stopping…');
  expect(restarts).toBe(1);
});

test('Save sends one request, even with Enter pressed meanwhile, and both buttons wait for its answer', async ({ page }) => {
  const answer = held();
  let saves = 0;
  const { save, restart } = await openSettingsPage(page, async (path, route) => {
    if (path === '/api/config') saves += 1;
    await answer.until;
    await route.fulfill({ json: { config, status: running } });
  });
  await save.click();
  await expect(save).toBeDisabled();
  await expect(restart).toBeDisabled();
  await page.locator('input[name="app_url"]').press('Enter');
  answer.release();
  await expect(save).toBeEnabled();
  await expect(restart).toBeEnabled();
  expect(saves).toBe(1);
});

test('a request that fails gives both buttons back', async ({ page }) => {
  let restarts = 0;
  const { save, restart } = await openSettingsPage(page, async (_path, route) => {
    restarts += 1;
    await route.abort();
  });
  await restart.click();
  await expect.poll(() => restarts).toBe(1);
  await expect(restart).toBeEnabled();
  await expect(save).toBeEnabled();
  await restart.click();
  await expect.poll(() => restarts).toBe(2);
});
```

- [ ] **Step 8: Run it and see it fail**

The config's web server (`vite preview`, port 4180) serves `frontend_source/dist`. This spec doesn't use that server,
but the config starts it anyway. If `frontend_source/dist` is missing, run `npm run build` first. If port 4180 is busy
(another Playwright run), wait and retry.

From `C:/Reachy_Mini/repo/MedAiCarePlus/frontend_source`:

```bash
npx playwright test --config "C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/pw-frontend.config.mjs" tests/robot-settings-page.spec.ts
```

Expected: `2 failed, 1 passed`.
- The two "one request" tests fail at their first `toBeDisabled()`: `Expected: disabled`, `Received: enabled`.
- "a request that fails gives both buttons back" already passes. It guards the `finally` added in the next step.

- [ ] **Step 9: Make the settings page send one request at a time**

In `reachy_app/medcare_reachy/static/main.js`, replace lines 1-11:

```js
const STATE_TEXT = {
  connecting_robot: "Connecting to the robot…",
  not_configured: "Not connected yet",
  starting: "Starting…",
  running: "Connected",
  stopped: "Stopped",
  error: "Error",
};

const form = document.getElementById("config");
const formError = document.getElementById("form-error");
```

with:

```js
const STATE_TEXT = {
  connecting_robot: "Connecting to the robot…",
  not_configured: "Not connected yet",
  starting: "Starting…",
  running: "Connected",
  stopping: "Stopping…",
  stopped: "Stopped",
  error: "Error",
};

const form = document.getElementById("config");
const formError = document.getElementById("form-error");
const saveButton = form.querySelector('button[type="submit"]');
const restartButton = document.getElementById("restart");

// Save and Restart both restart the robot's connection, so they send one request at a time: two at once
// (a double click) once left two connections driving the robot. Both buttons wait for the answer.
function oneAtATime(send) {
  return async (event) => {
    event.preventDefault();
    if (saveButton.disabled) return;
    saveButton.disabled = true;
    restartButton.disabled = true;
    try {
      await send();
    } finally {
      saveButton.disabled = false;
      restartButton.disabled = false;
    }
  };
}
```

Then replace the submit handler and the restart handler (old lines 60-89, from
`form.addEventListener("submit", async (event) => {` to the `});` after `show(await response.json());`) with the code
below. The submit body is unchanged. Only its first and last lines change, and `event.preventDefault()` moves into
`oneAtATime`:

```js
form.addEventListener("submit", oneAtATime(async () => {
  formError.textContent = "";
  const body = {
    app_url: form.app_url.value.trim(),
    device_token: form.device_token.value.trim(),
    language: form.language.value,
    capture_fps: Number(form.capture_fps.value),
    checkin_ack: form.checkin_ack.checked,
    checkin_gestures: form.checkin_gestures.checked,
  };
  const response = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    formError.textContent = data.detail || "Could not save.";
    return;
  }
  form.device_token.value = "";
  show(data.status);
  await loadConfig();
}));

restartButton.addEventListener("click", oneAtATime(async () => {
  const response = await fetch("/api/restart", { method: "POST" });
  show(await response.json());
}));
```

The last three lines of the file (`loadConfig().catch(...)`, `refreshStatus();`, `setInterval(refreshStatus, 3000);`)
stay as they are.

In `reachy_app/medcare_reachy/static/style.css`, replace line 18:

```css
.state[data-state="not_configured"], .state[data-state="starting"] { color: var(--warn); }
```

with:

```css
.state[data-state="not_configured"], .state[data-state="starting"], .state[data-state="stopping"] { color: var(--warn); }
```

Then add this line right after line 32
(`button.secondary { background: transparent; color: var(--accent); border: 1px solid var(--border); }`).
Without it, a disabled button looks the same as an enabled one, because the buttons set their own background:

```css
button:disabled { opacity: 0.55; cursor: progress; }
```

- [ ] **Step 10: Run the settings-page test and see it pass**

From `C:/Reachy_Mini/repo/MedAiCarePlus/frontend_source`, run the same command as in Step 8:

```bash
npx playwright test --config "C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/pw-frontend.config.mjs" tests/robot-settings-page.spec.ts
```

Expected: `3 passed`. It passed 4 of 4 runs during planning, about 4 s each.

- [ ] **Step 11: Look at the page while a request is pending (390 and 1280 px)**

Create this temporary file, `frontend_source/tests/robot-settings-shot.spec.ts`. **Don't commit it.**

```ts
import { readFileSync } from 'node:fs';
import { test } from '@playwright/test';

// Temporary visual check, not committed: the settings page while a Restart request is pending.
const STATIC = new URL('../../reachy_app/medcare_reachy/static/', import.meta.url);
const TYPES: Record<string, string> = { html: 'text/html', js: 'text/javascript', css: 'text/css' };
const OUT = process.env.SHOT_DIR ?? '.';

test('robot settings page while a restart is pending', async ({ page }) => {
  await page.route('http://robot.test/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'POST') return;   // never answered: the request stays pending
    if (path === '/api/config') {
      return route.fulfill({ json: { app_url: 'http://192.168.1.20:8001', language: 'zh-TW', capture_fps: 15,
                                     checkin_ack: true, checkin_gestures: true, device_token_set: true } });
    }
    if (path === '/api/status') {
      return route.fulfill({ json: { state: 'stopping', detail: 'The previous connection is still closing. Press Restart in a moment.', since: 0 } });
    }
    const name = path === '/' ? 'index.html' : path.replace('/static/', '');
    return route.fulfill({ body: readFileSync(new URL(name, STATIC)), contentType: TYPES[name.split('.').pop() ?? ''] });
  });
  for (const width of [390, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('http://robot.test/');
    await page.getByRole('button', { name: 'Restart' }).click();
    await page.waitForTimeout(300);
    await page.screenshot({ path: `${OUT}/robot-settings-${width}.png`, fullPage: true });
  }
});
```

Run it from `C:/Reachy_Mini/repo/MedAiCarePlus/frontend_source` (Git Bash), then delete it:

```bash
SHOT_DIR="C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad" npx playwright test --config "C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/pw-frontend.config.mjs" tests/robot-settings-shot.spec.ts
rm tests/robot-settings-shot.spec.ts
```

Open `robot-settings-390.png` and `robot-settings-1280.png` in the scratchpad. Pass when:
- both "Restart" and "Save and connect" are visibly faded;
- "Stopping…" is in the amber warning colour, with the detail line under it;
- the 390 px shot has no horizontal scroll.

- [ ] **Step 12: Check line endings and the working tree**

From `C:/Reachy_Mini/repo/MedAiCarePlus/reachy_app`:

```bash
../.venv/Scripts/python.exe -c "import sys; [print(f, open(f, 'rb').read().count(b'\r\n'), open(f, 'rb').read().count(b'\n')) for f in sys.argv[1:]]" medcare_reachy/service.py medcare_reachy/static/main.js medcare_reachy/static/style.css tests/test_app.py
cd .. && git status --short
```

Expected:
- For each file, the two numbers are equal, so the file is still all CRLF.
- `git status` lists exactly the four modified files, plus the new `frontend_source/tests/robot-settings-page.spec.ts`.
- `robot-settings-shot.spec.ts` is not listed.

- [ ] **Step 13: Commit**

From `C:/Reachy_Mini/repo/MedAiCarePlus` (Git Bash). The trailer names the model that writes the commit; this is the
current session's model:

```bash
git add reachy_app/medcare_reachy/service.py reachy_app/medcare_reachy/static/main.js reachy_app/medcare_reachy/static/style.css reachy_app/tests/test_app.py frontend_source/tests/robot-settings-page.spec.ts
git commit -F - <<'EOF'
Robot app: one bridge thread, even with overlapping restarts

Two restarts at once (a double click on the settings page) interleaved stop() and start(): the second
stop() forgot the thread the first start() had made, so two bridges drove the same robot until the app
exited. restart() now runs under its own lock, stop() keeps tracking a bridge thread until it has ended
(the page shows "Stopping…" meanwhile), start() refuses while one is alive, and a stop() that comes
while a bridge is still starting stops it as soon as it has a runner. The settings page sends one
request at a time: Save and Restart stay disabled until the answer.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
git log -1 --stat
```

Expected: one commit with the 5 files.

#### NOTES

**Deviations from the contract:** none. The contract item "restart() serialized by a new threading.Lock named
_restart_lock; never two bridge threads" is met exactly.

**Additions beyond the contract.** None of these are used by any other task.
- `_stop_requested`. Without it, a second restart that comes while the first one's new bridge is still starting would
  wait out the full 15 s `stop()` timeout. The bridge would then miss the shutdown signal and keep running with the
  older settings. Planning measured this with a fake bridge that takes 0.3-1.5 s to start.
- The service state `stopping` and its detail text.
- The WARNING in `start()`.
- `button:disabled` CSS.

**Deviation from the global "en and zh-TW" rule.** The robot app's settings page is English only today
(`static/index.html` has `lang="en"`, and no strings are translated). The two new strings are English only, like the
rest of that page:
- `"Stopping…"` in `main.js`.
- The detail text in `service.py`.

The rule applies to the web app's `i18n.ts`, which this task doesn't touch.

**Not in this task.** The version bump to 0.6.0 is not in this task. It is Task 2's Part F (spec A4); the deploy to
the robot is Task 10's Part B:
- `reachy_app/pyproject.toml:8` (`version = "0.5.4"`).
- `reachy_app/medcare_reachy/bridge/__init__.py:3` (`__version__ = "0.5.4"`).

No other file in `reachy_app` pins the version.

**Assumptions:**
- The settings server keeps running the endpoints in `web.py` as plain `def`, on a thread pool.
  - The robot runtime's own uvicorn does this (SDK 1.11.0 `reachy_mini/apps/app.py:106-132`, read on the robot).
  - That is why the lock lives in the service, not in the page.
- `Runner.request_shutdown()` stays idempotent and safe to call before `Runner.run()`. Verified in
  `bridge/runner.py:381-406`.

**Risks:**
- A bridge whose shutdown hangs longer than 15 s blocks restarts until it ends. This is the spec's choice: `start()`
  refuses while a bridge is alive.
  - The page then shows "Stopping…" with "Press Restart in a moment".
  - When the old thread ends, the state becomes "Stopped" (or "Error"), and the user presses Restart.
  - Starting a second bridge would bring back F9.
- Two overlapping requests now take longer. The second request returns only after both restarts.
  - Measured through a real uvicorn server with a fake bridge (0.3 s to start, 1 s to shut down): 1.7 s and 2.9 s.
  - On the robot, the shutdown includes `monitor_end`, `robot.sleep()` and `voice.stop()`. The buttons stay disabled
    meanwhile.
- **Not handled:** a restart request that arrives during app shutdown.
  - In that case, `run()`'s final `stop()` (`main.py:34-37`) races the request, so a bridge can start after the
    final stop.
  - The SDK then leaves the `with ReachyMini(...)` block, and the process exits. The thread is a daemon.
  - This is unlikely and has no lasting effect, so it is left as is.
- **Not changed (existing tiny window):**
  - If a bridge ends by itself between `stop()` reading `_loop` and calling `loop.call_soon_threadsafe(...)`, the loop
    may already be closed.
  - `_thread_main` closes the loop before it clears `_loop` (`service.py:152-157`).
  - The call then raises `RuntimeError: Event loop is closed`, and that request gets a 500.
  - The window is a few microseconds. Fixing it means reordering `_thread_main`'s `finally`, which is outside A1.
- Placement of the Playwright test: the robot page's test lives in `frontend_source/tests` and reads
  `../../reachy_app/medcare_reachy/static/`. If either folder moves, the spec's `STATIC` URL must follow.

**Facts verified during planning (file:line on HEAD `37ba84e`, branch `robot-camera-thinking`, clean tree):**
- **Current code:**
  - `service.py:110-122`: `stop()` sets `self._thread = None` unconditionally.
  - `service.py:113-114`: it signals only a bridge that has a runner.
  - `service.py:124-126`: `restart()` takes no lock.
  - `service.py:99-100`: `start()` already returns silently while a thread is alive.
- **Endpoints:** `web.py:23-41`: `save_config` and `restart` are `def` endpoints, and both call `service.restart()`.
- **Settings page:** `static/main.js:60-89`: the buttons are never disabled. `static/index.html:20` is the `Restart`
  button (`id="restart"`), and line 56 is the submit button "Save and connect".
- **Bridge start-up and shutdown:**
  - `service.py:48-64`: `default_bridge` does robot, clips, voice and speaker set-up and opens the server client before
    `on_runner(runner)`. Then `await runner.run()`.
  - `bridge/runner.py:381-406`: `request_shutdown` and `run()`.
- **The race, reproduced before the fix:**
  - The investigation script `scratchpad/inv_restart_race.py` printed 2 bridge threads (5 of 5 runs in the
    investigation).
  - The new race test failed 8 of 8 runs on the unchanged code.
  - Through a real uvicorn server with the real `web.register_routes`, an orphaned bridge (#2) survived a third restart
    and `service.stop()`.
- **After the change**, in a clean `git archive` copy of HEAD (scratch `plan-drafts/t1_verify`; the repo was not
  modified):
  - The script prints 1 bridge thread (3 of 3 runs).
  - The uvicorn probe leaves one bridge after two overlapping restarts, again after a third restart, and none after
    `stop()`.
  - Tested with a 0.3 s start and 1.0 s shutdown, both overlapping and back-to-back, and with a 1.5 s start and 0.5 s
    shutdown.
  - `tests/test_app.py` passed 27 of 27, 5 times.
  - The full robot suite gave 496 passed, 6 skipped, and 1 known speech flake.
- **Playwright:**
  - The spec fails 2 tests on the old page (`toBeDisabled`, received enabled) and passes 3 of 3 on the new page in 4
    runs.
  - The file filter `tests/robot-settings-page.spec.ts` works with an absolute `testDir`.
  - Screenshots at 390 and 1280 px were checked.
- **Baseline flakes:** at HEAD in a clean copy, the full suite gave 492 passed and 2 failed. The two failures were
  `test_speech.py::test_say_tells_when_its_first_sound_is_about_to_play` and
  `test_gestures.py::test_a_clip_handed_over_without_waiting_still_plays_before_the_next_one`, and both pass on rerun.
  The gestures test failed 2 of 6 isolated runs.
- **Line endings:** `git ls-files --eol` shows `i/lf w/crlf` for `service.py`, `main.js` and `test_app.py`.
  `style.css`, `web.py` and `index.html` are CRLF in the working tree too.
- **Ignore rules:** `frontend_source/test-results` and `frontend_source/dist` are git-ignored (`.gitignore:67`,
  `frontend_source/.gitignore:11`).

---

### Task 2: Robot app 0.6.0: camera watchdog, recovery, camera state in the heartbeat, early end of a search

Spec: `docs/superpowers/specs/2026-10-11-robot-camera-identity-thinking-design.md` sections A2, A3 and the version part
of A4. Work in `C:/Reachy_Mini/repo/MedAiCarePlus` on branch `robot-camera-thinking` (check with
`git -C C:/Reachy_Mini/repo/MedAiCarePlus branch --show-current`). This task touches only `reachy_app/`; it does not
deploy to the robot (the deploy and the live camera check are Task 10, Part B).

What it builds, in plain words: while the robot streams camera frames to the server, a small watchdog notices when no
new frame has come for 2 s, logs it once with the robot daemon's media status, asks the robot SDK to give the app its
camera back (at most once every 5 s), and gives up 15 s after the stall began. A search for the patient then ends at
once with task status `not_found` and the reason `camera_unavailable`. Every heartbeat carries the camera's state.

**Files:**
- Create: `reachy_app/medcare_reachy/bridge/camera_watch.py`
- Create: `reachy_app/medcare_reachy/bridge/tests/test_camera_watch.py`
- Create: `reachy_app/medcare_reachy/bridge/tests/test_media_recovery.py`
- Modify: `reachy_app/medcare_reachy/bridge/media.py`: imports (line 15), constants after `RECENTER_LIMIT` (line 36),
  `ReachyRobot.__init__` (lines 128-136), `get_camera_frame` / `get_audio` / `play_clip` (lines 175-217), two new
  methods before `gesture` (line 219)
- Modify: `reachy_app/medcare_reachy/bridge/app_client.py`: `heartbeat` (lines 171-172)
- Modify: `reachy_app/medcare_reachy/bridge/runner.py`: imports (lines 8-10), two helpers before
  `encode_stream_jpeg` (line 38), `MonitorStream` docstring and `__init__` (lines 76-124), `attach` / `detach`
  (lines 127-141), `step` (lines 185-207), `_stream_frame` (lines 209-247), new `_camera_seen` before
  `_note_camera_frame` (line 278), `Runner.heartbeat_once` (line 467)
- Modify: `reachy_app/medcare_reachy/bridge/session.py`: `_searching` (lines 209-218)
- Modify: `reachy_app/medcare_reachy/bridge/tests/fakes.py`: `FakeStream` (lines 171-176)
- Modify: `reachy_app/medcare_reachy/bridge/__init__.py` (line 3) and `reachy_app/pyproject.toml` (line 8): version
- Test: `reachy_app/medcare_reachy/bridge/tests/test_runner.py` (imports lines 5-9, `HeartbeatApp` / `StubStream`
  lines 437-454, `test_heartbeat_payload_and_stop_all` lines 472-479, `PollingApp.heartbeat` lines 543-544, new tests
  at the end, after line 669)
- Test: `reachy_app/medcare_reachy/bridge/tests/test_app_client.py` (one test at the end, after line 280)
- Test: `reachy_app/medcare_reachy/bridge/tests/test_session.py` (one parametrized test at the end, after line 494)

Every existing file here has CRLF line endings (`core.autocrlf=true`). The Edit tool keeps them. After creating a new
file with the Write tool, give it CRLF too (run from `C:/Reachy_Mini/repo/MedAiCarePlus/reachy_app`):

```bash
../.venv/Scripts/python.exe -c "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.write_bytes(p.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))" <path of the new file>
```

**Interfaces:**
- Consumes: nothing from other tasks. Task 1 (`medcare_reachy/service.py` restart lock) is independent of these files.
  The tests use the existing fakes (`medcare_reachy/bridge/tests/fakes.py`: `FakeClock`, `Harness`, `FakeStream`,
  `FakeClips`, `FakeRobot`, `make_task`, `dose`).
- Produces (the contract names, exactly):
  - `medcare_reachy.bridge.camera_watch`: `CAMERA_STALL_SECONDS = 2.0`, `CAMERA_RECOVER_INTERVAL = 5.0`,
    `CAMERA_GIVE_UP_SECONDS = 15.0`, `class CameraWatchdog(recover: Callable[[], str], media_status: Callable[[],
    dict | None], clock: Callable[[], float] = time.monotonic, stall_seconds: float = 2.0, recover_interval: float =
    5.0, give_up_seconds: float = 15.0, *, wall: Callable[[], float] = time.time)` with `attach()`, `detach()`,
    `frame()`, `tick() -> str`, property `state` in `{"idle", "ok", "stalled", "recovering", "unavailable"}`,
    `snapshot() -> {"state", "fps", "since", "media"}`. Also `NOT_WORKING` (the three states the card warns about)
    and `FPS_WINDOW`.
  - `ReachyRobot.reopen_camera() -> str` (`"acquired"`, `"reopened"` or `"failed: <short reason>"`, never raises) and
    `ReachyRobot.media_status() -> dict | None` (GET `http://127.0.0.1:8000/api/media/status`, 2 s timeout, None on
    any error).
  - `MonitorStream.camera` (one `CameraWatchdog` per stream; optional constructor keyword `camera=None`).
  - `AppClient.heartbeat(payload: dict, camera: dict | None = None)`: the JSON body gets `"camera"` only when given.
  - Heartbeat JSON from the robot: `{..., "camera": {"state": "ok", "fps": 9.9, "since": 1760180000.5, "media": null
    | {"available": bool, "released": bool, "no_media": bool}}}` (what the server's `CameraReport` validates).
  - When the watchdog gives up during SEARCHING: `POST /api/device/tasks/{id}/status` with `{"status": "not_found",
    "detail": {"reason": "camera_unavailable", "outcomes": {...}}}` (the existing not_found path adds `outcomes`).
  - `__version__ == "0.6.0"`; heartbeats report `bridge_version` 0.6.0.

Commands (run from `C:/Reachy_Mini/repo/MedAiCarePlus/reachy_app`):
- one file: `../.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/<file>.py -q -p no:cacheprovider`
- whole robot app suite: `../.venv/Scripts/python.exe -m pytest tests medcare_reachy/bridge/tests -q -p no:cacheprovider`
  (baseline before this task: 497 passed, 6 skipped, which is 494 at `37ba84e` plus Task 1's 3 tests in
  `tests/test_app.py`; `test_speech.py::test_say_tells_when_its_first_sound_is_about_to_play` flakes, and so, rarely, do
  `test_filler_echo.py::test_its_echo_goes_and_the_patient_s_next_words_stay` and
  `test_gestures.py::test_a_clip_handed_over_without_waiting_still_plays_before_the_next_one` (see Task 1, Step 6):
  rerun once before treating any of them as a failure).

---

#### Part A: the watchdog (pure logic)

- [ ] **Step 1: Write the failing watchdog tests**

Create `reachy_app/medcare_reachy/bridge/tests/test_camera_watch.py` (then give it CRLF, see above):

```python
"""CameraWatchdog (spec 2026-10-11 A2, A3): stall, recovery, give-up and the heartbeat snapshot, on a fake clock.

On 11 Oct 2026 the robot's camera reader gave one stale frame and then nothing, and the app searched for 10 minutes
without a log line: the TCU companion had released the daemon's camera.
"""

from medcare_reachy.bridge.camera_watch import (
    CAMERA_GIVE_UP_SECONDS, CAMERA_RECOVER_INTERVAL, CAMERA_STALL_SECONDS, CameraWatchdog)
from medcare_reachy.bridge.tests.fakes import FakeClock

LOGGER = "medcare_reachy.bridge.camera_watch"
RELEASED = {"available": False, "released": True, "no_media": False}
WALL = 1_760_000_000.0
STEP = 0.25   # a read every 0.25 s: exact in binary, so the timings below are exact too


class Recovery:
    """reopen_camera() stand-in: the clock time of each call; answers from `results` in turn (the last one repeats;
    an exception is raised). `during` runs inside the call, as another thread would while a recovery blocks."""

    def __init__(self, clock, *results, during=None):
        self.clock, self.results, self.during = clock, list(results) or ["reopened"], during
        self.calls = []

    def __call__(self):
        result = self.results[min(len(self.calls), len(self.results) - 1)]
        self.calls.append(self.clock.t)
        if self.during is not None:
            self.during()
        if isinstance(result, Exception):
            raise result
        return result


def make_watchdog(*results, media=RELEASED, during=None):
    clock = FakeClock()
    recovery = Recovery(clock, *results, during=during)

    def media_status():
        if isinstance(media, Exception):
            raise media
        return media

    watchdog = CameraWatchdog(recovery, media_status, clock=clock.monotonic, wall=lambda: WALL + clock.t)
    return watchdog, clock, recovery


def reads(watchdog, clock, seconds):
    """Reads that find no new frame, one every STEP s, for `seconds`; the states tick() returned."""
    states = []
    for _ in range(round(seconds / STEP)):
        clock.t += STEP
        states.append(watchdog.tick())
    return states


def lines(caplog, level):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelname == level]


def test_the_limits_are_the_spec_s():
    assert (CAMERA_STALL_SECONDS, CAMERA_RECOVER_INTERVAL, CAMERA_GIVE_UP_SECONDS) == (2.0, 5.0, 15.0)


def test_idle_without_a_session_never_tries_a_recovery():
    watchdog, clock, recovery = make_watchdog()
    assert watchdog.state == "idle"
    assert set(reads(watchdog, clock, 30)) == {"idle"}
    assert recovery.calls == []
    assert watchdog.snapshot() == {"state": "idle", "fps": 0.0, "since": WALL + 1000.0, "media": None}


def test_a_steady_camera_stays_ok_and_reports_its_fps():
    watchdog, clock, recovery = make_watchdog()
    watchdog.attach()
    for _ in range(120):        # 12 s of a 10 fps camera, with reads that find nothing in between
        clock.t += 0.05
        assert watchdog.tick() == "ok"
        clock.t += 0.05
        watchdog.frame()
    snapshot = watchdog.snapshot()
    assert snapshot["state"] == "ok" and 9.9 <= snapshot["fps"] <= 10.1
    assert snapshot["since"] == WALL + 1000.0 and snapshot["media"] is None   # ok since the session started
    assert recovery.calls == []


def test_a_slow_camera_is_not_a_stall():
    """Review focus 2: a camera that delivers 5 fps, or one 1.75 s gap, must not count as stalled."""
    watchdog, clock, recovery = make_watchdog()
    watchdog.attach()
    for _ in range(50):         # 10 s at 5 fps, with a read that finds nothing between two frames
        clock.t += 0.1
        assert watchdog.tick() == "ok"
        clock.t += 0.1
        watchdog.frame()
    assert set(reads(watchdog, clock, 1.75)) == {"ok"}
    watchdog.frame()
    assert recovery.calls == []


def test_two_seconds_without_a_frame_is_one_stall_logged_with_the_daemon_s_media_status(caplog):
    watchdog, clock, recovery = make_watchdog()
    watchdog.attach()
    watchdog.frame()
    with caplog.at_level("INFO", logger=LOGGER):
        states = reads(watchdog, clock, 1.75)
        assert set(states) == {"ok"} and recovery.calls == []
        assert reads(watchdog, clock, STEP) == ["recovering"]    # 2.0 s: stalled, and a recovery at once
        reads(watchdog, clock, 4)
    assert recovery.calls == [1002.0]
    [error] = lines(caplog, "ERROR")
    assert "no new frame for 2.0 s" in error and "'released': True" in error
    assert watchdog.snapshot()["media"] == RELEASED


def test_recovery_is_tried_at_most_once_per_interval_until_it_gives_up(caplog):
    watchdog, clock, recovery = make_watchdog()
    watchdog.attach()
    with caplog.at_level("INFO", logger=LOGGER):
        states = reads(watchdog, clock, 30)
    assert recovery.calls == [1002.0, 1007.0, 1012.0]             # the stall began at 1002: given up at 1017
    given_up = states.index("unavailable")                       # states[i] is the read at 1000 + (i + 1) * STEP
    assert given_up == round(17 / STEP) - 1 and states[given_up - 1] == "recovering"
    assert len([line for line in lines(caplog, "ERROR") if line.startswith("camera unavailable")]) == 1
    assert len([line for line in lines(caplog, "ERROR") if line.startswith("camera stalled")]) == 1


def test_a_failed_attempt_leaves_the_camera_stalled_and_a_good_one_recovering(caplog):
    watchdog, clock, recovery = make_watchdog("failed: the daemon did not give the media back", "acquired")
    watchdog.attach()
    with caplog.at_level("INFO", logger=LOGGER):
        assert reads(watchdog, clock, 2)[-1] == "stalled"
        assert watchdog.snapshot()["state"] == "stalled"
        assert reads(watchdog, clock, 5)[-1] == "recovering"
    warnings = lines(caplog, "WARNING")
    assert "camera recovery attempt 1 failed: failed: the daemon did not give the media back" in warnings
    assert "camera recovery attempt 2: acquired; waiting for a new frame" in lines(caplog, "INFO")


def test_a_recovery_or_a_status_read_that_raises_is_logged_and_counted_and_never_raises(caplog):
    watchdog, clock, recovery = make_watchdog(RuntimeError("gstreamer died"), media=OSError("daemon gone"))
    watchdog.attach()
    with caplog.at_level("INFO", logger=LOGGER):
        states = reads(watchdog, clock, 18)
    assert states[round(2 / STEP) - 1] == "stalled" and states[-1] == "unavailable"
    assert len(recovery.calls) == 3
    assert any("camera recovery raised" in r.getMessage() and r.exc_info for r in caplog.records)
    assert any("could not read the daemon's media status" in line for line in lines(caplog, "WARNING"))
    assert "3 recovery attempt(s), 3 failed" in [line for line in lines(caplog, "ERROR")
                                                  if line.startswith("camera unavailable")][0]
    assert watchdog.snapshot()["media"] is None


def test_a_new_frame_after_a_stall_logs_its_length_and_starts_afresh(caplog):
    watchdog, clock, recovery = make_watchdog()
    watchdog.attach()
    with caplog.at_level("INFO", logger=LOGGER):
        reads(watchdog, clock, 3.5)
        watchdog.frame()
        assert watchdog.state == "ok" and watchdog.snapshot()["media"] is None
        assert "camera recovered: a new frame 3.5 s after the last one (1 recovery attempt(s), 0 failed)" in \
            lines(caplog, "INFO")
        reads(watchdog, clock, 2)                                 # a second stall: logged and recovered at once
    assert len([line for line in lines(caplog, "ERROR") if line.startswith("camera stalled")]) == 2
    assert recovery.calls == [1002.0, 1005.5]


def test_the_state_gives_up_on_time_even_while_a_recovery_blocks():
    """The slot session reads `state` on the event loop while tick() runs a recovery in a worker thread."""
    seen = []
    watchdog, clock, recovery = make_watchdog(during=lambda: (setattr(clock, "t", clock.t + 16),
                                                              seen.append(watchdog.state)))
    watchdog.attach()
    reads(watchdog, clock, 2)
    assert seen == ["unavailable"]                 # 15 s after the stall began, during the 16 s recovery
    assert watchdog.state == "unavailable"


def test_unavailable_outlasts_its_session_until_the_next_one_starts():
    watchdog, clock, recovery = make_watchdog("failed: the daemon runs without media")
    watchdog.attach()
    reads(watchdog, clock, 17)
    assert watchdog.state == "unavailable"
    watchdog.detach()                                                # the search ended
    assert watchdog.state == "unavailable"
    assert watchdog.snapshot()["media"] == RELEASED                 # the card can still say why
    since = watchdog.snapshot()["since"]
    clock.t += 60
    assert watchdog.snapshot()["since"] == since
    watchdog.attach()                                                # the next task: a fresh try
    assert watchdog.state == "ok" and watchdog.snapshot()["media"] is None
    assert set(reads(watchdog, clock, 1.75)) == {"ok"}
    calls = len(recovery.calls)
    reads(watchdog, clock, STEP)
    assert len(recovery.calls) == calls + 1                          # the new session's stall tries again at once


def test_detach_otherwise_makes_the_camera_idle():
    watchdog, clock, recovery = make_watchdog()
    watchdog.attach()
    reads(watchdog, clock, 3)
    assert watchdog.state == "recovering"
    watchdog.detach()
    assert watchdog.state == "idle" and watchdog.snapshot()["media"] is None
    assert set(reads(watchdog, clock, 20)) == {"idle"} and len(recovery.calls) == 1


def test_a_recovery_that_outlives_its_session_changes_nothing():
    watchdog, clock, recovery = make_watchdog("failed: no robot connection", during=lambda: watchdog.detach())
    watchdog.attach()
    assert reads(watchdog, clock, 2)[-1] == "idle"
    assert watchdog.snapshot() == {"state": "idle", "fps": 0.0, "since": WALL + 1002.0, "media": None}


def test_since_is_the_wall_time_of_the_last_state_change():
    watchdog, clock, recovery = make_watchdog("failed: x")
    assert watchdog.snapshot()["since"] == WALL + 1000.0
    clock.t = 1010.0
    watchdog.attach()
    assert watchdog.snapshot()["since"] == WALL + 1010.0
    reads(watchdog, clock, 2)                                        # stalled at 1012 (via recovering, then failed)
    assert watchdog.snapshot()["since"] == WALL + 1012.0
    reads(watchdog, clock, 1)
    assert watchdog.snapshot()["since"] == WALL + 1012.0             # no change, no new time
```

- [ ] **Step 2: Run it and see it fail**

Run: `../.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_camera_watch.py -q -p no:cacheprovider`
Expected: 1 error during collection, `ModuleNotFoundError: No module named 'medcare_reachy.bridge.camera_watch'`.

- [ ] **Step 3: Write the watchdog**

Create `reachy_app/medcare_reachy/bridge/camera_watch.py` (then give it CRLF):

```python
"""Camera watchdog (spec 2026-10-11 A2, A3): notices when the camera stops handing this app new frames while a
monitor session streams, asks the robot for its camera back, and gives up after a while, so a search ends with a
reason instead of looking around blind for 10 minutes.

On 11 Oct 2026 the TCU companion connected with media_backend="no_media", which in reachy_mini 1.11.0 releases the
daemon's camera. The SDK's camera reader then gave one stale frame and None for ever, and the app searched for 10
minutes without a log line.

Pure logic: the clock, the recovery and the daemon's media status are passed in, so the tests run on a fake clock.
tick() may block for as long as `recover` takes (a daemon release/acquire cycle): call it off the event loop.
"""

import logging
import threading
import time
from collections import deque
from typing import Callable

log = logging.getLogger(__name__)

CAMERA_STALL_SECONDS = 2.0       # no new frame this long while a session streams: the camera has stalled
CAMERA_RECOVER_INTERVAL = 5.0    # at most one recovery attempt per this long
CAMERA_GIVE_UP_SECONDS = 15.0    # no new frame this long after the stall began: the camera is unavailable
FPS_WINDOW = 10.0                # the heartbeat's camera fps, over the same window as the stream's other rates

IDLE, OK, STALLED, RECOVERING, UNAVAILABLE = "idle", "ok", "stalled", "recovering", "unavailable"
NOT_WORKING = frozenset({STALLED, RECOVERING, UNAVAILABLE})   # the Reachy card warns about these


class CameraWatchdog:
    """The camera's state while a monitor session streams:

    - idle: no session, so the camera isn't read (0 fps is normal);
    - ok: new frames keep coming (a slow camera is ok: only a gap of stall_seconds counts);
    - stalled: no new frame for stall_seconds, or the last recovery attempt failed;
    - recovering: a recovery attempt is running, or succeeded and the first new frame is awaited;
    - unavailable: no new frame for give_up_seconds since the stall began. It outlasts the session (detach() keeps
      it, so the heartbeat still reports it while the robot sleeps) until the next session starts a fresh try.

    The stream calls attach()/detach() with its session, frame() for every new frame and tick() for every read that
    found none. Only tick() calls `recover` and `media_status`, outside the lock, so the heartbeat's snapshot() and the
    slot session's `state` never wait for a recovery.
    """

    def __init__(self, recover: Callable[[], str], media_status: Callable[[], dict | None],
                 clock: Callable[[], float] = time.monotonic, stall_seconds: float = CAMERA_STALL_SECONDS,
                 recover_interval: float = CAMERA_RECOVER_INTERVAL, give_up_seconds: float = CAMERA_GIVE_UP_SECONDS,
                 *, wall: Callable[[], float] = time.time):
        self._recover, self._media_status = recover, media_status
        self.clock, self.wall = clock, wall
        self.stall_seconds, self.recover_interval = stall_seconds, recover_interval
        self.give_up_seconds = give_up_seconds
        self._lock = threading.Lock()
        self._frames: deque = deque()   # when each new frame came, for the fps
        self._state = IDLE
        self._since = wall()            # unix seconds of the last state change
        self._media: dict | None = None   # the daemon's media status, as read during the current stall
        self._generation = 0            # bumped by attach() and detach(): a late recovery result is ignored
        self._reset(clock())

    # ── the stream's interface ───────────────────────────────────────────
    @property
    def state(self) -> str:
        with self._lock:
            self._give_up_if_due(self.clock())   # also while a recovery blocks in tick()
            return self._state

    def attach(self) -> None:
        """A monitor session starts streaming: a fresh try, whatever happened before."""
        with self._lock:
            self._generation += 1
            self._reset(self.clock())
            self._frames.clear()
            self._media = None
            self._set(OK)

    def detach(self) -> None:
        """No session any more: the camera isn't read. "unavailable" stays until the next attach()."""
        with self._lock:
            self._generation += 1
            self._give_up_if_due(self.clock())
            if self._state != UNAVAILABLE:
                self._media = None
                self._set(IDLE)

    def frame(self) -> None:
        """A new frame came: any stall is over."""
        with self._lock:
            now = self.clock()
            self._frames.append(now)
            self._prune(now)
            if self._state in NOT_WORKING:
                log.info("camera recovered: a new frame %.1f s after the last one (%d recovery attempt(s), %d failed)",
                         now - self._last_frame, self._attempts, self._failures)
                self._media = None
                self._set(OK)
            self._reset(now)

    def tick(self) -> str:
        """A read found no new frame. Notices a stall (one ERROR line, with the daemon's media status), tries a
        recovery at most once per recover_interval, and gives up give_up_seconds after the stall began.
        Never raises; returns the state."""
        try:
            return self._tick()
        except Exception:   # the watchdog must never break the stream
            log.exception("camera watchdog failed")
            return self._state

    def snapshot(self) -> dict:
        """For the heartbeat: the state, new frames per second over the last FPS_WINDOW s, the unix time of the last
        state change, and the daemon's media status read during the current stall (None otherwise)."""
        with self._lock:
            now = self.clock()
            self._give_up_if_due(now)
            self._prune(now)
            return {"state": self._state, "fps": round(len(self._frames) / FPS_WINDOW, 1), "since": self._since,
                    "media": None if self._media is None else dict(self._media)}

    # ── inside ───────────────────────────────────────────────────────────
    def _tick(self) -> str:
        with self._lock:
            now = self.clock()
            self._give_up_if_due(now)
            if self._state in (IDLE, UNAVAILABLE):
                return self._state
            new_stall = self._state == OK and now - self._last_frame >= self.stall_seconds
            if self._state == OK and not new_stall:
                return OK
            if new_stall:
                self._stall_since = now
                self._set(STALLED)
            if now - self._last_attempt < self.recover_interval:
                return self._state
            self._last_attempt = now
            self._attempts += 1
            attempt, generation, gap = self._attempts, self._generation, now - self._last_frame
            self._set(RECOVERING)
        media = self._read_media_status()   # HTTP and the recovery run without the lock
        if new_stall:
            log.error("camera stalled: no new frame for %.1f s while a session streams; daemon media status: %s",
                      gap, media)
        result = self._attempt_recovery()
        failed = result.startswith("failed")
        if failed:
            log.warning("camera recovery attempt %d failed: %s", attempt, result)
        else:
            log.info("camera recovery attempt %d: %s; waiting for a new frame", attempt, result)
        with self._lock:
            if generation != self._generation:
                return self._state   # the session ended or restarted meanwhile: this result no longer applies
            self._media = media
            if failed:
                self._failures += 1
            self._give_up_if_due(self.clock())
            if failed and self._state == RECOVERING:
                self._set(STALLED)
            return self._state

    def _read_media_status(self) -> dict | None:
        try:
            status = self._media_status()
        except Exception as exc:
            log.warning("could not read the daemon's media status: %r", exc)
            return None
        return status if isinstance(status, dict) else None

    def _attempt_recovery(self) -> str:
        try:
            return str(self._recover())
        except Exception as exc:   # counted as a failed attempt; the stream goes on
            log.exception("camera recovery raised")
            return f"failed: {exc!r}"

    def _give_up_if_due(self, now: float) -> None:
        """With the lock held."""
        if self._state in (STALLED, RECOVERING) and now - self._stall_since >= self.give_up_seconds:
            log.error("camera unavailable: no new frame for %.1f s since the stall began (%d recovery attempt(s), "
                      "%d failed); a search ends with camera_unavailable",
                      now - self._stall_since, self._attempts, self._failures)
            self._set(UNAVAILABLE)

    def _reset(self, now: float) -> None:
        self._last_frame = now
        self._stall_since = None
        self._last_attempt = float("-inf")
        self._attempts = 0
        self._failures = 0

    def _set(self, state: str) -> None:
        if state != self._state:
            self._state, self._since = state, self.wall()

    def _prune(self, now: float) -> None:
        while self._frames and self._frames[0] < now - FPS_WINDOW:
            self._frames.popleft()
```

- [ ] **Step 4: Run the watchdog tests**

Run: `../.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_camera_watch.py -q -p no:cacheprovider`
Expected: `14 passed`.

- [ ] **Step 5: Commit**

```bash
cd C:/Reachy_Mini/repo/MedAiCarePlus
git add reachy_app/medcare_reachy/bridge/camera_watch.py reachy_app/medcare_reachy/bridge/tests/test_camera_watch.py
git commit -m "Robot app: camera watchdog (stall after 2 s, recovery every 5 s, give up after 15 s)" -m "Pure logic with an injectable clock; logs one ERROR per stall with the daemon's media status and one INFO on recovery. Spec 2026-10-11 A2, A3." -m "Co-Authored-By: <the model that wrote the commit> <noreply@anthropic.com>"
```

(Write your own model's name in the last line, as the global constraints say.)

---

#### Part B: getting the camera back through the SDK (`media.py`)

How the SDK on the robot works (reachy_mini 1.11.0, read on the robot, see NOTES): `ReachyMini.acquire_media()` does
nothing unless this app's own `ReachyMini` object is marked released, so after another program (the TCU companion)
released the daemon's camera, `acquire_media()` alone is a no-op. The public way is `release_media()` (closes this
app's media manager, marks it released, and asks the daemon to release, which the daemon ignores when it already has)
followed by `acquire_media()` (asks the daemon for its media back, which restarts the daemon's pipeline, and builds a
fresh media manager). No private SDK call is used, so no version check is needed. The media manager also holds the
speaker and the microphone, so the recovery waits for a clip still playing, holds the playback lock, and restarts the
fresh manager's audio pipeline (the app never calls `start_recording()` itself: today every clip's
`start_playing()` starts the shared record-and-play pipeline).

- [ ] **Step 6: Write the failing media tests**

Create `reachy_app/medcare_reachy/bridge/tests/test_media_recovery.py` (then give it CRLF):

```python
"""ReachyRobot.reopen_camera() and media_status(): the camera watchdog's recovery (spec 2026-10-11 A2) through the
SDK's public release_media() and acquire_media() (reachy_mini 1.11.0, reachy_mini.py:203-240)."""

import time

import httpx
import numpy as np
import pytest

from medcare_reachy.bridge import media as media_module
from medcare_reachy.bridge.media import ReachyRobot

RELEASED = {"available": False, "released": True, "no_media": False}
AVAILABLE = {"available": True, "released": False, "no_media": False}


class Media:
    """One SDK media manager: hands out frames and microphone samples until it is closed."""

    def __init__(self, name):
        self.name, self.closed, self.recording, self.pushed = name, False, False, 0

    def get_frame(self):
        return None if self.closed else np.zeros((720, 1280, 3), np.uint8)

    def get_audio_sample(self):
        return None if self.closed else np.zeros((160, 2), np.float32)

    def get_input_audio_samplerate(self):
        return 16000

    def get_output_audio_samplerate(self):
        return 16000

    def start_playing(self):
        pass

    def push_audio_sample(self, samples):
        self.pushed += 1

    def start_recording(self):
        self.recording = True

    def close(self):
        self.closed = True


class MediaMini:
    """ReachyMini's media calls as SDK 1.11.0 makes them: release_media() closes the manager and marks it released
    (once); acquire_media() does nothing unless released, else asks the daemon and builds a fresh manager, or logs and
    stays released when the daemon refuses (`gives_back=False`)."""

    def __init__(self, gives_back=True, error=None):
        self.calls, self.gives_back, self.error = [], gives_back, error
        self.media_released = False
        self.managers = [Media("first")]

    @property
    def media(self):
        return self.managers[-1]

    def release_media(self):
        self.calls.append("release_media")
        if self.error is not None:
            raise self.error
        if self.media_released:
            return
        self.media.close()
        self.media_released = True

    def acquire_media(self):
        self.calls.append("acquire_media")
        if not self.media_released or not self.gives_back:
            return
        self.media.close()
        self.managers.append(Media(f"fresh {len(self.managers)}"))
        self.media_released = False


def robot_with(mini, status=RELEASED):
    robot = ReachyRobot.attach(mini)
    robot.media_status = lambda: status
    return robot


def test_a_camera_the_daemon_released_is_acquired_back_with_a_fresh_media_manager():
    mini = MediaMini()
    robot = robot_with(mini, RELEASED)
    assert robot.reopen_camera() == "acquired"
    assert mini.calls == ["release_media", "acquire_media"]
    assert mini.managers[0].closed and mini.media.name == "fresh 1"
    assert mini.media.recording                       # the microphone's pipeline runs again
    assert robot.get_camera_frame() is not None       # read from the fresh manager


def test_a_stale_reader_of_a_daemon_that_has_its_media_is_reopened():
    mini = MediaMini()
    robot = robot_with(mini, AVAILABLE)
    assert robot.reopen_camera() == "reopened"
    assert mini.calls == ["release_media", "acquire_media"] and mini.media.name == "fresh 1"


def test_a_refused_acquire_fails_and_the_next_attempt_only_asks_again():
    mini = MediaMini(gives_back=False)
    robot = robot_with(mini, RELEASED)
    assert robot.reopen_camera() == "failed: the daemon did not give the media back"
    assert mini.media_released
    # Meanwhile nothing reads the closed manager (the SDK would log a warning on every call).
    assert robot.get_camera_frame() is None and robot.get_audio() is None
    mini.gives_back = True
    assert robot.reopen_camera() == "acquired"
    assert mini.calls == ["release_media", "acquire_media", "acquire_media"]
    assert robot.get_camera_frame() is not None and robot.get_audio() is not None


def test_a_daemon_without_media_is_not_cycled():
    mini = MediaMini()
    robot = robot_with(mini, {"available": False, "released": False, "no_media": True})
    assert robot.reopen_camera() == "failed: the daemon runs without media"
    assert mini.calls == []


def test_an_sdk_error_is_a_failed_attempt_never_an_exception(caplog):
    mini = MediaMini(error=RuntimeError("daemon unreachable"))
    robot = robot_with(mini, None)
    with caplog.at_level("ERROR", logger="medcare_reachy.bridge.media"):
        assert robot.reopen_camera() == "failed: RuntimeError('daemon unreachable')"
    assert any("camera recovery failed" in r.getMessage() for r in caplog.records)


def test_no_connection_cannot_reopen():
    robot = ReachyRobot("192.168.1.50")
    assert robot.reopen_camera() == "failed: no robot connection"


def test_a_recovery_waits_for_the_clip_still_playing():
    mini = MediaMini()
    robot = robot_with(mini, AVAILABLE)
    robot._quiet_at = time.monotonic() + 0.3      # a clip still playing for 0.3 s
    began = time.perf_counter()                   # monotonic() ticks every 15.6 ms on Windows
    assert robot.reopen_camera() == "reopened"
    assert time.perf_counter() - began >= 0.25


def test_a_clip_after_a_recovery_plays_on_the_fresh_manager(tmp_path):
    import wave

    path = tmp_path / "thanks.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(np.zeros(1600, "<i2").tobytes())
    mini = MediaMini()
    robot = robot_with(mini, RELEASED)
    robot.reopen_camera()
    assert robot.play_clip(path, wait=False) is True
    assert mini.media.pushed > 0 and mini.managers[0].pushed == 0
    mini.media_released = True                                    # a recovery between its release and its acquire
    assert robot.play_clip(path, wait=False) is False


class Response:
    def __init__(self, status=200, body=None):
        self.status_code, self.body = status, body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


@pytest.mark.parametrize("answer, expected", [
    (Response(200, RELEASED), RELEASED),
    (Response(503, {"detail": "Backend not running"}), None),
    (Response(200, ValueError("not JSON")), None),
    (Response(200, ["not", "a", "dict"]), None),
    (httpx.ConnectError("refused"), None),
])
def test_media_status_reads_the_local_daemon_within_2_s(monkeypatch, answer, expected):
    seen = []

    def get(url, timeout):
        seen.append((url, timeout))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(media_module.httpx, "get", get)
    assert ReachyRobot.attach(MediaMini()).media_status() == expected
    assert seen == [("http://127.0.0.1:8000/api/media/status", 2.0)]
```

- [ ] **Step 7: Run them and see them fail**

Run: `../.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_media_recovery.py -q -p no:cacheprovider`
Expected: `13 failed`: eight with `AttributeError: 'ReachyRobot' object has no attribute 'reopen_camera'`, five with
`AttributeError: module 'medcare_reachy.bridge.media' has no attribute 'httpx'`.

- [ ] **Step 8: Implement `media_status()`, `reopen_camera()` and the released-media guard in `media.py`**

8a. Imports (line 15). Replace

```python
import numpy as np

from medcare_reachy.bridge import moves
```

with

```python
import httpx
import numpy as np

from medcare_reachy.bridge import moves
```

8b. Constants (after line 36). Replace

```python
RECENTER_LIMIT = {"head_deg": 15.0, "head_mm": 20.0, "body_deg": 8.0, "antenna_deg": 30.0}
```

with

```python
RECENTER_LIMIT = {"head_deg": 15.0, "head_mm": 20.0, "body_deg": 8.0, "antenna_deg": 30.0}
DAEMON_PORT = 8000
MEDIA_STATUS_TIMEOUT = 2.0   # seconds: the camera watchdog logs the daemon's media status with every stall
```

8c. `ReachyRobot.__init__` (lines 128-136) becomes:

```python
    def __init__(self, host: str):
        self.host = host
        self._mini = None
        self._owned = True              # False when a Reachy Mini app runtime owns the connection
        daemon_host = "127.0.0.1" if host == "localhost" else host
        self._media_status_url = f"http://{daemon_host}:{DAEMON_PORT}/api/media/status"
        self._lock = threading.Lock()   # audio: the SDK is driven from worker threads
        self._quiet_at = 0.0            # when the last clip handed to the speaker has finished playing
        # Gestures start only after hold_head() and stop for every other move (wake, sleep, look_around, close).
        self._gestures = Gestures(self._send_gesture, ready=self._motors_on, start_from=self._start_pose,
                                  move=thinking_move())
```

8d. Replace everything from `def get_frame(self)` (line 171) through the end of `play_clip` (line 217) with the block
below. `get_frame` is unchanged; `_media` is new; `get_camera_frame`, `get_audio` and `play_clip` now go through
`_media()` (`play_clip` looks the manager up again inside its lock, because a recovery may have replaced it while the
clip waited); `media_status`, `reopen_camera` and `_renew_media` are new. `def gesture(` (old line 219) follows this
block unchanged.

```python
    def get_frame(self) -> np.ndarray | None:
        frame = self.get_camera_frame()
        return None if frame is None else crop_4_3(frame)

    def _media(self):
        """The SDK's media manager now, or None: no connection, or this app's media is released (during a camera
        recovery, or after one whose acquire failed; the SDK would log a warning on every call then).

        acquire_media() replaces the manager, so it is looked up on every call, never kept.
        """
        if self._mini is None or getattr(self._mini, "media_released", False):
            return None
        return self._mini.media

    def get_camera_frame(self) -> np.ndarray | None:
        """The camera's 1280x720 BGR frame, or None when no new one came within ~20 ms.

        The daemon shares the camera with apps at its own rate: 10 fps on reachy_mini 1.11 (IPC_FPS), whatever
        this app asks for.
        """
        media = self._media()
        return None if media is None else media.get_frame()

    def get_audio(self) -> tuple[np.ndarray, int] | None:
        """The microphone samples received since the last call (float32) and their rate, or None."""
        media = self._media()
        if media is None:
            return None
        samples = media.get_audio_sample()
        if samples is None or len(samples) == 0:
            return None
        return np.asarray(samples, dtype=np.float32), int(media.get_input_audio_samplerate())

    def play_clip(self, path: Path, wait: bool = True) -> bool:
        """Play a WAV, blocking for its length; `wait=False` returns as soon as it is handed to the speaker.

        A clip never starts while the previous one is still playing: the SDK's start_playing() restarts the
        stream's timestamps, so overlapping clips would clip or mix.
        """
        media = self._media()
        if media is None:
            return False
        samples, rate = read_wav(Path(path))
        out_rate = int(media.get_output_audio_samplerate())
        samples = resample(samples, rate, out_rate)
        seconds = len(samples) / out_rate
        with self._lock:
            time.sleep(max(0.0, self._quiet_at - time.monotonic()))
            media = self._media()   # a camera recovery may have replaced the manager while this waited
            if media is None:
                return False
            media.start_playing()
            for start in range(0, len(samples), self.CHUNK):
                media.push_audio_sample(samples[start:start + self.CHUNK])
            self._quiet_at = time.monotonic() + seconds
        if wait:
            # Playback is asynchronous in the SDK; keep the robot quiet for the clip's length.
            time.sleep(seconds)
        return True

    def media_status(self) -> dict | None:
        """The daemon's GET /api/media/status ({"available", "released", "no_media"}), or None on any error."""
        try:
            response = httpx.get(self._media_status_url, timeout=MEDIA_STATUS_TIMEOUT)
            response.raise_for_status()
            status = response.json()
        except Exception as exc:   # the daemon busy or gone, or not JSON: the status is only logged
            log.warning("daemon media status unavailable: %r", exc)
            return None
        return status if isinstance(status, dict) else None

    def reopen_camera(self) -> str:
        """Get the camera feed back after it stalled (the camera watchdog's recovery, spec 2026-10-11 A2).

        Uses the SDK's public media calls (reachy_mini 1.11.0, reachy_mini.py:203-240):
        - release_media() closes this app's media manager and marks it released; it also asks the daemon to release
          its media, which the daemon ignores when it already has (daemon.py:145-146), e.g. after the TCU companion
          released it;
        - acquire_media() asks the daemon for its media back (daemon.py:155-169 restarts its pipeline) and builds a
          fresh media manager. On its own it does nothing unless this app's media is marked released, hence the
          release first.
        Returns "acquired" (the daemon had released its media, or lost it), "reopened" (the daemon had it: the cycle
        renewed this app's reader) or "failed: <reason>". Never raises.

        The media manager holds the speaker and the microphone too, so this waits for a clip still playing, holds the
        playback lock throughout, and starts the fresh manager's audio pipeline, which every clip used to leave
        running (play_clip and get_audio look the manager up on every call).
        """
        mini = self._mini
        if mini is None:
            return "failed: no robot connection"
        try:
            with self._lock:
                time.sleep(max(0.0, self._quiet_at - time.monotonic()))   # never cut a clip short
                return self._renew_media(mini)
        except Exception as exc:   # a failed attempt: the watchdog tries again later
            log.exception("camera recovery failed")
            return f"failed: {exc!r}"

    def _renew_media(self, mini) -> str:
        """reopen_camera's release/acquire cycle, with the playback lock held."""
        status = None
        if getattr(mini, "media_released", False):
            outcome = "acquired"   # an earlier attempt released this app's media and the daemon kept it: ask again
        else:
            status = self.media_status()
            if status is not None and status.get("no_media"):
                return "failed: the daemon runs without media"
            daemon_has_it = status is None or (status.get("available") and not status.get("released"))
            outcome = "reopened" if daemon_has_it else "acquired"
            mini.release_media()
        mini.acquire_media()
        if getattr(mini, "media_released", False):
            return "failed: the daemon did not give the media back"
        try:
            mini.media.start_recording()
        except Exception:   # the camera is back; the microphone restarts with the next clip anyway
            log.exception("the microphone did not restart after the camera recovery")
        # The fresh manager is LOCAL when the daemon's camera socket is there; a "webrtc" here means it wasn't yet.
        log.info("camera recovery: %s with the SDK's release_media() and acquire_media() (daemon media status %s, "
                 "media backend %s)", outcome, status, getattr(mini.media, "backend", None))
        return outcome
```

Do not change the `Robot` Protocol or `VideoFileRobot`: the stream looks `reopen_camera` / `media_status` up with
`getattr` and has a fallback for robots without them (Part D).

- [ ] **Step 9: Run the media tests and the existing tests of the robot facade**

Run: `../.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_media_recovery.py medcare_reachy/bridge/tests/test_clips_media_config.py medcare_reachy/bridge/tests/test_gestures.py medcare_reachy/bridge/tests/test_thinking_move.py medcare_reachy/bridge/tests/test_gesture_recenter.py -q -p no:cacheprovider`
Expected: all pass (`test_reachy_sdk_is_not_imported_by_the_package` still passes: `httpx` is not the SDK; the fake
minis there have no `media_released`, which `_media()` reads with `getattr(..., False)`).

- [ ] **Step 10: Commit**

```bash
cd C:/Reachy_Mini/repo/MedAiCarePlus
git add reachy_app/medcare_reachy/bridge/media.py reachy_app/medcare_reachy/bridge/tests/test_media_recovery.py
git commit -m "Robot app: reopen the camera through the SDK's release_media() and acquire_media()" -m "media_status() reads the daemon's GET /api/media/status (2 s). The recovery waits for a playing clip, holds the playback lock, restarts the microphone pipeline, and frames, audio and clips are never read from a released media manager." -m "Co-Authored-By: <the model that wrote the commit> <noreply@anthropic.com>"
```

---

#### Part C: the heartbeat carries the camera (`app_client.py`)

- [ ] **Step 11: Write the failing client test**

Append to the end of `reachy_app/medcare_reachy/bridge/tests/test_app_client.py` (after line 280; `json`, `httpx`,
`make_client` and `run` are already there):

```python


def test_heartbeat_sends_the_camera_only_when_given():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"stop_all": False})

    camera = {"state": "stalled", "fps": 0.0, "since": 1760000000.0,
              "media": {"available": False, "released": True, "no_media": False}}

    async def scenario():
        async with make_client(handler) as client:
            await client.heartbeat({"robot_reachable": True})
            await client.heartbeat({"robot_reachable": True}, camera=camera)

    run(scenario())
    assert bodies == [{"robot_reachable": True}, {"robot_reachable": True, "camera": camera}]
```

- [ ] **Step 12: Run it and see it fail**

Run: `../.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_app_client.py -q -p no:cacheprovider`
Expected: `1 failed, 20 passed`, with `TypeError: AppClient.heartbeat() got an unexpected keyword argument 'camera'`.

- [ ] **Step 13: Implement**

In `reachy_app/medcare_reachy/bridge/app_client.py`, replace `heartbeat` (lines 171-172):

```python
    async def heartbeat(self, payload: dict) -> dict:
        return await self._request("POST", "/api/device/heartbeat", json=payload) or {}
```

with

```python
    async def heartbeat(self, payload: dict, camera: dict | None = None) -> dict:
        """`camera`: the camera watchdog's snapshot (state, fps, since, media), sent as "camera" when given. Servers
        before it ignore the key."""
        body = payload if camera is None else {**payload, "camera": camera}
        return await self._request("POST", "/api/device/heartbeat", json=body) or {}
```

- [ ] **Step 14: Run the client tests, then commit**

Run: `../.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_app_client.py -q -p no:cacheprovider`
Expected: `21 passed`.

```bash
cd C:/Reachy_Mini/repo/MedAiCarePlus
git add reachy_app/medcare_reachy/bridge/app_client.py reachy_app/medcare_reachy/bridge/tests/test_app_client.py
git commit -m "Robot app: the heartbeat can carry the camera's state" -m "Sent as \"camera\" only when given; servers before it ignore the key (pydantic's default extra=ignore)." -m "Co-Authored-By: <the model that wrote the commit> <noreply@anthropic.com>"
```

---

#### Part D: the stream owns the watchdog (`runner.py`)

- [ ] **Step 15: Write the failing runner tests**

15a. In `reachy_app/medcare_reachy/bridge/tests/test_runner.py`, imports (lines 7-8): replace

```python
from medcare_reachy.bridge.app_client import AppUnreachable, NotAuthorised, SessionLost
from medcare_reachy.bridge.runner import MonitorStream, Runner
```

with

```python
from medcare_reachy.bridge.app_client import AppUnreachable, NotAuthorised, SessionLost
from medcare_reachy.bridge.camera_watch import CameraWatchdog
from medcare_reachy.bridge.runner import MonitorStream, Runner
```

15b. Replace `HeartbeatApp` and `StubStream` (lines 437-454):

```python
class HeartbeatApp:
    def __init__(self, response=None, error=None):
        self.payloads, self.response, self.error = [], response, error
        self.unreachable_since = None

    async def heartbeat(self, payload):
        self.payloads.append(payload)
        if self.error:
            raise self.error
        return self.response


class StubStream:
    def landmark_fps(self):
        return 14.8

    def vision_fps(self):
        return 4.9
```

with

```python
class HeartbeatApp:
    def __init__(self, response=None, error=None):
        self.payloads, self.cameras, self.response, self.error = [], [], response, error
        self.unreachable_since = None

    async def heartbeat(self, payload, camera=None):
        self.payloads.append(payload)
        self.cameras.append(camera)
        if self.error:
            raise self.error
        return self.response


class StubCamera:
    SNAPSHOT = {"state": "ok", "fps": 10.0, "since": 1760000000.0, "media": None}

    def snapshot(self):
        return dict(self.SNAPSHOT)


class StubStream:
    def __init__(self):
        self.camera = StubCamera()

    def landmark_fps(self):
        return 14.8

    def vision_fps(self):
        return 4.9
```

15c. `test_heartbeat_payload_and_stop_all` (lines 472-479) becomes:

```python
def test_heartbeat_payload_and_stop_all():
    app = HeartbeatApp({"stop_all": True, "server_time": "t"})
    runner = heartbeat_runner(app)
    runner.slot = StubSlot()
    asyncio.run(runner.heartbeat_once())
    assert app.payloads == [{"robot_reachable": True, "landmark_fps": 14.8, "vision_fps": 4.9,
                             "bridge_version": __version__, "missing_clips": 2}]
    assert app.cameras == [StubCamera.SNAPSHOT]
    assert runner.slot.stopped == ("stop_all", True)
```

15d. In `test_shutdown_does_not_wait_out_the_task_long_poll`, `PollingApp.heartbeat` (lines 543-544): replace

```python
        async def heartbeat(self, payload):
            return {}
```

with

```python
        async def heartbeat(self, payload, camera=None):
            return {}
```

15e. Append to the end of the file (after line 669). `Camera`, `Clock`, `Engine`, `FrameApp`, `inline`,
`make_server_stream`, `FakeClips`, `FakeRobot`, `HeartbeatApp` and `Runner` are already defined or imported there:

```python


# ── camera watchdog (spec 2026-10-11 A2, A3) ──
RELEASED = {"available": False, "released": True, "no_media": False}


class DyingCamera(Camera):
    """A camera whose feed dies (`frame = None`) until reopen_camera() brings it back (`revives`)."""

    def __init__(self, revives=True):
        super().__init__()
        self.alive_frame, self.revives, self.reopened = self.frame, revives, 0

    def reopen_camera(self):
        self.reopened += 1
        if not self.revives:
            return "failed: the daemon did not give the media back"
        self.frame = self.alive_frame
        return "acquired"

    def media_status(self):
        return RELEASED


def make_dying_stream(engine=None, revives=True):
    clock, app, camera = Clock(), FrameApp(), DyingCamera(revives)
    stream = MonitorStream(app, camera, engine, clock=clock, run_blocking=inline, encode=lambda frame: b"big",
                           stream_encode=lambda frame: b"jpg")
    return stream, app, camera, clock


def test_the_stream_notices_a_dead_camera_and_gets_it_back(caplog):
    stream, app, camera, clock = make_dying_stream()

    async def scenario():
        stream.attach({"session_id": "s1", "generation": "g1"})
        await stream.step()                       # frame 1 at 50.0
        await stream.drain()
        camera.frame = None                       # the daemon's camera was released
        states = []
        for i in range(1, 9):                     # reads every 0.25 s
            clock.t = 50 + i * 0.25
            await stream.step()
            states.append(stream.camera.state)
        clock.t = 52.25
        await stream.step()                       # the reopened camera hands over a frame again
        await stream.drain()
        return states

    with caplog.at_level("INFO", logger="medcare_reachy.bridge.camera_watch"):
        states = asyncio.run(scenario())
    assert states == ["ok"] * 7 + ["recovering"] and camera.reopened == 1
    assert stream.camera.state == "ok" and [frame[2] for frame in app.frames] == [1, 2]
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("camera stalled: no new frame for 2.0 s") and "'released': True" in m for m in messages)
    assert any(m.startswith("camera recovered: a new frame 2.2 s after the last one") for m in messages)


def test_a_camera_that_stays_dead_is_unavailable_after_15_s_of_stall():
    stream, app, camera, clock = make_dying_stream(revives=False)

    async def scenario():
        stream.attach({"session_id": "s1", "generation": "g1"})
        await stream.step()
        await stream.drain()
        camera.frame = None
        for i in range(1, 69):                    # 17 s of reads that find nothing
            clock.t = 50 + i * 0.25
            await stream.step()

    asyncio.run(scenario())
    assert stream.camera.state == "unavailable" and camera.reopened == 3   # at 52, 57 and 62
    stream.detach()                                                        # the search ended: still reported
    assert stream.camera.snapshot()["state"] == "unavailable"


def test_on_robot_vision_watches_the_camera_too():
    stream, app, camera, clock = make_dying_stream(engine=Engine())

    async def scenario():
        stream.attach({"session_id": "s1", "generation": "g1"})
        camera.frame = None
        for i in range(1, 9):
            clock.t = 50 + i * 0.25
            await stream.step()

    asyncio.run(scenario())
    assert camera.reopened == 1 and stream.camera.state == "recovering"


def test_the_slow_camera_warning_can_fire_again_after_a_recovery(caplog):
    stream, app, camera, clock = make_dying_stream()

    async def scenario():
        stream.attach({"session_id": "s1", "generation": "g1"})
        for i in range(101):                      # 10 s at 10 fps, the daemon's cap: warned
            clock.t = 50 + i / 10
            await stream.step()
            await stream.drain()
        camera.frame = None
        for i in range(1, 13):                    # 3 s without a frame: stall, recovery
            clock.t = 60 + i * 0.25
            await stream.step()
        for i in range(101):                      # 10 s at 10 fps again: warned again
            clock.t = 63.25 + i / 10
            await stream.step()
            await stream.drain()

    with caplog.at_level("WARNING", logger="medcare_reachy.bridge.runner"):
        asyncio.run(scenario())
    assert camera.reopened == 1
    assert len([r for r in caplog.records if "camera hands this app only" in r.getMessage()]) == 2


def test_the_heartbeat_reports_the_camera_in_each_state():
    stream, app, camera, clock = make_dying_stream(revives=False)
    beats = HeartbeatApp({"stop_all": False})
    clips = FakeClips()
    clips.missing_count = 0
    runner = Runner(app=beats, robot=FakeRobot(), clips=clips, stream=stream, run_blocking=inline)

    async def scenario():
        await runner.heartbeat_once()                         # no session: idle
        stream.attach({"session_id": "s1", "generation": "g1"})
        await stream.step()
        await stream.drain()
        await runner.heartbeat_once()                         # streaming: ok
        camera.frame = None
        for i in range(1, 9):
            clock.t = 50 + i * 0.25
            await stream.step()
        await runner.heartbeat_once()                         # the recovery failed: stalled
        for i in range(9, 69):
            clock.t = 50 + i * 0.25
            await stream.step()
        await runner.heartbeat_once()                         # given up: unavailable
        stream.detach()
        await runner.heartbeat_once()                         # still, while no session runs

    asyncio.run(scenario())
    assert [beat["state"] for beat in beats.cameras] == ["idle", "ok", "stalled", "unavailable", "unavailable"]
    assert beats.cameras[1]["fps"] == 0.1 and beats.cameras[2]["media"] == RELEASED
    assert all(set(beat) == {"state", "fps", "since", "media"} for beat in beats.cameras)


def test_a_robot_that_cannot_reopen_its_camera_only_reports_it():
    stream, app, camera, clock = make_server_stream()
    assert isinstance(stream.camera, CameraWatchdog)

    async def scenario():
        stream.attach({"session_id": "s1", "generation": "g1"})
        await stream.step()
        await stream.drain()
        camera.frame = None
        for i in range(1, 9):
            clock.t = 50 + i * 0.25
            await stream.step()

    asyncio.run(scenario())
    assert stream.camera.state == "stalled"
```

- [ ] **Step 16: Run them and see them fail**

Run: `../.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_runner.py -q -p no:cacheprovider`
Expected: `7 failed, 32 passed`: `test_heartbeat_payload_and_stop_all` (`assert [None] == [{...}]`: the runner sends no
camera yet) and the six new tests (`AttributeError: 'MonitorStream' object has no attribute 'camera'`).

- [ ] **Step 17: Wire the watchdog into `runner.py`**

17a. Imports (lines 8-10). Replace

```python
from medcare_reachy.bridge import __version__
from medcare_reachy.bridge.app_client import BridgeError, NotAuthorised
from medcare_reachy.bridge.session import SlotSession
```

with

```python
from medcare_reachy.bridge import __version__
from medcare_reachy.bridge.app_client import BridgeError, NotAuthorised
from medcare_reachy.bridge.camera_watch import NOT_WORKING, CameraWatchdog
from medcare_reachy.bridge.session import SlotSession
```

17b. Insert these two functions between `RESUMABLE = ...` (line 35) and `def encode_stream_jpeg(frame) -> bytes:`
(line 38), keeping two blank lines around each top-level definition:

```python
def _cannot_reopen() -> str:
    """The camera watchdog's recovery for a robot without reopen_camera() (VideoFileRobot, test cameras)."""
    return "failed: this robot can't reopen its camera"


def _no_media_status() -> None:
    return None
```

17c. `MonitorStream`: the docstring's last line (line 88) gets three more lines, and `__init__` (lines 91-124) gets the
`camera` keyword and builds the watchdog right after `self.app, self.robot, self.engine = ...`. The full new docstring
end and `__init__`:

```python
    A gap over STALL_SECONDS between streamed frames is logged with what held it up (see _note_stall).
    `camera` (camera_watch.CameraWatchdog) follows every read of an attached session: frame() for a new frame,
    tick() (off the event loop: a recovery blocks) for none. By default it recovers with the robot's
    reopen_camera() and logs its media_status(); the heartbeat sends its snapshot().
    """

    def __init__(self, app, robot, engine, *, clock=time.monotonic, run_blocking=asyncio.to_thread,
                 encode=encode_jpeg, stream_encode=encode_stream_jpeg, fps=CAPTURE_FPS,
                 vision_interval=VISION_INTERVAL, emotion=None, emotion_interval=EMOTION_INTERVAL,
                 max_in_flight=MAX_FRAMES_IN_FLIGHT, camera=None):
        self.app, self.robot, self.engine = app, robot, engine
        self.camera = camera if camera is not None else CameraWatchdog(
            getattr(robot, "reopen_camera", _cannot_reopen), getattr(robot, "media_status", _no_media_status),
            clock=clock)
        self.emotion, self.emotion_interval = emotion, emotion_interval
        self._last_emotion = float("-inf")
        self.clock, self.run_blocking, self.encode, self.stream_encode = clock, run_blocking, encode, stream_encode
        self.max_in_flight = max_in_flight
        self._frames_in_flight = 0
        self.interval = 1.0 / fps
        self.vision_interval = vision_interval
        self.session: dict | None = None
        self.latest: dict | None = None
        self.frame_seq = 0
        self._latest_seq = 0
        self._error: Exception | None = None
        self._landmark_busy = False
        self._vision_busy = False
        self._last_vision = float("-inf")
        self._landmark_times: deque = deque()
        self._vision_times: deque = deque()
        self._camera_times: deque = deque()
        self._next_due = float("-inf")   # when the server stream's next frame is due
        self._streaming_since: float | None = None
        self._slow_camera_logged = False
        self._tasks: set = set()
        # What held the server stream up, for _note_stall.
        self._sent_at: dict = {}         # one key per unanswered request -> when it went out
        self._last_streamed: float | None = None
        self._last_stall_log = float("-inf")
        self._stalls = 0
        self._camera_asked: float | None = None   # since when the stream has waited for a new camera frame
        self._clear_stall_notes()
```

17d. `attach` and `detach` (lines 127-141) become:

```python
    def attach(self, monitor: dict) -> None:
        self.session = {"session_id": monitor["session_id"], "generation": monitor["generation"]}
        self.latest = dict(monitor)
        self.frame_seq = 0
        self._latest_seq = 0
        self._error = None
        self._streaming_since = None
        self._last_streamed = None
        self._stalls = 0
        self._camera_asked = None
        self._clear_stall_notes()
        self.camera.attach()

    def detach(self) -> None:
        self.session = None
        self.latest = None
        self.camera.detach()
```

17e. `step` (lines 185-207; the on-robot vision path) becomes:

```python
    async def step(self) -> None:
        session = self.session
        if session is not None and self.engine is None:
            await self._stream_frame(session)
            return
        if session is None or self._landmark_busy:
            return
        frame = await self.run_blocking(self.robot.get_frame)
        captured = self.clock()
        if self.session is not session:
            return
        if frame is None:
            await self.run_blocking(self.camera.tick)   # a recovery blocks for a while: never on the event loop
            return
        self._camera_seen()
        self.frame_seq += 1
        seq = self.frame_seq
        packet = await self.run_blocking(self.engine.process, frame, seq, captured * 1000.0)
        if self.session is not session:
            return   # session changed while the landmark models ran: this frame is stale
        packet = await self._with_emotion(frame, packet, captured)
        jpeg = None
        if not self._vision_busy and captured - self._last_vision >= self.vision_interval:
            self._last_vision = captured
            jpeg = await self.run_blocking(self.encode, frame)
        self._landmark_busy = True
        self._spawn(self._send_landmarks(session, {**session, **packet}, seq, jpeg))
```

17f. `_stream_frame` (lines 209-247; the server vision path, the default) becomes:

```python
    async def _stream_frame(self, session) -> None:
        # The camera's own frame: encode_stream_jpeg crops and scales it in one pass.
        asked = self.clock()
        frame = await self.run_blocking(self.robot.get_camera_frame)
        captured = self.clock()
        if self.session is not session:
            return
        if frame is None:
            if self._camera_asked is None:
                self._camera_asked = asked
            await self.run_blocking(self.camera.tick)   # a recovery blocks for a while: never on the event loop
            return
        self._camera_wait = max(self._camera_wait, captured - (asked if self._camera_asked is None
                                                               else self._camera_asked))
        self._camera_asked = None
        self._camera_seen()
        self._note_camera_frame(captured)
        if captured < self._next_due - self.interval * PACE_EARLY:
            return   # the camera runs faster than the stream: skip this one
        jpeg = await self.run_blocking(self.stream_encode, frame)
        for _ in range(FRAME_WAIT_STEPS):
            if self._frames_in_flight < self.max_in_flight or self.session is not session:
                break
            await asyncio.sleep(0.005)
        if self.session is not session:
            return
        if self._frames_in_flight >= self.max_in_flight:
            self._no_slot += 1
            if self._sent_at:
                self._oldest_request = max(self._oldest_request, self.clock() - min(self._sent_at.values()))
            return
        self.frame_seq += 1
        self._frames_in_flight += 1
        self._note_stall(captured)
        # Keep to the schedule (a 15 fps camera streamed at 10 fps sends 2 frames in 3), unless the camera fell
        # more than a frame behind it: then start again from this frame.
        on_time = captured - self._next_due <= self.interval
        self._next_due = (self._next_due if on_time else captured) + self.interval
        request = object()
        self._sent_at[request] = self.clock()
        self._spawn(self._send_frame(session, self.frame_seq, captured, jpeg, request))
```

17g. Insert `_camera_seen` right before `def _note_camera_frame(self, captured: float) -> None:` (line 278):

```python
    def _camera_seen(self) -> None:
        """A new camera frame for the watchdog. After a stall the slow-camera warning may fire again, measured from
        the recovery."""
        recovered = self.camera.state in NOT_WORKING
        self.camera.frame()
        if recovered:
            self._slow_camera_logged = False
            self._streaming_since = None
```

17h. `Runner.heartbeat_once` (lines 458-479) becomes (only the `heartbeat` call changed, line 467):

```python
    async def heartbeat_once(self) -> dict | None:
        try:
            self.robot_reachable = bool(await self.run_blocking(self.robot.is_reachable))
        except Exception:
            self.robot_reachable = False
        payload = {"robot_reachable": self.robot_reachable, "landmark_fps": self.stream.landmark_fps(),
                   "vision_fps": self.stream.vision_fps(), "bridge_version": __version__,
                   "missing_clips": self.clips.missing_count}
        try:
            response = await self.app.heartbeat(payload, camera=self.stream.camera.snapshot())
        except NotAuthorised:
            response = {"stop_all": True, "microphone": False}   # revoked device or withdrawn consent
        except BridgeError as exc:
            log.warning("heartbeat failed: %s", exc)
            return None
        if response.get("stop_all") and self.slot:
            log.warning("stop_all from app: stopping the current slot")
            self.slot.stop("stop_all")
        if self.slot and "microphone" in response:
            # Withdrawn microphone consent takes effect within one heartbeat, not only at the next task.
            self.slot.task["microphone"] = bool(response["microphone"])
        return response
```

`medcare_reachy/service.py` needs no change: `default_bridge` builds `MonitorStream(app, robot, engine, ...)` with the
real `ReachyRobot`, whose `reopen_camera` and `media_status` the watchdog then uses.

- [ ] **Step 18: Run the runner tests, then commit**

Run: `../.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_runner.py -q -p no:cacheprovider`
Expected: `39 passed`.

```bash
cd C:/Reachy_Mini/repo/MedAiCarePlus
git add reachy_app/medcare_reachy/bridge/runner.py reachy_app/medcare_reachy/bridge/tests/test_runner.py
git commit -m "Robot app: the camera stream runs the watchdog and the heartbeat reports it" -m "Every read of an attached session feeds the watchdog (tick() off the event loop, since a recovery blocks); the slow-camera warning can fire again after a recovery; heartbeats send camera = the watchdog's snapshot." -m "Co-Authored-By: <the model that wrote the commit> <noreply@anthropic.com>"
```

---

#### Part E: a search ends when the camera is unavailable (`session.py`)

- [ ] **Step 19: Write the failing session test (and give the fake stream a camera)**

19a. In `reachy_app/medcare_reachy/bridge/tests/fakes.py`, replace the start of `FakeStream` (lines 171-176):

```python
class FakeStream:
    def __init__(self):
        self.attached = None
        self.latest = None
        self.errors = []
        self.history = []
```

with

```python
class FakeCamera:
    """CameraWatchdog stand-in: tests set `state`."""

    def __init__(self):
        self.state = "idle"

    def snapshot(self):
        return {"state": self.state, "fps": 0.0, "since": None, "media": None}


class FakeStream:
    def __init__(self):
        self.attached = None
        self.latest = None
        self.errors = []
        self.history = []
        self.camera = FakeCamera()
```

19b. Append to the end of `reachy_app/medcare_reachy/bridge/tests/test_session.py` (after line 494; `pytest`,
`Harness`, `make_task` and `dose` are already imported):

```python


@pytest.mark.parametrize("reason", ["upcoming", "checkin"])
def test_a_search_whose_camera_is_unavailable_ends_as_not_found_with_the_reason(reason):
    h = Harness(make_task([] if reason == "checkin" else [dose(1)], reason=reason))
    h.to_state("SEARCHING")
    h.tick()                                              # the search session streams
    h.stream.camera.state = "stalled"                     # recovering: the search goes on
    h.tick(5)
    h.stream.camera.state = "recovering"
    h.tick(5)
    assert not h.slot.done and h.slot.state == "SEARCHING"
    played = list(h.clips.played)
    h.stream.camera.state = "unavailable"                 # the watchdog gave up
    h.tick(0.5)
    assert h.slot.done and h.slot.result == "camera_unavailable"
    assert h.statuses() == ["searching", "not_found"]
    assert h.app.named("task_status")[-1] == ("task_status", "not_found",
                                              {"reason": "camera_unavailable", "outcomes": {}})
    assert ("monitor_end", "s1") in h.app.calls and h.robot.calls[-1] == "sleep"
    assert h.clips.played == played                       # nothing extra said
    assert h.app.named("confirmation") == []
```

- [ ] **Step 20: Run it and see it fail**

Run: `../.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_session.py -q -p no:cacheprovider`
Expected: `2 failed, 39 passed`; both parameters fail at `assert h.slot.done and h.slot.result == "camera_unavailable"`
(`assert (False)`): the search ignores the camera.

- [ ] **Step 21: Implement the early end in `_searching`**

In `reachy_app/medcare_reachy/bridge/session.py`, `_searching` (lines 209-242) becomes (the new block sits before the
existing `SEARCH_TIMEOUT` check; it uses the same not_found path, `_shutdown`, which ends the monitor session, posts
`not_found` with `{"reason": ..., "outcomes": ...}` and puts the robot to sleep without playing anything):

```python
    async def _searching(self):
        await self._ensure_session("observe")
        now = self.clock.monotonic()
        if not self.entered:
            self.entered = True
            self.next_scan_at = now
            self.next_search_clip_at = now + SEARCH_CLIP_INTERVAL
        if self.stream.camera.state == "unavailable":
            # The camera watchdog gave up (no new frame for 15 s after the camera stalled, recoveries included): end
            # the search now, as for any not_found, rather than look around blind for 10 minutes.
            log.warning("slot %s: the camera is unavailable; the search ends", self.task_id)
            await self._shutdown("camera_unavailable", status="not_found")
            return
        if now - self.since >= SEARCH_TIMEOUT:
            await self._shutdown("not_found", status="not_found")
            return
        identity = self._latest().get("identity_status")
        if identity == "verified":
            await self._robot("hold_head")
            if self._is_checkin_task():
                await self._advance("in_progress")
                self._go(CHECKIN)
                return
            # Restart recovery and "taken earlier in the app": re-read before any prompt.
            await self._refresh_doses()
            if not any(self._unresolved(dose) for dose in self.doses):
                self._go(ALREADY_TAKEN)
                return
            await self._advance("in_progress")
            self.index = 0
            self._go(MED_PROMPT)
            return
        await self._check_wrong_person(identity)
        if now >= self.next_scan_at:
            await self._robot("look_around", self.scan_step)
            self.scan_step += 1
            self.next_scan_at = now + SCAN_INTERVAL
        if now >= self.next_search_clip_at:
            self.next_search_clip_at = now + SEARCH_CLIP_INTERVAL
            await self._play("searching")
```

- [ ] **Step 22: Run the session tests, then commit**

Run: `../.venv/Scripts/python.exe -m pytest medcare_reachy/bridge/tests/test_session.py medcare_reachy/bridge/tests/test_session_checkin.py medcare_reachy/bridge/tests/test_session_refusal.py medcare_reachy/bridge/tests/test_session_voice.py medcare_reachy/bridge/tests/test_runner.py -q -p no:cacheprovider`
Expected: all pass (`test_session.py` alone: `41 passed`).

```bash
cd C:/Reachy_Mini/repo/MedAiCarePlus
git add reachy_app/medcare_reachy/bridge/session.py reachy_app/medcare_reachy/bridge/tests/fakes.py reachy_app/medcare_reachy/bridge/tests/test_session.py
git commit -m "Robot app: a search with an unavailable camera ends as not_found (camera_unavailable)" -m "The task status detail is {\"reason\": \"camera_unavailable\", \"outcomes\": {...}} through the existing not_found path: no extra words, the robot sleeps, and the server handles a dose as for any not_found." -m "Co-Authored-By: <the model that wrote the commit> <noreply@anthropic.com>"
```

---

#### Part F: version 0.6.0

The version is pinned in two places only (searched the whole repo for `0.5.4` and `__version__`):
`reachy_app/pyproject.toml:8` and `reachy_app/medcare_reachy/bridge/__init__.py:3`. `test_runner.py` reads
`__version__`, so no test pins the number. Mentions of 0.5.4 in `CLAUDE.md`, `docs/*.md` and `reachy_app/README.md`
describe history and belong to the docs update, not this task.

- [ ] **Step 23: Bump the version**

In `reachy_app/medcare_reachy/bridge/__init__.py` line 3, replace `__version__ = "0.5.4"` with
`__version__ = "0.6.0"`. In `reachy_app/pyproject.toml` line 8, replace `version = "0.5.4"` with
`version = "0.6.0"`.

- [ ] **Step 24: Run the whole robot app suite**

Run: `../.venv/Scripts/python.exe -m pytest tests medcare_reachy/bridge/tests -q -p no:cacheprovider`
Expected: `533 passed, 6 skipped` (497 after Task 1 + 36 new: 14 watchdog, 13 media, 1 client, 6 runner, 2 session;
the prototype, run without Task 1, gave 530). If exactly one of the known flaky tests fails
(`test_speech.py::test_say_tells_when_its_first_sound_is_about_to_play`,
`test_filler_echo.py::test_its_echo_goes_and_the_patient_s_next_words_stay` or
`test_gestures.py::test_a_clip_handed_over_without_waiting_still_plays_before_the_next_one`), rerun the suite once; any
other failure is real.

The server suite is not affected by this task (no file under `app/` or `tests/` changes).

- [ ] **Step 25: Commit**

```bash
cd C:/Reachy_Mini/repo/MedAiCarePlus
git add reachy_app/medcare_reachy/bridge/__init__.py reachy_app/pyproject.toml
git commit -m "Robot app 0.6.0: camera watchdog, recovery, camera state in the heartbeat" -m "Heartbeats now report bridge_version 0.6.0. Not deployed yet." -m "Co-Authored-By: <the model that wrote the commit> <noreply@anthropic.com>"
```

#### NOTES

**Deviations from the interface contract (read these):**
1. **`detach()` keeps "unavailable".** The contract says `detach()` sets "idle". The spec (A3) says "`unavailable`
   stays until a later session gets frames again". The give-up ends the search, and ending the search detaches the
   stream at once, so a plain "idle" would hide the failure from every heartbeat (they come every 10 s) and the Reachy
   card would never warn. So `detach()` sets "idle" unless the state is "unavailable", which stays (with its `media`
   status) until the next `attach()`. `attach()` is as in the contract ("ok", timers reset): the next session gets a
   fresh try; with a still-dead camera it is "stalled" again 2 s later. The card may show "ok" for at most one
   heartbeat in those 2 s.
2. **`CameraWatchdog` has one more, keyword-only parameter: `wall=time.time`.** The snapshot's `since` must be unix
   seconds (spec A3) and `clock` is `time.monotonic`. Every contract call still works unchanged.
3. Additive names, not in the contract: `camera_watch.NOT_WORKING`, `camera_watch.FPS_WINDOW`, the state constants
   (`IDLE`, `OK`, ...), `MonitorStream(..., camera=None)` (inject a watchdog), `media.DAEMON_PORT`,
   `media.MEDIA_STATUS_TIMEOUT`, `ReachyRobot._media()`, `ReachyRobot._renew_media()`.
4. **Task status detail is `{"reason": "camera_unavailable", "outcomes": {...}}`**, not exactly
   `{"reason": "camera_unavailable"}`: the existing not_found path (`_shutdown`, session.py:772-796, line 789) always
   adds `outcomes`. Server-side code must read `detail["reason"]`.
5. `media_status()` asks `127.0.0.1:8000` when the robot object was made by `ReachyRobot.attach()` (the app runtime,
   `host="localhost"`, which is how the app runs); a `ReachyRobot(host)` connected from another machine asks that
   host's daemon.
6. `AppClient.heartbeat`'s `camera` is a normal keyword parameter with default None (callable by keyword, as the
   contract says).
7. Beyond the contract, `get_camera_frame`, `get_audio` and `play_clip` return None/False while this app's media is
   released (between a recovery's release and acquire, or after an acquire the daemon refused), and `play_clip`
   looks the manager up again inside its lock. Without this a clip queued behind a recovery would be pushed into a
   closed manager, and the SDK would log "Camera/Audio system is not initialized." on every call (media_manager.py
   252-254, 294-296: the voice listener polls every 20 ms).

**Assumptions and design choices:**
- Give-up comes 15 s after the stall began, so about 17 s after the last frame (2 s to notice the stall + 15 s), as
  the contract says ("since the stall began").
- Recovery attempts: at the stall, then every 5 s (at 2, 7 and 12 s after the last frame); none after the give-up
  within that session.
- Only SEARCHING ends early (contract). A camera that dies during MED_PROMPT or WATCHING is logged and reported in
  heartbeats but those states keep their existing timeouts (MED_PROMPT waits up to 10 min for verification).
- Both paths are watched: server vision (`_stream_frame`, the default) and on-robot vision (`step` with an engine).
  An empty read right after a long busy period is still a real stall: the SDK's appsink keeps the newest frame
  (`max-buffers=1`, `drop=True`), so a live camera always has one waiting.
- `tick()` runs through `run_blocking` (a worker thread) on every empty read, because a recovery blocks: at most
  ~2 s status read + two daemon HTTP calls with the SDK's 10 s timeout (`ws_client.py:328`) + up to 2 s for the new
  camera's first frame (`camera_gstreamer.py:248-254`). The lock is never held during HTTP or the recovery, and the
  `state` property applies the give-up on time even while a recovery blocks (tested).

**Risks:**
- **Not yet tried on the robot (spec review focus 1).** The release/acquire cycle restarts the daemon's media pipeline
  and gives the app a new media manager, which also holds the speaker and microphone. Task 10 (Part B, Step 12) checks
  this on the robot, during a search: `curl -X POST http://127.0.0.1:8000/api/media/release`, then the app log shows
  `camera stalled ...`, `camera recovery: acquired with the SDK's release_media() and acquire_media() (..., media
  backend MediaBackend.LOCAL)`, `camera recovered ...`; the next clip plays; and in a dose, the "I finished" listener
  still hears.
- If the daemon's camera socket did not exist yet when the SDK built the fresh manager, the SDK would choose WebRTC
  (`reachy_mini.py:369-378`). The recovery's log line names the backend so this shows up.
- If the TCU companion is ever enabled again, it releases the daemon's camera on start, and this recovery takes it back
  every 5 s while a MedCare session runs. Keep the companion disabled at boot (done by hand on 11 Oct).
- A recovery when the daemon still has its media (a stale reader) also stops and restarts the daemon's own pipeline
  for a moment (its WebRTC stream and daemon-side sounds), because the SDK has no public "reopen my reader only" call.
- Timing tests on Windows: `time.monotonic()` ticks every 15.6 ms on the repo's Python 3.11.9 (`GetTickCount64`), so
  the one real-time test (`test_a_recovery_waits_for_the_clip_still_playing`) measures with `perf_counter` and a wide
  margin. All other new tests use fake clocks.

**Facts verified (file:line):**
- Robot SDK, read on the robot (read-only), `/venvs/apps_venv/lib/python3.12/site-packages/reachy_mini/`, version
  1.11.0 (`reachy_mini-*.dist-info/METADATA:3`):
  - `reachy_mini.py:165-167` `_media_released`, `_log_level`, `_media_backend` set in `__init__`; `:171`
    `media_manager = _configure_mediamanager(...)`; `:186-191` `__exit__` re-acquires if released; `:193-196` the
    `media` property returns `media_manager`; `:198-201` public `media_released` property.
  - `reachy_mini.py:203-220` `release_media()`: returns at once if already released; calls
    `client.release_media()` (result ignored), `media_manager.close()`, sets `_media_released = True`.
  - `reachy_mini.py:222-240` `acquire_media()`: no-op unless `_media_released`; if `client.acquire_media()` is False it
    logs "Failed to re-acquire media on daemon." and returns, still released; else closes the old manager, builds a new
    one with `_configure_mediamanager(self._media_backend, self._log_level)` and clears the flag.
  - `reachy_mini.py:327-401` `_configure_mediamanager`: `"no_media"` releases the daemon's media (350-358, the TCU
    companion's path); LOCAL when `connection_mode == "localhost_only"` and `is_local_camera_available()` (369-373),
    else WebRTC.
  - `io/ws_client.py:301-317` `release_media()` / `acquire_media()` POST `/api/media/release|acquire`; `:319-330`
    `requests.post(url, timeout=10)`, True/False.
  - `daemon/app/routers/media.py:41-62`: POST release/acquire; GET `/media/status` returns `{"available": not released
    and media server present, "released", "no_media"}`. Same md5 (`2da25d7d...`) in `/venvs/mini_daemon` (the daemon
    that runs).
  - `daemon/daemon.py:139-153` `release_media()` (no-op if already released, line 145-146; stops the pipeline);
    `:155-169` `acquire_media()` (no-op if not released; restarts the pipeline). Same md5 (`445154c7...`) in
    `/venvs/mini_daemon`.
  - `/venvs/mini_daemon/.../media/media_server.py:1329-1337` `start()` rebuilds the pipeline; `:1087-1090` removes a
    stale socket and puts `unixfdsink` at the camera socket.
  - `daemon/utils.py:52-69` `is_local_camera_available()` = the socket file exists.
  - `media/camera_gstreamer.py:129-134` appsink `drop=True`, `max-buffers=1`; `:177-184` `unixfdsrc` on the socket,
    no reconnect; `:212-225` pipeline errors only logged as warnings; `:240-254` `open()` waits up to 2 s for a frame.
  - `media/media_manager.py:162-170` LOCAL builds camera (failure logged, continues) and audio; `:179-190` `close()`;
    `:243-255` and `:287-297` with no camera/audio: a WARNING per call and None; `:306-311` output rate -1 with no
    audio.
  - `media/audio_gstreamer.py:127-133` one pipeline for recording and playback; `:480-482` `start_recording()` and
    `:488-494` `start_playing()` both set it PLAYING. `grep` over `reachy_app`: the app never calls
    `start_recording()`; its microphone runs because `play_clip`'s `start_playing()` starts the shared pipeline.
  - `apps/app.py:44-49` the app runtime uses media backend "default"; `:140-149` `ReachyMini(media_backend=...,
    connection_mode="localhost_only")` when the daemon is local.
  - Live, at planning time: `GET http://127.0.0.1:8000/api/media/status` on the robot =
    `{"available":true,"released":false,"no_media":false}`; `/tmp/reachymini_camera_socket` exists.
- Repo (branch `robot-camera-thinking`, HEAD `37ba84e`):
  - `reachy_app/medcare_reachy/bridge/runner.py:216-219` an empty read just returns (no log); `:278-290` the
    slow-camera warning fires once per process (`_slow_camera_logged` is never reset); `:458-479` heartbeat.
  - `reachy_app/medcare_reachy/bridge/session.py:209-218` SEARCHING and its not_found path; `:772-796` `_shutdown`
    posts `{"reason", "outcomes"}` (789) and sleeps the robot (793).
  - `reachy_app/medcare_reachy/main.py:21` `request_media_backend = None` (so "default": LOCAL on the robot);
    `service.py:48` `ReachyRobot.attach(reachy_mini)`, `:60` `MonitorStream(app, robot, engine, ...)`.
  - `app/routers/api_device.py:58-60` `StatusPayload` accepts `not_found` with any dict detail; `:76-81`
    `HeartbeatPayload` uses pydantic's default (extra keys ignored), so a 0.6.0 robot works with an older server.
- Prototype: the complete change was applied to a scratch copy of `reachy_app` and run: RED for each part exactly as
  written above, then `530 passed, 6 skipped` (and once `529 passed` plus the known `test_speech.py` flake).

---

### Task 3: Server + Reachy card: the robot camera state

Spec part: B3 (robot camera state on the Reachy card). The robot app's side (the watchdog and `camera` in the heartbeat)
is Task 2; this task only receives, stores, returns and shows it. Nothing changes in `sql/init.sql`:
`reachy_device.status_detail JSONB` already exists (`sql/init.sql:290`).

**Files** (line numbers at commit `37ba84e`; if an earlier task already changed a file, find the place by the quoted
text, not by the number):
- Modify `app/routers/api_device.py` (working tree is **LF**, 709 lines):
  - new class `CameraReport` between `ExtraEventPayload` (lines 69-72) and `HeartbeatPayload` (lines 75-80);
  - `HeartbeatPayload` gains `camera` (after line 80, `missing_clips`);
  - `heartbeat()` (lines 523-537): two lines after `detail = {...}` (lines 528-529).
- Modify `app/routers/api_reachy.py` (CRLF, 163 lines):
  - `import json` before line 3 (`import uuid`);
  - new helper `_camera()` after `_iso()` (lines 29-30);
  - `_status()` (lines 33-57): the SELECT (lines 34-36) gains `status_detail`; the returned dict gains `"camera"`
    after line 53 (`"landmark_fps": ...`).
- Modify `tests/test_device_api.py` (CRLF, 772 lines):
  - `import json` before line 3 (`import os`);
  - `FakeConn.__init__` (lines 50-64): one field after line 64;
  - `FakeConn.fetchrow` (lines 69-94): two branches before `raise AssertionError(query)` (line 94);
  - `FakeConn.fetchval` (lines 118-132): one branch before line 132;
  - `FakeConn.execute` (lines 134-135): stores what the heartbeat wrote;
  - new tests after `test_heartbeat_reports_microphone_consent` (lines 324-333), before the
    `# ── Frame stream: ...` header (line 336).
- Modify `tests/test_device_auth.py` (CRLF): the fake status row (lines 53-54) gains `"status_detail": None`, because
  `_status()` now reads that column (without it `test_pairing_returns_token_once_stores_hash_and_replaces_active_device`
  fails with `KeyError: 'status_detail'`).
- Modify `frontend_source/src/lib/reachy-api.ts` (CRLF): new `RobotCamera` type before `interface ReachyStatus`
  (line 3); `camera` field after line 11 (`landmark_fps`).
- Modify `frontend_source/src/components/ReachyCard.tsx` (CRLF, 342 lines): import (line 9), a constant after line 11
  (`CHECKIN_SCOPES`), two `const`s before `return (` (line 210), a row inside the `<dl>` after the camera-rate row
  (lines 262-263), and the warning after `</dl>` (line 266).
- Modify `frontend_source/src/i18n.ts` (CRLF): two keys after `cameraRate` in `reachy` for `en` (line 91) and for
  `zh-TW` (line 771).
- Modify `frontend_source/tests/reachy.spec.ts` (CRLF, 367 lines): `MockOptions` (lines 34-47), the mock's `state`
  and `robotStatus` (lines 50-66), and two new tests before the last test (line 359).

**Interfaces:**
- Consumes (Task 2, the robot): the heartbeat body gains `"camera": {"state", "fps", "since", "media"}`, the dict from
  `CameraWatchdog.snapshot()` (contract: `state` in `idle|ok|stalled|recovering|unavailable`; `media` is
  `media_status()`, a dict or `None`). Robots older than 0.6.0 send no `camera`.
- Produces (exact contract names):
  - `app/routers/api_device.py`: `class CameraReport(BaseModel)` with `state: Literal["idle","ok","stalled",
    "recovering","unavailable"]`, `fps: float | None = Field(default=None, ge=0, le=240)`, `since: float | None = None`,
    `media: dict | None = None`; `HeartbeatPayload.camera: CameraReport | None = None`, stored as
    `status_detail["camera"]` (`model_dump()`), and the key is left out when `camera` is `None`. A bad camera object
    refuses the whole heartbeat with 422 (FastAPI's default answer; only `/api/ai/*` gets the bare code).
  - `GET /api/reachy/status` (and `PATCH /api/reachy/settings`, which returns the same dict): `"camera": dict | None`.
  - `frontend_source/src/lib/reachy-api.ts`: `export type RobotCamera = { state: 'idle'|'ok'|'stalled'|'recovering'|
    'unavailable'; fps?: number | null; since?: number | null; media?: Record<string, unknown> | null }` and
    `ReachyStatus.camera?: RobotCamera | null`.
  - i18n keys `reachy.robotCamera` ("Robot camera" / 「機器人鏡頭」) and `reachy.cameraNotWorking`
    ("Robot camera not working" / 「機器人鏡頭無法使用」), in `en` and `zh-TW`.
- What the card shows (spec B3):
  - the warning `reachy.cameraNotWorking` (an amber `role="alert"` box under the status rows) only when the robot is
    online and `camera.state` is `stalled`, `recovering` or `unavailable`;
  - a "Robot camera" row with `camera.fps` (one decimal, "fps") only when the robot is online and the state is not
    `idle` (a session is attached);
  - nothing new when `camera` is missing or null (older robot) or the robot is offline.

**Line endings.** `api_device.py` is LF in the working tree; the other seven files are CRLF (`git ls-files --eol`:
`w/crlf`; the index is LF and `core.autocrlf=true`). Use the Edit tool, which keeps each file's line endings. After the
edits, `file <path>` must still say "with CRLF line terminators" for the seven, and must not say it for
`api_device.py`. `git diff --stat` must show only the changed lines (no whole-file rewrite).

All commands run in Git Bash from `C:/Reachy_Mini/repo/MedAiCarePlus` unless a step says otherwise.

- [ ] **Step 0: Check the branch**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && git branch --show-current && git status --short
```
Expected: `robot-camera-thinking`, and none of this task's eight files listed as changed. Tasks 1 and 2 touch none of
these files, so they are as at `37ba84e`. Two later tasks edit two of them after this one (Task 4 adds a test to
`tests/test_device_api.py` near line 500; Task 6 changes `reachy.checkinsHelp` in `i18n.ts` and appends a test to
`reachy.spec.ts`); nothing of theirs is there yet. The edits below replace only the exact text they quote. Where a
step shows a whole function (Steps 1c, 1d, 1e, 3b, 4c), each one names what is new.

- [ ] **Step 1: Write the failing server tests**

1a. `tests/test_device_api.py`, imports. Replace

```python
import os
import sys
import types
import uuid
```
with
```python
import json
import os
import sys
import types
import uuid
```

1b. `FakeConn.__init__`: after the line
```python
        self.source = self.doses   # monitor starts read `doses`, manual tasks `intakes`
```
add
```python
        self.status_detail = None  # what the heartbeat stored (asyncpg hands JSONB back as text)
```

1c. Replace the whole `FakeConn.fetchrow` with this (the existing branches are unchanged; two branches are new, just
before the final `raise`):

```python
    async def fetchrow(self, query, *args):
        if "FROM reachy_task WHERE task_id = $1::uuid AND u_id = $2 AND lease_owner = $3::uuid" in query:
            t = self.task
            ok = str(t["task_id"]) == args[0] and t["u_id"] == args[1] and t["lease_owner"] == args[2]
            return dict(t) if ok else None
        if "SELECT m.dose_form, m.units_per_dose, i.intake_time_stamp FROM intake i" in query:
            self.source = self.doses
            dose = self.doses.get(args[0])
            return dict(dose) if dose and args[1] == 7 else None
        if "SELECT intake_time_stamp FROM intake WHERE intk_id = $1" in query:
            self.source = self.intakes
            stamp = self.intakes.get(args[0])
            return {"intake_time_stamp": stamp} if stamp and args[1] == 7 else None
        if "FROM notification_settings" in query:
            return None
        if "SELECT task_id, status, slot_time, intk_ids FROM reachy_task" in query:
            return {"task_id": uuid.UUID(args[0]), "status": "queued", "slot_time": SLOT, "intk_ids": [101, 102]}
        if 'SELECT u_id, name, face_label FROM "user"' in query:
            return {"u_id": 7, "name": "Pearl", "face_label": "pearl"}
        if "SELECT i.intk_id, m.dose_form, m.units_per_dose, i.intake_time_stamp FROM intake i JOIN medication m" \
                in query:
            self.source = self.doses
            return {"intk_id": args[0],
                    **self.doses.get(args[0], {"dose_form": "solid_oral", "units_per_dose": Decimal("1.00"),
                                               "intake_time_stamp": SLOT})}
        if "FROM reachy_device WHERE u_id = $1 AND revoked_at IS NULL" in query:
            assert "status_detail" in query   # api_reachy._status reads the robot's camera from it
            return {"device_id": uuid.UUID(DEVICE_ID), "label": "Reachy Mini", "auto_record": False,
                    "last_seen_at": datetime.now(timezone.utc), "robot_reachable": True, "landmark_fps": 9.9,
                    "status_detail": self.status_detail}
        if "SELECT task_id, status, slot_time, finished_at FROM reachy_task WHERE u_id = $1" in query:
            return None
        raise AssertionError(query)
```

1d. Replace the whole `FakeConn.fetchval` with:

```python
    async def fetchval(self, query, *args):
        if "INSERT INTO monitor_extra_event" in query:
            assert self.depth > 0
            if args[0] in self.extra:
                return None
            self.extra.add(args[0])
            self.executed.append((query, args))
            return args[0]
        if "SELECT device_id FROM reachy_device" in query:
            return self.device_row["device_id"] if self.device_row else None
        if 'SELECT name FROM "user"' in query:
            return "Pearl"
        if "FROM notification_outbox" in query and "kind = 'double_dose_alert'" in query:
            return None      # no alert for this dose within the hour
        if "FROM family_contacts" in query:
            return 1         # contacts that can receive family alerts (the Reachy card's status)
        raise AssertionError(query)
```

1e. Replace the whole `FakeConn.execute` with:

```python
    async def execute(self, query, *args):
        self.executed.append((query, args, self.depth))
        if "status_detail = $4::jsonb" in query:
            self.status_detail = args[3]
```

1f. After `test_heartbeat_reports_microphone_consent` (it ends with
`    assert env.robot.post("/api/device/heartbeat", json={}).json()["microphone"] is True`) and before the line
`# ── Frame stream: the server computes the landmarks ─────────────────────────`, insert (two blank lines before and
after, as between the other tests):

```python
# ── The robot's camera (robot app 0.6.0): stored with the heartbeat, shown on the Reachy card ──

CAMERA_MEDIA = {"available": True, "released": False, "no_media": False}   # the daemon's GET /api/media/status


def _no_leases(monkeypatch):
    async def extend_leases(conn, device_id):
        return None

    monkeypatch.setattr(reachy_tasks, "extend_leases", extend_leases)


def _stored_detail(env) -> dict:
    update = next(entry for entry in env.conn.executed if "last_seen_at = NOW()" in entry[0])
    return json.loads(update[1][3])


@pytest.mark.parametrize("state", ["idle", "ok", "stalled", "recovering", "unavailable"])
def test_heartbeat_stores_the_camera_report_and_status_returns_it(env, monkeypatch, state):
    _no_leases(monkeypatch)
    camera = {"state": state, "fps": 9.8 if state == "ok" else 0.0, "since": 1791700000.5, "media": CAMERA_MEDIA}
    response = env.robot.post("/api/device/heartbeat", json={"bridge_version": "0.6.0", "camera": camera})
    assert response.status_code == 200
    assert _stored_detail(env) == {"vision_fps": None, "bridge_version": "0.6.0", "missing_clips": None,
                                   "camera": camera}
    status = env.browser.get("/api/reachy/status")
    assert status.status_code == 200 and status.json()["camera"] == camera


def test_an_older_robot_sends_no_camera_and_status_returns_none(env, monkeypatch):
    _no_leases(monkeypatch)
    assert env.browser.get("/api/reachy/status").json()["camera"] is None       # no heartbeat yet
    assert env.robot.post("/api/device/heartbeat", json={"bridge_version": "0.5.4"}).status_code == 200
    assert "camera" not in _stored_detail(env)
    assert env.browser.get("/api/reachy/status").json()["camera"] is None


@pytest.mark.parametrize("camera", [
    {"state": "broken"}, {"state": "OK"}, {"fps": 9.8}, {"state": "ok", "fps": -1}, {"state": "ok", "fps": 241},
    {"state": "ok", "media": "released"}, "stalled",
])
def test_heartbeat_refuses_a_bad_camera_report(env, monkeypatch, camera):
    _no_leases(monkeypatch)
    response = env.robot.post("/api/device/heartbeat", json={"camera": camera})
    assert response.status_code == 422
    assert not any("last_seen_at = NOW()" in entry[0] for entry in env.conn.executed)
```

How these work: `env.robot` is the private-port client (`get_device_for_heartbeat` is overridden in the `env`
fixture); `env.browser` is the public-port client, with `get_current_user` overridden and the fake consent state
holding current `core` consent, so `get_consented_user` lets `/api/reachy/status` through. `FakeConn.execute` keeps the
JSON text the heartbeat wrote, and the new `fetchrow` branch hands it back as `status_detail`, as asyncpg does.

1g. `tests/test_device_auth.py`, `FakeConn.fetchrow`. Replace

```python
                    return {"device_id": row["device_id"], "label": "Reachy Mini", "auto_record": row["auto_record"],
                            "last_seen_at": None, "robot_reachable": None, "landmark_fps": None}
```
with
```python
                    return {"device_id": row["device_id"], "label": "Reachy Mini", "auto_record": row["auto_record"],
                            "last_seen_at": None, "robot_reachable": None, "landmark_fps": None,
                            "status_detail": None}
```

- [ ] **Step 2: Run the server tests and see the new ones fail**

```bash
.venv/Scripts/python.exe -m pytest tests/test_device_api.py tests/test_device_auth.py -q -p no:cacheprovider
```
Expected: `13 failed, 64 passed`. The failures are:
- the 5 `test_heartbeat_stores_the_camera_report_and_status_returns_it[...]`: `AssertionError` at the
  `_stored_detail(env) == {...}` line (the heartbeat ignores the unknown `camera` field, so the stored detail has no
  `"camera"` key);
- `test_an_older_robot_sends_no_camera_and_status_returns_none`: `AssertionError: assert 'status_detail' in 'SELECT
  device_id, label, auto_record, last_seen_at, robot_reachable, landmark_fps FROM reachy_device ...'`;
- the 7 `test_heartbeat_refuses_a_bad_camera_report[...]`: `assert 200 == 422`.

- [ ] **Step 3: Accept and store the camera report in the heartbeat**

3a. `app/routers/api_device.py`: between `class ExtraEventPayload` and `class HeartbeatPayload`, and in
`HeartbeatPayload`. Replace

```python
class HeartbeatPayload(BaseModel):
    robot_reachable: bool | None = None
    landmark_fps: float | None = Field(default=None, ge=0, le=240)
    vision_fps: float | None = Field(default=None, ge=0, le=240)
    bridge_version: str | None = Field(default=None, max_length=50)
    missing_clips: int | list[str] | None = None
```
with
```python
class CameraReport(BaseModel):
    """The robot's camera as its watchdog reports it (robot app 0.6.0+). idle: no session, so 0 fps is normal."""
    state: Literal["idle", "ok", "stalled", "recovering", "unavailable"]
    fps: float | None = Field(default=None, ge=0, le=240)
    since: float | None = None   # unix seconds (robot clock) of the last state change
    media: dict | None = None    # the robot daemon's GET /api/media/status answer, e.g. {"available", "released"}


class HeartbeatPayload(BaseModel):
    robot_reachable: bool | None = None
    landmark_fps: float | None = Field(default=None, ge=0, le=240)
    vision_fps: float | None = Field(default=None, ge=0, le=240)
    bridge_version: str | None = Field(default=None, max_length=50)
    missing_clips: int | list[str] | None = None
    camera: CameraReport | None = None   # robots older than 0.6.0 send none
```
(`Literal`, `BaseModel` and `Field` are already imported, lines 14 and 17.)

3b. The whole `heartbeat()` route becomes (only the two lines after `detail = {...}` are new):

```python
@router.post("/heartbeat")
async def heartbeat(payload: HeartbeatPayload, device: dict = Depends(get_device_for_heartbeat)):
    server_time = datetime.now(timezone.utc).isoformat()
    if device["revoked"] or not device["consent_current"]:
        return {"stop_all": True, "microphone": False, "server_time": server_time}
    detail = {"vision_fps": payload.vision_fps, "bridge_version": payload.bridge_version,
              "missing_clips": payload.missing_clips}
    if payload.camera is not None:
        detail["camera"] = payload.camera.model_dump()   # the Reachy card reads it through /api/reachy/status
    async with get_pool().acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE reachy_device SET last_seen_at = NOW(), robot_reachable = $2, landmark_fps = $3, "
            "status_detail = $4::jsonb WHERE device_id = $1::uuid",
            device["device_id"], payload.robot_reachable, payload.landmark_fps, json.dumps(detail))
        await reachy_tasks.extend_leases(conn, device["device_id"])
    microphone = consent_service.is_current(await consent_service.get_state(device["u_id"]), "robot_microphone")
    return {"stop_all": False, "microphone": microphone, "server_time": server_time}
```
`status_detail` is replaced on every heartbeat (not merged), so a robot downgraded to an older version leaves no stale
`camera` behind.

- [ ] **Step 4: Return the camera from `/api/reachy/status`**

4a. `app/routers/api_reachy.py`, imports. Replace
```python
import uuid
from datetime import datetime, timedelta, timezone
```
with
```python
import json
import uuid
from datetime import datetime, timedelta, timezone
```

4b. After `_iso()`:
```python
def _iso(value):
    return value.isoformat() if value is not None else None
```
add (two blank lines before and after):
```python
def _camera(status_detail) -> dict | None:
    """The robot's camera as its last heartbeat reported it, or None (robots older than 0.6.0 send none).
    asyncpg hands JSONB back as text."""
    if status_detail is None:
        return None
    detail = json.loads(status_detail) if isinstance(status_detail, str) else status_detail
    camera = detail.get("camera") if isinstance(detail, dict) else None
    return camera if isinstance(camera, dict) else None
```
(`app/database.py` sets no JSONB codec, so asyncpg returns `status_detail` as a string; `api_conversations._metrics`
and `api_device.py:497` read JSONB the same way.)

4c. The whole `_status()` becomes (the SELECT gains `status_detail`; the dict gains `"camera"`):

```python
async def _status(conn, u_id: int) -> dict:
    device = await conn.fetchrow(
        "SELECT device_id, label, auto_record, last_seen_at, robot_reachable, landmark_fps, status_detail "
        "FROM reachy_device WHERE u_id = $1 AND revoked_at IS NULL", u_id)
    if not device:
        return {"paired": False}
    task = await conn.fetchrow(
        "SELECT task_id, status, slot_time, finished_at FROM reachy_task WHERE u_id = $1 "
        "ORDER BY created_at DESC LIMIT 1", u_id)
    last_seen = device["last_seen_at"]
    online = last_seen is not None and datetime.now(timezone.utc) - last_seen <= timedelta(seconds=ONLINE_SECONDS)
    # Family alerts (safety, confirmations, missed doses) go only to verified non-patient contacts on LINE.
    alert_contacts = await conn.fetchval(
        "SELECT COUNT(*) FROM family_contacts WHERE u_id = $1 AND verified = TRUE AND line_id IS NOT NULL "
        "AND relationship IS DISTINCT FROM 'user'", u_id)
    return {
        "alert_contacts": int(alert_contacts or 0),
        "paired": True, "device_id": str(device["device_id"]), "label": device["label"],
        "auto_record": bool(device["auto_record"]), "last_seen_at": _iso(last_seen), "online": online,
        "robot_reachable": device["robot_reachable"],
        "landmark_fps": float(device["landmark_fps"]) if device["landmark_fps"] is not None else None,
        "camera": _camera(device["status_detail"]),
        "last_task": {"task_id": str(task["task_id"]), "status": task["status"],
                      "slot_time": _iso(task["slot_time"]), "finished_at": _iso(task["finished_at"])}
        if task else None,
    }
```
The server returns the last report whatever the connection state; the card decides (it shows camera information only
while `online` is true).

- [ ] **Step 5: Run the server tests and see them pass**

```bash
.venv/Scripts/python.exe -m pytest tests/test_device_api.py tests/test_device_auth.py -q -p no:cacheprovider
```
Expected: `77 passed` (64 before plus 13 new; Tasks 1 and 2 add no tests to these two files).

- [ ] **Step 6: Check the server line endings and run the whole backend suite**

```bash
file app/routers/api_device.py app/routers/api_reachy.py tests/test_device_api.py tests/test_device_auth.py && git diff --stat
.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider
```
Expected:
- `file`: `api_device.py` without "CRLF"; the other three "with CRLF line terminators".
- `git diff --stat`: `app/routers/api_device.py | 11`, `app/routers/api_reachy.py | 14`, `tests/test_device_api.py | 61`,
  `tests/test_device_auth.py | 3` (measured on the scratch copy; no whole-file rewrite), plus whatever earlier tasks
  left uncommitted.
- pytest: the count before this task plus 13 passed (1609 on the spec's 1596 baseline; more if earlier tasks added
  tests), and the one known failure `tests/test_conversation.py::test_a_broken_background_never_costs_the_reply`
  (BUG-37). If a BUG-39 timing test fails, run it alone; it must pass there.

- [ ] **Step 7: Commit the server part**

```bash
git add app/routers/api_device.py app/routers/api_reachy.py tests/test_device_api.py tests/test_device_auth.py
git diff --cached --stat
git commit -F - <<'EOF'
Server: store the robot camera state from the heartbeat, return it in status

- HeartbeatPayload accepts an optional camera object (robot app 0.6.0+):
  state idle|ok|stalled|recovering|unavailable, fps 0-240, since, and the
  daemon's media status. An unknown state or a bad field refuses the
  heartbeat with 422. It is stored as status_detail["camera"]; robots
  older than 0.6.0 send none and nothing is stored.
- GET /api/reachy/status (and PATCH /api/reachy/settings) returns camera
  (null when the robot never sent one).

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
```
The trailer must name the model that actually writes the commit; change it if another model runs this task.
`git diff --cached --stat` must list exactly the 4 files above.

- [ ] **Step 8: Write the failing Playwright tests**

All frontend paths below are under `C:/Reachy_Mini/repo/MedAiCarePlus/frontend_source`.

8a. `tests/reachy.spec.ts`, `interface MockOptions`. Replace

```ts
  /** What GET /api/memory lists. */
  memoryFacts?: Record<string, unknown>[];
}
```
with
```ts
  /** What GET /api/memory lists. */
  memoryFacts?: Record<string, unknown>[];
  /** The robot's camera from its last heartbeat (robot app 0.6.0+); left out: an older robot, no camera field. */
  camera?: Record<string, unknown> | null;
  online?: boolean;
}
```

8b. In `mockApi`, replace

```ts
    taskPosts: [] as Record<string, unknown>[],
    memoryDeletes: [] as string[],
  };
  const robotStatus = () => state.paired
    ? { paired: true, device_id: 'd-1', label: 'Reachy Mini', auto_record: state.autoRecord, last_seen_at: new Date().toISOString(),
        online: true, robot_reachable: true, landmark_fps: 15.2, last_task: null, alert_contacts: 1 }
    : { paired: false };
```
with
```ts
    taskPosts: [] as Record<string, unknown>[],
    memoryDeletes: [] as string[],
    camera: options.camera,
    online: options.online ?? true,
  };
  const robotStatus = () => state.paired
    ? { paired: true, device_id: 'd-1', label: 'Reachy Mini', auto_record: state.autoRecord, last_seen_at: new Date().toISOString(),
        online: state.online, robot_reachable: true, landmark_fps: 15.2, last_task: null, alert_contacts: 1,
        ...(state.camera === undefined ? {} : { camera: state.camera }) }
    : { paired: false };
```
The existing tests pass no `camera`, so their status has no `camera` field (an older robot), as before.

8c. Before the last test, `test('when every dose today is closed the test alert says so', ...)`, insert:

```ts
// ── The robot's camera (robot app 0.6.0): a warning while it is not working, its rate while a session runs ──

const reachyCard = (page: Page) => page.locator('section[aria-labelledby="reachy-title"]');
const CAMERA_WARNING = /Robot camera not working|機器人鏡頭無法使用/;
const robotCamera = (state: string, fps: number) =>
  ({ state, fps, since: 1791700000.5, media: { available: true, released: false, no_media: false } });

test('the Reachy card warns when the robot camera is stalled, recovering or unavailable', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true, camera: robotCamera('stalled', 0) });
  await page.goto('/settings');
  for (const state of ['stalled', 'recovering', 'unavailable']) {
    api.camera = robotCamera(state, 0);
    await page.reload();
    await expect(reachyCard(page).getByRole('alert')).toHaveText(CAMERA_WARNING);
    await expect(reachyCard(page).getByText('0.0 fps')).toBeVisible();     // a session is running
  }
});

test('the Reachy card shows no camera warning when the camera works, the robot is offline or older', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true, camera: robotCamera('ok', 9.8) });
  await page.goto('/settings');
  await expect(reachyCard(page).getByText('9.8 fps')).toBeVisible();        // the camera's rate while a session runs
  await expect(reachyCard(page).getByText(CAMERA_WARNING)).toHaveCount(0);

  api.camera = robotCamera('idle', 0);                                       // no session: no rate, no warning
  await page.reload();
  await expect(reachyCard(page).getByText(/Robot camera|機器人鏡頭/)).toHaveCount(0);

  api.camera = robotCamera('unavailable', 0);                               // last report of a robot now offline
  api.online = false;
  await page.reload();
  await expect(reachyCard(page).getByText(/Offline|離線/)).toBeVisible();
  await expect(reachyCard(page).getByText(CAMERA_WARNING)).toHaveCount(0);

  api.camera = undefined;                                                    // a robot older than 0.6.0
  api.online = true;
  await page.reload();
  await expect(reachyCard(page).getByText(/Online|在線/)).toBeVisible();
  await expect(reachyCard(page).getByText(/Robot camera|機器人鏡頭/)).toHaveCount(0);
  await expect(reachyCard(page).getByRole('alert')).toHaveCount(0);
});

```
The app starts in Traditional Chinese (`i18n.ts` sets `lng: 'zh-TW'`), so the zh-TW strings are what these tests see;
the regexes accept both languages, like the file's other tests. The mock's other card data has `alert_contacts: 1` and
check-ins off, so the camera warning is the card's only `alert`.

- [ ] **Step 9: Build the unchanged app and see the new tests fail**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus/frontend_source && npm run build && npx playwright test --config "C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/pw-frontend.config.mjs" tests/reachy.spec.ts
```
Expected: `2 failed, 14 passed`. The first new test fails at `toHaveText(CAMERA_WARNING)` with "element(s) not found";
the second at `getByText('9.8 fps')` `toBeVisible()` with "element(s) not found". (On Windows a closing line
`Assertion failed: !(handle->flags & UV_HANDLE_CLOSING)` from Node may appear; it is harmless.)

- [ ] **Step 10: Add the `RobotCamera` type**

`src/lib/reachy-api.ts`. Replace

```ts
export interface ReachyStatus {
  paired: boolean;
```
with
```ts
/** The robot's camera as its last heartbeat reported it (robot app 0.6.0+; older robots send none). */
export type RobotCamera = {
  state: 'idle' | 'ok' | 'stalled' | 'recovering' | 'unavailable';
  fps?: number | null;
  since?: number | null;
  media?: Record<string, unknown> | null;
};

export interface ReachyStatus {
  paired: boolean;
```
and replace
```ts
  landmark_fps?: number | null;
  alert_contacts?: number;
```
with
```ts
  landmark_fps?: number | null;
  camera?: RobotCamera | null;
  alert_contacts?: number;
```

- [ ] **Step 11: Add the strings (en and zh-TW)**

`src/i18n.ts`, in `en.translation.reachy`. Replace
```ts
        cameraRate: 'Camera rate',
        lastTask: 'Last reminder',
```
with
```ts
        cameraRate: 'Camera rate',
        robotCamera: 'Robot camera',
        cameraNotWorking: 'Robot camera not working',
        lastTask: 'Last reminder',
```
In `zh-TW.translation.reachy`, replace
```ts
        cameraRate: '鏡頭速率',
        lastTask: '最近一次提醒',
```
with
```ts
        cameraRate: '鏡頭速率',
        robotCamera: '機器人鏡頭',
        cameraNotWorking: '機器人鏡頭無法使用',
        lastTask: '最近一次提醒',
```
(`cameraRate`, the existing row, is the rate the server receives frames at (`landmark_fps`). The new row is the rate the
robot's camera hands frames to the robot app, so it gets its own label.)

- [ ] **Step 12: Show the warning and the rate on the card**

`src/components/ReachyCard.tsx`. Four exact replacements.

12a. The import and the constants at the top. Replace
```tsx
import { ApiError, fetchReachyStatus, fetchTodayDoses, pairReachy, queueReachyTask, setAutoRecord, startCheckin, unpairReachy, type ReachyStatus } from '../lib/reachy-api';

const CHECKIN_SCOPES = ['robot_microphone', 'cloud_voice', 'conversation_analysis', 'safety_alerts'];
```
with
```tsx
import { ApiError, fetchReachyStatus, fetchTodayDoses, pairReachy, queueReachyTask, setAutoRecord, startCheckin, unpairReachy, type ReachyStatus, type RobotCamera } from '../lib/reachy-api';

const CHECKIN_SCOPES = ['robot_microphone', 'cloud_voice', 'conversation_analysis', 'safety_alerts'];
/** Camera states the robot's watchdog reports when it gets no frames (robot app 0.6.0+). */
const CAMERA_PROBLEMS: RobotCamera['state'][] = ['stalled', 'recovering', 'unavailable'];
```

12b. Just before the JSX. Replace
```tsx
    setTestNotice({ key: 'reachy.testAlertSent', name: dose.name, time: clockTime(dose.scheduled_time) });
    await refresh();
  });

  return (
```
with
```tsx
    setTestNotice({ key: 'reachy.testAlertSent', name: dose.name, time: clockTime(dose.scheduled_time) });
    await refresh();
  });

  // Only an online robot's report is current. `idle`: no session is attached, so the camera isn't read.
  const camera = status?.online ? status.camera ?? null : null;
  const cameraFps = camera && camera.state !== 'idle' && camera.fps != null ? camera.fps : null;

  return (
```

12c. The rate row inside the `<dl>`. Replace
```tsx
            <dt className="text-gray-500">{t('reachy.cameraRate')}</dt>
            <dd>{status.landmark_fps != null ? `${status.landmark_fps.toFixed(1)} fps` : '—'}</dd>
```
with
```tsx
            <dt className="text-gray-500">{t('reachy.cameraRate')}</dt>
            <dd>{status.landmark_fps != null ? `${status.landmark_fps.toFixed(1)} fps` : '—'}</dd>
            {cameraFps != null && (
              <>
                <dt className="text-gray-500">{t('reachy.robotCamera')}</dt>
                <dd>{`${cameraFps.toFixed(1)} fps`}</dd>
              </>
            )}
```

12d. The warning under the status rows. Replace
```tsx
            <dd>{status.last_task ? `${new Date(status.last_task.slot_time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} · ${status.last_task.status}` : '—'}</dd>
          </dl>
```
with
```tsx
            <dd>{status.last_task ? `${new Date(status.last_task.slot_time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} · ${status.last_task.status}` : '—'}</dd>
          </dl>
          {camera && CAMERA_PROBLEMS.includes(camera.state) && (
            <p role="alert" className="rounded-xl bg-amber-50 border border-amber-200 p-3 text-sm text-amber-900">
              {t('reachy.cameraNotWorking')}
            </p>
          )}
```
(The amber box matches the card's existing `noAlertContacts` warning. `<>` needs no import: `tsconfig.app.json` uses
`"jsx": "react-jsx"`, and `Layout.tsx` already uses fragments.)

- [ ] **Step 13: Type-check, build, and run the Reachy Playwright tests**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus/frontend_source && npx tsc -b && npm run build && npx playwright test --config "C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/pw-frontend.config.mjs" tests/reachy.spec.ts
```
Expected: `tsc -b` prints nothing and exits 0; the build ends with "built in"; Playwright `16 passed`.
Then lint the three changed source files (CLAUDE.md: don't add eslint errors):
```bash
npx eslint src/components/ReachyCard.tsx src/lib/reachy-api.ts src/i18n.ts
```
Expected: no output (these files had no errors before either).

- [ ] **Step 14: Screenshots at 390 and 1280 px (spec §5)**

The screenshot spec lives in the scratchpad, never in the repo. If these files are missing, create them with exactly the
contents below.

`C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/t3-shots/camera-card.spec.ts`:
```ts
// Screenshots of the Reachy card's camera warning (Task 3). Not part of the repo: run with t3-shots.config.mjs.
import { expect, test, type Page } from '@playwright/test';

const OUT = process.env.T3_SHOTS_OUT ?? '.';

async function open(page: Page, camera: Record<string, unknown>) {
  await page.addInitScript(() => {
    for (const key of ['face_auth_session', 'onboarding_complete', 'onboarding_face_done']) localStorage.setItem(key, 'true');
    localStorage.setItem('face_auth_user', JSON.stringify({ name: 'Robot Test', u_id: 7, loginAt: new Date().toISOString() }));
    localStorage.setItem('face_auth_token', 'test-face-token');
  });
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/consent/status') {
      return route.fulfill({ json: { terms_version: '2026-10', core_current: true, robot_current: true,
        scopes: { robot_camera: { granted: true, terms_version: '2026-10', kind: 'robot' } } } });
    }
    if (path === '/api/reachy/status') {
      return route.fulfill({ json: { paired: true, device_id: 'd-1', label: 'Reachy Mini', auto_record: false,
        last_seen_at: new Date().toISOString(), online: true, robot_reachable: true, landmark_fps: 9.6,
        last_task: null, alert_contacts: 1, camera } });
    }
    if (path === '/api/notify/settings') return route.fulfill({ json: {} });
    return route.fulfill({ json: [] });
  });
  await page.goto('/settings');
}

for (const width of [390, 1280]) {
  test(`camera warning and rate at ${width} px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const card = page.locator('section[aria-labelledby="reachy-title"]');
    await open(page, { state: 'stalled', fps: 0, since: 1791700000.5, media: { available: true, released: true, no_media: false } });
    await expect(card.getByRole('alert')).toBeVisible();
    await card.scrollIntoViewIfNeeded();
    await card.screenshot({ path: `${OUT}/reachy-camera-stalled-zh-${width}.png` });
    await page.getByRole('button', { name: '中文' }).first().click();          // switch the page to English
    await expect(card.getByRole('alert')).toHaveText('Robot camera not working');
    await card.screenshot({ path: `${OUT}/reachy-camera-stalled-en-${width}.png` });
  });
}
```

`C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/t3-shots.config.mjs`:
```js
export default {
  testDir: 'C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/t3-shots',
  testMatch: ['*.spec.ts'],
  timeout: 30000,
  workers: 1,
  reporter: 'list',
  use: { baseURL: 'http://localhost:4187' },
  webServer: {
    command: 'npx vite preview --port 4187 --strictPort',
    cwd: process.env.T3_FRONTEND ?? 'C:/Reachy_Mini/repo/MedAiCarePlus/frontend_source',
    url: 'http://localhost:4187',
    reuseExistingServer: false,
    timeout: 60000,
  },
};
```
The spec resolves `@playwright/test` through a `node_modules` junction next to it. Create it once if it is missing, then
run against the build from Step 13:
```bash
SP=/c/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad
[ -e "$SP/t3-shots/node_modules" ] || (cd "$SP/t3-shots" && cmd //c "mklink /J node_modules C:\\Reachy_Mini\\repo\\MedAiCarePlus\\frontend_source\\node_modules")
mkdir -p "$SP/t3-shots-out"
cd /c/Reachy_Mini/repo/MedAiCarePlus/frontend_source && T3_SHOTS_OUT="C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/t3-shots-out" npx playwright test --config "C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/t3-shots.config.mjs"
```
Expected: `2 passed`, and four PNGs in `t3-shots-out` (`reachy-camera-stalled-{zh,en}-{390,1280}.png`). Open them
(Read tool). Expected: a "Robot camera / 機器人鏡頭 0.0 fps" row under "Camera rate / 鏡頭速率", then an amber box
"Robot camera not working" / 「機器人鏡頭無法使用」 above the switches. Nothing overflows at 390 px. (At 390 px the
fixed top bar may cover the card's title in the element screenshot; that is the page's header, not a layout bug.)

- [ ] **Step 15: Check the frontend line endings and commit**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus
file frontend_source/src/lib/reachy-api.ts frontend_source/src/components/ReachyCard.tsx frontend_source/src/i18n.ts frontend_source/tests/reachy.spec.ts
git status --short
git add frontend_source/src/lib/reachy-api.ts frontend_source/src/components/ReachyCard.tsx frontend_source/src/i18n.ts frontend_source/tests/reachy.spec.ts
git diff --cached --stat
git commit -F - <<'EOF'
Reachy card: warn when the robot camera is not working

- The card shows "Robot camera not working" (zh-TW 機器人鏡頭無法使用)
  when the robot is online and its camera state is stalled, recovering
  or unavailable, and a "Robot camera" row with the camera's fps while a
  session runs (state not idle).
- Robots older than 0.6.0 send no camera; the card shows nothing new.
- reachy-api.ts: RobotCamera type and ReachyStatus.camera.
- Playwright: the warning for each failing state, and no warning when the
  camera works, the robot is idle, offline or older.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
```
Expected: all four files "with CRLF line terminators"; `git status --short` shows no `dist/` or `test-results/` (both
are ignored); `git diff --cached --stat` lists exactly the 4 files (`reachy-api.ts | 9`, `ReachyCard.tsx | 19`,
`i18n.ts | 4`, `reachy.spec.ts | 52`, measured on the scratch copy). The trailer must name the model that actually writes the commit.

#### NOTES

**Verified (read or run on 2026-10-11, at `37ba84e`):**
- `HeartbeatPayload` is at `app/routers/api_device.py:75-80`; `heartbeat()` at `:523-537` writes `status_detail` with
  `json.dumps(detail)` (`:528-534`) and replaces it on every call. `Literal`, `Field` and `BaseModel` are imported at
  `:14` and `:17`.
- `_status()` is at `app/routers/api_reachy.py:33-57` and did not select `status_detail`. Nothing in `app/` reads
  `status_detail` today (grep), so adding a key breaks no reader.
- `reachy_device.status_detail JSONB` exists (`sql/init.sql:290`); no schema change is needed.
- asyncpg has no JSONB codec here (`app/database.py`), so JSONB comes back as text; `api_conversations.py:39-44` and
  `api_device.py:497` already handle that the same way.
- The app's `RequestValidationError` handler (`app/main.py:67-73`) keeps FastAPI's default 422 for every route except
  `/api/ai/*`, so a bad camera object gives 422 on the device port.
- `tests/test_device_auth.py:50-54` fakes the status row; without `status_detail` there,
  `test_pairing_returns_token_once_stores_hash_and_replaces_active_device` breaks with a `KeyError`. Step 1g covers
  it. No other test fakes the status query (grep of `tests/`).
- The robot daemon's `GET http://127.0.0.1:8000/api/media/status` answered `{"available":true,"released":false,
  "no_media":false}` (read-only curl on the robot): small and harmless to show the browser.
- The whole task was applied to a scratch copy of the repo (`git archive HEAD`; the repo was not touched) and run:
  - Steps 2 and 9 failed exactly as stated.
  - Then `tests/test_device_api.py` + `tests/test_device_auth.py` gave 77 passed.
  - The full backend suite gave 1608 passed, 2 failed in the copy: BUG-37, and `test_env_example_...`, which failed
    only because the copy had no `.env.example`. With `.env.example` the OCR file passed, so the repo should give
    1609 passed plus BUG-37.
  - `npx tsc -b` exited 0, `npm run build` succeeded, Playwright `reachy.spec.ts` gave 16 passed, and eslint was clean
    on the three source files.
  - The screenshots (zh-TW and en, 390 and 1280 px) look right.
- `tsc -b` covers `src` only (`tsconfig.app.json` `include: ["src"]`); the spec files are not type-checked by it.

**Assumptions:**
- "Shows the camera fps while a session runs" means: a separate "Robot camera" row with `camera.fps`, shown when the
  robot is online and `camera.state !== 'idle'`. The existing "Camera rate" row (`landmark_fps`, what the server
  receives) is left unchanged, because the two rates differ (the camera's ceiling vs what the stream delivered).
- The warning is amber with `role="alert"`, like `noAlertContacts`. The existing tests at `reachy.spec.ts:217` and
  `:230` assert the card has no `alert`. They still pass, because their mock sends no `camera`.
- `since` is stored and returned but not shown. The card does not poll (it loads on mount and after actions), so a
  family member sees a state change on the next page load. The spec does not ask for live updates.

**Contract deviations:** none. All names are exactly as in the contract (`CameraReport`, `HeartbeatPayload.camera`,
`status_detail["camera"]` via `model_dump()`, status `"camera"`, `RobotCamera`, `status.camera?`). The new i18n keys
are `reachy.robotCamera` and `reachy.cameraNotWorking`; the contract names only the `reachy.*` namespace.

**Risks for other tasks (loud):**
1. **A bad camera object refuses the whole heartbeat (422).** That is the spec's rule ("unknown states are refused
   with 422"). But a 422 means no `last_seen_at`, no lease extension, and the robot looks offline. So Task 2's
   `CameraWatchdog.snapshot()` must always fit `CameraReport`:
   - `state` must be one of the five values;
   - `fps` must be between 0 and 240, or `None`;
   - `since` must be a number or `None`;
   - `media` must be a dict or `None`, never a list or a string, even if the daemon answers something odd.

   (Verified: `app_client._request` raises `RequestRejected`, a `BridgeError`, on a 422 (`app_client.py:144`), and
   `runner.heartbeat_once` logs "heartbeat failed" and returns (`runner.py:470-472`), so that heartbeat is lost.)
   Cross-check: Task 2's `snapshot()` fits: `state` is one of its five constants, `fps` is a rounded frame count over
   10 s (never negative, far below 240), `since` is `time.time()`, and `media` is a dict or `None`
   (`_read_media_status` drops anything else). Task 2's tests check the snapshot's keys and values in each state.
2. **Spec vs contract on `detach()`.** Resolved in Task 2 (NOTES, deviation 1): `detach()` keeps `unavailable` (with
   its `media`) and makes every other state `idle`, as spec A3 asks, so the card keeps warning after the search ends,
   until the next session starts.
3. `since` is unix seconds: Task 2's watchdog takes a separate wall clock (`wall=time.time`) for it.
4. `media` is stored as the robot sent it, with no size limit (only an authenticated robot can send it, like
   `missing_clips`). The daemon's answer is three booleans today.
5. Docs: `docs/DATA_FLOW.md:877-878` and `docs/reachy-data-transfer/02-data-contracts-and-failure-modes.md:39` list
   the heartbeat fields without `camera`. Spec section 6 lists only `CLAUDE.md` and `reachy_app/README.md`, so Task 10
   documents `camera` in `CLAUDE.md` and leaves these two files as they are (named as unresolved in the cross-check).

---

### Task 4: Server: the search before talking verifies with the face match alone, and identity is logged

Spec parts: B1 (identity rule for the search before talking) and B2 (identity log). Nothing on the robot, the
frontend, the heartbeat or the database changes here.

**Files:**
- Modify `app/services/monitor_service.py` (CRLF, 594 lines at `37ba84e`):
  - constants: one new constant after line 48 (`DOSE_VISION_INTERVAL = 0.25`), before line 50 (`log = ...`);
  - `MonitorSession` (lines 129-223): new field `started_mode` after line 138 (`mode: str = "dose"`); two new fields
    after line 156 (`identity_status: str = "searching"`); new `__post_init__` between line 200 (`started_at: ...`)
    and line 202 (`def verified`);
  - two new module functions `openvino_only()` and `_log_identity()` between `vision_interval()` (lines 304-310) and
    `class MonitorRegistry:` (line 313);
  - `MonitorRegistry.vision()` (lines 485-591): the identity block, lines 518-542, changes.
  - `MonitorRegistry.start()` (lines 324-352) does **not** change: `__post_init__` fills `started_mode` from `mode`.
- No change to `app/routers/api_device.py`: `monitor_start` (line 548) already passes the robot's `payload.mode` to
  `registry.replace(...)` (line 577), which calls `start()`, which builds `MonitorSession(..., mode=mode, ...)`
  (monitor_service.py:348-349). The mode switch after a record (monitor_service.py:590, api_monitor.py:145) changes
  only `mode`, never `started_mode`.
- Create `tests/test_search_identity.py` (new file; LF is fine, `core.autocrlf=true` stores LF either way).
- Modify `tests/test_device_api.py` (CRLF): one new test between `test_device_observe_session_has_no_dose`
  (lines 494-499) and `test_device_start_returns_503_when_models_not_ready` (line 502).

**Interfaces:**
- Consumes (existing code, unchanged): `MonitorRegistry.start(u_id, intk_id, face_label, display_name, *, mode,
  client_type, auto_commit)`; `FaceRecognitionService.identify_faces(frame) -> {"faces": [{"box": [x, y, w, h]
  (pixels, ints), "label": str, "distance": float}], "error": str | None, "saturated": bool}`
  (face_recognition_service.py:253-276; with 4 or more faces it returns `faces: []` and `saturated: True`); the
  landmark packet's `faces` (MediaPipe faces, `box` normalized); `overlap()`; the module logger `log`
  (`logging.getLogger(__name__)`, name `app.services.monitor_service`).
- Produces (contract names):
  - `MonitorSession.started_mode: str`: the mode the session started in, never changed.
  - `openvino_only(state) -> bool`: `state.client_type == "reachy" and state.started_mode == "observe"`.
  - `_log_identity(state, rule, distance, box_px, ov_faces, mp_faces) -> None`: one INFO line on the module logger for
    robot sessions only, on every change of `identity_status`, otherwise at most once per 5 s per session.
- Also produces (helpers not named in the contract, used only inside this module and its tests):
  `IDENTITY_LOG_SECONDS = 5.0`, `MonitorSession.identity_logged_status: str | None`,
  `MonitorSession.identity_logged_at: float`.
- The log line is exactly:
  `identity of session <session_id>: status=<identity_status> rule=<openvino_only|with_mediapipe> distance=<0.000|none> box_px=<int|none> ov_faces=<int> mp_faces=<int>`.
  No image, name or face label is ever logged.

**Line endings.** `app/services/monitor_service.py` and `tests/test_device_api.py` are CRLF. Use the Edit tool (it keeps
a file's line endings). After the edits, `file <path>` must still say "with CRLF line terminators", and
`git diff --stat` must show only the changed lines (no whole-file rewrite).

All commands run in Git Bash from `C:/Reachy_Mini/repo/MedAiCarePlus`.

- [ ] **Step 0: Check the branch**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && git branch --show-current && git status --short
```
Expected: `robot-camera-thinking`, and none of this task's three files listed as changed. If an earlier task already
changed `app/services/monitor_service.py`, keep its lines: the edits below replace only the exact text they quote.

- [ ] **Step 1: Write the failing tests (new file)**

Create `tests/test_search_identity.py` with exactly this content. It reuses the fakes of `tests/test_monitor_backend.py`
and `tests/test_monitor_modes.py`, the way `tests/test_robot_emotion.py` does:

```python
"""The search before talking verifies the patient with the OpenVINO match alone; identity is logged.

A robot session that started in observe mode (the search before a check-in or a dose prompt) no longer needs
MediaPipe's short-range face detector to see the face too: it misses faces under about 50 px wide (beyond about
1.35 m from the robot). Every other session keeps the MediaPipe gate. Robot sessions log each identity outcome,
rate-limited, with numbers only (spec 2026-10-11, B1 and B2).
"""

import asyncio
import logging

import pytest

from tests.test_monitor_backend import _Detector, _Emotion, _jpeg, _packet
from tests.test_monitor_modes import _Commits, _Detector as _ScriptedDetector, _feed, _verify
from tests.test_monitor_modes import _packet as _timed_packet

from app.services import monitor_service as service
from app.services.monitor_service import MonitorRegistry

# OpenVINO boxes are pixels of the 100x100 test frame: [10, 10, 40, 40] is the normalized [0.1, 0.1, 0.4, 0.4] of
# the MediaPipe face in _packet().
PATIENT = {"label": "pearl", "distance": 0.31, "box": [10, 10, 40, 40]}


class _OpenVino:
    """FaceRecognitionService.identify_faces, with the faces a test sets."""

    def __init__(self):
        self.faces = [dict(PATIENT)]
        self.saturated = False

    def identify_faces(self, frame):
        return {"faces": [dict(face) for face in self.faces], "error": None, "saturated": self.saturated}


@pytest.fixture
def faces(monkeypatch):
    openvino = _OpenVino()
    monkeypatch.setattr(service.IntakeDetectionService, "get_instance", classmethod(lambda cls: _Detector()))
    monkeypatch.setattr(service.FaceRecognitionService, "get_instance", classmethod(lambda cls: openvino))
    monkeypatch.setattr(service.EmotionService, "get_instance", classmethod(lambda cls: _Emotion()))
    return openvino


def _far(seq):
    """The patient beyond about 1.35 m: OpenVINO finds the face, MediaPipe's short-range detector doesn't."""
    return {**_packet(seq), "faces": []}


async def _never_commit(state, candidate, method):
    raise AssertionError("an identity check must not record a dose")


async def _identity_checks(registry, state, packets):
    """One landmark packet, then one identity check on the same frame, as the robot's frame stream does."""
    result = None
    for packet in packets:
        await registry.landmarks(state, packet)
        result = await registry.vision(state, packet["frame_seq"], _jpeg(), _never_commit)
    return result


def _identity_lines(caplog):
    return [record for record in caplog.records
            if record.name == service.log.name and record.getMessage().startswith("identity of session ")]


# ── B1: which sessions use the OpenVINO-only rule ────────────────────────────

@pytest.mark.parametrize("client_type,mode,expected", [
    ("reachy", "observe", True), ("reachy", "dose", False), ("browser", "observe", False), ("browser", "dose", False)])
def test_only_a_robot_session_that_started_in_observe_uses_the_openvino_only_rule(client_type, mode, expected):
    state = MonitorRegistry().start(7, 11 if mode == "dose" else None, "Pearl", "Pearl",
                                    mode=mode, client_type=client_type)
    assert state.started_mode == mode
    assert service.openvino_only(state) is expected


def test_a_search_session_verifies_the_patient_without_a_mediapipe_face(faces):
    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, None, "Pearl", "Pearl", mode="observe", client_type="reachy")
        first = await _identity_checks(registry, state, [_far(1)])
        assert first["identity_status"] == "verifying" and state.identity_hits == 1
        result = await _identity_checks(registry, state, [_far(2)])
        assert result["identity_status"] == "verified"
        assert result["target_box"] == [0.1, 0.1, 0.4, 0.4]
        assert result["intk_id"] is None and result["recorded"] is None
    asyncio.run(scenario())


@pytest.mark.parametrize("openvino,saturated,status", [
    ([], True, "searching"),                                                    # 4 or more faces: none is trusted
    ([PATIENT, {**PATIENT, "box": [55, 55, 30, 30]}], False, "mismatch"),       # two faces labelled as the patient
    ([{**PATIENT, "label": "bob"}], False, "mismatch"),                         # someone else
])
def test_a_search_session_still_needs_exactly_one_patient_face_among_fewer_than_four(faces, openvino, saturated,
                                                                                       status):
    faces.faces, faces.saturated = openvino, saturated

    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, None, "Pearl", "Pearl", mode="observe", client_type="reachy")
        result = await _identity_checks(registry, state, [_far(1), _far(2), _far(3)])
        assert result["identity_status"] == status
        assert state.identity_hits == 0 and result["target_box"] is None
    asyncio.run(scenario())


def test_a_search_session_needs_two_hits_in_a_row_on_overlapping_boxes(faces):
    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, None, "Pearl", "Pearl", mode="observe", client_type="reachy")
        await _identity_checks(registry, state, [_far(1)])
        faces.faces = [{**PATIENT, "box": [60, 60, 30, 30]}]      # the match jumped across the frame: start again
        result = await _identity_checks(registry, state, [_far(2)])
        assert result["identity_status"] == "verifying" and state.identity_hits == 1
        result = await _identity_checks(registry, state, [_far(3)])
        assert result["identity_status"] == "verified"
    asyncio.run(scenario())


@pytest.mark.parametrize("client_type", ["reachy", "browser"])
def test_a_dose_session_still_needs_the_mediapipe_face(faces, client_type):
    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl", client_type=client_type, auto_commit=False)
        result = await _identity_checks(registry, state, [_far(1), _far(2), _far(3)])
        assert result["identity_status"] == "ambiguous"
        assert state.identity_hits == 0 and result["target_box"] is None
        # The same patient, seen by MediaPipe too, verifies as before.
        result = await _identity_checks(registry, state, [_packet(4), _packet(5)])
        assert result["identity_status"] == "verified"
    asyncio.run(scenario())


def test_a_robot_dose_session_switched_to_observe_after_a_record_still_needs_the_mediapipe_face(faces, monkeypatch):
    detector = _ScriptedDetector()
    monkeypatch.setattr(service.IntakeDetectionService, "get_instance", classmethod(lambda cls: detector))

    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl", client_type="reachy", auto_commit=True)
        last = await _feed(registry, state, 15, 2.0)
        _verify(state)
        detector.script = {last + 1: ("confirmed", "evt-dose")}
        commits = _Commits()
        await registry.landmarks(state, _timed_packet(last + 1, last / 15))
        await registry.vision(state, last + 1, _jpeg(), commits)
        assert commits.calls == [("evt-dose", "auto")] and state.mode == "observe"
        assert state.started_mode == "dose" and service.openvino_only(state) is False
        # The next identity check is due (once verified it runs every 0.5 s), and the patient has stepped back:
        # OpenVINO still matches, MediaPipe sees no face.
        state.verified_at -= 1.0
        for seq in (last + 2, last + 3):
            await registry.landmarks(state, {**_timed_packet(seq, seq / 15), "faces": []})
            result = await registry.vision(state, seq, _jpeg(), commits)
        assert result["identity_status"] == "ambiguous" and state.identity_hits == 0
        assert commits.calls == [("evt-dose", "auto")]
    asyncio.run(scenario())


# ── B2: the identity log ─────────────────────────────────────────────────────

def test_identity_log_lines_carry_numbers_and_no_names(faces, caplog):
    faces.faces = [dict(PATIENT), {"label": "bob", "distance": 0.52, "box": [55, 20, 30, 30]}]

    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, None, "Pearl", "Pearl Chen", mode="observe", client_type="reachy")
        await _identity_checks(registry, state, [_far(1), _far(2)])
        return state

    with caplog.at_level(logging.INFO, logger=service.log.name):
        state = asyncio.run(scenario())
    lines = [record.getMessage() for record in _identity_lines(caplog)]
    # The first check logs its status (verifying), the second a change (verified).
    assert lines == [
        f"identity of session {state.session_id}: status=verifying rule=openvino_only distance=0.310 box_px=40 "
        "ov_faces=2 mp_faces=0",
        f"identity of session {state.session_id}: status=verified rule=openvino_only distance=0.310 box_px=40 "
        "ov_faces=2 mp_faces=0",
    ]
    for line in lines:
        assert "pearl" not in line.lower() and "chen" not in line.lower() and "bob" not in line.lower()


def test_identity_log_is_rate_limited_per_session_and_robot_only(caplog):
    registry = MonitorRegistry()
    state = registry.start(7, None, "Pearl", "Pearl", mode="observe", client_type="reachy")
    with caplog.at_level(logging.INFO, logger=service.log.name):
        service._log_identity(state, "openvino_only", None, None, 0, 0)          # searching: the first line
        service._log_identity(state, "openvino_only", None, None, 0, 0)          # same status within 5 s: dropped
        assert len(_identity_lines(caplog)) == 1
        state.identity_status = "verifying"
        service._log_identity(state, "openvino_only", 0.31, 40, 1, 0)            # a change always logs
        service._log_identity(state, "openvino_only", 0.30, 41, 1, 0)
        assert len(_identity_lines(caplog)) == 2
        state.identity_logged_at -= service.IDENTITY_LOG_SECONDS                  # 5 s later
        service._log_identity(state, "openvino_only", 0.29, 43, 1, 1)
        assert len(_identity_lines(caplog)) == 3
        browser = MonitorRegistry().start(8, 11, "Jane", "Jane")
        service._log_identity(browser, "with_mediapipe", 0.2, 40, 1, 1)          # browser sessions: never
    lines = _identity_lines(caplog)
    assert len(lines) == 3 and all(record.levelno == logging.INFO for record in lines)
    assert lines[0].getMessage().endswith("status=searching rule=openvino_only distance=none box_px=none "
                                          "ov_faces=0 mp_faces=0")
    assert lines[2].getMessage().endswith("status=verifying rule=openvino_only distance=0.290 box_px=43 "
                                          "ov_faces=1 mp_faces=1")
```

Notes on the fakes (all checked against the current files):
- `tests/test_monitor_backend.py`: `_Detector` (always WAITING), `_Emotion`, `_jpeg()` (a 100x100 black JPEG),
  `_packet(seq)` (one MediaPipe face at `[0.1, 0.1, 0.4, 0.4]`, timestamp `seq / 15`).
- `tests/test_monitor_modes.py`: `_Detector(script)` (scripted decisions per frame), `_Commits`, `_feed(registry,
  state, fps, seconds)`, `_verify(state)`, `_packet(seq, timestamp)`.
- `tests/` has no `__init__.py`; `from tests.… import` works because `python -m pytest` runs from the repo root
  (`tests/test_robot_emotion.py` does the same).

- [ ] **Step 2: Add the failing device test**

In `tests/test_device_api.py`, use the Edit tool. Replace:

```python
    assert body["mode"] == "observe" and body["intk_id"] is None and body["auto_commit"] is False


def test_device_start_returns_503_when_models_not_ready(env, monkeypatch):
```

with:

```python
    assert body["mode"] == "observe" and body["intk_id"] is None and body["auto_commit"] is False


def test_device_sessions_keep_the_mode_they_started_in(env):
    # The search before talking (observe) verifies with the OpenVINO match alone; a dose session never does.
    search = env.registry.sessions[_start(env, mode="observe").json()["session_id"]]
    assert search.started_mode == "observe" and monitor_service.openvino_only(search) is True
    dose = env.registry.sessions[_start(env, mode="dose", intk_id=101, task_id=TASK_ID).json()["session_id"]]
    assert dose.started_mode == "dose" and monitor_service.openvino_only(dose) is False


def test_device_start_returns_503_when_models_not_ready(env, monkeypatch):
```

(`monitor_service`, `_start`, `TASK_ID` and the `env` fixture already exist in that file: import at line 25,
`_start` at line 468, `TASK_ID` at line 30, `env` at lines 159-160. Both starts already work in that fixture:
`test_device_observe_session_has_no_dose` and `test_robot_then_browser_gets_busy_other_client` use them.)

- [ ] **Step 3: Run the new tests and see them fail**

```bash
.venv/Scripts/python.exe -m pytest tests/test_search_identity.py "tests/test_device_api.py::test_device_sessions_keep_the_mode_they_started_in" -q -p no:cacheprovider
```
Expected: `10 failed, 5 passed`. The failures:
- 6 x `AttributeError: 'MonitorSession' object has no attribute 'started_mode'` (the 4 `openvino_only_rule` cases, the
  switched-to-observe test, the device test);
- 2 x `AssertionError: assert 'ambiguous' == 'verifying'` (`verifies_the_patient_without_a_mediapipe_face`,
  `two_hits_in_a_row`): today's rule needs a MediaPipe face;
- `assert [] == [...]` in `test_identity_log_lines_carry_numbers_and_no_names` (nothing is logged yet);
- `AttributeError: module 'app.services.monitor_service' has no attribute '_log_identity'`.

The 5 that already pass are guards for what must not change: `still_needs_exactly_one_patient_face_among_fewer_than_four`
(3 cases) and `a_dose_session_still_needs_the_mediapipe_face` (2 cases).

- [ ] **Step 4: Add the log constant**

In `app/services/monitor_service.py`, replace:

```python
VISION_INTERVAL = 0.5
DOSE_VISION_INTERVAL = 0.25

log = logging.getLogger(__name__)
```

with:

```python
VISION_INTERVAL = 0.5
DOSE_VISION_INTERVAL = 0.25
# A robot session's identity log (_log_identity): a line on every change of identity_status, otherwise at most one
# per IDENTITY_LOG_SECONDS (identity runs up to 2 Hz).
IDENTITY_LOG_SECONDS = 5.0

log = logging.getLogger(__name__)
```

- [ ] **Step 5: Add the session fields and `__post_init__`**

Still in `app/services/monitor_service.py`, three edits in `MonitorSession`.

(a) Replace:

```python
    # 'observe' sessions carry no dose and have no commit path (see vision()).
    mode: str = "dose"
    client_type: str = "browser"
```

with:

```python
    # 'observe' sessions carry no dose and have no commit path (see vision()).
    mode: str = "dose"
    # The mode the session started in (set in __post_init__), never changed: `mode` becomes 'observe' after a record,
    # so only this tells the search before talking (a robot session started in 'observe') apart (openvino_only).
    started_mode: str = ""
    client_type: str = "browser"
```

(b) Replace:

```python
    identity_status: str = "searching"
    # The live expression shown to the client: only ever an uncovered face.
```

with:

```python
    identity_status: str = "searching"
    # The identity log (_log_identity): the status it last logged, and when (monotonic).
    identity_logged_status: str | None = None
    identity_logged_at: float = float("-inf")
    # The live expression shown to the client: only ever an uncovered face.
```

(c) Replace:

```python
    started_at: datetime.datetime = field(default_factory=schedule.current_time)

    def verified(self, now: float | None = None) -> bool:
```

with:

```python
    started_at: datetime.datetime = field(default_factory=schedule.current_time)

    def __post_init__(self) -> None:
        if not self.started_mode:
            self.started_mode = self.mode

    def verified(self, now: float | None = None) -> bool:
```

`MonitorSession` is built in exactly one place (`MonitorRegistry.start`, line 348, positional arguments for the first
six fields only), so the new fields with defaults change nothing else.

- [ ] **Step 6: Add `openvino_only` and `_log_identity`**

Replace:

```python
    if state.intk_id is not None and not state.dose_emotion_done and state.verified():
        return DOSE_VISION_INTERVAL
    return VISION_INTERVAL


class MonitorRegistry:
```

with:

```python
    if state.intk_id is not None and not state.dose_emotion_done and state.verified():
        return DOSE_VISION_INTERVAL
    return VISION_INTERVAL


def openvino_only(state: MonitorSession) -> bool:
    """The identity rule of the search before talking: a robot session that started in observe mode verifies the
    patient with the OpenVINO match alone. MediaPipe's short-range face detector misses faces under about 50 px wide
    (beyond about 1.35 m from the robot), where dashboard login, which uses OpenVINO alone, still works. No dose is
    ever recorded from such a session. Every other session (dose sessions, also once a record switched them to
    observe, and browser sessions) also needs a MediaPipe face on the same box."""
    return state.client_type == "reachy" and state.started_mode == "observe"


def _log_identity(state: MonitorSession, rule: str, distance: float | None, box_px: int | None,
                  ov_faces: int, mp_faces: int) -> None:
    """One INFO line for a robot session's identity check: on every change of identity_status, otherwise at most one
    per IDENTITY_LOG_SECONDS. Numbers only: no image, name or face label."""
    if state.client_type != "reachy":
        return
    now = time.monotonic()
    if state.identity_status == state.identity_logged_status and now - state.identity_logged_at < IDENTITY_LOG_SECONDS:
        return
    state.identity_logged_status, state.identity_logged_at = state.identity_status, now
    log.info("identity of session %s: status=%s rule=%s distance=%s box_px=%s ov_faces=%d mp_faces=%d",
             state.session_id, state.identity_status, rule,
             "none" if distance is None else f"{distance:.3f}", "none" if box_px is None else box_px,
             ov_faces, mp_faces)


class MonitorRegistry:
```

- [ ] **Step 7: Use the rule and the log in `vision()`**

Two edits inside `MonitorRegistry.vision()`.

(a) Replace:

```python
            if matches is not None:
                targets = [face for face in matches["faces"] if face["label"].lower() == state.face_label]
                if matches["error"] or matches["saturated"] or len(targets) != 1:
                    state.identity_hits = 0
                    state.target_box = None
                    state.identity_status = "mismatch" if matches["faces"] else "searching"
                else:
                    target = targets[0]
                    h, w = frame.shape[:2]
                    box = [target["box"][0] / w, target["box"][1] / h, target["box"][2] / w, target["box"][3] / h]
                    geometric_matches = sorted(((overlap(face["box"], box), i) for i, face in enumerate(faces)), reverse=True)
                    if not geometric_matches or geometric_matches[0][0] < .20 or len(faces) >= 4:
                        state.identity_hits = 0
                        state.target_box = None
                        state.identity_status = "ambiguous"
                    else:
```

with:

```python
            if matches is not None:
                rule = "openvino_only" if openvino_only(state) else "with_mediapipe"
                targets = [face for face in matches["faces"] if face["label"].lower() == state.face_label]
                if matches["error"] or matches["saturated"] or len(targets) != 1:
                    state.identity_hits = 0
                    state.target_box = None
                    state.identity_status = "mismatch" if matches["faces"] else "searching"
                else:
                    target = targets[0]
                    h, w = frame.shape[:2]
                    box = [target["box"][0] / w, target["box"][1] / h, target["box"][2] / w, target["box"][3] / h]
                    ambiguous = False
                    if rule == "with_mediapipe":
                        # The MediaPipe gate: a MediaPipe face on the same box, in a frame of fewer than 4 faces.
                        geometric_matches = sorted(((overlap(face["box"], box), i) for i, face in enumerate(faces)),
                                                   reverse=True)
                        ambiguous = not geometric_matches or geometric_matches[0][0] < .20 or len(faces) >= 4
                    if ambiguous:
                        state.identity_hits = 0
                        state.target_box = None
                        state.identity_status = "ambiguous"
                    else:
```

(b) Replace:

```python
                        state.verified_at = now
                        state.identity_status = "verified" if state.identity_hits >= 2 else "verifying"
            if not state.verified(now) or state.target_box is None:
```

with:

```python
                        state.verified_at = now
                        state.identity_status = "verified" if state.identity_hits >= 2 else "verifying"
                # The closest patient-labelled face (its OpenVINO box is in pixels of this frame). A frame of 4 or
                # more faces comes back saturated with no faces at all: logged as 4.
                best = min(targets, key=lambda face: face["distance"], default=None)
                _log_identity(state, rule, None if best is None else best["distance"],
                              None if best is None else best["box"][2],
                              4 if matches["saturated"] else len(matches["faces"]), len(faces))
            if not state.verified(now) or state.target_box is None:
```

After both edits the whole method reads as follows (check it against the file; everything outside the identity
block is unchanged):

```python
    async def vision(self, state: MonitorSession, frame_seq: int, image_bytes: bytes, commit_callback) -> dict:
        frame = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if frame is None or frame.shape[0] > 1080 or frame.shape[1] > 1920:
            raise ValueError("Invalid camera frame")
        async with state.lock:
            now = time.monotonic()
            _expire_candidate(state, now)
            # Use the landmark packet for this exact camera frame. Reading
            # last_packet here made normal request skew discard almost every
            # identity result once landmark capture ran faster than JPEG
            # uploads, and could associate a face with another frame.
            packet = state.packets.get(frame_seq)
            if frame_seq <= state.last_vision_seq or packet is None:
                return state.public()
            state.last_vision_seq = frame_seq
            pending_candidate = (state.candidate is not None and not state.candidate.get("ready", False)
                                 and frame_seq >= int(state.candidate.get("frame_seq", frame_seq)))
            # A detector candidate can be emitted while the previous identity
            # proof is still fresh. Require a new face match from this event
            # frame (or a later one) before allowing a commit.
            run_identity = pending_candidate or time.monotonic() - state.verified_at >= .5 or not state.verified()
            faces = packet["faces"]
        loop = asyncio.get_running_loop()
        matches = None
        if run_identity:
            matches = await loop.run_in_executor(None, FaceRecognitionService.get_instance().identify_faces, frame)
        async with state.lock:
            # A newer vision call owns the state now; never let this slower
            # inference overwrite its identity, emotion, or candidate.
            packet = state.packets.get(frame_seq)
            if state.ended or state.last_vision_seq != frame_seq or packet is None:
                return state.public()
            now = time.monotonic()
            if matches is not None:
                rule = "openvino_only" if openvino_only(state) else "with_mediapipe"
                targets = [face for face in matches["faces"] if face["label"].lower() == state.face_label]
                if matches["error"] or matches["saturated"] or len(targets) != 1:
                    state.identity_hits = 0
                    state.target_box = None
                    state.identity_status = "mismatch" if matches["faces"] else "searching"
                else:
                    target = targets[0]
                    h, w = frame.shape[:2]
                    box = [target["box"][0] / w, target["box"][1] / h, target["box"][2] / w, target["box"][3] / h]
                    ambiguous = False
                    if rule == "with_mediapipe":
                        # The MediaPipe gate: a MediaPipe face on the same box, in a frame of fewer than 4 faces.
                        geometric_matches = sorted(((overlap(face["box"], box), i) for i, face in enumerate(faces)),
                                                   reverse=True)
                        ambiguous = not geometric_matches or geometric_matches[0][0] < .20 or len(faces) >= 4
                    if ambiguous:
                        state.identity_hits = 0
                        state.target_box = None
                        state.identity_status = "ambiguous"
                    else:
                        if state.last_identity_box and overlap(box, state.last_identity_box) < .20:
                            state.identity_hits = 0
                        state.identity_hits += 1
                        state.last_identity_box = box
                        state.target_box = box
                        state.identity_frame_seq = frame_seq
                        state.identity_distance = target["distance"]
                        state.verified_at = now
                        state.identity_status = "verified" if state.identity_hits >= 2 else "verifying"
                # The closest patient-labelled face (its OpenVINO box is in pixels of this frame). A frame of 4 or
                # more faces comes back saturated with no faces at all: logged as 4.
                best = min(targets, key=lambda face: face["distance"], default=None)
                _log_identity(state, rule, None if best is None else best["distance"],
                              None if best is None else best["box"][2],
                              4 if matches["saturated"] else len(matches["faces"]), len(faces))
            if not state.verified(now) or state.target_box is None:
                state.emotion, state.emotion_occluded = None, False
                return state.public()
            box = state.target_box
            h, w = frame.shape[:2]
            pixel_box = (int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h))
            selected = select_owned_observations(faces, packet["poses"], packet["hands"], box)
            if selected is None:
                state.emotion, state.emotion_occluded = None, False
                return state.public()
            # A covered mouth (the pill going in) is still scored, as an occluded sample for the dose's result
            # (dose_emotion prefers uncovered faces around the event); the live emotion shows only uncovered faces.
            occluded = _mouth_hidden(selected, packet["hands"], box)
            if occluded:
                state.emotion = None
            sample_time = state.packet_times.get(frame_seq, now)
        # A robot doing its own vision scores emotion itself and sends it with its landmark packets, so its
        # snapshot is identity-only. A robot streaming frames (vision_engine set) relies on the server for both.
        robot_scores_emotion = state.client_type == "reachy" and state.vision_engine is None
        emotion = None if robot_scores_emotion else await loop.run_in_executor(
            None, EmotionService.get_instance().predict_crop, crop_face(frame, pixel_box))
        async with state.lock:
            if not state.verified() or state.ended or state.last_vision_seq != frame_seq:
                return state.public()
            if emotion is not None:
                if emotion.get("detected"):
                    dose_emotion.add_sample(state, sample_time, emotion["probabilities"], occluded, "server")
                    state.emotion_occluded = occluded
                    if not occluded:
                        state.emotion = emotion
                elif not occluded:
                    state.emotion = None
            if occluded:
                # Unchanged recording policy: a candidate becomes ready only on a frame with the mouth uncovered.
                return state.public()
            if state.candidate and not state.candidate["ready"]:
                if time.monotonic() - state.candidate["created_at"] > CANDIDATE_TIMEOUT_SECONDS:
                    state.candidate = None
                elif (frame_seq >= state.candidate["frame_seq"]
                      and state.identity_frame_seq >= state.candidate["frame_seq"]
                      and state.verified()):
                    state.candidate["ready"] = True
                    reason = _hold_reason(state, state.candidate)
                    if reason:
                        _hold(state.candidate, reason)
                    elif state.candidate["decision"] == "confirmed" and state.mode == "dose":
                        state.recorded = await commit_callback(state, state.candidate, "auto")
                        state.mode = "observe"
            return state.public()
```

Why a search session still never records: it has `intk_id=None` and `mode="observe"` from `start()` (lines 332-333),
so `_hold_reason` returns `"observe"` for any candidate (line 287-288) and the commit branch needs `mode == "dose"`.

- [ ] **Step 8: Check the line endings**

```bash
file app/services/monitor_service.py tests/test_device_api.py && git diff --stat
```
Expected: both say "with CRLF line terminators"; `git diff --stat` shows `app/services/monitor_service.py | 54 ++-`
(52 insertions, 2 deletions) and `tests/test_device_api.py | 8 +` (no whole-file rewrite), plus whatever earlier
tasks left uncommitted. `tests/test_search_identity.py` is untracked.

- [ ] **Step 9: Run the new tests and see them pass**

```bash
.venv/Scripts/python.exe -m pytest tests/test_search_identity.py "tests/test_device_api.py::test_device_sessions_keep_the_mode_they_started_in" -q -p no:cacheprovider
```
Expected: `15 passed`.

- [ ] **Step 10: Run the monitor and device suites**

```bash
.venv/Scripts/python.exe -m pytest tests/test_search_identity.py tests/test_device_api.py tests/test_monitor_modes.py tests/test_monitor_backend.py tests/test_robot_emotion.py tests/test_dose_emotion.py -q -p no:cacheprovider
```
Expected: all pass, `164 passed` (136 in these files at `37ba84e`, plus Task 3's 13 tests in
`tests/test_device_api.py`, plus this task's 15; the prototype without Task 3 gave 151).

- [ ] **Step 11: Run the whole backend suite**

```bash
.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider
```
Expected: the count before this task plus 15 passed (`1624 passed`: the spec's 1596 baseline, plus Task 3's 13, plus
these 15; the prototype without Task 3 gave 1611), and the one known failure `test_conversation.py::test_a_broken_background_never_costs_the_reply` (BUG-37). If a
BUG-39 timing test fails, run it alone; it must pass there.

- [ ] **Step 12: Commit**

```bash
git add app/services/monitor_service.py tests/test_search_identity.py tests/test_device_api.py
git diff --cached --stat
git commit -F - <<'EOF'
Search before talking: verify with the face match alone, log identity

- A robot session that started in observe mode (the search before a
  check-in or a dose prompt) verifies the patient without a MediaPipe face
  on the same frame: exactly one patient face, fewer than 4 OpenVINO
  faces, 2 hits in a row on overlapping boxes. Dose sessions (also after a
  record switched them to observe) and browser sessions keep the
  MediaPipe gate. A search session still never records a dose.
- MonitorSession.started_mode keeps the mode a session started in.
- Robot sessions log each identity outcome at INFO (status, rule, patient
  distance, patient box width, OpenVINO and MediaPipe face counts): on
  every status change, otherwise at most once per 5 s. No names or labels.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
```
The trailer must name the model that actually writes the commit; change it if another model runs this task.
`git diff --cached --stat` must list exactly the 3 files above.

#### NOTES

**Contract.** All contract names are used as written: `MonitorSession.started_mode: str`, `openvino_only(state) ->
bool`, `_log_identity(state, rule, distance, box_px, ov_faces, mp_faces)` at INFO on the module logger, rate-limited on
every status change, else at most once per 5 s per session. Additions not named in the contract (internal only):
`IDENTITY_LOG_SECONDS = 5.0`, `MonitorSession.identity_logged_status`, `MonitorSession.identity_logged_at`.
`_log_identity` returns `None`.

**Small decisions (not contract deviations, but say so in review):**
- `started_mode` is a dataclass field with default `""`, filled from `mode` by `__post_init__`. This needs no change to
  `start()` and is right for any other way a `MonitorSession` is ever built. A dataclass field with a required value
  is impossible here: it comes after fields with defaults.
- The OpenVINO-only rule also drops today's MediaPipe-side "4 or more MediaPipe faces" check (`len(faces) >= 4`). The
  spec counts only OpenVINO faces for this rule ("fewer than 4 OpenVINO faces (`saturated` false)"), and OpenVINO's own
  limit (`saturated`) still applies. The MediaPipe rule keeps all of today's checks unchanged.
- `_log_identity` itself skips non-robot sessions (spec B2: "For robot sessions"), so browser sessions log nothing.
- Log values: `distance` and `box_px` are those of the patient-labelled face with the smallest distance (`none` when
  no face is labelled as the patient); `box_px` is the OpenVINO box width in pixels of the decoded frame (480x360 for
  the robot's server-vision stream). OpenVINO's face detector returns no faces at all when it sees 4 or more
  (`saturated`), so the line then says `ov_faces=4` (meaning "4 or more"). Face labels are never logged; the status
  words are the existing `searching / mismatch / ambiguous / verifying / verified`.
- The log is called only when identity actually ran on that frame (`matches is not None`), inside the session lock;
  it is one `log.info` call, so no extra work on the event loop.

**Risks:**
1. The dose prompt still needs the MediaPipe gate. After a relaxed verification in the search (`_searching`,
   `reachy_app/medcare_reachy/bridge/session.py:209-242`, observe session at line 210), the robot starts a new `dose`
   session for the prompt (`_med_prompt` from line 244, `_ensure_session("dose", ...)` at line 258; `_ensure_session`,
   lines 729-735, starts a new session whenever `(mode, intk_id)` changes). A patient verified at 2 m in the search
   is not verified there until MediaPipe sees the face (about 1.35 m). Meanwhile the robot waits up to
   `SEARCH_TIMEOUT` (600 s, line 34) and plays `waiting_for_patient` after `WRONG_PERSON_SECONDS` (20 s, line 45) of
   `ambiguous` (`_check_wrong_person`, lines 678-688). This is what the spec asks (B1: "Everything else keeps today's
   rule"); watch it in the robot check.
2. Other observe sessions of the robot use the relaxed rule too, because they also start in `observe`: the check-in
   conversation (it runs in the search's session: `self._go(CHECKIN)` at session.py:224) and `POST_SLOT_OBSERVE` (a
   new observe session, `_post_slot` at session.py:513-514). Both never record (`_hold_reason` returns `"observe"`); POST_SLOT_OBSERVE only reports
   `extra_events`, and its landmark detection still needs MediaPipe (`select_owned_observations`, unchanged).
3. OpenVINO alone on small faces may mislabel someone as the patient. The guards that remain: exactly one patient
   label, fewer than 4 faces, 2 hits in a row on overlapping boxes, and the gallery's own distance threshold. The new
   log lines (`distance`, `box_px`) are what the spec wants measured before any further change.
4. Log volume: one line per status change plus one per 5 s per session. Under the MediaPipe rule a status that flips
   between `verifying` and `ambiguous` can log at the identity rate (at most 2 Hz, monitor_service.py:43-46). Only
   robot sessions log.

**Verified facts (file:line at `37ba84e`):**
- `MonitorSession.mode` default `"dose"` at monitor_service.py:138; `identity_status` at :156; `started_at` at :200;
  `verified()` at :202-203 (2 hits and 1.5 s freshness).
- `MonitorRegistry.start` (:324-352) sets `intk_id, auto_commit = None, False` for observe (:332-333) and builds the
  session at :348-349. The only `MonitorSession(` construction in the repo is there (grep over `*.py`).
- Today's identity block: monitor_service.py:518-542; the MediaPipe gate is :528-532 (`geometric_matches`, `< .20`,
  `len(faces) >= 4`). The mode switch after a record: monitor_service.py:590 and api_monitor.py:145.
- `identify_faces` returns pixel boxes and `saturated: True` with no faces at 4 or more faces:
  face_recognition_service.py:253-276.
- The robot's device start passes `payload.mode` to `registry.replace` (api_device.py:548-582, call at :577); the
  robot searches in `observe` (`session.py:210`) and prompts in a new `dose` session (`session.py:258`).
- Logger name `app.services.monitor_service` (monitor_service.py:50, `logging.getLogger(__name__)`); caplog usage in
  the same style: tests/test_dose_emotion.py:302-308, tests/test_device_api.py:406-412.
- Prototype run (this exact code applied to a scratch copy of `git archive HEAD` at `37ba84e`, nothing in the repo
  changed): before the change, the new tests gave `9 failed, 5 passed` (plus the device test failing on
  `started_mode`); after it, `tests/test_search_identity.py` 14 passed, the six monitor/device files 151 passed, and the
  whole backend suite `1 failed, 1611 passed` (the failure is BUG-37's
  `test_a_broken_background_never_costs_the_reply`).

**Not in this task:** the CLAUDE.md lines for the search identity rule (spec §6) are Task 10's (Edit A).

---

### Task 5: AI connections backend: usable-model check per job, OpenRouter key from .env, voice-only model message

Spec parts: C1 (backend part and the startup warnings), C2, C3 (backend part). The card's texts for C1 and C3
(`jobs_usable` warnings, the voice-only message) are Task 6's, not here.

**Files:**
- Modify `app/services/ai_settings.py` (CRLF): new `job_usable()` inserted between `problems()` (ends line 173) and
  `with_model()` (line 176); one new line in `public()` after line 194 (`"no_key_in_use": ...`).
- Modify `app/services/ai_store.py` (CRLF): new `fill_openrouter_keys_from_env()` inserted between `seed_from_env()`
  (lines 45-71) and `change()` (decorator at line 74).
- Modify `app/main.py` (CRLF): `import logging` before line 2 (`import os`); `from app import config` before line 10
  (`from app.database import init_pool, close_pool`); a module logger after line 30
  (`from app.services.schedule import DoseRefused`); `lifespan()` lines 33-54.
- Modify `app/services/ai_profile.py` (CRLF): constants after line 27 (`_SETTINGS = ...`); new `_not_chat_model()`
  before `_failed()` (line 93); two new lines in `detect()` after line 117 (`message = _error_message(body)`).
- Modify `app/routers/api_ai.py` (CRLF): the docstring of `test_model()` (line 237) only. The route already returns
  `detect()`'s result (line 268) and saves it with `ai_store.save_test` (ai_store.py lines 138-145), so the new code
  reaches the card and `last_error` with no code change there.
- Test: append to `tests/test_ai_settings.py` (CRLF, ends line 110), `tests/test_ai_store.py` (CRLF, ends line 269),
  `tests/test_ai_profile.py` (CRLF, ends line 144), `tests/test_api_ai.py` (LF, ends line 380). Create
  `tests/test_ai_startup.py`.

**Interfaces:**
- Consumes (existing code, unchanged): `ai_settings.JOBS`, `ai_settings.Snapshot`, `ai_settings.job_targets(job,
  snapshot)`, `Target.model.profile`, `Target.connection.key_readable`, `Target.api_key`; `ai_store.change(conn)`,
  `ai_store.get_pool`, `ai_crypto.encrypt`, `ai_crypto.last4`; `config.OPENROUTER_API_KEY` (app/config.py:95,
  `os.getenv("OPENROUTER_API_KEY", "")`, not stripped); `ai_profile.detect`, `ai_profile._failed`.
- Produces (contract names, exactly):
  - `ai_settings.job_usable(snapshot, job) -> bool`
  - `ai_settings.public(snapshot)["jobs_usable"] == {"reply": bool, "safety": bool, "after_chat": bool}`
  - `ai_store.fill_openrouter_keys_from_env(api_key: str | None) -> int` (async)
  - `app/main.py` lifespan: the fill after `seed_from_env` and before `set_current(load)`; after the load, one WARNING
    per job where `not ai_settings.job_usable(...)` (logger `app.main`)
  - `ai_profile.NOT_CHAT_MODEL = "not_chat_model"` (the Test result's `error`, and so the model's `last_error`)
- Does not touch anything thinking-related. The thinking tasks (7-9) run after this one, so `public()`, `detect()`,
  `test_model()` and the lifespan are as at `37ba84e` here; Tasks 7 and 8 keep this task's lines.

**Line endings.** Every file above except `tests/test_api_ai.py` is CRLF (`git ls-files --eol` shows `w/crlf`;
`tests/test_api_ai.py` shows `w/lf`). Use the Edit tool (it keeps a file's line endings) and create
`tests/test_ai_startup.py` with the Write tool (LF, like `tests/test_api_ai.py`). After each edit, `file <path>` must
still say "with CRLF line terminators" for the CRLF files, and `git diff --stat` must show only the added lines (no
whole-file rewrite).

All commands run in Git Bash from `C:/Reachy_Mini/repo/MedAiCarePlus`.

- [ ] **Step 0: Check the branch**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && git branch --show-current && git status --short
```
Expected: `robot-camera-thinking`, and none of this task's files listed as changed.

#### Part A: which jobs have a usable model (C1)

- [ ] **Step 1: Write the failing tests for `job_usable` and `jobs_usable`**

Append to the end of `tests/test_ai_settings.py` (the file already imports `dataclasses`, `config`, `ai_profile`,
`ai_settings`, and has `_card_snapshot()`: connection 1 OpenAI with key `sk-proj-secret-abcd`, connection 2 OpenRouter
with no key; model 11 on 1, model 21 on 2, both with a profile; jobs `reply (11, 21)`, `safety (21, None)`,
`after_chat (21, None)`; the autouse fixture sets `config.OPENROUTER_API_KEY = "sk-or-v1-envkey1234"`):

```python


def test_a_job_is_usable_with_a_tested_model_whose_key_can_be_read():
    snapshot = _card_snapshot()   # reply: the OpenAI model (keyed); safety and after_chat: the keyless OpenRouter one
    assert [ai_settings.job_usable(snapshot, job) for job in ai_settings.JOBS] == [True, False, False]
    view = ai_settings.public(snapshot)
    assert view["jobs_usable"] == {"reply": True, "safety": False, "after_chat": False}
    assert view["no_key_in_use"] is False   # the older warning stays as it was


def test_a_usable_backup_is_enough():
    snapshot = dataclasses.replace(_card_snapshot(), jobs={**_card_snapshot().jobs, "safety": (21, 11)})
    assert ai_settings.job_usable(snapshot, "safety") is True


def test_an_untested_model_or_an_unreadable_key_is_not_usable():
    snapshot = _card_snapshot()
    untested = ai_settings.with_model(snapshot, dataclasses.replace(snapshot.models[11], profile=None))
    assert ai_settings.job_usable(untested, "reply") is False   # and its backup (21) has no key
    unreadable = dataclasses.replace(snapshot.connections[1], api_key="", key_readable=False)
    snapshot = dataclasses.replace(snapshot, connections={**snapshot.connections, 1: unreadable})
    assert ai_settings.job_usable(snapshot, "reply") is False


def test_a_job_with_no_model_is_not_usable():
    snapshot = dataclasses.replace(_card_snapshot(), jobs={"reply": (11, None)})
    assert ai_settings.public(snapshot)["jobs_usable"] == {"reply": True, "safety": False, "after_chat": False}


def test_before_loading_the_jobs_are_usable_only_with_the_env_key(monkeypatch):
    assert all(ai_settings.job_usable(ai_settings.current(), job) for job in ai_settings.JOBS)
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    assert not any(ai_settings.job_usable(ai_settings.current(), job) for job in ai_settings.JOBS)
```

- [ ] **Step 2: Run them and see them fail**

```bash
.venv/Scripts/python.exe -m pytest tests/test_ai_settings.py -q -p no:cacheprovider
```
Expected: `5 failed, 10 passed`. Four fail with `AttributeError: module 'app.services.ai_settings' has no attribute
'job_usable'`, `test_a_job_with_no_model_is_not_usable` with `KeyError: 'jobs_usable'`.

- [ ] **Step 3: Add `job_usable` and `jobs_usable`**

In `app/services/ai_settings.py`, insert this function between the end of `problems()` (`    return found`, line 173)
and `def with_model(` (line 176), keeping two blank lines on each side:

```python
def job_usable(snapshot: Snapshot, job: str) -> bool:
    """The job has a model it can use (spec C1): its main or its backup model is tested (has a profile) and its
    connection has a key this SECRET_KEY can read. The card warns under a job without one, and the start logs it."""
    return any(target.model.profile is not None and target.connection.key_readable and bool(target.api_key)
               for target in job_targets(job, snapshot))
```

Then `public()` becomes (one line added after `"no_key_in_use"`):

```python
def public(snapshot: Snapshot) -> dict:
    """What the card shows: everything but the keys (only their last 4 characters)."""
    in_use = [target for job in JOBS for target in job_targets(job, snapshot)]
    return {
        "connections": [{"connection_id": c.connection_id, "name": c.name, "kind": c.kind, "base_url": c.base_url,
                         "key_last4": c.key_last4, "key_readable": c.key_readable, "has_key": bool(c.key_last4),
                         "service_name": c.service_name} for c in snapshot.connections.values()],
        "models": [{"model_ref": m.model_ref, "connection_id": m.connection_id, "model_id": m.model_id,
                    "company": m.company, "region": m.region, "retention": m.retention, "profile": m.profile,
                    "tested": m.profile is not None, "copied_from_env": m.profile is not None and m.tested_at is None,
                    "tested_at": m.tested_at.isoformat() if m.tested_at else None, "test_ms": m.test_ms,
                    "last_error": m.last_error} for m in snapshot.models.values()],
        "jobs": {job: {"primary_ref": refs[0], "fallback_ref": refs[1]} for job, refs in snapshot.jobs.items()},
        "seeded_at": snapshot.seeded_at.isoformat() if snapshot.seeded_at else None,
        "no_key_in_use": not any(target.api_key for target in in_use),
        "jobs_usable": {job: job_usable(snapshot, job) for job in JOBS},
    }
```
(Task 8 later changes only the `"jobs"` line and keeps the `"jobs_usable"` line.)

Why both `key_readable` and `api_key`: `ai_store.load` (ai_store.py:30-33) gives a connection with no key
`api_key=""` and `key_readable=True`, and one whose key this `SECRET_KEY` can't read `api_key=""` and
`key_readable=False`. Either way it has no key it can send.

- [ ] **Step 4: Run them and see them pass**

```bash
.venv/Scripts/python.exe -m pytest tests/test_ai_settings.py -q -p no:cacheprovider
```
Expected: `15 passed`.

#### Part B: a keyless OpenRouter connection takes `.env`'s key at start (C2)

- [ ] **Step 5: Write the failing tests for `fill_openrouter_keys_from_env`**

Append to the end of `tests/test_ai_store.py`. It reuses the file's `Conn` fake (fetch answers by table name;
`statements(prefix)` gives the arguments of every statement starting with `prefix`; `transaction()` records
`"BEGIN"` and `"COMMIT"`/`"ROLLBACK"`), its autouse fixture (`SECRET_KEY = "the-secret"`), and its imports
(`asyncio`, `ai_crypto`, `ai_settings`, `ai_store`, `legal_service`):

```python


def _pool(monkeypatch, conn):
    """get_pool() answering with conn; the change() around the writes reloads an empty setup and registers nothing."""
    class Pool:
        def acquire(self):
            class Ctx:
                async def __aenter__(self):
                    return conn

                async def __aexit__(self, *exc):
                    return False
            return Ctx()

    async def load(conn):
        return ai_settings.Snapshot({}, {}, {}, None)

    async def register(conn):
        return None

    monkeypatch.setattr(ai_store, "get_pool", lambda: Pool())
    monkeypatch.setattr(ai_store, "load", load)
    monkeypatch.setattr(legal_service, "register_documents", register)


def test_a_keyless_openrouter_connection_takes_the_env_key_at_start(monkeypatch, caplog):
    key = "sk-or-v1-envFILL-2468"
    conn = Conn({"ai_connection": [
        {"connection_id": 1, "kind": "openrouter", "api_key_enc": None},
        {"connection_id": 2, "kind": "openrouter", "api_key_enc": ai_crypto.encrypt("sk-or-v1-card-1357")},
        {"connection_id": 3, "kind": "openai", "api_key_enc": None},
        {"connection_id": 4, "kind": "openrouter", "api_key_enc": None},
        {"connection_id": 5, "kind": "openrouter",   # saved under an older SECRET_KEY: still a key, never replaced
         "api_key_enc": ai_crypto.encrypt("sk-or-v1-old-9753", secret="an-older-secret")}]})
    _pool(monkeypatch, conn)
    with caplog.at_level("DEBUG", logger=ai_store.log.name):
        assert asyncio.run(ai_store.fill_openrouter_keys_from_env(f"  {key}\n")) == 2
    updates = conn.statements("UPDATE ai_connection")
    assert [args[0] for args in updates] == [1, 4]   # the keyed ones and the OpenAI connection are untouched
    assert all(ai_crypto.decrypt(args[1]) == key and args[2] == "2468" for args in updates)
    assert all("api_key_enc IS NULL" in entry[0] for entry in conn.sql
               if isinstance(entry, tuple) and entry[0].startswith("UPDATE"))
    assert conn.sql[1] == "BEGIN" and conn.sql[-1] == "COMMIT"   # both in one change()
    assert "filled the key of 2 OpenRouter connection(s) from .env" in caplog.text
    assert "envFILL" not in caplog.text   # the key is never logged


def test_without_an_env_key_the_start_fills_nothing(monkeypatch):
    def no_pool():
        raise AssertionError("no database work without a key")

    monkeypatch.setattr(ai_store, "get_pool", no_pool)
    for empty in (None, "", "   "):
        assert asyncio.run(ai_store.fill_openrouter_keys_from_env(empty)) == 0


def test_when_every_openrouter_connection_has_a_key_the_start_changes_nothing(monkeypatch, caplog):
    conn = Conn({"ai_connection": [
        {"connection_id": 1, "kind": "openrouter", "api_key_enc": ai_crypto.encrypt("sk-or-v1-card-1357")}]})
    _pool(monkeypatch, conn)
    with caplog.at_level("INFO", logger=ai_store.log.name):
        assert asyncio.run(ai_store.fill_openrouter_keys_from_env("sk-or-v1-envFILL-2468")) == 0
    assert conn.statements("UPDATE") == [] and "BEGIN" not in conn.sql and "filled" not in caplog.text
```

The `"  {key}\n"` argument is on purpose: `config.OPENROUTER_API_KEY` is not stripped (config.py:95), and
`seed_from_env` strips it (ai_store.py:58), so the fill must too.

- [ ] **Step 6: Run them and see them fail**

```bash
.venv/Scripts/python.exe -m pytest tests/test_ai_store.py -q -p no:cacheprovider
```
Expected: `3 failed, 14 passed`, each with `AttributeError: module 'app.services.ai_store' has no attribute
'fill_openrouter_keys_from_env'`.

- [ ] **Step 7: Add `fill_openrouter_keys_from_env`**

In `app/services/ai_store.py`, insert between the end of `seed_from_env()` (`    return True`, line 71) and
`@asynccontextmanager` (line 74), with two blank lines on each side:

```python
async def fill_openrouter_keys_from_env(api_key: str | None) -> int:
    """Every start, after the first-start copy: each OpenRouter connection with no key gets .env's
    OPENROUTER_API_KEY (spec C2), in one change(); returns how many. The first start copies whatever .env held then,
    so a key added to .env later never reached the card. A connection that has a key (even one this SECRET_KEY
    can't read) is never touched: the card stays the place to change keys. The key is never logged."""
    api_key = (api_key or "").strip()
    if not api_key:
        return 0
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT connection_id, kind, api_key_enc FROM ai_connection ORDER BY connection_id")
        keyless = [row["connection_id"] for row in rows if row["kind"] == "openrouter" and row["api_key_enc"] is None]
        if not keyless:
            return 0
        async with change(conn):
            for connection_id in keyless:
                await conn.execute("UPDATE ai_connection SET api_key_enc = $2, api_key_last4 = $3, updated_at = NOW() "
                                   "WHERE connection_id = $1 AND api_key_enc IS NULL", connection_id,
                                   ai_crypto.encrypt(api_key), ai_crypto.last4(api_key))
    log.info("filled the key of %d OpenRouter connection(s) from .env", len(keyless))
    return len(keyless)
```

Notes for the implementer:
- `change()` is defined lower in the same module; that is fine, it is looked up when the function runs.
- It takes its own pool connection (the contract has no `conn` argument). The lifespan holds one connection at that
  moment; the pool has `min_size=2, max_size=10` (app/database.py:10), so this cannot block.
- `change()` (and so the notices' re-registration) runs only when there is something to fill, not on every start.
- `api_key_enc IS NULL` in the UPDATE also guards against a key saved on the card between the SELECT and the UPDATE.

- [ ] **Step 8: Run them and see them pass**

```bash
.venv/Scripts/python.exe -m pytest tests/test_ai_store.py -q -p no:cacheprovider
```
Expected: `17 passed`.

#### Part C: the start runs the fill and logs one warning per unusable job (C1, C2)

- [ ] **Step 9: Write the failing lifespan test**

Create `tests/test_ai_startup.py` with exactly this content (it replaces every outside service of the lifespan with a
stub, so it needs no database, model or scheduler):

```python
"""The start (app/main.py lifespan): .env's OpenRouter key fills keyless OpenRouter connections after the first-start
copy and before the settings are loaded, and each job left with no usable model is logged once."""

import asyncio
import sys
import types

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.services import ai_profile, ai_settings, ai_store

ENV_KEY = "sk-or-v1-envSTART-8642"


def _snapshot():
    """The reply job's model has a key; the safety and after-chat model's OpenRouter connection has none."""
    openai = ai_settings.Connection(1, "OpenAI", "openai", ai_profile.BASE_URLS["openai"], "sk-proj-k-1234", "1234",
                                    True, "OpenAI")
    openrouter = ai_settings.Connection(2, "OpenRouter", "openrouter", ai_profile.BASE_URLS["openrouter"], "", None,
                                        True, "OpenRouter")
    models = {11: ai_settings.Model(11, 1, "gpt-5.4-mini", "OpenAI", "USA", "30 days",
                                    ai_profile.starting_profile("openai")),
              21: ai_settings.Model(21, 2, "google/gemini-2.5-flash", "Google", "global", "not kept",
                                    ai_profile.starting_profile("openrouter"))}
    return ai_settings.Snapshot({1: openai, 2: openrouter}, models,
                                {"reply": (11, None), "safety": (21, None), "after_chat": (21, None)})


class Pool:
    def acquire(self):
        class Ctx:
            async def __aenter__(self):
                return "conn"

            async def __aexit__(self, *exc):
                return False
        return Ctx()


def test_the_start_fills_keys_after_the_first_start_copy_and_warns_per_unusable_job(monkeypatch, caplog):
    from app import main
    calls = []

    async def nothing(*args):
        return None

    async def seed(conn):
        calls.append("seed")
        return False

    async def fill(api_key):
        calls.append(("fill", api_key))
        return 0

    async def load(conn):
        calls.append("load")
        return _snapshot()

    def check(snapshot):
        calls.append("check")
        return []

    monkeypatch.setattr(config, "OPENROUTER_API_KEY", ENV_KEY)
    monkeypatch.setattr(ai_settings, "_snapshot", None)
    monkeypatch.setattr(main, "run_startup_checks", lambda: [])
    monkeypatch.setattr(main, "init_pool", nothing)
    monkeypatch.setattr(main, "close_pool", nothing)
    monkeypatch.setattr(main, "get_pool", lambda: Pool())
    monkeypatch.setattr(main, "check_restore_state", nothing)
    monkeypatch.setattr(main, "check_ai_settings", check)
    monkeypatch.setattr(main, "register_documents", nothing)
    monkeypatch.setattr(ai_store, "seed_from_env", seed)
    monkeypatch.setattr(ai_store, "fill_openrouter_keys_from_env", fill)
    monkeypatch.setattr(ai_store, "load", load)
    for service in ("FaceRecognitionService", "EmotionService", "OCRService", "LineService",
                    "IntakeDetectionService", "LandmarkService"):
        monkeypatch.setattr(main, service, types.SimpleNamespace(get_instance=lambda: None))
    for name in ("start_scheduler", "stop_scheduler"):
        monkeypatch.setattr(main, name, lambda: None)
    for name in ("start_dispatcher", "stop_dispatcher"):
        monkeypatch.setattr(main, name, nothing)

    async def start_and_stop():
        async with main.lifespan(main.app):
            pass

    with caplog.at_level("WARNING"):
        asyncio.run(start_and_stop())
    assert calls == ["seed", ("fill", ENV_KEY), "load", "check"]
    unusable = [record.getMessage() for record in caplog.records
                if record.levelname == "WARNING" and "no usable model" in record.getMessage()]
    assert len(unusable) == 2 and "AI job safety" in unusable[0] and "AI job after_chat" in unusable[1]
    assert "envSTART" not in caplog.text
```

If an earlier task added another outside call to `lifespan()` (a new service warmed, a new job started), stub it the
same way in this test.

- [ ] **Step 10: Run it and see it fail**

```bash
.venv/Scripts/python.exe -m pytest tests/test_ai_startup.py -q -p no:cacheprovider
```
Expected: `1 failed`, with `AssertionError: assert ['seed', 'load', 'check'] == ['seed', ('fi...oad', 'check']`
and `At index 1 diff: 'load' != ('fill', 'sk-or-v1-envSTART-8642')` (the lifespan does not call the fill yet).

- [ ] **Step 11: Call the fill and log the warnings in the lifespan**

In `app/main.py`, four edits:

1. Lines 1-2 become (add `import logging`):
   ```python
   from contextlib import asynccontextmanager
   import logging
   import os
   ```
2. Before line 10 (`from app.database import init_pool, close_pool`) add one line, so lines 10-11 become:
   ```python
   from app import config
   from app.database import init_pool, close_pool
   ```
   (Read the key as `config.OPENROUTER_API_KEY` at start time, not `from app.config import OPENROUTER_API_KEY`, so the
   test can patch it.)
3. After line 30 (`from app.services.schedule import DoseRefused`) add the module logger, so that part reads:
   ```python
   from app.services.schedule import DoseRefused

   log = logging.getLogger(__name__)


   @asynccontextmanager
   ```
4. `lifespan()` becomes:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    run_startup_checks()
    await init_pool()
    async with get_pool().acquire() as conn:
        await check_restore_state(conn)
        await ai_store.seed_from_env(conn)   # first start only; checks .env (check_ai_settings) before copying it
        await ai_store.fill_openrouter_keys_from_env(config.OPENROUTER_API_KEY)   # every start: keyless ones only
        ai_settings.set_current(await ai_store.load(conn))
        for job in ai_settings.JOBS:   # before check_ai_settings, which can stop a prod start
            if not ai_settings.job_usable(ai_settings.current(), job):
                log.warning("AI job %s has no usable model (a tested model whose connection has a key that can be "
                            "read), so it can't run; choose one on the Settings page's AI connections card", job)
        check_ai_settings(ai_settings.current())
        await register_documents(conn)   # after the AI settings: the notices name the services in use
    FaceRecognitionService.get_instance()
    EmotionService.get_instance()
    OCRService.get_instance()
    LineService.get_instance()
    IntakeDetectionService.get_instance()
    LandmarkService.get_instance()
    start_scheduler()
    await start_dispatcher()
    yield
    await stop_dispatcher()
    stop_scheduler()
    await close_pool()
```
The warnings come before `check_ai_settings` on purpose: in prod with the robot feature that check can stop the start
(`SystemExit`), and the log should still say which jobs can't run. They never stop a start themselves, in prod either:
the card is where they are fixed.

- [ ] **Step 12: Run Parts A-C and see them pass**

```bash
.venv/Scripts/python.exe -m pytest tests/test_ai_settings.py tests/test_ai_store.py tests/test_ai_startup.py tests/test_api_ai.py tests/test_consent_enforcement.py tests/test_startup_checks.py -q -p no:cacheprovider
```
Expected: all pass, 0 failed (`test_api_ai.py` and `test_consent_enforcement.py` import `app.main`, so they prove the
new import lines work).

- [ ] **Step 13: Run the whole backend suite**

```bash
.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider
```
Expected: the count before this task plus 9 passed (`1633 passed`: the 1596 baseline, plus Task 3's 13 and Task 4's
15, plus these 9), and the one known failure `test_conversation.py::test_a_broken_background_never_costs_the_reply` (BUG-37). If a timing test
fails (BUG-39; seen during planning: `test_dose_emotion.py::test_the_event_window_starts_when_the_detector_skips_approaching`),
run it alone; it must pass there.

- [ ] **Step 14: Commit Parts A-C**

```bash
git add app/services/ai_settings.py app/services/ai_store.py app/main.py tests/test_ai_settings.py tests/test_ai_store.py tests/test_ai_startup.py
git diff --cached --stat
git commit -F - <<'EOF'
AI card: usable model per job, startup warnings, OpenRouter key from .env

- ai_settings.job_usable: a job is usable when its main or backup model is
  tested and its connection has a readable key; public() adds jobs_usable.
- The start logs one warning per job with no usable model.
- Every start fills keyless OpenRouter connections with .env's
  OPENROUTER_API_KEY in one change(); keyed connections are never touched
  and the key is never logged.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
```
The trailer must name the model that actually writes the commit; change it if another model runs this task.
`git diff --cached --stat` must list exactly the 6 files above.

#### Part D: a clear Test result for voice-only models (C3)

- [ ] **Step 15: Write the failing tests**

Append to the end of `tests/test_ai_profile.py` (it already has `pytest`, `ai_profile`, `_bad(message, status=400)`
returning `(status, {"error": {"message": message}}, 120)`, and `_service(*rules)`):

```python


NOT_CHAT = ("This is not a chat model and thus not supported in the v1/chat/completions endpoint. "
            "Did you mean to use v1/completions?")   # OpenAI's real 404 for a voice-only model (gpt-live-1)


@pytest.mark.parametrize("response", [
    _bad(NOT_CHAT, 404),
    _bad(NOT_CHAT, 400),
    _bad("THIS IS NOT A CHAT MODEL.", 404),
    _bad("gpt-x-tts is not supported in the v1/chat/completions endpoint.", 404),
])
def test_a_voice_only_model_gets_the_not_chat_model_code(response):
    send, sent = _service((lambda p: True, response))
    result = ai_profile.detect(send, "openai")
    assert result == {"ok": False, "profile": None, "ms": 120, "sample": None, "error": "not_chat_model"}
    assert ai_profile.NOT_CHAT_MODEL == "not_chat_model" and len(sent) == 1   # no setting can fix it: one request


@pytest.mark.parametrize("response, error", [
    (_bad(NOT_CHAT, 500), f"HTTP 500: {NOT_CHAT}"),   # only a 404 or a 400 says so
    (_bad("The model `gpt-9` does not exist", 404), "HTTP 404: The model `gpt-9` does not exist"),
])
def test_other_failures_keep_the_services_words(response, error):
    send, _ = _service((lambda p: True, response))
    assert ai_profile.detect(send, "openai")["error"] == error
```

Append to the end of `tests/test_api_ai.py` (LF file; it already has `dataclasses`, `ai_profile`, `ai_settings`,
`api_ai`, `Reply`, `_snapshot()` and the `api` fixture with `FakeStore.saved_tests`):

```python


def test_a_voice_only_model_gets_a_clear_code_saved_as_its_last_error(api, monkeypatch):
    snapshot = _snapshot()
    openai = ai_settings.Connection(2, "OpenAI", "openai", ai_profile.BASE_URLS["openai"], "sk-proj-live-4444", "4444",
                                    True, "OpenAI")
    live = ai_settings.Model(31, 2, "gpt-live-1", "OpenAI", "USA", "30 days", None)
    monkeypatch.setattr(ai_settings, "_snapshot", dataclasses.replace(
        snapshot, connections={**snapshot.connections, 2: openai}, models={**snapshot.models, 31: live}))
    sent = []

    def post(url, json, timeout, headers):
        sent.append(url)
        return Reply(404, {"error": {"message": "This is not a chat model and thus not supported in the "
                                                "v1/chat/completions endpoint. Did you mean to use v1/completions?",
                                     "type": "invalid_request_error", "param": "model", "code": None}})

    monkeypatch.setattr(api_ai.requests, "post", post)
    response = api.client.post("/api/ai/models/31/test")
    assert response.status_code == 200
    assert (response.json()["ok"], response.json()["error"]) == (False, "not_chat_model")
    assert api.store.saved_tests[-1]["error"] == "not_chat_model"   # last_error stores the same code
    assert sent == ["https://api.openai.com/v1/chat/completions"]
```

- [ ] **Step 16: Run them and see them fail**

```bash
.venv/Scripts/python.exe -m pytest tests/test_ai_profile.py tests/test_api_ai.py -q -p no:cacheprovider
```
Expected: `5 failed, 55 passed`. The four `test_a_voice_only_model_gets_the_not_chat_model_code` cases fail with an
`AssertionError` comparing the result dicts (today the error is `HTTP 404: This is not a chat model ...`), and
`test_a_voice_only_model_gets_a_clear_code_saved_as_its_last_error` with `assert (False, 'HTTP...completions?') ==
(False, 'not_chat_model')`. The two `test_other_failures_keep_the_services_words` cases already pass (they guard the
existing behaviour).

- [ ] **Step 17: Map the not-chat answer to `not_chat_model`**

In `app/services/ai_profile.py`, after line 27 (`_SETTINGS = (...)`) add:

```python
# The Test result's error for a model that can't write chat replies (a voice-only model such as gpt-live-1, or a TTS
# model). OpenAI answers /chat/completions for one with HTTP 404: "This is not a chat model and thus not supported in
# the v1/chat/completions endpoint. Did you mean to use v1/completions?"
NOT_CHAT_MODEL = "not_chat_model"
_NOT_CHAT = re.compile(r"not a chat model|not supported in the v1/chat/completions endpoint", re.IGNORECASE)
```
(`re` is already imported, line 11.)

Before `def _failed(` (line 93), add with two blank lines on each side:

```python
def _not_chat_model(status: int, message: str) -> bool:
    """A 404 (or 400) that says the model is not a chat model: no request setting can fix that."""
    return status in (400, 404) and bool(_NOT_CHAT.search(message))
```

`detect()` becomes (two lines added after `message = _error_message(body)`, before `adjust` can try to change a
setting):

```python
def detect(send, kind: str, secret: str = "") -> dict:
    """Find the profile a model accepts. send(profile) -> (HTTP status, parsed body or text, ms) does one request;
    status 0 means no answer came back. secret: the key sent, removed from any error text that echoes it.

    Returns {"ok", "profile", "ms", "sample", "error"}: on success the profile to save, the successful request's
    time and the answer's first 40 characters; otherwise the service's own words (cut to 300), and nothing to save."""
    profile = starting_profile(kind)
    body, ms = None, None
    for _ in range(MAX_TRIES):
        status, body, ms = send(profile)
        if status == 200:
            choices = (body.get("choices") if isinstance(body, dict) else None) or []
            choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
            message_part = choice.get("message")
            content = message_part.get("content") if isinstance(message_part, dict) else None
            if not isinstance(content, str) or not content.strip():
                return _failed(ms, "The model answered with no text.", secret)
            if choice.get("finish_reason") == "length":
                return _failed(ms, "The answer was cut off by the length limit.", secret)
            return {"ok": True, "profile": profile, "ms": ms, "sample": content.strip()[:40], "error": None}
        message = _error_message(body)
        if _not_chat_model(status, message):
            return _failed(ms, NOT_CHAT_MODEL, secret)
        changed = adjust(profile, message) if status == 400 else None
        if changed is None:
            return _failed(ms, f"HTTP {status}: {message}" if status else message, secret)
        profile = changed
    return _failed(ms, f"Gave up after {MAX_TRIES} tries: {_error_message(body)}", secret)
```
(Task 7 later leaves `detect()` as it is here. A not-chat model must end the Test there, before any thinking probe:
Task 7 runs its probes only after a passed base test.)

In `app/routers/api_ai.py`, the docstring of `test_model()` (line 237) becomes these three lines (no code change: the
route returns `detect()`'s result, line 268, and `save_test` writes `result["error"]` to `last_error`, ai_store.py:145):

```python
    """Find the request settings this model accepts (ai_profile.detect) and save them, or why it failed. A model that
    is not a chat model (a voice-only one such as gpt-live-1) fails with the code ai_profile.NOT_CHAT_MODEL, which the
    card explains and last_error keeps."""
```

- [ ] **Step 18: Run them and see them pass**

```bash
.venv/Scripts/python.exe -m pytest tests/test_ai_profile.py tests/test_api_ai.py -q -p no:cacheprovider
```
Expected: `60 passed` (28 in test_ai_profile.py, 32 in test_api_ai.py, plus any tests earlier tasks added).

- [ ] **Step 19: Run the whole backend suite**

```bash
.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider
```
Expected: the count after Step 13 plus 7 passed (`1640 passed`), and only the known failure
`test_a_broken_background_never_costs_the_reply` (BUG-37); a BUG-39 timing test may flake (rerun it alone).

- [ ] **Step 20: Commit Part D**

```bash
git add app/services/ai_profile.py app/routers/api_ai.py tests/test_ai_profile.py tests/test_api_ai.py
git diff --cached --stat
git commit -F - <<'EOF'
AI card: a clear Test result for voice-only models

A 404 or 400 saying "not a chat model" or "not supported in the
v1/chat/completions endpoint" (OpenAI's answer for gpt-live-1) now gives
the Test error code not_chat_model, also saved as the model's last_error.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
```
`git diff --cached --stat` must list exactly these 4 files. The trailer must name the model that writes the commit.

#### NOTES

**Contract deviations:** none in names or signatures. Clarifications:
1. `fill_openrouter_keys_from_env(api_key)` has no `conn` argument, as the contract says, so it takes its own pool
   connection (like `ai_store.note_call`, ai_store.py:156-168). The lifespan holds one connection then; the pool is
   `min_size=2, max_size=10` (app/database.py:10). It enters `change()` only when a keyless OpenRouter connection exists,
   so a normal start doesn't register the notices twice.
2. "Test maps the not-chat 404/400 to `not_chat_model`" is done in `ai_profile.detect`, where a failed probe's status
   and message are handled (ai_profile.py:117-120) and where spec C3 puts it. In `api_ai.py` only the Test route's
   docstring changes (it names `ai_profile.NOT_CHAT_MODEL`); `test_api_ai.py` proves the route returns the code and
   saves it as `last_error`.
3. The startup warnings are logged in `main.lifespan` itself with `ai_settings.job_usable(...)` (logger `app.main`),
   as the contract says, right after `set_current(load)` and before `check_ai_settings` (which can exit in prod). They
   are logged whatever `REACHY_FEATURE_ENABLED` is, because the spec states no condition.
4. Task 10's code check (scratchpad `plan-drafts/task-10.md`, lines 80 and 86) looks for `job_usable(` in
   `app/main.py` and `NOT_CHAT_MODEL` or `not_chat_model` in `app/routers/api_ai.py`; with this task both are there.
   An earlier draft of this task put the warnings in a `startup_checks.warn_unusable_jobs` helper and left `api_ai.py`
   alone, which would have failed that check; that version is dropped.

**Assumptions:**
- "Usable" follows the spec literally: tested (`model.profile is not None`; a model copied from `.env` has the starting
  profile and counts as tested, as `public()` already treats it, ai_settings.py:189) and a readable key. An untested
  model is still called by `conversation.py` with its starting profile, so "not usable" means "the card can't vouch for
  it", not "no call is made". `conversation.py:411` skips only keyless targets.
- Spec C2 says "every connection of kind openrouter whose key is NULL". That includes an OpenRouter connection someone
  added on the card without a key; it gets `.env`'s key too. The card cannot clear a key (`update_connection` sets a key
  only when one is given, ai_store.py:102), so a NULL key means "never had one".
- The `not_chat_model` match is limited to HTTP 404 and 400 (spec C3). A 500 with the same words keeps today's text.

**Left to other tasks:**
- Frontend: `jobs_usable` warnings under each job, the `not_chat_model` message, `settings.jobs_usable` in
  `ai-connections-api.ts`, and the en/zh-TW strings.
- Docs (`CLAUDE.md`: the OpenRouter key fill and the not-chat-model message, spec §6): Task 10.
- Live check (spec §5): rebuild the stack, then the app log should show `filled the key of 1 OpenRouter connection(s)
  from .env` once, and `GET /api/ai/settings` should show the OpenRouter connection with `has_key: true` and
  `jobs_usable` all true. Not run here.

**Risks:**
- Task 7 replaces `test_model()` (with a docstring that still names `ai_profile.NOT_CHAT_MODEL`) and leaves `detect()`
  alone, so the two not-chat lines stay before `adjust` and before any thinking probe.
- `tests/test_ai_startup.py` stubs every outside call of `lifespan()`. A later task that adds a new outside call there
  (a new service or job) must stub it in this test too, or the test will try to start it for real.
- The `.env` key is copied into every keyless OpenRouter connection. If `.env` holds a wrong key, Test shows the
  service's 401 on the card, and the card can replace it as usual.

**Verified (read in this session):**
- `ai_settings.py`: `JOBS` line 15; `Target` 46-70 (`profile` falls back to the starting profile, 69-70); `Snapshot`
  73-78; `job_targets` 123-131; `problems` 158-173; `with_model` 176; `public` 180-195 (`no_key_in_use` 194).
- `ai_store.py`: logger `log` line 14; `load` 26-42 (no key gives `api_key=""`, `key_readable=True`; an unreadable key
  gives `key_readable=False`, 30-33); `seed_from_env` 45-71 (strips the key, 58); `change` 74-88;
  `update_connection` 99-109; `save_test` 138-145.
- `main.py`: lines 1-2 `from contextlib import asynccontextmanager` / `import os`; line 10 `from app.database import
  init_pool, close_pool`; line 30 `from app.services.schedule import DoseRefused`; lifespan 33-54 (seed 39, load 40,
  `check_ai_settings` 41, `register_documents` 42). No test entered the lifespan before (only
  `test_consent_enforcement.py` imports `app.main`, without it).
- `config.py:95` `OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")`; docker-compose.yml passes `.env` with
  `env_file: .env` (lines 28, 75).
- `ai_profile.py`: `re` imported line 11; `_SETTINGS` 27; `_failed` 93-94 (redacts, cuts to 300); `detect` 97-122 (a
  non-200 goes to `adjust` only for 400, 118).
- `api_ai.py` Test route 235-268 (docstring 237); `database.py:10` pool `min_size=2, max_size=10`.
- Line endings (`git ls-files --eol`): `app/main.py`, `app/routers/api_ai.py` and the other app files `w/crlf`;
  `tests/test_api_ai.py` `w/lf`; `tests/test_ai_settings.py`, `tests/test_ai_store.py`, `tests/test_ai_profile.py` CRLF.
- Prototype run of this exact text: its code blocks were applied by script to a scratch copy (`git archive HEAD` of
  `app/ tests/ static/ sql/ models/landmarks .env.example`). Each "fail" step failed with the reason given above and
  each "pass" step passed (15, 17, 1, then 28 + 32); Step 12's set gave 87 passed. The edited files stayed CRLF, each
  file's diff only adds lines (plus the one replaced docstring line in `api_ai.py`), and no new line is over 120
  characters. Whole suite in that copy: 1589 passed before, 1605 after (+16), the only failure BUG-37 both times (the
  copy has 7 fewer tests than the repo's 1596, hence the different base). In an earlier run of the same code,
  `test_dose_emotion.py::test_the_event_window_starts_when_the_detector_skips_approaching` failed once in the full run
  and passed alone, in the copy and in the repo (a timing flake). The needles Task 10's check looks for in the files
  this task touches are all present.

---

### Task 6: AI connections card: job warnings, voice-only message, check-ins help text

Spec: `docs/superpowers/specs/2026-10-11-robot-camera-identity-thinking-design.md` sections C1 (card part), C3 (card part), C4.
Frontend only. The Playwright tests mock the whole API, so this task does not need the server tasks to be done first.

**Files:**
- Modify: `frontend_source/src/lib/ai-connections-api.ts` (CRLF): `AiSettings` (lines 17-20), `isAiSettings` (lines 33-43).
- Modify: `frontend_source/src/components/AiConnectionsCard.tsx` (LF): `KNOWN_ERRORS` (lines 11-13), `status` (lines 131-143), the jobs list `AI_JOBS.map(...)` (lines 339-362).
- Modify: `frontend_source/src/i18n.ts` (CRLF): en `aiConnections.onlyTested` / `errors` (lines 59-60), en `reachy.checkinsHelp` (line 99), zh-TW `aiConnections.onlyTested` / `errors` (lines 739-740), zh-TW `reachy.checkinsHelp` (line 779).
- Test: `frontend_source/tests/ai-connections.spec.ts` (CRLF): `startingSettings` (lines 6-19), `mockApi` signature (line 29), model 21 Test handler (lines 58-63), new tests appended after line 198.
- Test: `frontend_source/tests/reachy.spec.ts` (CRLF): one new test appended after line 367 (uses `checkinSwitch`, line 252).

Keep each file's line endings. The Edit tool keeps them. Check with `file` in Step 7.

**Interfaces:**
- Consumes (contract; the server side is built by other tasks, and the tests mock it):
  - `GET /api/ai/settings` and every change answer carry `jobs_usable: {"reply": bool, "safety": bool, "after_chat": bool}` (`ai_settings.public()`).
  - The Test answer (`POST /api/ai/models/{ref}/test`) can carry `ok: false, error: "not_chat_model"`, and the model's `last_error` then holds the same text `"not_chat_model"` (`ai_store.save_test` saves `result["error"]`).
- Produces:
  - `AiSettings.jobs_usable: Record<string, boolean>` in `ai-connections-api.ts`. `fetchAiSettings` refuses (load failure) an answer whose `jobs_usable` is missing, not an object, or lacks a boolean for any of `reply`, `safety`, `after_chat`.
  - `MODEL_ERRORS` in `AiConnectionsCard.tsx`: the codes a Test result or `last_error` can carry. Each code is shown as `aiConnections.modelErrors.<code>`. Task 9 (Edits C8, I3 and I4) adds `thinking_budget` (Task 8's code) there and under `aiConnections.modelErrors` in both languages.
  - i18n keys (en and zh-TW): `aiConnections.jobUnusable`, `aiConnections.jobUnusableEffect.safety`, `aiConnections.jobUnusableEffect.after_chat`, `aiConnections.modelErrors.not_chat_model`. The text of `reachy.checkinsHelp` changes.
  - Each job row is now a `role="group"` named after the job (for example "Safety check" or 「安全檢查」), so tests can find a job's row.

---

- [ ] **Step 1: Write the failing tests in `frontend_source/tests/ai-connections.spec.ts`**

Re-read lines 1-72 and 185-198 before editing.

1a. In `startingSettings()`, add `jobs_usable` after `jobs`, then add the new fixture function right after `startingSettings()`. Lines 15-19 become:

```ts
    jobs: { reply: { primary_ref: 11, fallback_ref: 12 }, safety: { primary_ref: 11, fallback_ref: 12 },
      after_chat: { primary_ref: 11, fallback_ref: 12 } },
    jobs_usable: { reply: true, safety: true, after_chat: true },
    seeded_at: '2026-10-10T12:00:00+00:00', weak_secret: true, no_key_in_use: false,
  };
}

/** Flaw F12 (11 Oct 2026): the replies run on an OpenAI model with a key, but the safety check and the after-chat
 *  work run on an OpenRouter connection whose key was never filled in. A key is in use, so no_key_in_use is false. */
function keylessOpenRouterSettings() {
  const settings: any = startingSettings();
  settings.connections = [
    { ...settings.connections[0], key_last4: null, has_key: false },
    { connection_id: 2, name: 'OpenAI', kind: 'openai', base_url: 'https://api.openai.com/v1', key_last4: '9876',
      key_readable: true, has_key: true, service_name: 'OpenAI' },
  ];
  settings.models.push({ model_ref: 21, connection_id: 2, model_id: 'gpt-5.4-mini', company: 'OpenAI', region: 'USA',
    retention: '30 days', profile: { token_param: 'max_completion_tokens', temperature: true, reasoning_off: { reasoning_effort: 'none' } },
    tested: true, copied_from_env: false, tested_at: '2026-10-10T13:00:00+00:00', test_ms: 812, last_error: null });
  settings.jobs.reply = { primary_ref: 21, fallback_ref: null };
  settings.jobs_usable = { reply: true, safety: false, after_chat: false };
  settings.weak_secret = false;
  return settings;
}
```

1b. Line 29, the `mockApi` signature, gains `testError`:

```ts
async function mockApi(page: Page, options: { aiDown?: boolean; aiBody?: unknown; jobsRefused?: boolean; testError?: string } = {}) {
```

1c. The model 21 Test handler (lines 58-63) becomes:

```ts
    if (path === '/api/ai/models/21/test') {
      const model: any = mock.settings.models.find(m => m.model_ref === 21);
      if (options.testError) {
        model.last_error = options.testError;   // as ai_store.save_test: the reason is saved, the model stays untested
        return route.fulfill({ json: { ok: false, ms: 412, sample: null, profile: null, error: options.testError, settings: mock.settings } });
      }
      Object.assign(model, { profile: { token_param: 'max_completion_tokens', temperature: true, reasoning_off: { reasoning_effort: 'none' } },
        tested: true, tested_at: '2026-10-10T13:00:00+00:00', test_ms: 812 });
      return route.fulfill({ json: { ok: true, ms: 812, sample: '您好！很高興見到您。', profile: model.profile, error: null, settings: mock.settings } });
    }
```

1d. Append at the end of the file, after the last test (`a job choice not yet saved survives adding another model`):

```ts

// ── Spec 2026-10-11 C1 and C3: a warning under each job that can't run, and the voice-only Test message ──

const job = (page: Page, name: string) => card(page).getByRole('group', { name, exact: true });

test('each job without a usable model says so and what stops with it, in both languages', async ({ page }) => {
  await seedSession(page);
  await mockApi(page, { aiBody: keylessOpenRouterSettings() });
  await page.goto('/settings');   // the app starts in Traditional Chinese
  await expect(job(page, '安全檢查')).toContainText('沒有可用的模型：這項工作無法執行。');
  await expect(job(page, '安全檢查')).toContainText('Reachy 的模型安全檢查已停止。');
  await expect(job(page, '聊天後的摘要與記憶')).toContainText('沒有可用的模型：這項工作無法執行。');
  await expect(job(page, '聊天後的摘要與記憶')).toContainText('摘要與摘要安全檢查已停止。');
  await page.getByRole('button', { name: '中文' }).first().click();
  const safety = job(page, 'Safety check');
  await expect(safety.getByRole('alert')).toContainText("No usable model: this job can't run.");
  await expect(safety.getByRole('alert')).toContainText("Reachy's model safety check is off.");
  const afterChat = job(page, 'After-chat summary and memory');
  await expect(afterChat.getByRole('alert')).toContainText("No usable model: this job can't run.");
  await expect(afterChat.getByRole('alert')).toContainText('Summaries and the summary safety check are off.');
  await expect(afterChat).not.toContainText("Reachy's model safety check is off.");
  // the replies have a model with a key: no warning there, and the card-wide "no key" warning stays silent
  await expect(job(page, "Reachy's replies").getByRole('alert')).toHaveCount(0);
  await expect(card(page).getByText('No model in use has an API key')).toHaveCount(0);
});

for (const [what, jobsUsable] of [['missing', undefined], ['incomplete', { reply: true, safety: 'no' }]] as const) {
  test(`an AI settings answer with jobs_usable ${what} is a load failure, not a crash`, async ({ page }) => {
    await seedSession(page);
    await mockApi(page, { aiBody: { ...startingSettings(), jobs_usable: jobsUsable } });
    await openSettings(page);
    await expect(card(page)).toContainText('Could not load the AI settings.');
    await expect(page.locator('section[aria-labelledby="reachy-title"]')).toBeVisible();
  });
}

test("Test on a voice-only model shows the card's own words, not the code", async ({ page }) => {
  await seedSession(page);
  await mockApi(page, { testError: 'not_chat_model' });
  await openSettings(page);
  await addOpenAi(page);
  await card(page).getByRole('button', { name: 'Add model' }).click();
  await card(page).getByLabel('Connection', { exact: true }).selectOption({ label: 'OpenAI' });
  await card(page).getByLabel('Model name').fill('gpt-live-1');
  await card(page).getByRole('button', { name: 'Add', exact: true }).click();
  const row = card(page).getByRole('group', { name: 'OpenAI · gpt-live-1' });
  await row.getByRole('button', { name: 'Test' }).click();
  await expect(row).toContainText("Voice-only model (like GPT-Live or a TTS model): it can't write Reachy's text replies.");
  await expect(row).not.toContainText('not_chat_model');
  // still untested, so no job can be given to it
  await expect(card(page).getByLabel("Reachy's replies: Main model").locator('option', { hasText: 'gpt-live-1' })).toHaveCount(0);
});

test('a saved voice-only failure shows the same words as the last call, in both languages', async ({ page }) => {
  await seedSession(page);
  const settings: any = startingSettings();
  settings.models.push({ model_ref: 22, connection_id: 1, model_id: 'openai/gpt-live-1', company: 'OpenAI', region: 'USA',
    retention: '30 days', profile: null, tested: false, copied_from_env: false, tested_at: null, test_ms: null,
    last_error: 'not_chat_model' });
  await mockApi(page, { aiBody: settings });
  await page.goto('/settings');   // the app starts in Traditional Chinese
  const row = card(page).getByRole('group', { name: 'OpenRouter · openai/gpt-live-1', exact: true });
  await expect(row).toContainText('上次呼叫失敗：語音專用模型（例如 GPT-Live 或 TTS）：無法撰寫 Reachy 的文字回覆。');
  await page.getByRole('button', { name: '中文' }).first().click();
  await expect(row).toContainText("Last call failed: Voice-only model (like GPT-Live or a TTS model): it can't write Reachy's text replies.");
  await expect(row).not.toContainText('not_chat_model');
});
```

- [ ] **Step 2: Write the failing test for C4 in `frontend_source/tests/reachy.spec.ts`**

Re-read the last test of the file (lines 359-367 at `37ba84e`; Task 3 inserted two tests and a few helper lines before it, so it is about 47 lines lower now: find it by its name). Append after the last test (`when every dose today is closed the test alert says so`). `checkinSwitch` is the existing helper on line 252:

```ts

test('the check-ins help names the AI connections card, not a company', async ({ page }) => {
  await seedSession(page);
  await mockApi(page, { paired: true, robotConsent: true });
  await page.goto('/settings');   // the app starts in Traditional Chinese
  const help = page.locator('label').filter({ has: checkinSwitch(page) });
  await expect(help).toContainText('只有文字會傳送到「AI 連線」卡片上選定的 AI 服務');
  await expect(help).not.toContainText('OpenRouter');
  await page.getByRole('button', { name: '中文' }).first().click();
  await expect(help).toContainText('only the text is sent to the AI service chosen on the AI connections card');
  await expect(help).not.toContainText('OpenRouter');
});
```

- [ ] **Step 3: Run the new tests and watch them fail**

Build the current source first, because `dist/` may be stale from another task:

```bash
cd C:/Reachy_Mini/repo/MedAiCarePlus/frontend_source
npm run build
npx playwright test --config "C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/pw-frontend.config.mjs" tests/ai-connections.spec.ts tests/reachy.spec.ts
```

Expected: the 8 old tests in `ai-connections.spec.ts` and the 16 old tests in `reachy.spec.ts` (14 at `37ba84e` plus Task 3's 2 camera tests) still pass, because the card ignores the new `jobs_usable` field. These 6 new tests FAIL:
- `each job without a usable model says so…`: `element(s) not found` for `getByRole('group', { name: '安全檢查', exact: true })`, because job rows are not groups yet.
- `an AI settings answer with jobs_usable missing…` and `…incomplete…`: the card shows the full settings instead of "Could not load the AI settings.", because `jobs_usable` is not validated yet.
- `Test on a voice-only model…`: the row reads `OpenAI · gpt-live-1 not_chat_model…`, because the raw code is shown.
- `a saved voice-only failure…`: the row reads `上次呼叫失敗：not_chat_model`.
- `the check-ins help names the AI connections card…`: the label still says `只有文字會傳送到 OpenRouter（免費 AI 模型，示範用）`.

- [ ] **Step 4: Implement `frontend_source/src/lib/ai-connections-api.ts`**

Re-read lines 16-43. Replace the `AiSettings` interface (lines 17-20) with:

```ts
export interface AiSettings {
  connections: AiConnection[]; models: AiModel[]; jobs: Record<AiJob, AiJobChoice>;
  /** Per job: true when its main or backup model is tested and its connection has a readable key (as saved). */
  jobs_usable: Record<string, boolean>;
  seeded_at: string | null; weak_secret: boolean; no_key_in_use: boolean;
}
```

Replace `isAiSettings` (lines 33-43, keep its doc comment on line 33) with:

```ts
/** True only when every part the card reads has the shape it expects. */
function isAiSettings(value: unknown): value is AiSettings {
  if (!isObject(value) || !Array.isArray(value.connections) || !Array.isArray(value.models) || !isObject(value.jobs)
    || !isObject(value.jobs_usable)) return false;
  const jobs = value.jobs;
  const usable = value.jobs_usable;
  return value.connections.every(c => isObject(c) && isNumber(c.connection_id) && typeof c.name === 'string' && typeof c.kind === 'string')
    && value.models.every(m => isObject(m) && isNumber(m.model_ref) && isNumber(m.connection_id) && typeof m.model_id === 'string')
    && AI_JOBS.every(job => {
      const choice = jobs[job];
      return isObject(choice) && isNumber(choice.primary_ref) && (choice.fallback_ref === null || isNumber(choice.fallback_ref))
        && typeof usable[job] === 'boolean';
    });
}
```

Use `const usable = value.jobs_usable;`, not destructuring. Destructuring loses TypeScript's narrowing to `Record<string, unknown>`. The existing `const jobs = value.jobs;` uses the same pattern.

No earlier task changes `isAiSettings`; Task 9 later adds its `thinking` checks on top of this version and keeps the two `jobs_usable` parts (`|| !isObject(value.jobs_usable)` in the first `if`, and `&& typeof usable[job] === 'boolean'` in the per-job check).

- [ ] **Step 5: Implement `frontend_source/src/components/AiConnectionsCard.tsx`**

Re-read lines 1-30, 124-145 and 336-370 before each edit.

5a. Directly after `KNOWN_ERRORS` (lines 11-13), add:

```ts
/** Codes a model's Test result or last call can carry instead of the service's own words (ai_profile.py). */
const MODEL_ERRORS = ['not_chat_model'];
```

5b. Add the `modelError` helper right above `status` (line 131), and change the two lines that show an error (lines 133-134). With the helper, `status` becomes:

```tsx
  /** A known code in the card's own words; anything else is the service's message, shown as it came. */
  const modelError = (error: string | null) =>
    (error && MODEL_ERRORS.includes(error) ? t(`aiConnections.modelErrors.${error}`) : error);

  const status = (model: AiModel) => {
    const result = results[model.model_ref];
    if (result && !result.ok) return <span className="text-red-700"><XCircle className="inline w-4 h-4" /> {modelError(result.error)}</span>;
    if (model.last_error) return <span className="text-red-700">{t('aiConnections.lastCallFailed', { error: modelError(model.last_error) })}</span>;
    if (model.test_ms != null) {
      return (
        <span className="text-green-700"><CheckCircle2 className="inline w-4 h-4" /> {t('aiConnections.testOk', { seconds: (model.test_ms / 1000).toFixed(1) })}
          {result?.sample ? ` · ${result.sample}` : ''}</span>
      );
    }
    if (model.copied_from_env) return <span className="text-gray-500">{t('aiConnections.copiedFromEnv')}</span>;
    return <span className="text-gray-500">{t('aiConnections.notTested')}</span>;
  };
```

If another task has already changed the lines after `if (model.last_error)` (for example a "Can think" line), keep its version of those lines. Only the `modelError` helper and the two lines that call it are this task's.

5c. Replace the jobs list `{AI_JOBS.map(job => ( … ))}` (lines 339-362) with the version below. Each job becomes a named group holding the old grid plus the warning:

```tsx
        {AI_JOBS.map(job => (
          <div key={job} role="group" aria-label={t(`aiConnections.jobNames.${job}`)} className="space-y-2">
            <div className="grid gap-2 sm:grid-cols-3 sm:items-end">
              <span className="text-sm text-gray-900 sm:pb-2">{t(`aiConnections.jobNames.${job}`)}</span>
              <label className="block text-xs text-gray-600">{t('aiConnections.main')}
                <select className={field} aria-label={`${t(`aiConnections.jobNames.${job}`)}: ${t('aiConnections.main')}`} value={jobs[job].primary_ref}
                  onChange={event => {
                    setJobsSaved(false);
                    setJobs({ ...jobs, [job]: { ...jobs[job], primary_ref: Number(event.target.value) } });
                  }}>
                  {tested.map(model => <option key={model.model_ref} value={model.model_ref}>{label(model)}</option>)}
                </select>
              </label>
              <label className="block text-xs text-gray-600">{t('aiConnections.backup')}
                <select className={field} aria-label={`${t(`aiConnections.jobNames.${job}`)}: ${t('aiConnections.backup')}`} value={jobs[job].fallback_ref ?? ''}
                  onChange={event => {
                    setJobsSaved(false);
                    setJobs({ ...jobs, [job]: { ...jobs[job], fallback_ref: event.target.value ? Number(event.target.value) : null } });
                  }}>
                  <option value="">{t('aiConnections.noBackup')}</option>
                  {tested.map(model => <option key={model.model_ref} value={model.model_ref}>{label(model)}</option>)}
                </select>
              </label>
            </div>
            {/* The saved setup, as the server judged it; `?.`: change answers are not shape-checked like the first load. */}
            {settings.jobs_usable?.[job] === false && (
              <p role="alert" className={warning}>
                <TriangleAlert className="inline w-4 h-4" /> {t('aiConnections.jobUnusable')}
                {job !== 'reply' && <span className="block mt-1">{t(`aiConnections.jobUnusableEffect.${job}`)}</span>}
              </p>
            )}
          </div>
        ))}
```

Task 9 runs later and adds its "Thinking" select inside the inner `grid` div (its Edit C6b anchors on this layout). No new imports are needed: `TriangleAlert` and `warning` already exist (lines 3 and 17).

- [ ] **Step 6: Add the strings to `frontend_source/src/i18n.ts` (en and zh-TW)**

Re-read the four places by their text. At `37ba84e` they are lines 55-62, 97-100, 735-742 and 777-780; Task 3 added two keys after en `cameraRate` (line 91) and two after zh-TW `cameraRate`, so en `checkinsHelp` is now line 101, zh-TW `onlyTested` line 741 and zh-TW `checkinsHelp` line 783.

6a. en: between `onlyTested` (line 59) and `errors: {` (line 60), insert:

```ts
        jobUnusable: "No usable model: this job can't run.",
        jobUnusableEffect: { safety: "Reachy's model safety check is off.", after_chat: 'Summaries and the summary safety check are off.' },
        modelErrors: { not_chat_model: "Voice-only model (like GPT-Live or a TTS model): it can't write Reachy's text replies." },
```

6b. en `reachy.checkinsHelp` (line 99) becomes the line below. Only `OpenRouter (free AI models, demo)` changes:

```ts
        checkinsHelp: 'After your medicines Reachy has a short chat with you. Your voice becomes text on the robot; only the text is sent to the AI service chosen on the AI connections card to write Reachy\'s replies. Transcripts are kept 30 days. If you mention harming yourself or taking too much medicine, Reachy gives help-line numbers and alerts all your verified family contacts.',
```

6c. zh-TW: between `onlyTested: '只有測試過的模型可以指派工作。',` (line 739) and `errors: {` (line 740), insert:

```ts
        jobUnusable: '沒有可用的模型：這項工作無法執行。',
        jobUnusableEffect: { safety: 'Reachy 的模型安全檢查已停止。', after_chat: '摘要與摘要安全檢查已停止。' },
        modelErrors: { not_chat_model: '語音專用模型（例如 GPT-Live 或 TTS）：無法撰寫 Reachy 的文字回覆。' },
```

6d. zh-TW `reachy.checkinsHelp` (line 779) becomes the line below. Only `OpenRouter（免費 AI 模型，示範用）` changes. 「AI 連線」 is the card's zh-TW title (`aiConnections.title`, line 694):

```ts
        checkinsHelp: '吃完藥後，Reachy 會和您簡短聊天。您的聲音在機器人上轉成文字，只有文字會傳送到「AI 連線」卡片上選定的 AI 服務來產生 Reachy 的回答。對話紀錄保存 30 天。如果您提到想傷害自己或吃了過量的藥，Reachy 會提供求助專線，並通知您所有已驗證的家人。',
```

If another task has already added `aiConnections.modelErrors` (for example with `thinking_budget`), add `not_chat_model` to that object instead of a second `modelErrors` key. A duplicate key in one object literal fails `tsc -b`.

- [ ] **Step 6b: Make the "copied from .env" note true again (controller ruling R1)**

Task 5 makes a keyless OpenRouter connection take `OPENROUTER_API_KEY` from `.env` at every start, so the card's
`aiConnections.seeded` note ("the LLM lines in .env no longer apply") is no longer fully true. In
`frontend_source/src/i18n.ts`, replace the en line (today at line 19):

```ts
        seeded: 'These settings were copied from .env on {{date}}. Edit them here; the LLM lines in .env no longer apply.',
```

with:

```ts
        seeded: 'These settings were copied from .env on {{date}}. Edit them here; the LLM lines in .env no longer apply, except that an OpenRouter connection without a key takes OPENROUTER_API_KEY from .env when the server starts.',
```

and the zh-TW line (today at line 699):

```ts
        seeded: '這些設定於 {{date}} 從 .env 複製而來。請在這裡修改；.env 裡的 LLM 設定已不再使用。',
```

with:

```ts
        seeded: '這些設定於 {{date}} 從 .env 複製而來。請在這裡修改；.env 裡的 LLM 設定已不再使用，但沒有金鑰的 OpenRouter 連線會在伺服器啟動時使用 .env 的 OPENROUTER_API_KEY。',
```

Then `grep -rn "LLM lines in .env no longer apply\|LLM 設定已不再使用" frontend_source/tests` and update any test that
asserts the old full sentence to the new one (a test that matches only a prefix needs no change).

- [ ] **Step 7: Type-check, build, run the tests, and check line endings**

```bash
cd C:/Reachy_Mini/repo/MedAiCarePlus/frontend_source
npx tsc -b
npm run build
npx playwright test --config "C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/pw-frontend.config.mjs" tests/ai-connections.spec.ts tests/reachy.spec.ts
npx eslint src/components/AiConnectionsCard.tsx src/lib/ai-connections-api.ts src/i18n.ts
file src/lib/ai-connections-api.ts src/components/AiConnectionsCard.tsx src/i18n.ts tests/ai-connections.spec.ts tests/reachy.spec.ts
```

Expected:
- `tsc -b` prints nothing.
- The build ends with `✓ built in …`. The chunk-size warning is old.
- Playwright: `ai-connections.spec.ts` 13 passed (8 old + 5 new: the `jobs_usable` loop makes 2 tests) and `reachy.spec.ts` 17 passed (14 at `37ba84e` + Task 3's 2 + this task's 1), so `30 passed`.
- eslint prints nothing and exits 0.
- `file`: `ai-connections-api.ts`, `i18n.ts` and both spec files still say `with CRLF line terminators`. `AiConnectionsCard.tsx` does not (it is LF).

- [ ] **Step 8: Run the whole frontend Playwright suite**

```bash
cd C:/Reachy_Mini/repo/MedAiCarePlus/frontend_source
npx playwright test --config "C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/pw-frontend.config.mjs"
```

Expected: `59 passed, 1 failed`. The one failure is old and not caused by this task: `scan.spec.ts:148 › a course that already ended starts unticked, and is saved only with a new last day` (it waits for 「最後服藥日已經過了。請修改或清除日期後再加入。」). It also fails on the unchanged source (checked 2026-10-11). The suite before this task was 53 passed and that 1 failed: 48 at `37ba84e`, plus Task 1's 3 tests in `robot-settings-page.spec.ts` (the config's `testMatch` is `*.spec.ts`, so it runs them) and Task 3's 2. The planning run of this task, without Tasks 1 and 3, gave 54 passed. Any other failure is a regression: stop and fix it.

- [ ] **Step 8b: Look at the card at 390 and 1280 px (spec §5)**

The screenshot spec lives in the scratchpad, never in the repo (`S` is the scratchpad folder, as in Task 3 Step 14). Create `$S/t6-shots/ai-card-warnings.spec.ts` with exactly:

```ts
// Screenshots of the AI connections card's job warnings and the voice-only message (Task 6). Not part of the repo:
// run with t6-shots.config.mjs.
import { expect, test, type Page } from '@playwright/test';

const OUT = process.env.T6_SHOTS_OUT ?? '.';
const profile = { token_param: 'max_completion_tokens', temperature: true, reasoning_off: { reasoning_effort: 'none' } };
// Flaw F12: the replies have a keyed OpenAI model; the safety check and the after-chat work run on a keyless
// OpenRouter connection. gpt-live-1's last Test ended in not_chat_model.
const settings = {
  connections: [
    { connection_id: 1, name: 'OpenRouter', kind: 'openrouter', base_url: 'https://openrouter.ai/api/v1', key_last4: null,
      key_readable: true, has_key: false, service_name: 'OpenRouter' },
    { connection_id: 2, name: 'OpenAI', kind: 'openai', base_url: 'https://api.openai.com/v1', key_last4: '9876',
      key_readable: true, has_key: true, service_name: 'OpenAI' },
  ],
  models: [
    { model_ref: 11, connection_id: 1, model_id: 'google/gemini-2.5-flash', company: 'Google', region: 'global',
      retention: 'not kept', profile, tested: true, copied_from_env: true, tested_at: null, test_ms: null, last_error: null },
    { model_ref: 21, connection_id: 2, model_id: 'gpt-5.4-mini', company: 'OpenAI', region: 'USA', retention: '30 days',
      profile, tested: true, copied_from_env: false, tested_at: '2026-10-11T09:00:00+00:00', test_ms: 812, last_error: null },
    { model_ref: 22, connection_id: 2, model_id: 'gpt-live-1', company: 'OpenAI', region: 'USA', retention: '30 days',
      profile: null, tested: false, copied_from_env: false, tested_at: null, test_ms: null, last_error: 'not_chat_model' },
  ],
  jobs: { reply: { primary_ref: 21, fallback_ref: null }, safety: { primary_ref: 11, fallback_ref: null },
    after_chat: { primary_ref: 11, fallback_ref: null } },
  jobs_usable: { reply: true, safety: false, after_chat: false },
  seeded_at: null, weak_secret: false, no_key_in_use: false,
};

async function open(page: Page) {
  await page.addInitScript(() => {
    for (const key of ['face_auth_session', 'onboarding_complete', 'onboarding_face_done']) localStorage.setItem(key, 'true');
    localStorage.setItem('face_auth_user', JSON.stringify({ name: 'AI Test', u_id: 7, loginAt: new Date().toISOString() }));
    localStorage.setItem('face_auth_token', 'test-face-token');
  });
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/consent/status') {
      return route.fulfill({ json: { terms_version: '2026-10', core_current: true, robot_current: false,
        scopes: { core: { granted: true, terms_version: '2026-10', kind: 'core' } } } });
    }
    if (path === '/api/reachy/status') return route.fulfill({ json: { paired: false } });
    if (path === '/api/notify/settings') return route.fulfill({ json: {} });
    if (path === '/api/ai/settings') return route.fulfill({ json: settings });
    return route.fulfill({ json: [] });
  });
  await page.goto('/settings');
}

for (const width of [390, 1280]) {
  test(`AI card job warnings and the voice-only message at ${width} px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await open(page);
    const card = page.locator('section[aria-labelledby="ai-title"]');
    await expect(card.getByText('沒有可用的模型：這項工作無法執行。').first()).toBeVisible();
    // the app's fixed header and bottom bar would cover parts of a tall element screenshot
    await page.addStyleTag({ content: '.fixed, .sticky { position: static !important; }' });
    await card.screenshot({ path: `${OUT}/ai-card-warnings-zh-${width}.png` });
    await page.getByRole('button', { name: '中文' }).first().click();   // switch the page to English
    await expect(card.getByText("No usable model: this job can't run.").first()).toBeVisible();
    await card.screenshot({ path: `${OUT}/ai-card-warnings-en-${width}.png` });
  });
}
```

Create `$S/t6-shots.config.mjs` (Task 3's `t3-shots.config.mjs` with its own folder and port):

```js
export default {
  testDir: 'C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/t6-shots',
  testMatch: ['*.spec.ts'],
  timeout: 30000,
  workers: 1,
  reporter: 'list',
  use: { baseURL: 'http://localhost:4188' },
  webServer: {
    command: 'npx vite preview --port 4188 --strictPort',
    cwd: 'C:/Reachy_Mini/repo/MedAiCarePlus/frontend_source',
    url: 'http://localhost:4188',
    reuseExistingServer: false,
    timeout: 60000,
  },
};
```

Run it against the build from Step 7 (Git Bash):

```bash
S=/c/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad
[ -e "$S/t6-shots/node_modules" ] || (cd "$S/t6-shots" && cmd //c "mklink /J node_modules C:\\Reachy_Mini\\repo\\MedAiCarePlus\\frontend_source\\node_modules")
mkdir -p "$S/t6-shots-out"
cd /c/Reachy_Mini/repo/MedAiCarePlus/frontend_source && T6_SHOTS_OUT="C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/t6-shots-out" npx playwright test --config "C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/t6-shots.config.mjs"
```

Expected: `2 passed`, and four PNGs in `t6-shots-out` (`ai-card-warnings-{zh,en}-{390,1280}.png`). Open each with the Read tool. Pass when: under "Safety check" / 「安全檢查」 an amber box holds "No usable model: this job can't run." and, on its own line, "Reachy's model safety check is off."; under the after-chat job the same with "Summaries and the summary safety check are off."; no warning under "Reachy's replies"; the gpt-live-1 row says "Last call failed: Voice-only model (like GPT-Live or a TTS model): it can't write Reachy's text replies." (zh-TW 「上次呼叫失敗：語音專用模型（例如 GPT-Live 或 TTS）：無法撰寫 Reachy 的文字回覆。」), never `not_chat_model`; at 390 px nothing is cut off and there is no horizontal scroll.

- [ ] **Step 9: Commit**

```bash
cd C:/Reachy_Mini/repo/MedAiCarePlus
git add frontend_source/src/lib/ai-connections-api.ts frontend_source/src/components/AiConnectionsCard.tsx frontend_source/src/i18n.ts frontend_source/tests/ai-connections.spec.ts frontend_source/tests/reachy.spec.ts
git commit -F - <<'EOF'
AI connections card: job warnings, voice-only Test message, check-ins help

- Under each job whose main and backup model can't run (jobs_usable false), the
  card says so and what stops: the model safety check, or the summaries and the
  summary safety check. jobs_usable is shape-checked when the card loads.
- A Test or last call that ends in not_chat_model shows "Voice-only model (like
  GPT-Live or a TTS model)..." instead of the code.
- The check-ins help names the AI connections card instead of OpenRouter.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
git log -1 --stat
```

Before committing, replace `Claude Opus 5.5 (1M context)` with the name of the model that is making this commit. Expected `--stat`: the 5 files above and nothing else.

#### NOTES

**Assumptions**
- The warning follows the server's `jobs_usable`, which describes the **saved** setup. When someone picks a usable model but has not pressed "Save jobs" yet, the warning stays until the save, and the save answer brings a fresh `jobs_usable`. This is on purpose: the warning says what Reachy runs now, which matches the startup WARNING log (C1). The card does not work out usability itself, so the browser and the server cannot disagree.
- For the `reply` job only the general line shows. The spec gives an extra line only for `safety` and `after_chat`. The existing `noKeyInUse` warning stays unchanged (C1: "The existing no_key_in_use warning stays").
- The extra line is its own block line (`<span className="block mt-1">`), not joined with a space. In zh-TW a space between sentences reads wrong. Screenshots of the scratch build at 390 px (zh-TW) and 1280 px (en) showed two clean lines in an amber box under each affected job.
- `not_chat_model` is not added to `KNOWN_ERRORS`. That list holds HTTP refusal codes (`ApiError.message`), shown in red under `aiConnections.errors.*`. `not_chat_model` arrives inside a 200 Test answer (`error`) and in `last_error`, so it gets its own list `MODEL_ERRORS` and its own keys `aiConnections.modelErrors.*`. This is the "known error code list" for Test results.
- A saved failure shows as "Last call failed: Voice-only model (…)" / 「上次呼叫失敗：語音專用模型（…）」, using the existing `lastCallFailed` wrapper. This is the same way every other saved Test failure shows today.
- `settings.jobs_usable?.[job] === false` uses `?.` because only `fetchAiSettings` checks the answer's shape. The change answers (`addConnection`, `testModel`'s `settings`, and so on) are passed through as they come, so a missing field must not crash the Settings page. A missing value shows no warning.

**Contract deviations:** none. `AiSettings.jobs_usable` is `Record<string, boolean>` as the contract says. The validation still requires a boolean for each of the three jobs.

**Overlap with other tasks (read before editing)**
- Task 9 (D6, later) edits the same job row, `isAiSettings`, the `aiConnections` i18n block and `status()`, on top of this task's result. It adds `thinking_budget` to `MODEL_ERRORS` and to the same `aiConnections.modelErrors` objects (Edits C8, I3, I4), never a second `modelErrors` key.
- Task 3 (B3, earlier) inserted two camera tests before the last test of `reachy.spec.ts`, gave `MockOptions` the `camera` and `online` options, and added `reachy.robotCamera` / `reachy.cameraNotWorking`. The C4 test here appends after the last test, passes no `camera` option and changes no shared helper; Step 6 replaces only the `checkinsHelp` lines.
- After the backend tasks, the real server must send `jobs_usable` in **every** AI-card answer (`_card()` uses `ai_settings.public()`), or the first load becomes "Could not load the AI settings." That is the intended strict check. The backend task's `public()` change provides it.

**Risks**
- `role="alert"` on up to three job warnings plus `noKeyInUse` is announced by screen readers when the card loads. This matches the existing warnings on this card (`weakSecret`, `noKeyInUse`). Existing tests that pick an alert already filter by text or scope to a group, and they all passed.
- The new job groups' names ("Reachy's replies", "Safety check", …) do not clash with existing locators: `getByLabel("Reachy's replies: Main model")` and the `exact: true` connection and model group names all still pass.

**Facts verified (2026-10-11, branch `robot-camera-thinking`, repo untouched: `git status` empty)**
- The whole change was made and run in a scratch copy (`scratchpad/t06fe`, `node_modules` linked):
  - Before the change, the 6 new tests failed for the reasons in Step 3.
  - After it, `tsc -b` was clean, the build passed and `ai-connections.spec.ts` + `reachy.spec.ts` gave 28 passed.
  - eslint on the 3 source files printed nothing.
  - The full suite gave 54 passed + 1 old failure (`scan.spec.ts:148`, which also fails with the unchanged source).
- A reference diff of the exact verified change: `C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/plan-drafts/task-06.verified.diff`. It has CRs stripped, so use the Edit steps above, not `git apply`.
- `frontend_source/src/lib/ai-connections-api.ts:17-20` `AiSettings`; `:33-43` `isAiSettings`; `:46-50` `fetchAiSettings` (the only shape-checked call).
- `frontend_source/src/components/AiConnectionsCard.tsx:11-13` `KNOWN_ERRORS`; `:131-143` `status` (shows `result.error` raw and `lastCallFailed` with raw `last_error`); `:339-362` the job rows; `:3` imports `TriangleAlert`; `:17` `warning` class.
- `frontend_source/src/i18n.ts:59-60` and `:739-740` (`onlyTested` / `errors`); `:99` and `:779` `checkinsHelp` (both name OpenRouter); `:1366-1370` `lng: 'zh-TW'`, `escapeValue: false` (apostrophes in interpolated text are not escaped).
- `frontend_source/src/components/ReachyCard.tsx:283-290` the check-ins switch label holds `reachy.checkinsHelp`, so the label is the switch's accessible name.
- `frontend_source/tests/reachy.spec.ts:252` `checkinSwitch`; the AI card tests start in zh-TW and switch with the `中文` button (`ai-connections.spec.ts:75-78`).
- `app/services/ai_store.py:138-145` `save_test` stores `result["error"]` as `last_error` and keeps the profile on failure, as the new mock does. `app/services/ai_settings.py:180-195` `public()` has no `jobs_usable` yet (a backend task adds it). `app/routers/api_ai.py:71-72` `_card()` builds every card answer from `public()`.
- `frontend_source/tsconfig.app.json` includes only `src`, so the spec files are not type-checked by `tsc -b`. They run through Playwright's own TypeScript loader. That is why the fixtures use `any` freely, as the existing spec does (`null as any`).

---

### Task 7: Thinking: what each model can do, found by Test (and the Astra/Sol bug)

Spec parts: D2 (what a model can do, found by Test), the latent bug F14 (models that refuse `reasoning_effort: "none"`),
and the request helpers D3/D4 need (`effective_level`, `allowance`, `request_body(..., level=)`). Conversation calls,
the jobs API, storage and the card belong to other tasks; this task only finds and saves what each model can do and
gives later tasks the helpers that turn a job's level into a request.

**Files:**
- Modify `app/services/ai_profile.py` (CRLF). Line numbers below are today's; Task 5 adds 5 lines after line 27, 5
  lines before `_failed` (line 93) and 2 lines inside `detect()`, so after Task 5 the later numbers are 5 higher:
  - module docstring and imports, lines 1-13;
  - constants: `TEST_MAX_TOKENS` line 25, `TEST_TIMEOUT` line 26, `_SETTINGS` line 27 (after Task 5 followed by
    `NOT_CHAT_MODEL` and `_NOT_CHAT`, lines 28-32);
  - `adjust()` lines 42-62 (47-67 after Task 5), replaced, with three new helpers above it;
  - `request_body()` lines 65-72 (70-77 after Task 5), replaced, with `effective_level()` and `allowance()` above
    it and the new `probe_body()` below it;
  - four new functions appended after `detect()` (lines 97-122; 107-134 after Task 5), at the end of the file.
  - `detect()`, `headers()`, `_error_message()`, `_failed()` and Task 5's `_not_chat_model()` do not change.
- Modify `app/routers/api_ai.py` (CRLF): replace `test_model()`, lines 235-270 after Task 5 (decorator at 235, ends
  with `return {**result, "settings": _card()}` at 270; at `37ba84e` it ended at 268). Nothing else in the file
  changes. (Task 5 changed only this route's docstring, from one line to three, so it names
  `ai_profile.NOT_CHAT_MODEL`; the replacement below has its own docstring, which still names it. Task 8 changes
  `PUT /jobs` below it, later.)
- Test: modify `tests/test_ai_profile.py` (CRLF; lines 1-18 and 63-70 change, new tests appended at the end) and
  `tests/test_api_ai.py` (LF; lines 257-261 change, new tests appended at the end).
- No change to `app/services/ai_store.py`: `save_test()` (lines 138-145) already saves `result["profile"]` as JSON,
  so the new `"thinking"` key is stored with it. No change to `ai_settings.public()` (lines 187-188 pass the profile
  through), so the card receives it.

**Interfaces:**
- Consumes (existing code, unchanged): `ai_profile.detect(send, kind, secret)` and its `send(profile)` contract
  (returns `(HTTP status, parsed body or text, ms)`, status 0 when no answer came back); `_named_setting()`,
  `_error_message()`, `starting_profile()`, `TEST_MESSAGES`, `TEST_MAX_TOKENS = 200`, `TEST_TIMEOUT = 15`; from Task 5:
  `ai_profile.NOT_CHAT_MODEL` and the not-chat check inside `detect()` (a voice-only model fails the base test, so it
  never reaches the thinking probes); `api_ai.REMOTE_MODELS_TIMEOUT = 10`, `api_ai.log`, `conversation._provider()`,
  `ai_store.save_test(conn, model_ref, result)`.
- Produces (contract names, exactly):
  - `ai_profile.THINKING_LEVELS = ("low", "medium", "high")`
  - `ai_profile.ALLOWANCE = {"low": 800, "medium": 1600, "high": 3000}`
  - `ai_profile.effective_level(profile: dict | None, level: str) -> str`: the nearest lower level the model accepts,
    else `"off"`; a profile with `thinking.always` never gets `"off"` (it gets its lowest accepted level). A profile
    without `"thinking"` (every profile saved before this task) gives `"off"`.
  - `ai_profile.allowance(profile: dict | None, level: str) -> int`: `ALLOWANCE[effective_level(...)]`, 0 for off.
  - `ai_profile.request_body(profile, model_id, messages, max_tokens, temperature, extras=None, *, level="off")`:
    at `"off"` exactly today's body. At another level it sends `effective_level(profile, level)`: OpenAI-style
    `reasoning_effort: <level>` and no temperature; OpenRouter `reasoning: {"effort": <level>, "exclude": true}` and
    temperature only when `profile["thinking"]["temperature"]` is true. `max_tokens` is sent as given (callers add
    `allowance()`). **`temperature=None` sends no temperature** (Task 8 passes None for memory extraction at a
    thinking level).
  - `ai_profile.detect_thinking(send, kind, profile, openrouter_entry=None, clock=time.monotonic) -> dict`: the
    profile's `"thinking"` value, `{"param": "reasoning_effort" | "openrouter_effort" | None, "levels": [...],
    "ms": {level: int}, "always": bool, "temperature": bool}`. It never touches the base profile; the Test route
    saves the base profile even when it raises.
  - Extra names (not in the contract; used by this task's route and tests): `TEST_TEMPERATURE = 0.7`,
    `THINKING_SECONDS = 40`, `CANT_THINK` (the "can't think" value: param None, no levels), and
    `probe_body(profile, model_id, level="off", extras=None)` (Test's request: the greeting, `TEST_MAX_TOKENS` plus
    the allowance of the level sent).
  - The Test route (`POST /api/ai/models/{ref}/test`) now answers and saves a profile with `"thinking"` after a
    passed base test. A model that refuses `none` is saved with `reasoning_off: {"reasoning_effort": "low"}`,
    `temperature: false` and `thinking.always: true`.

**Line endings.** `ai_profile.py`, `api_ai.py` and `test_ai_profile.py` are CRLF; `test_api_ai.py` is LF. Use the
Edit tool for every change. After each step, `file <path>` must still say "with CRLF line terminators" for the three
CRLF files and must not say it for `test_api_ai.py`. If a CRLF file lost it, restore it with:
`.venv/Scripts/python.exe -c "import pathlib,sys; p=pathlib.Path(sys.argv[1]); t=p.read_bytes().replace(b'\r\n', b'\n'); p.write_bytes(t.replace(b'\n', b'\r\n'))" <path>`

All commands run in Git Bash from `C:/Reachy_Mini/repo/MedAiCarePlus`.

- [ ] **Step 0: Check the branch and Task 5**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && git branch --show-current && git status --short
grep -n "NOT_CHAT_MODEL\|def _not_chat_model" app/services/ai_profile.py
```
Expected: `robot-camera-thinking`; none of this task's four files listed as changed; the grep shows Task 5's
`NOT_CHAT_MODEL = "not_chat_model"`, the `_NOT_CHAT = re.compile(...)` line and `def _not_chat_model(`. The test
counts below assume Task 5 ran (it adds 6 tests to `test_ai_profile.py` and 1 to `test_api_ai.py`). If it has not
run, every count for those files is 6 (profile) or 1 (API) lower, and in Step 3c the new constants go right after the
`_SETTINGS = (...)` line instead.

#### Part A: what a model accepts, and the requests (ai_profile.py)

- [ ] **Step 1: Write the failing tests**

1a. In `tests/test_ai_profile.py`, replace lines 1-18 (the module docstring, the imports and the five message
constants, ending with `EFFORT_VALUE_REFUSED = (...)`) with:

```python
"""Request profiles: the settings each model accepts, found from the services' own 400 messages, and the thinking
levels each model accepts.

The first three messages were measured with OpenAI on 2026-10-10 (spec §7); the reasoning_effort value refusals
LUNA_REFUSES_MAX and ASTRA_REFUSES_NONE on 2026-10-11. ASTRA_REFUSES_MAX is the same shape, constructed for the
test. The OpenRouter entries are cut from its public model list, fetched 2026-10-11."""

import copy

import pytest

from app.services import ai_profile

MAX_TOKENS_REFUSED = ("Unsupported parameter: 'max_tokens' is not supported with this model. "
                      "Use 'max_completion_tokens' instead.")
TEMPERATURE_REFUSED = ("Unsupported value: 'temperature' does not support 0.7 with this model. "
                       "Only the default (1) value is supported.")
EFFORT_UNKNOWN = "Unrecognized request argument supplied: reasoning_effort"
LUNA_REFUSES_MAX = ("Unsupported value: 'reasoning_effort' does not support 'max' with this model. "
                    "Supported values are: 'none', 'low', 'medium', 'high', and 'xhigh'.")
ASTRA_REFUSES_NONE = ("Unsupported value: 'reasoning_effort' does not support 'none' with this model. "
                      "Supported values are: 'low', 'medium', 'high', and 'xhigh'.")
ASTRA_REFUSES_MAX = ("Unsupported value: 'reasoning_effort' does not support 'max' with this model. "
                     "Supported values are: 'low', 'medium', 'high', and 'xhigh'.")
```
(`EFFORT_VALUE_REFUSED` is gone: its only user is the test replaced in 1b. The three new messages are the live
OpenAI texts.)

1b. Replace the whole test `test_reasoning_effort_none_refused_tries_minimal_then_drops_it` (lines 63-70) with these
three tests (keep two blank lines before the next `@pytest.mark.parametrize("response, words", ...)`):

```python
def test_a_model_that_refuses_none_always_thinks_at_its_lowest_level():
    """GPT-6 Astra and 6.1 Sol (spec F14): never "no setting", which leaves the model thinking at its default level
    inside the reply's 200 tokens. Its "off" is low, without temperature, tried with low's allowance."""
    send, sent = _service((lambda p: p["reasoning_off"] == {"reasoning_effort": "none"}, _bad(ASTRA_REFUSES_NONE)))
    result = ai_profile.detect(send, "openai")
    assert [p["reasoning_off"] for p in sent] == [{"reasoning_effort": "none"}, {"reasoning_effort": "low"}]
    assert result["ok"] and result["profile"] == {
        "token_param": "max_completion_tokens", "temperature": False, "reasoning_off": {"reasoning_effort": "low"},
        "thinking": {"param": "reasoning_effort", "levels": ["low"], "ms": {}, "always": True, "temperature": False}}
    assert ai_profile.probe_body(result["profile"], "gpt-6-astra") == {
        "model": "gpt-6-astra", "messages": ai_profile.TEST_MESSAGES, "max_completion_tokens": 1000,
        "reasoning_effort": "low"}


def test_without_a_list_none_then_minimal_refused_moves_to_low_never_to_no_setting():
    refused = "Unsupported value: 'reasoning_effort' does not support '{}' with this model."
    send, sent = _service(
        (lambda p: p["reasoning_off"] == {"reasoning_effort": "none"}, _bad(refused.format("none"))),
        (lambda p: p["reasoning_off"] == {"reasoning_effort": "minimal"}, _bad(refused.format("minimal"))))
    result = ai_profile.detect(send, "openai")
    assert [p["reasoning_off"] for p in sent] == [{"reasoning_effort": "none"}, {"reasoning_effort": "minimal"},
                                                  {"reasoning_effort": "low"}]
    assert result["ok"] and result["profile"]["thinking"]["always"] is True


def test_a_model_that_lists_minimal_turns_thinking_down_to_minimal():
    message = ("Unsupported value: 'reasoning_effort' does not support 'none' with this model. "
               "Supported values are: 'minimal', 'low', 'medium', and 'high'.")
    send, _ = _service((lambda p: p["reasoning_off"] == {"reasoning_effort": "none"}, _bad(message)))
    profile = ai_profile.detect(send, "openai")["profile"]
    assert profile["reasoning_off"] == {"reasoning_effort": "minimal"} and "thinking" not in profile
```

1c. Append to the end of the file (after Task 5's tests). The file already has `copy`, `pytest`, `ai_profile`,
`_ok()`, `_bad(message, status=400)` and `_service(*rules)`; the new constants from 1a are used here too:

```python
# Thinking levels (spec 2026-10-11 D2): what each model accepts, found by Test.

OPENAI_BASE = {"token_param": "max_completion_tokens", "temperature": True,
               "reasoning_off": {"reasoning_effort": "none"}}
LEVEL_MS = {"low": 1250, "medium": 1640, "high": 1984}
OR_OPENAI_PARAMS = ["include_reasoning", "max_completion_tokens", "max_tokens", "reasoning", "reasoning_effort",
                    "response_format", "seed", "structured_outputs", "tool_choice", "tools", "verbosity"]
OR_LUNA = {"id": "openai/gpt-6-luna", "supported_parameters": OR_OPENAI_PARAMS,
           "reasoning": {"mandatory": False, "default_enabled": True, "default_effort": "medium",
                         "supported_efforts": ["max", "xhigh", "high", "medium", "low", "none"]}}
OR_SOL = {"id": "openai/gpt-6.1-sol", "supported_parameters": OR_OPENAI_PARAMS,
          "reasoning": {"mandatory": True, "default_enabled": True, "default_effort": "medium",
                        "supported_efforts": ["max", "xhigh", "high", "medium", "low"]}}
OR_STEP = {"id": "stepfun/step-5-preview",
           "supported_parameters": ["frequency_penalty", "include_reasoning", "logprobs", "max_tokens", "reasoning",
                                    "reasoning_effort", "response_format", "stop", "structured_outputs", "temperature",
                                    "tools", "top_logprobs", "top_p"],
           "reasoning": {"mandatory": True, "default_effort": "medium", "supported_efforts": ["high", "medium", "low"]}}
OR_GEMINI_25 = {"id": "google/gemini-2.5-flash", "reasoning": {"mandatory": False},
                "supported_parameters": ["include_reasoning", "max_tokens", "reasoning", "response_format", "seed",
                                         "stop", "structured_outputs", "temperature", "tool_choice", "tools", "top_p"]}


def _thinker(max_answer=None, refused=(), cut_off=(), model_id="gpt-6-luna"):
    """A fake thinking model for detect_thinking, answering the bodies Test really sends (ai_profile.probe_body).
    max_answer: the answer to reasoning_effort "max" (default: accepted); refused: levels answered with a 400;
    cut_off: levels whose thinking used the whole budget (no text, finish_reason length)."""
    sent = []

    def send(profile, level="off"):
        body = ai_profile.probe_body(profile, model_id, level)
        sent.append(body)
        effort = body.get("reasoning_effort") or (body.get("reasoning") or {}).get("effort")
        if effort == "max":
            return max_answer or _ok()
        if effort in refused:
            return _bad(f"Unsupported value: 'reasoning_effort' does not support '{effort}' with this model.")
        if effort in cut_off:
            return 200, {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}, 2100
        return 200, {"choices": [{"finish_reason": "stop", "message": {"content": "您好！"}}]}, LEVEL_MS[effort]
    return send, sent


def _can(levels, always=False, param="reasoning_effort", temperature=False):
    return {**OPENAI_BASE, "thinking": {"param": param, "levels": levels, "ms": {}, "always": always,
                                        "temperature": temperature}}


@pytest.mark.parametrize("message, values", [
    (LUNA_REFUSES_MAX, ["none", "low", "medium", "high", "xhigh"]),
    (ASTRA_REFUSES_NONE, ["low", "medium", "high", "xhigh"]),
    (EFFORT_UNKNOWN, None),
    ("Unsupported value: 'reasoning_effort' does not support 'max' with this model.", None),
])
def test_the_accepted_values_are_read_from_the_refusal(message, values):
    assert ai_profile._supported_values(message) == values


def test_one_refused_request_lists_the_levels_then_each_is_timed():
    send, sent = _thinker(max_answer=_bad(LUNA_REFUSES_MAX))
    thinking = ai_profile.detect_thinking(send, "openai", OPENAI_BASE)
    assert thinking == {"param": "reasoning_effort", "levels": ["low", "medium", "high"], "ms": LEVEL_MS,
                        "always": False, "temperature": False}
    assert sent[0] == {"model": "gpt-6-luna", "messages": ai_profile.TEST_MESSAGES, "max_completion_tokens": 200,
                       "reasoning_effort": "max"}
    assert [(body["reasoning_effort"], body["max_completion_tokens"]) for body in sent[1:]] == [
        ("low", 1000), ("medium", 1800), ("high", 3200)]
    assert not any("temperature" in body for body in sent)


def test_a_level_whose_thinking_uses_the_whole_budget_is_not_offered():
    send, _ = _thinker(max_answer=_bad(LUNA_REFUSES_MAX), cut_off={"high"})
    thinking = ai_profile.detect_thinking(send, "openai", OPENAI_BASE)
    assert thinking["levels"] == ["low", "medium"] and "high" not in thinking["ms"]


@pytest.mark.parametrize("max_answer", [None, _bad("Something new we do not understand"),
                                        _bad("Invalid value for 'reasoning_effort': 'max'."),
                                        (0, {"error": {"message": "No answer: ReadTimeout"}}, 15000)])
def test_without_a_list_each_level_is_tried(max_answer):
    send, sent = _thinker(max_answer=max_answer, refused={"high"})
    thinking = ai_profile.detect_thinking(send, "openai_compatible", OPENAI_BASE)
    assert thinking["levels"] == ["low", "medium"] and len(sent) == 4


def test_a_model_that_does_not_know_reasoning_effort_cannot_think():
    send, sent = _thinker(max_answer=_bad(EFFORT_UNKNOWN))
    thinking = ai_profile.detect_thinking(send, "openai", {**OPENAI_BASE, "reasoning_off": None})
    assert thinking == ai_profile.CANT_THINK and len(sent) == 1


def test_an_always_thinking_model_keeps_always_and_the_level_its_base_test_passed():
    send, _ = _service((lambda p: p["reasoning_off"] == {"reasoning_effort": "none"}, _bad(ASTRA_REFUSES_NONE)))
    profile = ai_profile.detect(send, "openai")["profile"]
    thinker, _ = _thinker(max_answer=_bad(ASTRA_REFUSES_MAX), model_id="gpt-6-astra")
    thinking = ai_profile.detect_thinking(thinker, "openai", profile)
    assert thinking == {"param": "reasoning_effort", "levels": ["low", "medium", "high"], "ms": LEVEL_MS,
                        "always": True, "temperature": False}
    flaky, _ = _thinker(max_answer=_bad(ASTRA_REFUSES_MAX), cut_off={"low"}, model_id="gpt-6-astra")
    assert ai_profile.detect_thinking(flaky, "openai", profile)["levels"] == ["low", "medium", "high"]


def test_openrouter_levels_come_from_its_public_model_list():
    send, sent = _thinker(model_id="openai/gpt-6-luna")
    thinking = ai_profile.detect_thinking(send, "openrouter", ai_profile.starting_profile("openrouter"),
                                          lambda: OR_LUNA)
    assert thinking == {"param": "openrouter_effort", "levels": ["low", "medium", "high"], "ms": LEVEL_MS,
                        "always": False, "temperature": False}
    assert [(body["reasoning"], body["max_tokens"]) for body in sent] == [
        ({"effort": "low", "exclude": True}, 1000), ({"effort": "medium", "exclude": True}, 1800),
        ({"effort": "high", "exclude": True}, 3200)]
    assert not any("temperature" in body for body in sent)   # and no "max" request: the list says it all


def test_a_mandatory_openrouter_model_always_thinks_and_keeps_temperature_only_when_listed():
    send, sent = _thinker(model_id="stepfun/step-5-preview")
    thinking = ai_profile.detect_thinking(send, "openrouter", ai_profile.starting_profile("openrouter"),
                                          lambda: OR_STEP)
    assert (thinking["always"], thinking["temperature"]) == (True, True)
    assert not any("temperature" in body for body in sent)   # the probes never carry it
    sol, _ = _thinker(model_id="openai/gpt-6.1-sol")
    thinking = ai_profile.detect_thinking(sol, "openrouter", ai_profile.starting_profile("openrouter"), lambda: OR_SOL)
    assert (thinking["always"], thinking["temperature"]) == (True, False)


@pytest.mark.parametrize("entry", [None, OR_GEMINI_25, {"id": "openai/gpt-4.1-mini", "reasoning": None}])
def test_an_openrouter_model_without_effort_levels_cannot_think(entry):
    send, sent = _thinker()
    thinking = ai_profile.detect_thinking(send, "openrouter", ai_profile.starting_profile("openrouter"),
                                          lambda: entry)
    assert thinking == ai_profile.CANT_THINK and sent == []


def test_openrouter_efforts_listed_as_null_mean_every_level_is_tried():
    """OpenRouter's docs: supported_efforts null means every effort is accepted (no model in the list did so on
    2026-10-11); the probes still decide."""
    send, sent = _thinker(refused={"high"}, model_id="x/any")
    entry = {"id": "x/any", "reasoning": {"mandatory": False, "supported_efforts": None}}
    thinking = ai_profile.detect_thinking(send, "openrouter", ai_profile.starting_profile("openrouter"),
                                          lambda: entry)
    assert thinking["levels"] == ["low", "medium"] and len(sent) == 3


def test_no_probe_starts_after_the_time_budget():
    ticks = iter([0, 0, ai_profile.THINKING_SECONDS + 1])
    send, sent = _thinker(max_answer=_bad(LUNA_REFUSES_MAX))
    thinking = ai_profile.detect_thinking(send, "openai", OPENAI_BASE, clock=lambda: next(ticks))
    assert thinking["levels"] == ["low"] and len(sent) == 2


@pytest.mark.parametrize("levels, always, asked, sent", [
    (["low", "medium", "high"], False, "medium", "medium"),
    (["low", "high"], False, "medium", "low"),
    (["medium", "high"], False, "low", "off"),
    (["low", "medium", "high"], False, "off", "off"),
    (["low", "medium", "high"], True, "off", "low"),
    (["medium", "high"], True, "low", "medium"),
    ([], False, "high", "off"),
])
def test_a_model_gets_the_nearest_lower_level_it_accepts(levels, always, asked, sent):
    assert ai_profile.effective_level(_can(levels, always), asked) == sent


def test_profiles_without_thinking_cannot_think():
    assert ai_profile.effective_level(OPENAI_BASE, "high") == "off"
    assert ai_profile.effective_level(None, "low") == "off"
    assert ai_profile.effective_level({**OPENAI_BASE, "thinking": ai_profile.CANT_THINK}, "low") == "off"
    assert ai_profile.allowance(None, "high") == 0


@pytest.mark.parametrize("profile, asked, extra", [
    (_can(["low", "medium"]), "high", 1600),
    (_can(["low", "medium", "high"]), "off", 0),
    (_can(["low", "medium", "high"], always=True), "off", 800),
    (_can(["high"]), "medium", 0),
])
def test_the_allowance_is_for_the_level_actually_sent(profile, asked, extra):
    assert ai_profile.allowance(profile, asked) == extra


def test_a_thinking_level_is_sent_without_temperature_or_reasoning_off():
    body = ai_profile.request_body(_can(["low", "medium"]), "gpt-6-luna", [], 1800, 0.7, level="high")
    assert body == {"model": "gpt-6-luna", "messages": [], "max_completion_tokens": 1800, "reasoning_effort": "medium"}
    assert ai_profile.request_body(_can(["low"]), "gpt-6-luna", [], 200, 0.7) == {
        "model": "gpt-6-luna", "messages": [], "max_completion_tokens": 200, "temperature": 0.7,
        "reasoning_effort": "none"}   # "off" is today's request


def test_openrouter_thinking_is_hidden_and_keeps_temperature_only_when_listed():
    profile = {**ai_profile.starting_profile("openrouter"),
               "thinking": {"param": "openrouter_effort", "levels": ["low", "medium", "high"], "ms": {},
                            "always": False, "temperature": True}}
    body = ai_profile.request_body(profile, "stepfun/step-5-preview", [], 1800, 0.7, {"provider": {"only": ["x"]}},
                                   level="medium")
    assert body == {"model": "stepfun/step-5-preview", "messages": [], "max_tokens": 1800, "temperature": 0.7,
                    "reasoning": {"effort": "medium", "exclude": True}, "provider": {"only": ["x"]}}
    assert "temperature" not in ai_profile.request_body(profile, "stepfun/step-5-preview", [], 1800, None,
                                                        level="medium")   # a caller's None sends none
    profile["thinking"]["temperature"] = False
    assert "temperature" not in ai_profile.request_body(profile, "openai/gpt-6-luna", [], 1800, 0.7, level="medium")
```

- [ ] **Step 2: Run them and see them fail**

```bash
.venv/Scripts/python.exe -m pytest tests/test_ai_profile.py -q -p no:cacheprovider
```
Expected: `35 failed, 28 passed`. The reasons:
- 14 `AttributeError: module 'app.services.ai_profile' has no attribute 'detect_thinking'`, 8 `... 'effective_level'`,
  4 `... 'allowance'`, 4 `... '_supported_values'`, 1 `... 'THINKING_SECONDS'`;
- 2 `TypeError: request_body() got an unexpected keyword argument 'level'`;
- `test_a_model_that_refuses_none_always_thinks_at_its_lowest_level`: `assert [{'reasoning_...': 'minimal'}] ==
  [{'reasoning_...fort': 'low'}]` (today it tries `minimal`);
- `test_without_a_list_none_then_minimal_refused_moves_to_low_never_to_no_setting`: `assert [{'reasoning_...nimal'},
  None] == [{'reasoning_...fort': 'low'}]`: today's third try sends no setting at all, the F14 bug.
`test_a_model_that_lists_minimal_turns_thinking_down_to_minimal` already passes (it guards today's `minimal` step).

- [ ] **Step 3: Implement in `app/services/ai_profile.py`**

3a. Replace lines 1-13 (the module docstring, `import copy`, `import re` and `from app.services.ai_crypto import
redact`) with (one paragraph added to the docstring, and `import time`):

```python
"""How to ask one model: the request settings it accepts (its profile), found by trying, and the request they give.

Models differ even within one service (measured 2026-10-10 with OpenAI): gpt-6-luna and gpt-5.4-mini refuse
max_tokens and want max_completion_tokens; gpt-6-luna refuses a temperature other than the default unless reasoning is turned off
(reasoning_effort none); gpt-4.1-mini refuses
reasoning_effort. So no request is fixed in code: the Test button starts from the service's usual request, reads
each 400 message, changes the one setting it names, and the card saves what worked.

Thinking (spec 2026-10-11 D2): after that, detect_thinking finds the levels (low, medium, high) the model accepts and
times each one; the profile saves them as "thinking". A model that refuses reasoning_effort "none" (GPT-6 Astra,
GPT-6.1 Sol) always thinks: its "off" is its lowest level, sent with that level's token allowance.
"""

import copy
import re
import time

from app.services.ai_crypto import redact
```

3b. Between `TEST_MAX_TOKENS = 200   # conversation.REPLY_MAX_TOKENS: ...` (line 25) and `TEST_TIMEOUT = 15` (line 26)
add one line, so the three lines read:

```python
TEST_MAX_TOKENS = 200   # conversation.REPLY_MAX_TOKENS: a model that cannot answer within it cannot serve replies
TEST_TEMPERATURE = 0.7
TEST_TIMEOUT = 15
```

3c. Directly above `def starting_profile(kind: str) -> dict:`, after the last constant (Task 5's
`_NOT_CHAT = re.compile(r"not a chat model|...", re.IGNORECASE)`) and keeping the two blank lines before the `def`,
add:

```python
THINKING_LEVELS = ("low", "medium", "high")
ALLOWANCE = {"low": 800, "medium": 1600, "high": 3000}   # tokens added to a call's cap at the level sent (spec D4)
THINKING_SECONDS = 40   # Test starts no thinking probe after this; a level not tried is not offered
CANT_THINK = {"param": None, "levels": [], "ms": {}, "always": False, "temperature": False}
_OFF_ORDER = ("none", "minimal") + THINKING_LEVELS   # reasoning_effort values for "off", the least thinking first
_NEXT_OFF = {"none": "minimal", "minimal": "low"}     # tried in turn when a refusal lists no accepted values
```

3d. Replace the whole function `adjust()` (lines 42-62; 47-67 after Task 5, from `def adjust(profile: dict,
message: str) -> dict | None:` to its `return new`) with the three helpers and the new `adjust()`. Only the
`reasoning_effort` branch of `adjust()` changes: a refused value now moves to the next "off" value the refusal
lists (none, minimal, or else the lowest level, which makes the model `always` think), never to "no setting":

```python
def _value_refused(message: str) -> bool:
    """The 400 refuses a value we sent ("does not support 'none'"), not a setting the model doesn't know."""
    lowered = message.lower()
    return "does not support" in lowered or "unsupported value" in lowered


def _supported_values(message: str) -> list[str] | None:
    """The values a refusal lists, in its order: "... Supported values are: 'none', 'low', 'medium', 'high', and
    'xhigh'." (OpenAI, measured 2026-10-11; not a documented format). None when it lists none."""
    match = re.search(r"supported values are:?(.*)", message, re.IGNORECASE | re.DOTALL)
    values = re.findall(r"'([A-Za-z_]+)'", match.group(1)) if match else []
    return [value.lower() for value in values] or None


def _off_value(supported: list[str] | None, refused: str) -> tuple[str, bool] | None:
    """The reasoning_effort to send as "off" after `refused` was refused, and whether the model then always thinks:
    none or minimal when listed, else the lowest listed level. Without a list, the next value up, as far as low.
    None when nothing is left. Never "no setting": that leaves the model thinking at its default level inside the
    reply's 200 tokens (spec F14)."""
    if supported is None:
        value = _NEXT_OFF.get(refused)
        return (value, value in THINKING_LEVELS) if value else None
    for value in _OFF_ORDER:
        if value in supported and value != refused:
            return value, value in THINKING_LEVELS
    return None


def adjust(profile: dict, message: str) -> dict | None:
    """The profile with the one setting the 400 message names changed, or None when it names nothing we send."""
    setting, new = _named_setting(message), copy.deepcopy(profile)
    sent_reasoning = profile["reasoning_off"] or {}
    if setting == "max_tokens" and profile["token_param"] == "max_tokens":
        new["token_param"] = "max_completion_tokens"
    elif setting == "max_completion_tokens" and profile["token_param"] == "max_completion_tokens":
        new["token_param"] = "max_tokens"
    elif setting == "temperature" and profile["temperature"]:
        new["temperature"] = False
    elif setting == "reasoning_effort" and "reasoning_effort" in sent_reasoning:
        if not _value_refused(message):
            new["reasoning_off"] = None   # a setting it doesn't know ("Unrecognized request argument"): it can't think
        else:
            off = _off_value(_supported_values(message), sent_reasoning["reasoning_effort"])
            if off is None:
                return None
            value, always = off
            new["reasoning_off"] = {"reasoning_effort": value}
            if always:
                # It can't stop thinking (GPT-6 Astra, 6.1 Sol): "off" is its lowest level, without the temperature
                # a thinking model refuses, and with that level's allowance (so Test tries it with that budget).
                new["temperature"] = False
                new["thinking"] = {"param": "reasoning_effort", "levels": [value], "ms": {}, "always": True,
                                   "temperature": False}
    elif setting == "reasoning" and sent_reasoning:
        # "Reasoning is mandatory for this endpoint": whichever form of reasoning-off we sent, stop sending it.
        new["reasoning_off"] = None
    else:
        return None
    return new
```

3e. Replace the whole function `request_body()` (lines 65-72; 70-77 after Task 5) with `effective_level()`,
`allowance()`, the new `request_body()` and `probe_body()`, in this order (two blank lines between functions, and
two before `def headers(`):

```python
def effective_level(profile: dict | None, level: str) -> str:
    """The thinking level actually sent when a job asks for `level`: the nearest lower level the model accepts, else
    "off". A model that always thinks never gets "off": below its lowest level it gets that level. A profile without
    "thinking" (tested before thinking levels, or untested) can't think."""
    thinking = (profile or {}).get("thinking") or {}
    accepted = [name for name in THINKING_LEVELS if name in (thinking.get("levels") or [])]
    if not thinking.get("param") or not accepted:
        return "off"
    wanted = THINKING_LEVELS.index(level) if level in THINKING_LEVELS else -1
    lower = [name for name in accepted if THINKING_LEVELS.index(name) <= wanted]
    if lower:
        return lower[-1]
    return accepted[0] if thinking.get("always") else "off"


def allowance(profile: dict | None, level: str) -> int:
    """The tokens to add to a call's cap when its job asks for `level`: the allowance of the level actually sent
    (effective_level), 0 at "off"."""
    return ALLOWANCE.get(effective_level(profile, level), 0)


def request_body(profile: dict, model_id: str, messages: list[dict], max_tokens: int, temperature: float | None,
                 extras: dict | None = None, *, level: str = "off") -> dict:
    """The request for this model. At "off" (the default) it is the request from before thinking levels, with the
    profile's reasoning_off. At another level it sends the level the model accepts (effective_level): OpenAI's
    reasoning_effort without temperature, or OpenRouter's reasoning effort (hidden from the answer) with temperature
    only when the model's OpenRouter entry lists it. temperature None sends none. max_tokens is sent as given:
    allowance() says what to add for thinking."""
    sent = effective_level(profile, level)
    body = {"model": model_id, "messages": messages, profile["token_param"]: max_tokens}
    if sent == "off":
        if profile["temperature"] and temperature is not None:
            body["temperature"] = temperature
        body.update(copy.deepcopy(profile["reasoning_off"] or {}))
    elif profile["thinking"]["param"] == "openrouter_effort":
        if profile["thinking"].get("temperature") and temperature is not None:
            body["temperature"] = temperature
        body["reasoning"] = {"effort": sent, "exclude": True}
    else:
        body["reasoning_effort"] = sent
    body.update(extras or {})
    return body


def probe_body(profile: dict, model_id: str, level: str = "off", extras: dict | None = None) -> dict:
    """Test's request at `level`: the short greeting with the reply's budget plus the allowance of the level sent (a
    model that always thinks is tried with the budget its replies get)."""
    return request_body(profile, model_id, TEST_MESSAGES, TEST_MAX_TOKENS + allowance(profile, level),
                        TEST_TEMPERATURE, extras, level=level)
```

3f. Append at the end of the file, after `detect()` (whose last line is `return _failed(ms, f"Gave up after
{MAX_TRIES} tries: {_error_message(body)}", secret)`), with two blank lines before the first new function:

```python
def _full_answer(body) -> bool:
    """A 200 body with text that was not cut off by the length limit (thinking can use up the whole budget)."""
    choices = (body.get("choices") if isinstance(body, dict) else None) or []
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message_part = choice.get("message")
    content = message_part.get("content") if isinstance(message_part, dict) else None
    return isinstance(content, str) and bool(content.strip()) and choice.get("finish_reason") != "length"


def _levels_to_try(send, profile: dict) -> list[str] | None:
    """OpenAI and compatible services: the levels worth a probe. One request asks for reasoning_effort "max" without
    temperature, and its refusal lists the values the model accepts. Without a list (an accepted "max", another
    error, no answer) all three are tried. None when the model doesn't know reasoning_effort ("Unrecognized request
    argument supplied: reasoning_effort"): it can't think."""
    asked = {**copy.deepcopy(profile), "temperature": False, "reasoning_off": {"reasoning_effort": "max"}}
    asked.pop("thinking", None)
    status, body, _ = send(asked)
    if status == 400:
        message = _error_message(body)
        listed = _supported_values(message)
        if listed is not None:
            return [level for level in THINKING_LEVELS if level in listed]
        if (_named_setting(message) == "reasoning_effort"
                and re.search(r"unrecognized request argument|unknown parameter", message, re.IGNORECASE)):
            return None
    return list(THINKING_LEVELS)


def _probe(send, profile: dict, param: str, level: str) -> int | None:
    """One short request at `level`, without temperature, with the reply's budget plus the level's allowance. Its
    time in ms, or None when the level is not offered: refused, no answer, empty, or cut off by the length limit."""
    trial = {**copy.deepcopy(profile), "thinking": {"param": param, "levels": [level], "ms": {}, "always": False,
                                                     "temperature": False}}
    status, body, ms = send(trial, level)
    return ms if status == 200 and _full_answer(body) else None


def detect_thinking(send, kind: str, profile: dict, openrouter_entry=None, clock=time.monotonic) -> dict:
    """The thinking levels a model accepts (spec D2), found by Test after detect() passed, and apart from it: the
    Test route saves the base profile whatever happens here. send(profile, level="off") is detect's send, with the
    level to ask for. openrouter_entry() gives the model's entry in OpenRouter's public model list (None: not
    listed); only OpenRouter connections call it, and it may raise.

    Returns the profile's "thinking": {"param": "reasoning_effort" | "openrouter_effort" | None, "levels": the levels
    offered, "ms": each level's time, "always": the model can't stop thinking, "temperature": a thinking request may
    carry temperature}; CANT_THINK when no level is offered. A level is offered only when its probe answered in
    full. No probe starts after THINKING_SECONDS."""
    started = clock()
    base = profile.get("thinking") or {}
    if kind == "openrouter":
        entry = openrouter_entry() if openrouter_entry else None
        entry = entry if isinstance(entry, dict) else {}
        reasoning = entry.get("reasoning") if isinstance(entry.get("reasoning"), dict) else {}
        efforts = reasoning.get("supported_efforts", [])
        if efforts is None:   # OpenRouter's docs: null means every effort is accepted; no key means none is
            efforts = THINKING_LEVELS
        parameters = entry.get("supported_parameters") if isinstance(entry.get("supported_parameters"), list) else []
        param, always = "openrouter_effort", bool(reasoning.get("mandatory"))
        temperature = "temperature" in parameters and bool(profile["temperature"])
        to_try = [level for level in THINKING_LEVELS if isinstance(efforts, (list, tuple)) and level in efforts]
    else:
        param, always, temperature = "reasoning_effort", bool(base.get("always")), False
        to_try = _levels_to_try(send, profile) or []
    levels, ms = [], {}
    for level in to_try:
        if clock() - started > THINKING_SECONDS:
            break
        took = _probe(send, profile, param, level)
        if took is not None:
            levels.append(level)
            ms[level] = took
    if always:
        # A model adjust() found always thinking passed the base test at its lowest level: that level stays.
        levels = [level for level in THINKING_LEVELS if level in levels or level in (base.get("levels") or [])]
    if not levels:
        return copy.deepcopy(CANT_THINK)
    return {"param": param, "levels": levels, "ms": ms, "always": always, "temperature": temperature}
```

Check the layout and the line endings:

```bash
grep -n "^def \|^THINKING_LEVELS\|^ALLOWANCE\|^TEST_TEMPERATURE\|^import time" app/services/ai_profile.py
file app/services/ai_profile.py tests/test_ai_profile.py
```
Expected order of the `def`s: `starting_profile`, `_named_setting`, `_value_refused`, `_supported_values`,
`_off_value`, `adjust`, `effective_level`, `allowance`, `request_body`, `probe_body`, `headers`, `_error_message`,
`_not_chat_model`, `_failed`, `detect`, `_full_answer`, `_levels_to_try`, `_probe`, `detect_thinking`. Both files
"with CRLF line terminators".

- [ ] **Step 4: Run them and see them pass; the Test route still works unchanged**

```bash
.venv/Scripts/python.exe -m pytest tests/test_ai_profile.py tests/test_api_ai.py -q -p no:cacheprovider
```
Expected: `95 passed` (63 in `test_ai_profile.py`, 32 in `test_api_ai.py`). The route still calls
`request_body(...)` positionally, which gives today's body at the default level `"off"`.

- [ ] **Step 5: Commit Part A**

```bash
git add app/services/ai_profile.py tests/test_ai_profile.py
git diff --cached --stat
git commit -F - <<'EOF'
AI profiles: thinking levels a model accepts, and never "no setting"

ai_profile gains THINKING_LEVELS, ALLOWANCE, effective_level(), allowance(),
request_body(..., level=) and detect_thinking(), which finds the levels a
model accepts (OpenAI: one refused reasoning_effort "max" lists them, else
one probe per level; OpenRouter: its public model entry) and times each.

A model that refuses reasoning_effort "none" (GPT-6 Astra, 6.1 Sol) now gets
its lowest level as "off", with thinking.always, instead of no setting,
which left it thinking at its default level inside a 200-token cap (F14).

Co-Authored-By: <the model that wrote the commit> <noreply@anthropic.com>
EOF
```
Replace the trailer's placeholder with the model actually writing the commit (for example `Claude Opus 5.5 (1M
context)`). `git diff --cached --stat` must list exactly these 2 files.

#### Part B: Test runs the thinking probes, apart from the base test (api_ai.py)

- [ ] **Step 6: Write the failing tests**

6a. In `tests/test_api_ai.py`, in `test_the_test_button_saves_what_the_model_accepted` (lines 247-264), its model 13
is on the OpenRouter connection, so after this task the route also asks OpenRouter's model list; the test must stub
that call (or it would reach the internet). Replace lines 257-261:

```python
    monkeypatch.setattr(api_ai.requests, "post", post)
    response = api.client.post("/api/ai/models/13/test")
    body = response.json()
    assert body["ok"] is True and body["profile"]["temperature"] is False and body["sample"] == "您好！"
    assert "routerSECRET" not in response.text and "connections" in body["settings"]
```
with:

```python
    monkeypatch.setattr(api_ai.requests, "post", post)
    monkeypatch.setattr(api_ai.requests, "get", lambda url, timeout: Reply(200, {"data": [{"id": "other/model"}]}))
    response = api.client.post("/api/ai/models/13/test")
    body = response.json()
    assert body["ok"] is True and body["profile"]["temperature"] is False and body["sample"] == "您好！"
    assert body["profile"]["thinking"] == ai_profile.CANT_THINK   # not in OpenRouter's list: it can't think
    assert "routerSECRET" not in response.text and "connections" in body["settings"]
```
(The three lines after them stay: the two `sent[0]` asserts and `assert api.store.calls[-1] == ("save_test", 13,
True, body["profile"])`.)

6b. Append to the end of the file (after Task 5's test). The file already has `dataclasses`, `ai_profile`,
`ai_settings`, `api_ai`, `NEW_KEY`, `Reply`, `_snapshot()` and the `api` fixture (with `FakeStore.calls`):

```python
# Thinking levels found by Test (spec 2026-10-11 D2)

LUNA_REFUSES_MAX = ("Unsupported value: 'reasoning_effort' does not support 'max' with this model. "
                    "Supported values are: 'none', 'low', 'medium', 'high', and 'xhigh'.")
ASTRA_REFUSES_NONE = ("Unsupported value: 'reasoning_effort' does not support 'none' with this model. "
                      "Supported values are: 'low', 'medium', 'high', and 'xhigh'.")
HELLO = {"choices": [{"finish_reason": "stop", "message": {"content": "您好！"}}]}


def _openai_model(monkeypatch, model_id="gpt-6-luna"):
    snapshot = _snapshot()
    openai = ai_settings.Connection(2, "OpenAI", "openai", ai_profile.BASE_URLS["openai"], NEW_KEY, "9876", True,
                                    "OpenAI")
    model = ai_settings.Model(21, 2, model_id, "OpenAI", "USA", "30 days", None)
    monkeypatch.setattr(ai_settings, "_snapshot", dataclasses.replace(
        snapshot, connections={**snapshot.connections, 2: openai}, models={**snapshot.models, 21: model}))


def test_the_test_button_saves_the_thinking_levels_the_model_accepts(api, monkeypatch):
    _openai_model(monkeypatch)
    sent = []

    def post(url, json, timeout, headers):
        sent.append(json)
        if json.get("reasoning_effort") == "max":
            return Reply(400, {"error": {"message": LUNA_REFUSES_MAX}})
        return Reply(200, HELLO)

    monkeypatch.setattr(api_ai.requests, "post", post)
    response = api.client.post("/api/ai/models/21/test")
    body = response.json()
    thinking = body["profile"]["thinking"]
    assert body["ok"] is True and body["profile"]["reasoning_off"] == {"reasoning_effort": "none"}
    assert (thinking["param"], thinking["levels"], thinking["always"], thinking["temperature"]) == (
        "reasoning_effort", ["low", "medium", "high"], False, False)
    assert sorted(thinking["ms"]) == ["high", "low", "medium"]
    assert [b.get("reasoning_effort") for b in sent] == ["none", "max", "low", "medium", "high"]
    assert [b["max_completion_tokens"] for b in sent] == [200, 200, 1000, 1800, 3200]
    assert "temperature" in sent[0] and not any("temperature" in b for b in sent[1:])
    assert api.store.calls[-1] == ("save_test", 21, True, body["profile"]) and "newSECRET" not in response.text


def test_a_model_that_refuses_none_is_saved_as_always_thinking(api, monkeypatch):
    """The Astra/Sol bug (spec F14): its "off" becomes low with low's allowance, never "no setting"."""
    _openai_model(monkeypatch, "gpt-6-astra")
    sent = []

    def post(url, json, timeout, headers):   # like GPT-6 Astra: no setting is its default level (medium)
        sent.append(json)
        effort = json.get("reasoning_effort")
        if effort is not None and effort not in ("low", "medium", "high", "xhigh"):
            return Reply(400, {"error": {"message": ASTRA_REFUSES_NONE.replace("'none'", f"'{effort}'", 1)}})
        return Reply(200, HELLO)

    monkeypatch.setattr(api_ai.requests, "post", post)
    profile = api.client.post("/api/ai/models/21/test").json()["profile"]
    assert profile["reasoning_off"] == {"reasoning_effort": "low"} and profile["temperature"] is False
    assert (profile["thinking"]["levels"], profile["thinking"]["always"]) == (["low", "medium", "high"], True)
    assert [(b.get("reasoning_effort"), b["max_completion_tokens"]) for b in sent] == [
        ("none", 200), ("low", 1000), ("max", 200), ("low", 1000), ("medium", 1800), ("high", 3200)]


def test_a_level_that_times_out_is_not_offered_and_the_base_profile_is_saved(api, monkeypatch):
    """Spec review focus 4: a slow level costs only that level."""
    _openai_model(monkeypatch)

    def post(url, json, timeout, headers):
        if json.get("reasoning_effort") in ("max", "high"):
            raise api_ai.requests.Timeout("slow")
        return Reply(200, HELLO)

    monkeypatch.setattr(api_ai.requests, "post", post)
    body = api.client.post("/api/ai/models/21/test").json()
    assert body["ok"] is True and body["profile"]["thinking"]["levels"] == ["low", "medium"]
    assert api.store.calls[-1] == ("save_test", 21, True, body["profile"])


def test_an_openrouter_model_gets_its_levels_from_the_public_list_without_the_key(api, monkeypatch):
    asked, sent = [], []

    def get(url, timeout):   # no headers argument: the public list is asked without the key
        asked.append(url)
        return Reply(200, {"data": [{"id": "other/model"}, {
            "id": "meta/llama-4", "supported_parameters": ["max_tokens", "reasoning", "temperature"],
            "reasoning": {"mandatory": False, "supported_efforts": ["high", "medium", "low"]}}]})

    def post(url, json, timeout, headers):
        sent.append(json)
        return Reply(200, HELLO)

    monkeypatch.setattr(api_ai.requests, "get", get)
    monkeypatch.setattr(api_ai.requests, "post", post)
    thinking = api.client.post("/api/ai/models/13/test").json()["profile"]["thinking"]
    assert asked == ["https://openrouter.ai/api/v1/models"]
    assert {**thinking, "ms": None} == {"param": "openrouter_effort", "levels": ["low", "medium", "high"], "ms": None,
                                        "always": False, "temperature": True}
    assert sorted(thinking["ms"]) == ["high", "low", "medium"]
    assert [b.get("reasoning") for b in sent] == [{"enabled": False}, {"effort": "low", "exclude": True},
                                                  {"effort": "medium", "exclude": True},
                                                  {"effort": "high", "exclude": True}]


def test_a_failed_thinking_step_still_saves_the_base_profile(api, monkeypatch, caplog):
    """OpenRouter's model list down, or anything else failing in the thinking step, never costs the profile the base
    test found."""
    def get(url, timeout):
        raise api_ai.requests.ConnectionError("down")

    monkeypatch.setattr(api_ai.requests, "get", get)
    monkeypatch.setattr(api_ai.requests, "post", lambda url, json, timeout, headers: Reply(200, HELLO))
    with caplog.at_level("WARNING", logger=api_ai.log.name):
        body = api.client.post("/api/ai/models/13/test").json()
    assert body["ok"] is True and body["profile"] == ai_profile.starting_profile("openrouter")
    assert api.store.calls[-1] == ("save_test", 13, True, ai_profile.starting_profile("openrouter"))
    assert "thinking levels of AI model 13 not found: ConnectionError" in caplog.text
    assert "routerSECRET" not in caplog.text
```

- [ ] **Step 7: Run them and see them fail**

```bash
.venv/Scripts/python.exe -m pytest tests/test_api_ai.py -q -p no:cacheprovider
```
Expected: `6 failed, 31 passed`:
- `test_the_test_button_saves_what_the_model_accepted`, `test_the_test_button_saves_the_thinking_levels_the_model_accepts`,
  `test_a_level_that_times_out_is_not_offered_and_the_base_profile_is_saved` and
  `test_an_openrouter_model_gets_its_levels_from_the_public_list_without_the_key`: `KeyError: 'thinking'` (the route
  saves no thinking yet);
- `test_a_model_that_refuses_none_is_saved_as_always_thinking`: `assert (['low'], True) == (['low', 'med...'high'],
  True)` (Part A's `adjust()` already saves `always`, but no level was probed);
- `test_a_failed_thinking_step_still_saves_the_base_profile`: `assert 'thinking levels of AI model 13 not found:
  ConnectionError' in ''`.

- [ ] **Step 8: Implement the Test route**

In `app/routers/api_ai.py`, replace the whole route `test_model()` (lines 235-270 after Task 5, from
`@router.post("/models/{model_ref}/test")` to `return {**result, "settings": _card()}`, including Task 5's three-line
docstring) with the code below. What changes: `send` takes the level and builds Test's request with `ai_profile.probe_body`; a new `openrouter_entry()`
reads the public model list without the key (like `remote_models()`, lines 175-185, but never with headers); after a
passed base test, `ai_profile.detect_thinking` runs in its own `try`, so whatever it raises only costs the thinking
levels, never the base profile. All names used are already imported in this file (`asyncio`, `time`, `requests`,
`log`, `REMOTE_MODELS_TIMEOUT`, `ai_profile`, `ai_settings`, `ai_store`, `conversation`, `get_pool`, `_card`, `_model`).

```python
@router.post("/models/{model_ref}/test")
async def test_model(model_ref: int, user: dict = Depends(get_consented_user)):
    """Find the request settings this model accepts (ai_profile.detect), then the thinking levels it accepts
    (ai_profile.detect_thinking), and save them, or why it failed. A failed base test (the service's words, or
    ai_profile.NOT_CHAT_MODEL for a voice-only model) saves only its reason and starts no thinking probe."""
    model = _model(model_ref)
    connection = ai_settings.current().connections[model.connection_id]
    if not connection.key_readable:
        raise HTTPException(400, "key_unreadable")
    if not connection.api_key:
        raise HTTPException(400, "no_key")
    target = ai_settings.Target(connection, model)
    provider = conversation._provider() if connection.kind == "openrouter" else None   # as real calls are routed

    def send(profile: dict, level: str = "off"):
        body = ai_profile.probe_body(profile, model.model_id, level, {"provider": provider} if provider else None)
        started = time.monotonic()
        try:
            response = requests.post(target.url, json=body, timeout=ai_profile.TEST_TIMEOUT,
                                     headers=ai_profile.headers(connection.kind, connection.api_key))
        except requests.RequestException as exc:
            return 0, {"error": {"message": f"No answer: {type(exc).__name__}"}}, int((time.monotonic() - started) * 1000)
        ms = int((time.monotonic() - started) * 1000)
        try:
            parsed = response.json()
        except ValueError:
            parsed = response.text   # cut by detect, after the key is removed (a cut key would escape the match)
        return response.status_code, parsed, ms

    def openrouter_entry() -> dict | None:
        """The model's entry in OpenRouter's public model list (asked without the key), or None when it isn't listed."""
        response = requests.get(f"{connection.base_url}/models", timeout=REMOTE_MODELS_TIMEOUT)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise RuntimeError("unexpected body")
        return next((item for item in data if isinstance(item, dict) and item.get("id") == model.model_id), None)

    # The exact key too: a service may echo a key of any shape in its error, which is saved and shown on the card.
    result = await asyncio.to_thread(ai_profile.detect, send, connection.kind, connection.api_key)
    if result["ok"]:
        # Apart from the base test (spec review focus 4): nothing here may cost the model the profile detect found.
        try:
            thinking = await asyncio.to_thread(ai_profile.detect_thinking, send, connection.kind, result["profile"],
                                               openrouter_entry)
        except Exception as exc:
            log.warning("thinking levels of AI model %s not found: %s", model_ref,
                        exc if isinstance(exc, RuntimeError) else type(exc).__name__)
        else:
            result = {**result, "profile": {**result["profile"], "thinking": thinking}}
    async with get_pool().acquire() as conn:
        async with ai_store.change(conn):
            await ai_store.save_test(conn, model_ref, result)
    return {**result, "settings": _card()}
```
(The `return 0, {...}` line inside `send` is unchanged from today and is 122 characters long, as it is now.)

- [ ] **Step 9: Run them and see them pass**

```bash
.venv/Scripts/python.exe -m pytest tests/test_ai_profile.py tests/test_api_ai.py -q -p no:cacheprovider
file app/routers/api_ai.py tests/test_api_ai.py
```
Expected: `100 passed` (63 + 37); `api_ai.py` "with CRLF line terminators", `test_api_ai.py` without.

- [ ] **Step 10: Run the whole backend suite**

```bash
.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider
```
Expected: the passed count from before this task plus 40 (`1680 passed`: 1640 after Tasks 3-5, plus 40), and the only
failure the known `test_a_broken_background_never_costs_the_reply` (BUG-37). The two timing tests of BUG-39 may flake:
rerun a flaky one alone. (Prototype on a copy of the branch with only Task 5's Part D applied: 1603 before, 1643 passed
after, plus BUG-37.)

- [ ] **Step 11: Commit Part B**

```bash
git add app/routers/api_ai.py tests/test_api_ai.py
git diff --cached --stat
git commit -F - <<'EOF'
AI card Test: find and save the thinking levels each model accepts

After the base test passes, Test runs ai_profile.detect_thinking (a
refused "max" or the OpenRouter public model list, then one timed probe
per level) and saves the result as the profile's "thinking". The probes
run apart from the base test: a probe that fails, times out or raises
never costs the model its base profile.

Co-Authored-By: <the model that wrote the commit> <noreply@anthropic.com>
EOF
```
Replace the placeholder with the model writing the commit. `git diff --cached --stat` must list exactly these 2
files, and `git diff HEAD~2 --stat` only the 4 files of this task.

#### NOTES

**Contract deviations:** none in names or signatures. Additions and clarifications, said loudly:
1. **`request_body(..., temperature=None)` sends no temperature**, at "off" and at a level. The contract types it as
   a float; Task 8's draft passes `None` for memory extraction when a thinking level is sent
   (`sent_temperature = temperature if thinking_temperature or target.thinking == "off" else None`). Without this,
   an OpenRouter model whose entry lists temperature would get `"temperature": null`.
2. **Extra public names:** `TEST_TEMPERATURE`, `THINKING_SECONDS`, `CANT_THINK`, `probe_body()`. The contract does
   not forbid them; Tasks 8-10 need none of them (Task 9's card may compare `profile.thinking.param === null` for
   "Can't think").
3. **`detect_thinking`'s full signature** is `(send, kind, profile, openrouter_entry=None, clock=time.monotonic)`;
   it returns only the `"thinking"` value, and the route merges it into the profile. `send(profile, level="off")` is
   the route's `send`; `detect()` still calls it with one argument.
4. **The probes carry no temperature**, as the brief and spec D2 say, even for an OpenRouter model whose entry lists
   `temperature` (then `thinking.temperature` is true and real requests at a level do send it). So the probe does not
   test that exact request. Risk is low: OpenRouter routes only to providers that accept the listed parameters.
5. **`adjust()` sets `thinking` itself** for a model that must always think (and drops its temperature), so the base
   test already runs at its lowest level with that level's allowance (`probe_body` gives 200 + 800 = 1000 tokens), and
   the saved profile keeps `always` even if `detect_thinking` later fails. `detect_thinking` keeps that base level in
   `levels` even if its own probe of it fails. Without this, a failed thinking step would leave such a model at
   `"off"` with no allowance, which is the F14 bug again.
6. **OpenRouter `supported_efforts: null`** means every effort is accepted (OpenRouter's reasoning docs, fetched
   2026-10-11), so all three levels are probed; a missing key or no `reasoning` object means "can't think", as spec D2
   says. No model in the 2026-10-11 list had null (198 lists, 129 without the key, 131 without `reasoning`).
7. **Only "Unrecognized request argument" or "Unknown parameter"** naming `reasoning_effort` on the "max" probe means
   "can't think". Any other answer without a list (an accepted "max", "Invalid value ...", a 429, no answer) falls
   back to one probe per level, as the brief says. A wrong guess here costs at most three unbilled 400s.
8. **`THINKING_SECONDS = 40`** (not in the spec): no new probe starts 40 s after the thinking step began, so a Test
   where every level times out (15 s each, `TEST_TIMEOUT`) adds at most about 55 s, not 60 s plus the list fetch.
9. **`api_ai.py` keeps naming `ai_profile.NOT_CHAT_MODEL`** in the route's docstring. Task 5 put it there (its only
   change to `api_ai.py`); this task's replacement docstring keeps it, because Task 10's name check expects
   `NOT_CHAT_MODEL` or `not_chat_model` in that file. The docstring also states a true rule (a failed base test starts
   no thinking probe; Task 5's API test shows only one request is sent).

**Assumptions:**
- Tasks run in order, and Task 5 (the not-chat code in `detect()`) ran before this task. This task does not change
  `detect()`, so Task 5's two lines stay where they are, before `adjust` and before any thinking probe.
- "OpenAI models set `thinking.temperature` false" is read as: connections of kind `openai` and
  `openai_compatible`. OpenRouter entries for OpenAI models (`openai/gpt-6-luna`, `openai/gpt-5.4-mini`,
  `openai/gpt-6.1-sol`) do not list `temperature`, so they get false from the list.
- An OpenRouter model's `always` comes from the entry's `mandatory`; an OpenAI model's from `adjust()`.
- The profile keeps `ms` only for levels whose probe answered; the card shows no time for a level without one.

**Risks:**
- **Every model on the card shows "Can't think" until someone presses Test again**, because profiles saved before
  this task have no `"thinking"` (by design: old profiles are sent as today). The live check (spec §5) must re-test
  gpt-5.4-mini, gpt-6-luna and one OpenRouter model.
- Test takes longer and costs a little: OpenAI up to 4 more requests (the "max" request is refused and not billed;
  three short billed probes with caps of 1000/1800/3200 tokens; measured answers used at most 237 completion tokens);
  OpenRouter one public list fetch (about 0.8 MB, `REMOTE_MODELS_TIMEOUT` 10 s) and three probes. Measured probe times
  were 1.0-3.6 s each (`inv-thinking.md` §4). The frontend's Test call has no timeout (`ai-connections-api.ts:87`).
- The "Supported values are: ..." text is measured, not documented. If OpenAI changes it, the fallback probes still
  find the levels (three requests instead of one).
- An OpenRouter model whose reasoning is mandatory still runs its base test with no reasoning setting at 200 tokens
  (today's behaviour after `adjust()` drops `{"enabled": false}`); if its default thinking fills the 200 tokens, the
  base test fails and no thinking probe runs. Not changed here.
- Any future test that presses Test on an OpenRouter model must stub `api_ai.requests.get`, or the route fetches the
  real list (an error there is caught and only logged, so the test would still pass, but it would use the network).
- Keys: the thinking step stores no error text; its only log line names the model ref and either a `RuntimeError`
  built from an HTTP status or an exception's class name. The public list is fetched without the key.

**Verified (read in this session):**
- `app/services/ai_profile.py` (CRLF, 122 lines): docstring 1-8, imports 10-13, `STARTING` 20-22, `MAX_TRIES` 23,
  `TEST_MAX_TOKENS` 25, `TEST_TIMEOUT` 26, `_SETTINGS` 27, `starting_profile` 30-31, `_named_setting` 34-39 (a word
  boundary keeps `reasoning` from matching inside `reasoning_effort`), `adjust` 42-62 (today: `none` refused → try
  `minimal`; anything else refused → `reasoning_off = None`, line 55-56), `request_body` 65-72, `headers` 75-79,
  `_error_message` 82-90, `_failed` 93-94, `detect` 97-122.
- `app/routers/api_ai.py` (CRLF): `log` 23, `REMOTE_MODELS_TIMEOUT = 10` 26, `remote_models` fetch 175-185 (sends the
  key when present), `test_model` 235-268 (`send` 247-261 with the literal 0.7 at 249; `detect` in a thread 264;
  `save_test` 265-267), `PUT /jobs` from 271.
- `app/services/ai_store.py:138-145` `save_test` writes `json.dumps(result["profile"])`; `ai_settings.py:187-189`
  `public()` passes `profile` to the card; `conversation.py:348` calls `request_body` positionally (unchanged at off);
  `conversation._provider()` 299-310 returns None when routing is unset.
- Tests: `tests/test_ai_profile.py` (CRLF, 144 lines; 170 after Task 5) with `_ok`, `_bad`, `_service` at 21-39 and
  `EFFORT_VALUE_REFUSED` used only at 65-67; `tests/test_api_ai.py` (LF, 380 lines; 403 after Task 5) with the `api`
  fixture 126-141 and the OpenRouter Test test 247-264.
- Live OpenAI refusal texts, exactly as used in the tests, from `scratchpad/probe_thinking_out.jsonl` (2026-10-11):
  gpt-5.4-mini and gpt-6-luna "Unsupported value: 'reasoning_effort' does not support 'max' with this model. Supported
  values are: 'none', 'low', 'medium', 'high', and 'xhigh'."; gpt-6-astra and gpt-6.1-sol "... does not support
  'none' ... Supported values are: 'low', 'medium', 'high', and 'xhigh'."; gpt-4.1-mini "Unrecognized request
  argument supplied: reasoning_effort"; both thinking models refuse temperature 0.7 at every level except none.
- OpenRouter entries in the tests are the real ones from the public list fetched 2026-10-11
  (`scratchpad/or_all_models_skeptic.json`, 458 models): `openai/gpt-6-luna` (efforts max..none, not mandatory),
  `openai/gpt-6.1-sol` (mandatory, no none), `stepfun/step-5-preview` (mandatory, lists temperature),
  `google/gemini-2.5-flash` (`{"mandatory": false}`, no efforts), `openai/gpt-4.1-mini` (`reasoning` null).
- OpenRouter reasoning docs (https://openrouter.ai/docs/guides/best-practices/reasoning-tokens.md, fetched
  2026-10-11): `reasoning.effort` takes max/xhigh/high/medium/low/minimal/none; `exclude: true` hides the reasoning
  but it is still billed and counts against `max_tokens` (so the allowance applies); a `mandatory` model must not be
  sent `effort: "none"`; `supported_efforts` null means all efforts, omitted means no effort selection.
- Prototype: the exact code and tests above were applied to a scratch copy of the branch (`app/`, `tests/`, `sql/`,
  `static/`, `models/`) after applying Task 5's Part D (its not-chat change). Part A: `35 failed, 28 passed` before, `63 passed` after
  (and `test_api_ai.py` 32 passed). Part B: `6 failed, 31 passed` before, `37 passed` after. Whole suite: 1603 before
  (1596 baseline + 7 from Task 5's Part D), 1643 passed after, plus only BUG-37. Line endings stayed CRLF/LF as above;
  no new line is over 120 characters.

---

### Task 8: Thinking: requests, token budgets, metrics and the jobs API

Spec parts: D1 (storage), D3 (requests, nearest lower level for main and backup, memory's temperature), D4 (token
caps), D5 (measuring), and the API part of D6 (`PUT /jobs` takes `thinking`, the card's settings show it). The Test
probes (D2) and the request helpers in `ai_profile.py` are Task 7's; the card's Thinking select is Task 9's.

**Depends on Task 7.** Task 7 adds to `app/services/ai_profile.py`: `THINKING_LEVELS`, `ALLOWANCE`,
`effective_level()`, `allowance()` and `request_body(..., *, level="off")` (with `temperature=None` sending no
temperature). This task uses them as they are and **does not edit `ai_profile.py`**. Step 0 checks they exist.

**Files** (line numbers are at commit `37ba84e`; earlier tasks move some of them, so every edit below is found by its
exact text, never by number):
- Modify `sql/init.sql` (CRLF): after the `ai_job` table, lines 533-538 (the end of the file).
- Modify `app/services/ai_settings.py` (CRLF): `Target` lines 46-50 (fields; its properties 52-70 stay);
  `Snapshot` lines 73-78; `job_targets()` lines 123-131; the `"jobs"` line of `public()`, line 192. Task 5 inserts
  `job_usable()` above `with_model()` and one line into `public()` after `"no_key_in_use"`; keep both.
- Modify `app/services/ai_store.py` (CRLF): the last 4 lines of `load()`, lines 39-42; `set_jobs()` lines 148-153
  (Task 5 inserts `fill_openrouter_keys_from_env()` above `change()`, so `set_jobs` is lower after it).
- Modify `app/routers/api_ai.py` (CRLF): one import after line 12; `JobChoice` lines 60-62; the `PUT /jobs` route
  lines 271-285 (Task 7 replaces `test_model()` just above it, so the route is lower after it).
- Modify `app/services/conversation.py` (CRLF): one constant after line 62 (`LATE_RISK_RATE_LIMIT_WAIT`); `_post()`
  lines 337-371; a new `_thinking_budget()` after `_call_error()` (lines 381-384); `_complete()` lines 397-443.
- Not modified: `app/services/memory.py`. Its extraction call stays `max_tokens=1000, temperature=0`
  (`memory.py:377-379`, pinned by `tests/test_after_chat.py:340`); the allowance and the "no temperature 0 while
  thinking" rule are applied per model in `conversation._post`, because the main and backup models can be sent
  different levels. `app/services/ai_profile.py` (Task 7's). No frontend file: the card still sends back each job
  entry it received (`AiConnectionsCard.tsx:346,355` spread `jobs[job]`; `saveJobs` in `ai-connections-api.ts:89`
  sends the whole object), so a saved level survives a Save until Task 9 adds the select.
- Test:
  - `tests/test_ai_profile.py` (CRLF): one import; a block appended at the end (18 tests).
  - `tests/test_ai_settings.py` (CRLF): line 95 changed; a block appended at the end (5 tests).
  - `tests/test_api_ai.py` (**LF**): line 210 changed (Part B); lines 43 and 92-94 changed (Part D); a block appended
    at the end (7 tests).
  - `tests/test_ai_store.py` (CRLF): one import; lines 80 and 87 changed; a block appended at the end (2 tests).
  - `tests/test_ai_calls.py` (CRLF): two import lines; a block appended at the end (11 tests).

**Interfaces:**
- Consumes (Task 7, exactly as the contract names them): `ai_profile.THINKING_LEVELS == ("low", "medium", "high")`;
  `ai_profile.ALLOWANCE == {"low": 800, "medium": 1600, "high": 3000}`;
  `ai_profile.effective_level(profile: dict | None, level: str) -> str` (nearest lower accepted level, else "off";
  a `thinking.always` profile never gets "off"); `ai_profile.allowance(profile: dict | None, level: str) -> int`;
  `ai_profile.request_body(profile, model_id, messages, max_tokens, temperature, extras=None, *, level="off")` (at
  "off" exactly today's body; `temperature=None` sends none; `max_tokens` sent as given); the saved profile's
  `"thinking": {"param": "reasoning_effort" | "openrouter_effort" | None, "levels": [...], "ms": {...},
  "always": bool, "temperature": bool}`.
  Existing code: `ai_settings.JOBS`, `Target.profile` (`ai_settings.py:68-70`), `ai_store.change(conn)`,
  `main.invalid_request` (`main.py:67-73`: any 422 under `/api/ai/` answers `{"detail": "invalid_request"}`).
- Produces (contract names):
  - `ai_settings.Snapshot.thinking: dict[str, str]` (job -> level; default `"off"` for every job).
  - `ai_settings.Target.thinking: str = "off"`: the level actually sent to that model; `job_targets()` sets it with
    `ai_profile.effective_level(target.profile, <job level>)`, so main and backup can differ.
  - `ai_settings.public(snapshot)["jobs"][job] == {"primary_ref": int, "fallback_ref": int | None, "thinking": str}`.
  - `ai_store.load(conn)` reads `ai_job.thinking`; `ai_store.set_jobs(conn, jobs, thinking: dict[str, str] | None =
    None)` saves each job's level (a job missing from `thinking` is saved as `"off"`).
  - `PUT /api/ai/jobs`: each job is `{"primary_ref": int, "fallback_ref": int | null, "thinking": "off" | "low" |
    "medium" | "high"}`; `thinking` is optional (default `"off"`); any other value is 422 `{"detail":
    "invalid_request"}`.
  - `conversation.THINKING_BUDGET = "thinking_budget"`. Every attempt in the metrics gains `"reasoning_tokens"` (int,
    or None when the service doesn't say) and `"level"` (the level sent); an attempt whose answer was cut off with
    every completion token spent on thinking also gets `"error": "thinking_budget"`, and that code becomes the model's
    `last_error` on the card (Task 9, Edits C8, I3 and I4, shows it as "Thinking used the whole budget, so no answer
    came back." / 「思考用完了全部的額度，所以沒有回答。」).
  - A call's cap is the job's base cap (reply 200, risk 32, summary 400, memory 1000, unchanged) plus
    `ai_profile.allowance(target.profile, target.thinking)`.
  - `sql/init.sql`: `ai_job.thinking VARCHAR(10) NOT NULL DEFAULT 'off'` and the constraint `ai_job_thinking_check`.

**Line endings.** Every file here is CRLF except `tests/test_api_ai.py` (LF). Make every change with the Edit tool
(it keeps a file's line endings); never `sed -i` (Git Bash's sed writes LF). To append a block at the end of a file,
read the file's last lines and use its final statement as the Edit anchor. After each part, `file <path>` must still
say "with CRLF line terminators" for the CRLF files and must not say it for `tests/test_api_ai.py`. If a CRLF file
lost it, restore it with:
`.venv/Scripts/python.exe -c "import pathlib,sys; p=pathlib.Path(sys.argv[1]); t=p.read_bytes().replace(b'\r\n', b'\n'); p.write_bytes(t.replace(b'\n', b'\r\n'))" <path>`

All commands run in Git Bash from `C:/Reachy_Mini/repo/MedAiCarePlus`. "The relevant suite" below means:

```bash
.venv/Scripts/python.exe -m pytest tests/test_ai_profile.py tests/test_ai_settings.py tests/test_ai_store.py tests/test_api_ai.py tests/test_ai_calls.py tests/test_conversation.py tests/test_conversation_metrics.py tests/test_after_chat.py tests/test_memory.py -q -p no:cacheprovider
```
Its expected result is always `1 failed`, the known `test_conversation.py::test_a_broken_background_never_costs_the_reply`
(BUG-37), and everything else passed.

- [ ] **Step 0: Check the branch, Task 7's helpers, and the baseline**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && git branch --show-current && git status --short
grep -n '^THINKING_LEVELS = \|^ALLOWANCE = \|^def effective_level(\|^def allowance(\|level: str = "off") -> dict' app/services/ai_profile.py
grep -n 'thinking_budget\|reasoning_tokens\|thinking: str\|ai_job_thinking_check\|saved_thinking' app/services/ai_settings.py app/services/ai_store.py app/services/conversation.py app/routers/api_ai.py sql/init.sql tests/test_api_ai.py
.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider 2>&1 | tail -2
```
Expected:
- `robot-camera-thinking`, and none of this task's files listed as changed.
- The first grep prints 5 lines: `THINKING_LEVELS = ("low", "medium", "high")`, `ALLOWANCE = {"low": 800, ...`,
  `def effective_level(...`, `def allowance(...` and the second line of `def request_body(` ending
  `extras: dict | None = None, *, level: str = "off") -> dict:`. **If any is missing, stop and report**: Task 7 has
  not run, and this task must not write its own copy of those helpers.
- The second grep prints nothing.
- The full suite: `1 failed, <BASE> passed` with the failure `test_a_broken_background_never_costs_the_reply`. Write
  down `<BASE>`; at the end of this task the count must be `<BASE> + 43`. After Tasks 3, 4, 5 and 7, `<BASE>` is 1680
  (1596 + 13 + 15 + 16 + 40), so the end count is 1723.

#### Part A: the request shapes this task relies on (tests only)

- [ ] **Step 1: Add the request-shape tests**

These guard what Task 8's calls depend on: level "off" sends today's body byte for byte (checked against a frozen
copy of the function as it was at `37ba84e`), each level for each kind of service, and no temperature when `None` is
given. They pass at once, because Task 7 wrote the helpers; they stay as the regression net for this task.

In `tests/test_ai_profile.py`, Edit the imports (`import copy` and `import pytest`, lines 6-8 at `37ba84e`):

Find:
```python
import copy

import pytest
```
Replace with:
```python
import copy
import json

import pytest
```

Then append at the end of the file (after its last test; two blank lines before the comment):

```python


# ── Thinking levels: the request shapes Task 8's calls rely on (spec 2026-10-11 D3) ──

THINKING_PART = {"param": "reasoning_effort", "levels": ["low", "medium", "high"],
                 "ms": {"low": 1900, "medium": 2700, "high": 4100}, "always": False, "temperature": False}
MINI_PROFILE = {"token_param": "max_completion_tokens", "temperature": True,
                "reasoning_off": {"reasoning_effort": "none"}, "thinking": THINKING_PART}
ROUTER_PROFILE = {"token_param": "max_tokens", "temperature": True, "reasoning_off": {"reasoning": {"enabled": False}},
                  "thinking": {**THINKING_PART, "param": "openrouter_effort", "temperature": True}}
HI_MESSAGES = [{"role": "user", "content": "hi"}]


def _todays_body(profile, model_id, messages, max_tokens, temperature, extras=None):
    """request_body as it was before thinking levels (commit 37ba84e): what level "off" must still send."""
    body = {"model": model_id, "messages": messages, profile["token_param"]: max_tokens}
    if profile["temperature"]:
        body["temperature"] = temperature
    body.update(copy.deepcopy(profile["reasoning_off"] or {}))
    body.update(extras or {})
    return body


@pytest.mark.parametrize("old", [
    ai_profile.starting_profile("openrouter"), ai_profile.starting_profile("openai"),
    {"token_param": "max_completion_tokens", "temperature": False, "reasoning_off": {"reasoning_effort": "none"}},
    {"token_param": "max_completion_tokens", "temperature": True, "reasoning_off": None},
])
@pytest.mark.parametrize("temperature", [0.7, 0])
def test_off_sends_todays_request_byte_for_byte(old, temperature):
    """Every job's default: not one byte changes, whether or not the profile has a thinking part."""
    extras = {"provider": {"only": ["x"]}}
    today = json.dumps(_todays_body(old, "m", HI_MESSAGES, 200, temperature, extras))
    for profile in (old, {**old, "thinking": THINKING_PART}):
        assert json.dumps(ai_profile.request_body(profile, "m", HI_MESSAGES, 200, temperature, extras)) == today
        assert json.dumps(ai_profile.request_body(profile, "m", HI_MESSAGES, 200, temperature, extras,
                                                  level="off")) == today


@pytest.mark.parametrize("level", ai_profile.THINKING_LEVELS)
def test_openai_thinking_sends_its_level_and_no_temperature(level):
    body = ai_profile.request_body(MINI_PROFILE, "gpt-5.4-mini", HI_MESSAGES, 1000, 0.7, level=level)
    assert body == {"model": "gpt-5.4-mini", "messages": HI_MESSAGES, "max_completion_tokens": 1000,
                    "reasoning_effort": level}


@pytest.mark.parametrize("level", ai_profile.THINKING_LEVELS)
def test_openrouter_thinking_sends_a_hidden_effort_and_a_temperature_only_when_listed(level):
    body = ai_profile.request_body(ROUTER_PROFILE, "deepseek/deepseek-r1", HI_MESSAGES, 1000, 0.7,
                                   {"provider": {"only": ["x"]}}, level=level)
    assert body == {"model": "deepseek/deepseek-r1", "messages": HI_MESSAGES, "max_tokens": 1000, "temperature": 0.7,
                    "reasoning": {"effort": level, "exclude": True}, "provider": {"only": ["x"]}}
    unlisted = {**ROUTER_PROFILE, "thinking": {**ROUTER_PROFILE["thinking"], "temperature": False}}
    body = ai_profile.request_body(unlisted, "deepseek/deepseek-r1", HI_MESSAGES, 1000, 0.7, level=level)
    assert body == {"model": "deepseek/deepseek-r1", "messages": HI_MESSAGES, "max_tokens": 1000,
                    "reasoning": {"effort": level, "exclude": True}}


@pytest.mark.parametrize("profile", [ai_profile.starting_profile("openai"), ai_profile.starting_profile("openrouter"),
                                     {**MINI_PROFILE, "thinking": {**THINKING_PART, "param": None, "levels": []}}])
def test_a_model_that_cannot_think_gets_the_off_request_at_any_level(profile):
    assert (ai_profile.request_body(profile, "m", HI_MESSAGES, 200, 0.7, level="high")
            == _todays_body(profile, "m", HI_MESSAGES, 200, 0.7))


def test_no_temperature_is_sent_when_none_is_given():
    body = ai_profile.request_body(ai_profile.starting_profile("openrouter"), "m", HI_MESSAGES, 1000, None)
    assert body == {"model": "m", "messages": HI_MESSAGES, "max_tokens": 1000, "reasoning": {"enabled": False}}
```

- [ ] **Step 2: Run them**

```bash
.venv/Scripts/python.exe -m pytest tests/test_ai_profile.py -q -p no:cacheprovider
```
Expected: `0 failed`; 18 more passed than before Step 1 (8 + 3 + 3 + 3 + 1). If any of the 18 fails, **stop and
report**: Task 7's `request_body`/`effective_level` differ from the contract, and this task must not patch
`ai_profile.py` to fit.

- [ ] **Step 3: Commit Part A**

```bash
git add tests/test_ai_profile.py
git commit -F - <<'EOF'
Thinking: tests for the request shapes at each level, and "off" byte for byte

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
```
(If another model writes the commit, put that model's name in the Co-Authored-By line.)

#### Part B: each job's level, and each model's level (ai_settings.py)

- [ ] **Step 4: Write the failing tests**

4a. In `tests/test_ai_settings.py`, line 95 (in `test_the_public_view_never_carries_a_key`):

Find:
```python
    assert view["jobs"]["safety"] == {"primary_ref": 21, "fallback_ref": None}
```
Replace with:
```python
    assert view["jobs"]["safety"] == {"primary_ref": 21, "fallback_ref": None, "thinking": "off"}
```

4b. In `tests/test_api_ai.py` (LF), line 210 (in `test_only_tested_models_can_be_given_a_job`):

Find:
```python
    assert response.status_code == 200 and response.json()["jobs"]["reply"] == {"primary_ref": 12, "fallback_ref": None}
```
Replace with:
```python
    assert response.status_code == 200
    assert response.json()["jobs"]["reply"] == {"primary_ref": 12, "fallback_ref": None, "thinking": "off"}
```

4c. Append at the end of `tests/test_ai_settings.py` (it already imports `dataclasses`, `ai_profile`, `ai_settings`
and has `_card_snapshot()`: model 11 `gpt-5.4-mini` on OpenAI connection 1, model 21 `google/gemini-2.5-flash` on
OpenRouter connection 2; jobs `reply (11, 21)`, `safety (21, None)`, `after_chat (21, None)`):

```python


# ── Thinking levels (spec 2026-10-11 D3) ──

THINKING_PART = {"param": "reasoning_effort", "levels": ["low", "medium", "high"],
                 "ms": {"low": 1900, "medium": 2700, "high": 4100}, "always": False, "temperature": False}


def _thinking_snapshot(thinking, backup_levels=("low",)):
    """_card_snapshot with the jobs' levels: the main model (11) thinks at every level, the backup (21) only at
    backup_levels."""
    snapshot = _card_snapshot()
    main = dataclasses.replace(snapshot.models[11],
                               profile={**snapshot.models[11].profile, "thinking": THINKING_PART})
    backup = dataclasses.replace(snapshot.models[21], profile={
        **snapshot.models[21].profile,
        "thinking": {**THINKING_PART, "param": "openrouter_effort", "levels": list(backup_levels)}})
    return dataclasses.replace(snapshot, models={11: main, 21: backup}, thinking=thinking)


def test_thinking_is_off_by_default_everywhere():
    assert ai_settings.env_snapshot().thinking == {"reply": "off", "safety": "off", "after_chat": "off"}
    assert _card_snapshot().thinking == {"reply": "off", "safety": "off", "after_chat": "off"}
    assert {target.thinking for job in ai_settings.JOBS for target in ai_settings.job_targets(job)} == {"off"}
    assert ai_settings.env_target("x/model").thinking == "off"


def test_main_and_backup_each_get_a_level_they_accept():   # spec review focus 5
    snapshot = _thinking_snapshot({"reply": "high", "safety": "medium", "after_chat": "off"})
    assert [target.thinking for target in ai_settings.job_targets("reply", snapshot)] == ["high", "low"]
    assert [target.thinking for target in ai_settings.job_targets("safety", snapshot)] == ["low"]
    assert [target.thinking for target in ai_settings.job_targets("after_chat", snapshot)] == ["off"]


def test_a_backup_that_cannot_think_gets_off():
    snapshot = _thinking_snapshot({"reply": "medium", "safety": "off", "after_chat": "off"}, backup_levels=())
    assert [target.thinking for target in ai_settings.job_targets("reply", snapshot)] == ["medium", "off"]


def test_a_model_that_always_thinks_gets_its_lowest_level_when_the_job_is_off():
    snapshot = _card_snapshot()
    astra = dataclasses.replace(snapshot.models[11], model_id="gpt-6-astra", profile={
        "token_param": "max_completion_tokens", "temperature": False, "reasoning_off": {"reasoning_effort": "low"},
        "thinking": {**THINKING_PART, "always": True}})
    snapshot = ai_settings.with_model(snapshot, astra)
    assert [target.thinking for target in ai_settings.job_targets("reply", snapshot)] == ["low", "off"]


def test_the_public_view_shows_each_jobs_thinking_level():
    view = ai_settings.public(_thinking_snapshot({"reply": "high", "safety": "off", "after_chat": "low"}))
    assert view["jobs"] == {"reply": {"primary_ref": 11, "fallback_ref": 21, "thinking": "high"},
                            "safety": {"primary_ref": 21, "fallback_ref": None, "thinking": "off"},
                            "after_chat": {"primary_ref": 21, "fallback_ref": None, "thinking": "low"}}
    assert view["models"][0]["profile"]["thinking"]["levels"] == ["low", "medium", "high"]
```

- [ ] **Step 5: Run them and see them fail**

```bash
.venv/Scripts/python.exe -m pytest tests/test_ai_settings.py tests/test_api_ai.py -q -p no:cacheprovider
```
Expected: `7 failed`:
- `test_ai_settings.py::test_the_public_view_never_carries_a_key`: `AssertionError` (the right side has
  `{'thinking': 'off'}`);
- `test_thinking_is_off_by_default_everywhere`: `AttributeError: 'Snapshot' object has no attribute 'thinking'`;
- `test_main_and_backup_each_get_a_level_they_accept`, `test_a_backup_that_cannot_think_gets_off`,
  `test_the_public_view_shows_each_jobs_thinking_level`: `TypeError: Snapshot.__init__() got an unexpected keyword
  argument 'thinking'`;
- `test_a_model_that_always_thinks_gets_its_lowest_level_when_the_job_is_off`: `AttributeError: 'Target' object has
  no attribute 'thinking'`;
- `test_api_ai.py::test_only_tested_models_can_be_given_a_job`: `AssertionError` (the job entry has no `thinking`).

- [ ] **Step 6: Implement in `app/services/ai_settings.py`**

6a. `Target` (lines 46-50). Find:
```python
class Target:
    """One model to call, with the connection it goes through."""
    connection: Connection
    model: Model
```
Replace with:
```python
class Target:
    """One model to call, with the connection it goes through and the thinking level to send it: the job's level,
    or the nearest lower one this model accepts (ai_profile.effective_level); "off" outside a job."""
    connection: Connection
    model: Model
    thinking: str = "off"
```
(The `@dataclass(frozen=True)` line above it and the properties below it stay as they are.)

6b. `Snapshot` (lines 73-78). Find:
```python
@dataclass(frozen=True)
class Snapshot:
    connections: dict[int, Connection]
    models: dict[int, Model]
    jobs: dict[str, tuple[int, int | None]]     # job -> (main model_ref, backup model_ref or None)
    seeded_at: datetime | None = None
```
Replace with:
```python
def _thinking_off() -> dict[str, str]:
    return {job: "off" for job in JOBS}


@dataclass(frozen=True)
class Snapshot:
    connections: dict[int, Connection]
    models: dict[int, Model]
    jobs: dict[str, tuple[int, int | None]]     # job -> (main model_ref, backup model_ref or None)
    seeded_at: datetime | None = None
    thinking: dict[str, str] = dataclasses.field(default_factory=_thinking_off)   # job -> level chosen on the card
```
(`dataclasses` is already imported at line 8; the two blank lines before `_thinking_off` come from the blank lines
already above `@dataclass(frozen=True)`.)

6c. Replace the whole function `job_targets()` (lines 123-131) with:
```python
def job_targets(job: str, snapshot: Snapshot | None = None) -> list[Target]:
    """The job's main model, then its backup (none when unset); the same model twice is tried twice. Each gets the
    job's thinking level, or the nearest lower level that model accepts, so main and backup can differ (spec D3)."""
    snapshot = snapshot or current()
    level = snapshot.thinking.get(job, "off")
    targets = []
    for ref in snapshot.jobs.get(job, ()):
        model = snapshot.models.get(ref) if ref is not None else None
        if model is not None:
            target = Target(snapshot.connections[model.connection_id], model)
            targets.append(dataclasses.replace(target, thinking=ai_profile.effective_level(target.profile, level)))
    return targets
```

6d. In `public()`, change only its `"jobs"` line (line 192). Do not replace the whole function: Task 5 added a
`"jobs_usable"` line to it. Find:
```python
        "jobs": {job: {"primary_ref": refs[0], "fallback_ref": refs[1]} for job, refs in snapshot.jobs.items()},
```
Replace with:
```python
        "jobs": {job: {"primary_ref": refs[0], "fallback_ref": refs[1], "thinking": snapshot.thinking.get(job, "off")}
                 for job, refs in snapshot.jobs.items()},
```

- [ ] **Step 7: Run the tests**

```bash
.venv/Scripts/python.exe -m pytest tests/test_ai_settings.py tests/test_api_ai.py -q -p no:cacheprovider
```
Expected: `0 failed`. Then the relevant suite (command above): only the BUG-37 failure.
`file app/services/ai_settings.py tests/test_ai_settings.py tests/test_api_ai.py`: the first two CRLF, the third not.

- [ ] **Step 8: Commit Part B**

```bash
git add app/services/ai_settings.py tests/test_ai_settings.py tests/test_api_ai.py
git commit -F - <<'EOF'
Thinking: each job's level, and each model gets it or the nearest lower level it accepts

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
```

#### Part C: storage (sql/init.sql, ai_store.py)

- [ ] **Step 9: Write the failing tests**

9a. In `tests/test_ai_store.py`, the imports (lines 4-7). Find:
```python
import json
import sys
```
Replace with:
```python
import json
import os
import sys
```

9b. In `test_load_reads_keys_and_profiles`, line 80. Find:
```python
        "ai_job": [{"job": "reply", "primary_ref": 11, "fallback_ref": None}]}
```
Replace with:
```python
        "ai_job": [{"job": "reply", "primary_ref": 11, "fallback_ref": None, "thinking": "medium"}]}
```

9c. In the same test, line 87. Find:
```python
    assert snapshot.jobs == {"reply": (11, None)}
```
Replace with:
```python
    assert snapshot.jobs == {"reply": (11, None)}
    # each job's thinking level; a job without a row is "off"
    assert snapshot.thinking == {"reply": "medium", "safety": "off", "after_chat": "off"}
```

9d. Append at the end of `tests/test_ai_store.py` (it reuses the file's `Conn` fake: `statements(prefix)` gives the
arguments of every statement starting with `prefix`, and `sql` holds `(query, args)` tuples):

```python


def test_set_jobs_saves_each_jobs_thinking_level():
    conn = Conn()
    asyncio.run(ai_store.set_jobs(conn, {"reply": (11, 12), "safety": (11, None)}, {"reply": "medium"}))
    # a job left out of the levels is set to off
    assert conn.statements("INSERT INTO ai_job") == [("reply", 11, 12, "medium"), ("safety", 11, None, "off")]
    assert all("thinking = EXCLUDED.thinking" in query for query, _ in conn.sql)


def test_the_schema_adds_the_thinking_level_with_its_check():
    with open(os.path.join(os.path.dirname(__file__), "..", "sql", "init.sql"), encoding="utf-8") as handle:
        sql = handle.read()
    assert "ALTER TABLE ai_job ADD COLUMN IF NOT EXISTS thinking VARCHAR(10) NOT NULL DEFAULT 'off';" in sql
    assert "ALTER TABLE ai_job DROP CONSTRAINT IF EXISTS ai_job_thinking_check;" in sql
    assert ("ALTER TABLE ai_job ADD CONSTRAINT ai_job_thinking_check CHECK (thinking IN ('off','low','medium','high'));"
            in sql)
```

- [ ] **Step 10: Run them and see them fail**

```bash
.venv/Scripts/python.exe -m pytest tests/test_ai_store.py -q -p no:cacheprovider
```
Expected: `3 failed`:
- `test_load_reads_keys_and_profiles`: `AssertionError` (`{'reply': 'off'} != {'reply': 'medium'}`);
- `test_set_jobs_saves_each_jobs_thinking_level`: `TypeError: set_jobs() takes 2 positional arguments but 3 were
  given`;
- `test_the_schema_adds_the_thinking_level_with_its_check`: `AssertionError` (the ALTER line is not in the file).

- [ ] **Step 11: Implement**

11a. `sql/init.sql`: the `ai_job` table (lines 533-538, the end of the file). Find:
```sql
CREATE TABLE IF NOT EXISTS ai_job (
    job            VARCHAR(20) PRIMARY KEY CHECK (job IN ('reply','safety','after_chat')),
    primary_ref    BIGINT NOT NULL REFERENCES ai_model(model_ref),
    fallback_ref   BIGINT REFERENCES ai_model(model_ref),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```
Replace with (the same table, then the migration in the file's idempotent style, as for `min_interval_minutes` and
its `medication_min_interval_minutes_check` at lines 266-270):
```sql
CREATE TABLE IF NOT EXISTS ai_job (
    job            VARCHAR(20) PRIMARY KEY CHECK (job IN ('reply','safety','after_chat')),
    primary_ref    BIGINT NOT NULL REFERENCES ai_model(model_ref),
    fallback_ref   BIGINT REFERENCES ai_model(model_ref),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Thinking level per job (spec 2026-10-11-robot-camera-identity-thinking-design.md D1). 'off' sends today's
-- requests unchanged; a model that can't do the job's level gets the nearest lower level it accepts.
ALTER TABLE ai_job ADD COLUMN IF NOT EXISTS thinking VARCHAR(10) NOT NULL DEFAULT 'off';
ALTER TABLE ai_job DROP CONSTRAINT IF EXISTS ai_job_thinking_check;
ALTER TABLE ai_job ADD CONSTRAINT ai_job_thinking_check CHECK (thinking IN ('off','low','medium','high'));
```
The file must still end with a newline (CRLF), as it does now.

11b. `app/services/ai_store.py`, the end of `load()` (lines 39-42). Find:
```python
    jobs = {row["job"]: (row["primary_ref"], row["fallback_ref"])
            for row in await conn.fetch("SELECT job, primary_ref, fallback_ref FROM ai_job")}
    seeded_at = await conn.fetchval("SELECT updated_at FROM ops_state WHERE key = 'ai_seeded_from_env'")
    return ai_settings.Snapshot(connections, models, jobs, seeded_at)
```
Replace with:
```python
    jobs, thinking = {}, {job: "off" for job in ai_settings.JOBS}
    for row in await conn.fetch("SELECT job, primary_ref, fallback_ref, thinking FROM ai_job"):
        jobs[row["job"]] = (row["primary_ref"], row["fallback_ref"])
        thinking[row["job"]] = row["thinking"]
    seeded_at = await conn.fetchval("SELECT updated_at FROM ops_state WHERE key = 'ai_seeded_from_env'")
    return ai_settings.Snapshot(connections, models, jobs, seeded_at, thinking)
```
The whole `load()` then reads:
```python
async def load(conn) -> ai_settings.Snapshot:
    connections = {}
    for row in await conn.fetch("SELECT connection_id, name, kind, base_url, api_key_enc, api_key_last4, "
                                "service_name FROM ai_connection ORDER BY connection_id"):
        key = ai_crypto.decrypt(row["api_key_enc"]) if row["api_key_enc"] else ""
        connections[row["connection_id"]] = ai_settings.Connection(
            row["connection_id"], row["name"], row["kind"], row["base_url"], key or "", row["api_key_last4"],
            key is not None, row["service_name"])
    models = {row["model_ref"]: ai_settings.Model(
                  row["model_ref"], row["connection_id"], row["model_id"], row["company"], row["region"],
                  row["retention"], _json(row["profile"]), row["tested_at"], row["test_ms"], row["last_error"])
              for row in await conn.fetch("SELECT model_ref, connection_id, model_id, company, region, retention, "
                                          "profile, tested_at, test_ms, last_error FROM ai_model ORDER BY model_ref")}
    jobs, thinking = {}, {job: "off" for job in ai_settings.JOBS}
    for row in await conn.fetch("SELECT job, primary_ref, fallback_ref, thinking FROM ai_job"):
        jobs[row["job"]] = (row["primary_ref"], row["fallback_ref"])
        thinking[row["job"]] = row["thinking"]
    seeded_at = await conn.fetchval("SELECT updated_at FROM ops_state WHERE key = 'ai_seeded_from_env'")
    return ai_settings.Snapshot(connections, models, jobs, seeded_at, thinking)
```

11c. Replace the whole function `set_jobs()` (lines 148-153 at `37ba84e`) with:
```python
async def set_jobs(conn, jobs: dict[str, tuple[int, int | None]], thinking: dict[str, str] | None = None) -> None:
    """Each job's main and backup model and its thinking level; a job missing from `thinking` is set to "off"."""
    thinking = thinking or {}
    for job, (primary_ref, fallback_ref) in jobs.items():
        await conn.execute(
            "INSERT INTO ai_job (job, primary_ref, fallback_ref, thinking) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (job) DO UPDATE SET primary_ref = EXCLUDED.primary_ref, "
            "fallback_ref = EXCLUDED.fallback_ref, thinking = EXCLUDED.thinking, updated_at = NOW()",
            job, primary_ref, fallback_ref, thinking.get(job, "off"))
```
`seed_from_env()` is unchanged: its `INSERT INTO ai_job (job, primary_ref, fallback_ref)` gets the column default
`'off'`.

- [ ] **Step 12: Run the tests**

```bash
.venv/Scripts/python.exe -m pytest tests/test_ai_store.py -q -p no:cacheprovider
```
Expected: `0 failed`. Then the relevant suite: only the BUG-37 failure. `file sql/init.sql app/services/ai_store.py
tests/test_ai_store.py`: all three CRLF.

- [ ] **Step 13 (needs Docker Desktop running; skip and say so if it is not): the SQL on a real PostgreSQL**

The tests use fakes, so check the upgrade of an existing installation, the CHECK and `set_jobs` on PostgreSQL 16, in
a throwaway container on port 55432. Never use the running stack's `medcareai2-postgres-1`.

Create `C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/task08_pg_check.py`
(outside the repo; never commit it) with exactly:

```python
"""Task 8: the thinking column on a real PostgreSQL 16, on a throwaway container (never the running stack's database).

Run from the repo root: python task08_pg_check.py <the schema before Task 8 (git show 37ba84e:sql/init.sql)>
"""
import asyncio
import os
import sys

os.environ["DATABASE_URL"] = "postgresql://postgres:test@localhost:55432/postgres"
sys.path.insert(0, os.getcwd())

import asyncpg  # noqa: E402

from app import config, database  # noqa: E402
from app.services import ai_store  # noqa: E402

ALL_OFF = {"reply": "off", "safety": "off", "after_chat": "off"}


async def main():
    for _ in range(30):   # the container takes a few seconds to accept connections
        try:
            conn = await asyncpg.connect(os.environ["DATABASE_URL"])
            break
        except (OSError, asyncpg.PostgresError):
            await asyncio.sleep(1)
    else:
        raise SystemExit("PostgreSQL on port 55432 did not answer")
    with open(sys.argv[1], encoding="utf-8") as handle:
        await conn.execute(handle.read())   # an installation from before this task
    await conn.close()
    config.OPENROUTER_API_KEY = ""          # nothing real goes into the throwaway database
    await database.init_pool()              # applies today's sql/init.sql: the upgrade
    async with database.get_pool().acquire() as conn:
        with open("sql/init.sql", encoding="utf-8") as handle:
            await conn.execute(handle.read())   # and once more: it must stay idempotent
        assert await ai_store.seed_from_env(conn) is True
        snapshot = await ai_store.load(conn)
        assert snapshot.thinking == ALL_OFF, snapshot.thinking
        await ai_store.set_jobs(conn, dict(snapshot.jobs), {"reply": "high", "safety": "low"})
        assert (await ai_store.load(conn)).thinking == {"reply": "high", "safety": "low", "after_chat": "off"}
        await ai_store.set_jobs(conn, dict(snapshot.jobs))
        assert (await ai_store.load(conn)).thinking == ALL_OFF
        try:
            await conn.execute("UPDATE ai_job SET thinking = 'max' WHERE job = 'reply'")
        except asyncpg.CheckViolationError:
            print("the CHECK refuses 'max'")
        else:
            raise AssertionError("the CHECK let 'max' in")
    await database.close_pool()
    print("PG CHECK OK")


asyncio.run(main())
```

Run it:
```bash
SCRATCH=/c/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad
git show 37ba84e:sql/init.sql > "$SCRATCH/init_before_task8.sql"
docker run --rm -d --name t8-pg-check -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16-alpine
.venv/Scripts/python.exe "$SCRATCH/task08_pg_check.py" "$SCRATCH/init_before_task8.sql"
docker stop t8-pg-check
```
Expected: `the CHECK refuses 'max'` then `PG CHECK OK`; after `docker stop`, `docker ps -a` lists no `t8-pg-check`
(`--rm` removed it). Port 55432 must be free first (`docker ps --format '{{.Ports}}' | grep 55432` prints nothing).

- [ ] **Step 14: Commit Part C**

```bash
git add sql/init.sql app/services/ai_store.py tests/test_ai_store.py
git commit -F - <<'EOF'
Thinking: store each job's level (ai_job.thinking, default off, with a CHECK)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
```

#### Part D: the jobs API (api_ai.py)

- [ ] **Step 15: Write the failing tests**

15a. `tests/test_api_ai.py` (LF), `FakeStore.__init__`, line 43. Find:
```python
        self.calls, self.next_id, self.saved_tests = [], 100, []
```
Replace with:
```python
        self.calls, self.next_id, self.saved_tests, self.saved_thinking = [], 100, [], []
```

15b. `FakeStore.set_jobs`, lines 92-94. Find:
```python
    async def set_jobs(self, conn, jobs):
        self.calls.append(("set_jobs", jobs))
        self._put(jobs=dict(jobs))
```
Replace with:
```python
    async def set_jobs(self, conn, jobs, thinking=None):
        self.calls.append(("set_jobs", jobs))
        self.saved_thinking.append(thinking)
        self._put(jobs=dict(jobs), thinking=dict(thinking or {}))
```
(The recorded `("set_jobs", jobs)` call is unchanged, so the existing assertion at line 211 still holds.)

15c. Append at the end of `tests/test_api_ai.py` (the `api` fixture and `_snapshot()`, with models 11 and 12 tested
and 13 untested, are at lines 26-35 and 126-141):

```python


def test_each_job_takes_a_thinking_level(api):
    jobs = {"reply": {"primary_ref": 11, "fallback_ref": 12, "thinking": "medium"}, "safety": {"primary_ref": 11},
            "after_chat": {"primary_ref": 11, "thinking": "low"}}
    response = api.client.put("/api/ai/jobs", json=jobs)
    assert response.status_code == 200
    assert api.store.saved_thinking[-1] == {"reply": "medium", "safety": "off", "after_chat": "low"}   # off by default
    assert {job: entry["thinking"] for job, entry in response.json()["jobs"].items()} == {
        "reply": "medium", "safety": "off", "after_chat": "low"}


@pytest.mark.parametrize("thinking", ["max", "OFF", "", None, 2, "minimal"])
def test_a_thinking_level_other_than_off_low_medium_high_is_refused(api, thinking):
    jobs = {"reply": {"primary_ref": 11, "thinking": thinking}, "safety": {"primary_ref": 11},
            "after_chat": {"primary_ref": 11}}
    response = api.client.put("/api/ai/jobs", json=jobs)
    assert (response.status_code, response.json()) == (422, {"detail": "invalid_request"})
    assert api.store.calls == []
```

- [ ] **Step 16: Run them and see them fail**

```bash
.venv/Scripts/python.exe -m pytest tests/test_api_ai.py -q -p no:cacheprovider
```
Expected: `7 failed`:
- `test_each_job_takes_a_thinking_level`: `AssertionError: assert None == {'reply': 'medium', 'safety': 'off',
  'after_chat': 'low'}` (the route doesn't pass levels yet; pydantic drops the unknown field);
- the 6 cases of `test_a_thinking_level_other_than_off_low_medium_high_is_refused`: `AssertionError` with
  `At index 0 diff: 200 != 422`.

- [ ] **Step 17: Implement in `app/routers/api_ai.py`**

17a. Imports (line 12). Find:
```python
from collections import Counter

import requests
```
Replace with:
```python
from collections import Counter
from typing import Literal

import requests
```
(If Task 7 already added `from typing import ...` here, add `Literal` to that line instead of a second import.)

17b. Replace the whole class `JobChoice` (lines 60-62) with:
```python
class JobChoice(BaseModel):
    primary_ref: int
    fallback_ref: int | None = None
    thinking: Literal["off", "low", "medium", "high"] = "off"   # each model gets it or its nearest lower level
```

17c. Replace the whole route `set_jobs()` (from `@router.put("/jobs")`, lines 271-285 at `37ba84e`, to the end of
the file) with:
```python
@router.put("/jobs")
async def set_jobs(body: Jobs, user: dict = Depends(get_consented_user)):
    snapshot = ai_settings.current()
    chosen = {job: (getattr(body, job).primary_ref, getattr(body, job).fallback_ref) for job in ai_settings.JOBS}
    thinking = {job: getattr(body, job).thinking for job in ai_settings.JOBS}
    for ref in (ref for refs in chosen.values() for ref in refs if ref is not None):
        model = snapshot.models.get(ref)
        if model is None:
            raise HTTPException(404, "not_found")
        if model.profile is None:
            raise HTTPException(400, "untested_model")
    _guard(dataclasses.replace(snapshot, jobs=chosen, thinking=thinking))
    async with get_pool().acquire() as conn:
        async with ai_store.change(conn):
            await ai_store.set_jobs(conn, chosen, thinking)
    return _card()
```
No level is refused because the main model can't do it: each model gets the nearest lower level it accepts
(`job_targets`), and the card offers only the main model's levels.

- [ ] **Step 18: Run the tests**

```bash
.venv/Scripts/python.exe -m pytest tests/test_api_ai.py -q -p no:cacheprovider
```
Expected: `0 failed`. Then the relevant suite: only the BUG-37 failure. `file app/routers/api_ai.py
tests/test_api_ai.py`: the first CRLF, the second not.

- [ ] **Step 19: Commit Part D**

```bash
git add app/routers/api_ai.py tests/test_api_ai.py
git commit -F - <<'EOF'
Thinking: PUT /api/ai/jobs takes each job's level (off, low, medium, high)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
```

#### Part E: the calls: levels, caps, metrics, thinking_budget (conversation.py)

- [ ] **Step 20: Write the failing tests**

20a. `tests/test_ai_calls.py`, the imports (lines 4-12). Find:
```python
import json

import pytest
```
Replace with:
```python
import json
from datetime import date

import pytest
```
Then find:
```python
from app.services import ai_profile, ai_settings, ai_store, conversation, legal_service
```
Replace with:
```python
from app.services import ai_profile, ai_settings, ai_store, conversation, legal_service, memory
```

20b. Append at the end of `tests/test_ai_calls.py`. It reuses the file's `Response` fake (status, content,
`usage.completion_tokens` 5, finish `stop`), the `service` fixture (fake `_http_session`: `sent` keeps every request's
url, headers and json; `answers[model id]` is the Response for that model), the `notes` fixture (records
`ai_store.note_call(model_ref, error)`), `_snapshot()` (model 11 on OpenAI connection 1 and model 21 on OpenRouter
connection 2, both with keys, main and backup of every job) and `_reply_after_notes()`:

```python


# ── Thinking levels (spec 2026-10-11 D3-D5) ──

THINKING_PART = {"param": "reasoning_effort", "levels": ["low", "medium", "high"],
                 "ms": {"low": 1900, "medium": 2700, "high": 4100}, "always": False, "temperature": False}
MINI_PROFILE = {"token_param": "max_completion_tokens", "temperature": True,
                "reasoning_off": {"reasoning_effort": "none"}, "thinking": THINKING_PART}
R1_PROFILE = {"token_param": "max_tokens", "temperature": True, "reasoning_off": {"reasoning": {"enabled": False}},
              "thinking": {**THINKING_PART, "param": "openrouter_effort", "levels": ["low"], "temperature": True}}
WALK = [{"role": "patient", "text": "我去散步了"}]


class Thought(Response):
    """An answer whose usage says how many of its completion tokens went on thinking."""

    def __init__(self, tokens=300, reasoning=256, finish_reason="stop", content="您好！"):
        super().__init__(content=content)
        self.tokens, self.reasoning, self.finish_reason = tokens, reasoning, finish_reason

    def json(self):
        return {"model": "served",
                "usage": {"completion_tokens": self.tokens,
                          "completion_tokens_details": {"reasoning_tokens": self.reasoning}},
                "choices": [{"finish_reason": self.finish_reason, "message": {"content": self.content}}]}


def _thinking_snapshot(levels, main=MINI_PROFILE, backup=R1_PROFILE):
    """_snapshot at the given job levels: gpt-5.4-mini on OpenAI (main, every level by default), then
    deepseek/deepseek-r1 on OpenRouter (backup, low only by default), for every job."""
    snapshot = _snapshot()
    models = {11: dataclasses.replace(snapshot.models[11], model_id="gpt-5.4-mini", profile=main),
              21: dataclasses.replace(snapshot.models[21], model_id="deepseek/deepseek-r1", profile=backup)}
    return dataclasses.replace(snapshot, models=models, thinking=levels)


def _levels(**levels):
    """Every job off, except the ones named."""
    return {**{job: "off" for job in ai_settings.JOBS}, **levels}


def _body(request):
    """A request's body without its messages, in the order it was sent."""
    return {key: value for key, value in request["json"].items() if key != "messages"}


@pytest.fixture
def no_routing(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_PROVIDER_ONLY", "")
    monkeypatch.setattr(config, "OPENROUTER_DATA_COLLECTION", "")


def test_thinking_off_sends_todays_request_and_records_the_level(service, notes, monkeypatch):
    sent, _ = service
    monkeypatch.setattr(ai_settings, "_snapshot", _thinking_snapshot(_levels()))
    text, metrics = _reply_after_notes()
    assert text == "您好！"
    assert json.dumps(_body(sent[0])) == json.dumps(
        {"model": "gpt-5.4-mini", "max_completion_tokens": 200, "temperature": 0.7, "reasoning_effort": "none"})
    (attempt,) = metrics["attempts"]
    assert attempt["level"] == "off" and attempt["reasoning_tokens"] is None and "error" not in attempt


def test_main_and_backup_each_get_a_level_they_accept_with_its_allowance(service, notes, no_routing, monkeypatch):
    sent, answers = service
    monkeypatch.setattr(ai_settings, "_snapshot", _thinking_snapshot(_levels(reply="high")))
    answers["gpt-5.4-mini"] = Response(status=503)
    text, metrics = _reply_after_notes()
    assert text == "您好！"
    assert _body(sent[0]) == {"model": "gpt-5.4-mini", "max_completion_tokens": 200 + 3000, "reasoning_effort": "high"}
    assert _body(sent[1]) == {"model": "deepseek/deepseek-r1", "max_tokens": 200 + 800, "temperature": 0.7,
                              "reasoning": {"effort": "low", "exclude": True}}
    assert [(a["model_requested"], a["level"]) for a in metrics["attempts"]] == [
        ("gpt-5.4-mini", "high"), ("deepseek/deepseek-r1", "low")]


@pytest.mark.parametrize("job, call, base", [
    ("reply", lambda: conversation.reply_with_metrics(WALK, "zh-TW"), conversation.REPLY_MAX_TOKENS),
    ("safety", lambda: conversation.classify_risk(WALK, "zh-TW"), conversation.RISK_MAX_TOKENS),
    ("after_chat", lambda: conversation.summarize(WALK, "zh-TW"), conversation.SUMMARY_MAX_TOKENS),
    ("after_chat", lambda: memory.after_chat_call(WALK, "zh-TW", [], date(2026, 10, 11)), 1000),
])
def test_every_calls_cap_is_its_base_plus_the_allowance_of_the_level_sent(service, notes, monkeypatch, job, call,
                                                                          base):
    sent, _ = service
    monkeypatch.setattr(ai_settings, "_snapshot", _thinking_snapshot(_levels(**{job: "medium"})))
    asyncio.run(call())
    assert sent[0]["json"]["max_completion_tokens"] == base + 1600
    assert sent[0]["json"]["reasoning_effort"] == "medium"


def test_memory_extraction_leaves_out_temperature_0_only_while_thinking(service, notes, no_routing, monkeypatch):
    """Spec D3: extraction's temperature 0 is dropped when thinking is on; a backup that doesn't think keeps it."""
    sent, answers = service
    thinker = {**R1_PROFILE, "thinking": {**R1_PROFILE["thinking"], "levels": ["low", "medium", "high"]}}
    snapshot = _thinking_snapshot(_levels(after_chat="low"), backup=ai_profile.starting_profile("openrouter"))
    snapshot = ai_settings.with_model(snapshot, dataclasses.replace(
        snapshot.models[11], connection_id=2, model_id="deepseek/deepseek-v4", profile=thinker))
    monkeypatch.setattr(ai_settings, "_snapshot", snapshot)
    answers["deepseek/deepseek-v4"] = Response(status=503)
    asyncio.run(memory.after_chat_call(WALK, "zh-TW", [], date(2026, 10, 11)))
    assert _body(sent[0]) == {"model": "deepseek/deepseek-v4", "max_tokens": 1000 + 800,
                              "reasoning": {"effort": "low", "exclude": True}}
    assert _body(sent[1]) == {"model": "deepseek/deepseek-r1", "max_tokens": 1000, "temperature": 0,
                              "reasoning": {"enabled": False}}
    sent.clear()
    asyncio.run(conversation.summarize(WALK, "zh-TW"))
    assert sent[0]["json"]["temperature"] == 0.7   # only extraction's 0 is left out


def test_a_model_that_always_thinks_gets_its_lowest_level_and_allowance_when_off(service, notes, monkeypatch):
    sent, _ = service
    astra = {"token_param": "max_completion_tokens", "temperature": False,
             "reasoning_off": {"reasoning_effort": "low"}, "thinking": {**THINKING_PART, "always": True}}
    monkeypatch.setattr(ai_settings, "_snapshot", _thinking_snapshot(_levels(), main=astra))
    _, metrics = _reply_after_notes()
    assert _body(sent[0]) == {"model": "gpt-5.4-mini", "max_completion_tokens": 200 + 800, "reasoning_effort": "low"}
    assert metrics["attempts"][0]["level"] == "low"


def test_reasoning_tokens_and_the_level_are_recorded_for_each_attempt(service, notes, monkeypatch):
    sent, answers = service
    monkeypatch.setattr(ai_settings, "_snapshot", _thinking_snapshot(_levels(reply="low")))
    answers["gpt-5.4-mini"] = Thought(tokens=300, reasoning=256)
    text, metrics = _reply_after_notes()
    (attempt,) = metrics["attempts"]
    assert text == "您好！" and attempt["reasoning_tokens"] == 256 and attempt["tokens"] == 300
    assert attempt["level"] == "low" and attempt["usable"] is True and "error" not in attempt


def test_thinking_that_uses_the_whole_cap_is_named_thinking_budget(service, notes, monkeypatch, caplog):
    sent, answers = service
    monkeypatch.setattr(ai_settings, "_snapshot", _thinking_snapshot(_levels(reply="low")))
    answers["gpt-5.4-mini"] = Thought(tokens=1000, reasoning=1000, finish_reason="length", content="")
    with caplog.at_level("WARNING"):
        text, metrics = _reply_after_notes()
    first, second = metrics["attempts"]
    assert first["error"] == conversation.THINKING_BUDGET == "thinking_budget" and first["usable"] is False
    assert first["finish_reason"] == "length" and first["reasoning_tokens"] == first["tokens"] == 1000
    assert "error" not in second and text == "您好！"   # the backup answered
    assert notes == [(11, "thinking_budget")]   # the card's last_error for the main model
    assert "thinking_budget" in caplog.text


def test_an_answer_cut_off_after_some_text_is_not_a_thinking_budget(service, notes, monkeypatch):
    sent, answers = service
    monkeypatch.setattr(ai_settings, "_snapshot", _thinking_snapshot(_levels(reply="low")))
    answers["gpt-5.4-mini"] = Thought(tokens=1000, reasoning=900, finish_reason="length", content="您好，今天")
    _, metrics = _reply_after_notes()
    assert "error" not in metrics["attempts"][0] and notes[0] == (11, "No usable answer")
```

- [ ] **Step 21: Run them and see them fail**

```bash
.venv/Scripts/python.exe -m pytest tests/test_ai_calls.py -q -p no:cacheprovider
```
Expected: `10 failed` (all new tests but the last). Reasons: the caps `assert 200 == (200 + 1600)`, `assert 32 ==
(32 + 1600)`, `assert 400 == (400 + 1600)`, `assert 1000 == (1000 + 1600)`; `KeyError: 'reasoning_tokens'` or
`KeyError: 'level'`; dict mismatches (`_post` still sends the "off" body: `reasoning_effort: 'none'`, a temperature,
the base cap). `test_an_answer_cut_off_after_some_text_is_not_a_thinking_budget` already passes: it guards today's
"No usable answer" for an answer cut off after some text.

- [ ] **Step 22: Implement in `app/services/conversation.py`**

22a. After line 62. Find:
```python
LATE_RISK_RATE_LIMIT_WAIT = 20    # after a 429 the re-check waits first: asking again at once meets the same limit
```
Replace with:
```python
LATE_RISK_RATE_LIMIT_WAIT = 20    # after a 429 the re-check waits first: asking again at once meets the same limit
# An attempt's error, and the model's last_error on the card, when thinking used the whole cap (spec D5).
THINKING_BUDGET = "thinking_budget"
```

22b. Replace the whole function `_post()` (lines 337-371) with:
```python
def _post(messages: list[dict], max_tokens: int, model: str | None = None, timeout: float = CALL_TIMEOUT,
          info: dict | None = None, temperature: float = 0.7,
          target: ai_settings.Target | None = None) -> str | None:
    """One model call through target's connection, shaped by its model's profile (ai_profile); without a target,
    OpenRouter with the .env key as before the AI connections card. `info`, when given, receives status,
    model_served, finish_reason, tokens, and reasoning_tokens when the service reports them. temperature: 0 for
    extraction (memory facts), 0.7 for everything else (left out for a model that accepts only its default).

    The target's thinking level (Target.thinking) goes into the request, and the cap grows by that level's
    allowance (max_tokens is the job's base cap). With thinking on, extraction's temperature 0 is left out."""
    info = {} if info is None else info
    target = target or ai_settings.env_target(model or config.LLM_FALLBACK_MODEL)
    provider = _provider() if target.kind == "openrouter" else None
    if target.thinking != "off" and temperature == 0:
        temperature = None
    # Level "off": the profile turns reasoning off, as free reasoning models otherwise spend every token thinking.
    payload = ai_profile.request_body(target.profile, target.model_id, messages,
                                      max_tokens + ai_profile.allowance(target.profile, target.thinking),
                                      temperature, {"provider": provider} if provider else None,
                                      level=target.thinking)
    response = _http_session().post(target.url, timeout=timeout,
                                    headers=ai_profile.headers(target.kind, target.api_key), json=payload)
    info["status"] = response.status_code
    if response.status_code in (429, 500, 502, 503):
        raise RuntimeError(f"HTTP {response.status_code}")
    if 400 <= response.status_code < 500:
        # The service's own words for the card's "last call failed" (e.g. a setting the model no longer accepts),
        # not requests' "400 Client Error ... for url"; without the key, which a service may echo.
        try:
            message = ai_profile._error_message(response.json())
        except ValueError:
            message = response.text
        raise RuntimeError(ai_crypto.redact(f"HTTP {response.status_code}: {message}", target.api_key)[:300])
    response.raise_for_status()
    body = response.json()
    choices = body.get("choices") or []
    usage = body.get("usage") or {}
    info.update(model_served=body.get("model"), tokens=usage.get("completion_tokens"),
                finish_reason=choices[0].get("finish_reason") if choices else None)
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
        info["reasoning_tokens"] = details["reasoning_tokens"]   # only when the service says (spec D5)
    if not choices or choices[0].get("finish_reason") == "length":
        return None   # cut off mid-sentence (often a reasoning model that ran out of tokens): never speak half a reply
    content = (choices[0].get("message") or {}).get("content")
    return content.strip() if isinstance(content, str) and content.strip() else None
```
(`info["reasoning_tokens"]` is set only when reported, so `tests/test_conversation_metrics.py:144`, which compares
`info` with an exact dict, stays as it is. Task 7's `request_body` applies `effective_level` again; that is harmless,
because `Target.thinking` is already a level the model accepts.)

22c. After `_call_error()` (lines 381-384), add a new function. Find:
```python
def _call_error(status, problem, api_key: str) -> str:
    if problem is not None:
        return ai_crypto.redact(str(problem), api_key)[:300]
    return "No answer in time" if status == "timeout" else "No usable answer"
```
Replace with:
```python
def _call_error(status, problem, api_key: str) -> str:
    if problem is not None:
        return ai_crypto.redact(str(problem), api_key)[:300]
    return "No answer in time" if status == "timeout" else "No usable answer"


def _thinking_budget(info: dict) -> bool:
    """Cut off by the length limit with every completion token spent on thinking: an empty answer (spec D5)."""
    reasoning = info.get("reasoning_tokens")
    return info.get("finish_reason") == "length" and bool(reasoning) and reasoning == info.get("tokens")
```

22d. Replace the whole function `_complete()` (lines 397-443) with:
```python
async def _complete(messages: list[dict], max_tokens: int, deadline_seconds: float, call_timeout: float,
                    accept, temperature: float = 0.7, *, job: str) -> tuple[object | None, dict]:
    """The job's main model, then its backup (ai_settings), never past `deadline_seconds` in total.

    accept(answer) turns an answer into the result, or returns None when it is unusable (the next model is tried).
    Returns (result or None, {"llm_ms", "attempts"}). Each attempt records connection, model_requested,
    model_served, ms, status (the HTTP status, or "timeout" / "error" when none came back), finish_reason, tokens,
    reasoning_tokens (None when the service doesn't say), level (the thinking level sent) and usable, plus
    error "thinking_budget" when thinking used the whole cap. A model whose connection has no readable key is
    skipped. max_tokens is the job's base cap: _post adds the allowance for each model's level.
    """
    started = time.monotonic()
    deadline = started + deadline_seconds
    attempts: list[dict] = []
    result = None
    loop = asyncio.get_running_loop()
    for target in [target for target in ai_settings.job_targets(job) if target.api_key]:
        left = deadline - time.monotonic()
        if left < MIN_CALL_SECONDS:
            break
        timeout = min(call_timeout, left)
        info: dict = {}
        problem = None
        call_started = time.monotonic()
        try:
            # requests' timeout bounds each socket wait, not the whole call; wait_for holds the deadline.
            answer = await asyncio.wait_for(
                loop.run_in_executor(None, _post, messages, max_tokens, target.model_id, timeout, info,
                                     temperature, target),
                timeout)
            result = accept(answer)
            status = info.get("status")
        except (asyncio.TimeoutError, requests.Timeout):
            status = "timeout"
        except Exception as exc:
            status, problem = info.get("status") or (429 if "429" in str(exc) else "error"), exc
        error = None if result is not None else _call_error(status, problem, target.api_key)
        if result is None and status == 200 and _thinking_budget(info):
            error = THINKING_BUDGET
        if result is None:
            log.warning("%s %s gave no usable answer (status %s, finish %s, level %s): %s", target.connection.name,
                        target.model_id, status, info.get("finish_reason"), target.thinking,
                        error if problem is not None or error == THINKING_BUDGET else "unusable text")
        attempt = {"connection": target.connection.name, "model_requested": target.model_id,
                   "model_served": info.get("model_served"), "ms": _ms_since(call_started),
                   "status": status, "finish_reason": info.get("finish_reason"), "tokens": info.get("tokens"),
                   "reasoning_tokens": info.get("reasoning_tokens"), "level": target.thinking,
                   "usable": result is not None}
        if error == THINKING_BUDGET:
            attempt["error"] = THINKING_BUDGET
        attempts.append(attempt)
        _note_call(target, error)
        if result is not None:
            break
    return result, {"llm_ms": _ms_since(started), "attempts": attempts}
```
(`status == 200`, not "no exception": after a timeout the executor thread can still fill `info` later, and a timeout
must stay "No answer in time".)

- [ ] **Step 23: Run the tests**

```bash
.venv/Scripts/python.exe -m pytest tests/test_ai_calls.py -q -p no:cacheprovider
```
Expected: `0 failed`. Then the relevant suite: only the BUG-37 failure. `file app/services/conversation.py
tests/test_ai_calls.py`: both CRLF.

- [ ] **Step 24: The whole backend suite, repeated names, line endings**

```bash
.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider 2>&1 | tail -3
.venv/Scripts/python.exe - <<'EOF'
import ast
import collections

for name in ("tests/test_ai_profile.py", "tests/test_ai_settings.py", "tests/test_ai_store.py", "tests/test_api_ai.py",
             "tests/test_ai_calls.py"):
    tree = ast.parse(open(name, encoding="utf-8").read())
    names = [node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    names += [target.id for node in tree.body if isinstance(node, ast.Assign)
              for target in node.targets if isinstance(target, ast.Name)]
    print(name, "repeated names:", [key for key, count in collections.Counter(names).items() if count > 1] or "none")
EOF
file sql/init.sql app/services/ai_settings.py app/services/ai_store.py app/services/conversation.py app/routers/api_ai.py tests/test_ai_profile.py tests/test_ai_settings.py tests/test_ai_store.py tests/test_api_ai.py tests/test_ai_calls.py
git diff --stat 37ba84e -- sql/init.sql app/services/ai_settings.py app/services/ai_store.py app/services/conversation.py
```
Expected:
- `1 failed, <BASE + 43> passed`; the failure is `test_a_broken_background_never_costs_the_reply` (BUG-37). If a
  timing test (BUG-39) also fails, run it alone; it must pass alone.
- "repeated names: none" for all five files (a second test with the same name would silently replace the first; if
  one shows, rename this task's copy and rerun).
- `file`: "with CRLF line terminators" for every file except `tests/test_api_ai.py`.
- `git diff --stat`: tens of changed lines per file, never a whole-file rewrite (hundreds of lines would mean the
  line endings changed).

- [ ] **Step 25: Commit Part E**

```bash
git add app/services/conversation.py tests/test_ai_calls.py
git commit -F - <<'EOF'
Thinking: send each model its level with the token allowance, record reasoning tokens, name thinking_budget

Calls send Target.thinking through ai_profile.request_body, with the job's base cap plus that level's allowance.
Memory extraction's temperature 0 is left out while a model thinks. Each attempt records reasoning_tokens and the
level; an empty answer cut off with every token spent thinking is "thinking_budget", also the model's last_error.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
git log --oneline -5
```

#### NOTES

**Contract deviations:** none in names or signatures. Additions and choices, said loudly:
1. **`ai_profile.py` is not touched.** The contract lists `THINKING_LEVELS`, `ALLOWANCE`, `effective_level`,
   `allowance` and `request_body(level=)` under `ai_profile`, and Task 7's plan writes them. This task only uses
   them; Step 0 stops if they are missing, so there is never a second copy.
2. **The allowance is added in `conversation._post`**, next to the level it belongs to (`max_tokens +
   ai_profile.allowance(target.profile, target.thinking)` and `level=target.thinking` in one call). `_complete` and
   every caller still pass the base cap, so `tests/test_conversation.py`'s fake `_post` (lines 115-130) and
   `tests/test_after_chat.py:340` keep seeing 200 / 1000.
3. **`memory.py` is not changed.** "Memory drops temperature=0 when thinking" is done per model in `_post`
   (`if target.thinking != "off" and temperature == 0: temperature = None`), because the main and backup of
   `after_chat` can be sent different levels: a backup that doesn't think keeps temperature 0. This relies on Task
   7's `request_body` sending no temperature for `None` (Task 7's NOTES item 1 says it does).
4. **New name `conversation.THINKING_BUDGET = "thinking_budget"`.** The attempt's `"error"` key exists only for that
   case (other failures keep today's attempt shape plus `reasoning_tokens` and `level`). The model's `last_error`
   stores the code, as C3 does for `not_chat_model`; Task 9 shows it as "thinking used the whole budget" in en and
   zh-TW (its Edits C8, I3, I4 and a Playwright test).
5. **`info["reasoning_tokens"]` only when the service reports it**; the attempt's `"reasoning_tokens"` is always
   present (None when not reported). This keeps `tests/test_conversation_metrics.py:144` (an exact `info` dict)
   unchanged.
6. **`ai_store.set_jobs(conn, jobs, thinking=None)`: a job missing from `thinking` is saved as "off"** (no "keep
   what was there"). The only caller, `PUT /jobs`, always passes all three jobs' levels.
7. **`PUT /jobs` without `thinking` sets that job to "off"** (the Literal's default). The current card sends back the
   job entries it received, so it keeps a saved level; an older client that builds the body from scratch would reset
   it.
8. **The server does not refuse a level the main model can't do**; each model gets the nearest lower accepted level.
   Spec D6 puts "only offered levels" on the card.

**Assumptions:**
- Tasks run in order, and Tasks 5 and 7 ran before this one (Step 0 checks Task 7). Task 5's `public()` line
  (`"jobs_usable"`) and `job_usable()` are kept, because this task edits only the `"jobs"` line of `public()`.
- Task 7's `effective_level()` returns "off" for a profile without `thinking.param` or with no levels, and the lowest
  level for `thinking.always`; its `request_body()` applies `effective_level()` itself. Both were checked: this task's
  code and all 43 new tests were run on top of Task 7's exact `effective_level`/`allowance`/`request_body` code,
  copied from its plan (`task-07.md`, step 3e).

**Risks:**
- **The safety job at a thinking level** keeps its 2.5 s per-call timeout and 4 s deadline (spec D4: timeouts don't
  change). A thinking risk check can time out and become "unknown" (re-checked later in the background); the card's
  time warning (frontend task) is the guard.
- **Always-thinking models (GPT-6 Astra, 6.1 Sol) now get +800 tokens at "off"** and `reasoning_effort: "low"`. That
  is the spec's F14 fix, and it changes their bytes at "off" by design; every other model's "off" request is
  byte-identical (Part A test, and `test_thinking_off_sends_todays_request_and_records_the_level`).
- **An OpenRouter model whose reasoning is mandatory but lists no efforts** gets "off" (Task 7: no levels), so no
  reasoning setting and no allowance; its default thinking can still fill the base cap (then it is a
  `thinking_budget` attempt, and the backup is tried). Task 7's NOTES name the same risk.
- **Metrics rows grow by two keys per attempt** (`conversation_turn.metrics`); `api_conversations.py:115-123` reads
  only `model_served`/`model_requested` and `ms`, so the summary and CSV are unchanged.

**Verified while writing this task (read or run in this session, at `37ba84e`):**
- Spec D1, D3-D6 (`docs/superpowers/specs/2026-10-11-robot-camera-identity-thinking-design.md:155-215`) and review
  focus 4-5 (lines 280-283).
- `app/services/ai_profile.py:65-72` `request_body` (today's "off" body, the key order the Part A oracle copies).
- `app/services/ai_settings.py`: `JOBS` 15, `Target` 46-70 (`profile` property 68-70), `Snapshot` 73-78,
  `env_snapshot` 108-115, `env_target` 118-120, `job_targets` 123-131, `public` 180-195 (`"jobs"` line 192).
- `app/services/ai_store.py`: `load` 26-42, `seed_from_env` 45-71 (inserts `ai_job` without `thinking`), `change`
  74-88, `set_jobs` 148-153.
- `app/services/conversation.py`: `LATE_RISK_RATE_LIMIT_WAIT` 62, base caps 51-56, `_post` 337-371 (the only
  `request_body` caller besides `api_ai.py:248`), `_call_error` 381-384, `_note_call` 387-394 (notes only on change),
  `_complete` 397-443 (calls `_post` positionally at 422-423).
- `app/services/memory.py:377-379` (`max_tokens=1000, temperature=0`, job `after_chat` via `complete_with_reason`).
- `app/routers/api_ai.py`: imports 7-21, `JobChoice` 60-62, `Jobs` 65-68, `PUT /jobs` 271-285; `app/main.py:67-73`
  (422 `invalid_request` for `/api/ai/`).
- `sql/init.sql`: `ai_job` 533-538 (end of file); idempotent CHECK style at 268-273 (`DROP CONSTRAINT IF EXISTS`
  then `ADD CONSTRAINT`).
- Tests: `tests/test_ai_settings.py:95`, `tests/test_api_ai.py:43,92-94,210` (LF file), `tests/test_ai_store.py:80,
  87`, `tests/test_ai_calls.py` fakes 17-79, `tests/test_conversation_metrics.py:139-144`,
  `tests/test_after_chat.py:340`.
- Frontend round-trip of `thinking`: `frontend_source/src/components/AiConnectionsCard.tsx:346,355`,
  `src/lib/ai-connections-api.ts:89`.
- Run, on a scratch copy of `37ba84e` (`git archive`, never the repo):
  - all 43 new tests fail on today's code and pass with this task's code;
  - the failure reasons of Steps 5, 10, 16 and 21 come from applying the parts one at a time;
  - the whole backend suite with this task's code: the BUG-37 failure only (plus a `.env.example` test that fails
    only because the scratch copy has no `.env.example`), and the same with Task 7's helper code swapped in;
  - Step 13's script on PostgreSQL 16 (`postgres:16-alpine`, throwaway container): the old schema upgraded, the file
    applied twice, `set_jobs`/`load` round-trip, the CHECK refusing `'max'`.
- The repo's baseline today: `1 failed, 1596 passed` (`.venv/Scripts/python.exe -m pytest tests -q -p
  no:cacheprovider`). With only this task on `37ba84e` it would be `1 failed, 1639 passed`.

---

### Task 9: Thinking on the card: a level per job, and what each model can do

Spec part D6 (the card), plus the last line of D2 ("the card shows the extra time on Test as a short 'testing thinking
levels' status") and the card side of D5 (a model whose `last_error` is Task 8's code `thinking_budget` says "thinking
used the whole budget"). Frontend only: no server file changes. The tests mock the whole API, so no server needs to
run.

This task runs **after Task 6** (AI card warnings per job and the voice-only Test message), which changes the same four
files, and after the backend thinking tasks (the model profile's `thinking` from Test; `ai_job.thinking`, the jobs'
`thinking` in `GET /api/ai/settings` and in `PUT /api/ai/jobs`). Every edit below is anchored on text Task 6 does not
change, except where a step says so. The whole task was rehearsed, step by step, on three scratch copies of
`frontend_source`: commit `37ba84e` alone, `37ba84e` plus Task 6's first draft, and `37ba84e` plus Task 6's latest
draft. Every check below passed on all three.

**Files:**
- Create (LF line endings, like any new file):
  - `frontend_source/src/lib/ai-thinking.ts`: the thinking rules the card needs (no browser code, so Node can test it).
  - `frontend_source/tests/ai-thinking.unit.ts`: its unit tests (Node's test runner, like `tests/doses.unit.ts`).
- Modify (line numbers at `37ba84e`; "after Task 6" is where Task 6's latest draft moves them; always find the place by
  the step's `old_string`):
  - `frontend_source/src/lib/ai-connections-api.ts` (CRLF): line 1 (import), line 10 (`AiProfile`), line 16
    (`AiJobChoice`), line 31 (`isNumber`; after Task 6: 33), line 38 (the models check in `isAiSettings`; after Task 6:
    42).
  - `frontend_source/src/components/AiConnectionsCard.tsx` (LF): lines 8-9 (imports); Task 6's `MODEL_ERRORS` line
    (after `KNOWN_ERRORS`, about line 15); 131-132 (start of `status`; after Task 6: 137-138); 142-143 (end of
    `status`; after Task 6: 148-149); 252 (model row status line; after Task 6: 258); 340 (job row `<div>`; Task 6
    already changes it); 346 (main model `onChange`; after Task 6: 353); 357-360 (end of the backup `<label>`; after
    Task 6: 364-367).
  - `frontend_source/src/i18n.ts` (CRLF): line 59 (en `aiConnections.onlyTested`; after Task 6: 59) and line 739
    (zh-TW `aiConnections.onlyTested`; after Tasks 3 and 6: 744); Task 6's two `modelErrors: { not_chat_model: ... }`
    lines (en and zh-TW).
  - `frontend_source/tests/ai-connections.spec.ts` (CRLF): lines 15-16 (`startingSettings` jobs), line 135 (Save jobs
    expectation; after Task 6: 158), lines 197-198 (end of the test "a job choice not yet saved survives adding another
    model"; after Task 6: 220-221); after Task 6 only, line 34 (the reply job of Task 6's keyless-OpenRouter fixture).
- Test: `tests/ai-thinking.unit.ts` (7 tests, new); `tests/ai-connections.spec.ts` (11 new Playwright tests; 2 fixture
  lines and 1 expectation updated).
- Scratch, never committed: `C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/t9_shots.mjs`
  and its screenshots.

**Interfaces:**
- Consumes:
  - From the backend thinking tasks (contract): every card answer (`GET /api/ai/settings` and each change) has
    `jobs[job] = {"primary_ref": int, "fallback_ref": int | null, "thinking": "off" | "low" | "medium" | "high"}`;
    `PUT /api/ai/jobs` takes `thinking` per job (`Literal["off","low","medium","high"] = "off"`, else 422
    `invalid_request`); a tested model's `profile` may hold `"thinking": {"param": "reasoning_effort" |
    "openrouter_effort" | null, "levels": [...], "ms": {"low": int, ...}, "always": bool, "temperature": bool}`
    (`ai_profile.detect_thinking`; a model that can't think has `param: null, levels: []`); a profile saved before
    thinking levels has no `thinking` key. The level the server sends a model is `ai_profile.effective_level(profile,
    level)`: the nearest lower level in `levels` (only when `param` is set), else `"off"`, except that an `always`
    model gets its lowest level instead of `"off"`.
  - From Task 6: `AiSettings.jobs_usable` and its check in `isAiSettings`; each job row is
    `<div key={job} role="group" aria-label={t(`aiConnections.jobNames.${job}`)} ...>` (Step 12 adds the `role` and
    `aria-label` if Task 6 did not). In the spec, `mockApi` reads `mock.settings` at every request (true at `37ba84e`
    and in both Task 6 drafts).
  - Existing: `mergeJobs(current, server, testedRefs)` keeps a whole unsaved job choice, so it keeps `thinking` too
    (no change needed); `saveJobs(jobs)` sends the jobs as they are, so `thinking` goes to `PUT /api/ai/jobs`.
- Produces:
  - `src/lib/ai-connections-api.ts`: `type AiThinkingLevel = 'off' | 'low' | 'medium' | 'high'`;
    `interface AiThinking { param: string | null; levels: string[]; ms: Record<string, number>; always: boolean;
    temperature: boolean }`; `AiProfile.thinking?: AiThinking`; `AiJobChoice.thinking: AiThinkingLevel`; the
    settings check refuses a job without a valid `thinking` and a malformed profile `thinking`.
  - `src/lib/ai-thinking.ts`: `THINKING_LEVELS`, `THINKING_CHOICES`, `JOB_CALL_LIMIT_MS` (`{reply: 6000, safety:
    2500, after_chat: 12000}`), `isThinkingChoice(value)`, `acceptedLevels(model)`, `nearestLevel(model, level)`,
    `sentLevel(model, level)`, `thinkingMs(model, level)`, `tooSlow(job, model, level)`, `offeredLevels(main, level)`.
  - i18n keys `aiConnections.thinking`, `levelNames.{off,low,medium,high}`, `levelShort.{low,medium,high}`,
    `levelSeparator`, `canThink`, `cantThink`, `testingThinking`, `thinkingSent`, `thinkingTime`, `thinkingTooSlow`,
    and `modelErrors.thinking_budget` (next to Task 6's `modelErrors.not_chat_model`), in en and zh-TW. Task 10's check
    looks for "Can think", "Can't think", "可思考" and "不可思考" in `i18n.ts`, and for "thinking" in
    `ai-connections-api.ts`.
  - `MODEL_ERRORS` (Task 6's list in `AiConnectionsCard.tsx`) gains `'thinking_budget'`, so a model whose last call
    ended with Task 8's code shows the card's words, never the code.

**What the card does (for the reviewer):**
- Each job row gets a "Thinking" list under its main model: Off always; Low, Medium and High when the main model
  accepts them; and the job's saved level even when the main model doesn't accept it (so the list never shows a level
  other than the one that will be saved).
- Under the backup list: "The main model uses Low for this job." when the level the server really sends the main model
  differs from the job's level (a model that can't do the level, or one that always thinks, at Off), and "Main model
  took 7.2 s in the test." (its Test time at the level it really gets; nothing at Off for a normal model).
- A warning (the spec's exact words) when that time is over the job's per-call limit: reply 6 s
  (`conversation.CALL_TIMEOUT`), safety 2.5 s (`RISK_CALL_TIMEOUT`), after-chat 12 s (`SUMMARY_CALL_TIMEOUT`).
- Picking another main model keeps the job's level if the new model accepts it, else the nearest lower level it
  accepts, else Off.
- Each tested model row says "Can think: low · medium · high" or "Can't think" (zh-TW 「可思考：低・中・高」 /
  「不可思考」). Untested rows say nothing about thinking.
- While Test runs, the model row says "Testing, including thinking levels…" (D2: Test now also probes each level).
- Saving goes through the existing Save jobs button; a level picked but not saved survives other changes on the card
  (as the main and backup choices already do).

All commands run in Git Bash. `S` below is the scratchpad:

```bash
S="C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad"
```

Use the Edit tool for every change to an existing file (it keeps CRLF files CRLF). Each `old_string` below is exact
and occurs once. The Edit tool also matches inside a line: if a multi-line `old_string` that starts with spaces is
found only partway into a line (the file indents that part further than shown), do not apply it as shown; indent every
line of the `new_string` like the matching lines of the file.

- [ ] **Step 0: Check the branch and what Task 6 left**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && git branch --show-current && git status --short && git log --oneline -12
grep -c 'role="group" aria-label={t(`aiConnections.jobNames' frontend_source/src/components/AiConnectionsCard.tsx
grep -c "settings.jobs.reply = { primary_ref: 21, fallback_ref: null };" frontend_source/tests/ai-connections.spec.ts
grep -n "{t('aiConnections.noBackup')}</option>" frontend_source/src/components/AiConnectionsCard.tsx
```

Expected: branch `robot-camera-thinking`, a clean tree, Task 6's commit and the backend thinking commits in the log.
- The first `grep -c` prints `1` when Task 6 already gave each job row its group role (then skip Edit C7 in Step 12),
  `0` otherwise (then do Edit C7).
- The second prints `1` when Task 6's keyless-OpenRouter fixture is there (then do Edit S3 in Step 7), `0` otherwise
  (skip Edit S3).
- The last `grep -n` shows the backup list's "No backup" option: 16 spaces before `<option` means Edit C6 in Step 12
  (the layout at `37ba84e`), 18 spaces means Edit C6b (Task 6's latest draft wraps the lists in one more `<div>`).
- With Task 6 done as planned (`task-06.md`, Step 5c), expect: the first `grep -c` prints `1` (skip C7), the second
  prints `1` (do S3), and the option has 18 spaces (do C6b). Also check Task 6's lists are there for Edits C8, I3 and
  I4: `grep -c "const MODEL_ERRORS = \['not_chat_model'\];" frontend_source/src/components/AiConnectionsCard.tsx`
  prints `1`, and `grep -c "modelErrors: { not_chat_model:" frontend_source/src/i18n.ts` prints `2`. If either
  prints `0`, Task 6 has not run: stop and report.

#### Part A: the thinking rules and the types

- [ ] **Step 1: Write the failing unit tests**

Create `frontend_source/tests/ai-thinking.unit.ts` with exactly:

```ts
// Which thinking levels a job offers and which level each model is really sent (lib/ai-thinking.ts). No browser needed:
//   node --experimental-strip-types --test tests/ai-thinking.unit.ts
// The rule must match the server's ai_profile.effective_level: the nearest lower level the model accepts, else off,
// and never off for a model that always thinks (GPT-6 Astra and 6.1 Sol refuse reasoning_effort "none").
import assert from 'node:assert/strict';
import { test } from 'node:test';
import type { AiModel } from '../src/lib/ai-connections-api.ts';
import {
  JOB_CALL_LIMIT_MS, acceptedLevels, isThinkingChoice, nearestLevel, offeredLevels, sentLevel, thinkingMs, tooSlow,
} from '../src/lib/ai-thinking.ts';

const tested = (thinking?: unknown): AiModel => ({
  model_ref: 1, connection_id: 1, model_id: 'gpt-6-luna', company: 'OpenAI', region: 'USA', retention: '30 days',
  profile: { token_param: 'max_completion_tokens', temperature: true, reasoning_off: { reasoning_effort: 'none' },
    ...(thinking === undefined ? {} : { thinking }) } as AiModel['profile'],
  tested: true, copied_from_env: false, tested_at: '2026-10-11T09:00:00+00:00', test_ms: 900, last_error: null,
});
const untested: AiModel = { ...tested(), profile: null, tested: false, tested_at: null, test_ms: null };
const thinks = (levels: string[], ms: Record<string, number> = {}, always = false) =>
  tested({ param: 'reasoning_effort', levels, ms, always, temperature: false });
const cantThink = tested({ param: null, levels: [], ms: {}, always: false, temperature: false });

test('a model can think only when Test saved a setting to send and at least one level', () => {
  assert.deepEqual(acceptedLevels(undefined), []);
  assert.deepEqual(acceptedLevels(untested), []);
  assert.deepEqual(acceptedLevels(tested()), []);   // a profile saved before thinking levels existed
  assert.deepEqual(acceptedLevels(cantThink), []);
  assert.deepEqual(acceptedLevels(tested({ param: null, levels: ['low'], ms: {}, always: false, temperature: false })), []);
  // lowest first, and only the card's levels (xhigh is never offered)
  assert.deepEqual(acceptedLevels(thinks(['xhigh', 'high', 'low'])), ['low', 'high']);
});

test('a job level the model refuses becomes the nearest lower level it accepts, else off', () => {
  const lowAndHigh = thinks(['low', 'high']);
  assert.equal(sentLevel(lowAndHigh, 'off'), 'off');
  assert.equal(sentLevel(lowAndHigh, 'low'), 'low');
  assert.equal(sentLevel(lowAndHigh, 'medium'), 'low');
  assert.equal(sentLevel(lowAndHigh, 'high'), 'high');
  assert.equal(sentLevel(thinks(['medium']), 'low'), 'off');
  assert.equal(sentLevel(cantThink, 'high'), 'off');
  assert.equal(sentLevel(untested, 'medium'), 'off');
});

test('a model that always thinks is sent its lowest level for off and for anything below it', () => {
  const astra = thinks(['medium', 'high'], { medium: 5200 }, true);
  assert.equal(sentLevel(astra, 'off'), 'medium');
  assert.equal(sentLevel(astra, 'low'), 'medium');
  assert.equal(sentLevel(astra, 'high'), 'high');
  // the job keeps off when it moves to such a model: off is always offered
  assert.equal(nearestLevel(astra, 'off'), 'off');
  assert.equal(nearestLevel(astra, 'low'), 'off');
  assert.equal(nearestLevel(astra, 'high'), 'high');
});

test('the time shown is the time at the level really sent', () => {
  const luna = thinks(['low', 'medium', 'high'], { low: 1500, medium: 2300 });
  assert.equal(thinkingMs(luna, 'off'), null);
  assert.equal(thinkingMs(luna, 'medium'), 2300);
  assert.equal(thinkingMs(luna, 'high'), null);   // no time saved for high
  assert.equal(thinkingMs(thinks(['low'], { low: 1900 }), 'high'), 1900);
  assert.equal(thinkingMs(thinks(['low', 'high'], { low: 2700 }, true), 'off'), 2700);
});

test("a time over the job's per-call limit is too slow; one exactly at it is not", () => {
  assert.deepEqual(JOB_CALL_LIMIT_MS, { reply: 6000, safety: 2500, after_chat: 12000 });
  const at = (ms: number) => thinks(['low'], { low: ms });
  assert.equal(tooSlow('reply', at(6000), 'low'), false);
  assert.equal(tooSlow('reply', at(6001), 'low'), true);
  assert.equal(tooSlow('safety', at(2700), 'low'), true);
  assert.equal(tooSlow('after_chat', at(9300), 'low'), false);
  assert.equal(tooSlow('safety', at(9000), 'off'), false);   // off is not timed for a model that can stop thinking
});

test("a job offers off, its main model's levels, and its own saved level", () => {
  assert.deepEqual(offeredLevels(thinks(['low', 'medium', 'high']), 'off'), ['off', 'low', 'medium', 'high']);
  assert.deepEqual(offeredLevels(cantThink, 'off'), ['off']);
  assert.deepEqual(offeredLevels(thinks(['low']), 'high'), ['off', 'low', 'high']);
  assert.deepEqual(offeredLevels(undefined, 'medium'), ['off', 'medium']);
});

test('only off, low, medium and high are job levels', () => {
  for (const level of ['off', 'low', 'medium', 'high']) assert.equal(isThinkingChoice(level), true);
  for (const level of ['max', 'xhigh', '', null, undefined, 1]) assert.equal(isThinkingChoice(level), false);
});
```

- [ ] **Step 2: Run them and see them fail**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus/frontend_source && node --experimental-strip-types tests/ai-thinking.unit.ts; echo "exit=$?"
```

Expected: `Error [ERR_MODULE_NOT_FOUND]: Cannot find module '...\src\lib\ai-thinking.ts'` and `exit=1`.
(In Git Bash on this PC, `node --test <file>` fails to start its child process because `node` is a shim without
`.exe`; running the file directly runs the same tests and sets the exit code.)

- [ ] **Step 3: Add the thinking types to `ai-connections-api.ts`**

Edit A2 (line 10), old_string:

```ts
export interface AiProfile { token_param: string; temperature: boolean; reasoning_off: Record<string, unknown> | null }
```

new_string:

```ts
/** A job's thinking level (ai_job.thinking). 'off' sends every request as before thinking levels existed. */
export type AiThinkingLevel = 'off' | 'low' | 'medium' | 'high';
/** What Test found about thinking (ai_profile.detect_thinking): the setting it sends ('reasoning_effort',
 *  'openrouter_effort', or null when the model can't think), the levels the model accepts, its time at each in ms,
 *  whether it always thinks (it refuses to stop), and whether a thinking request may carry a temperature. */
export interface AiThinking { param: string | null; levels: string[]; ms: Record<string, number>; always: boolean; temperature: boolean }
/** A profile saved before thinking levels existed has no `thinking`: that model can't think. */
export interface AiProfile {
  token_param: string; temperature: boolean; reasoning_off: Record<string, unknown> | null; thinking?: AiThinking;
}
```

Edit A3 (line 16), old_string:

```ts
export interface AiJobChoice { primary_ref: number; fallback_ref: number | null }
```

new_string:

```ts
export interface AiJobChoice { primary_ref: number; fallback_ref: number | null; thinking: AiThinkingLevel }
```

- [ ] **Step 4: Create `frontend_source/src/lib/ai-thinking.ts`**

It must give the same answer as the server's `ai_profile.effective_level`, so the server and the card agree on what
each model gets. Exactly:

```ts
// Thinking levels on the AI connections card (spec 2026-10-11 D6): what a job's Thinking list offers, the level each
// model is really sent, and its time in Test. No browser needed, so tests/ai-thinking.unit.ts runs it in Node.
import type { AiJob, AiModel, AiThinkingLevel } from './ai-connections-api';

/** The levels above off, lowest first (ai_profile.THINKING_LEVELS). */
export const THINKING_LEVELS: AiThinkingLevel[] = ['low', 'medium', 'high'];
/** Every value a job's thinking can have (ai_job.thinking's CHECK), lowest first. */
export const THINKING_CHOICES: AiThinkingLevel[] = ['off', ...THINKING_LEVELS];

/** The longest one model call of each job may take, in ms: conversation.py CALL_TIMEOUT (6 s), RISK_CALL_TIMEOUT
 *  (2.5 s) and SUMMARY_CALL_TIMEOUT (12 s). A main model slower than this in Test gets a warning on its job. */
export const JOB_CALL_LIMIT_MS: Record<AiJob, number> = { reply: 6000, safety: 2500, after_chat: 12000 };

export const isThinkingChoice = (value: unknown): value is AiThinkingLevel =>
  THINKING_CHOICES.includes(value as AiThinkingLevel);

const rank = (level: AiThinkingLevel) => THINKING_CHOICES.indexOf(level);

/** The levels the model accepts, lowest first. None when it is untested, was tested before thinking levels existed,
 *  or can't think (Test saved no setting to send). */
export function acceptedLevels(model: AiModel | undefined): AiThinkingLevel[] {
  const thinking = model?.profile?.thinking;
  if (!thinking?.param) return [];
  return THINKING_LEVELS.filter(level => thinking.levels.includes(level));
}

/** The level to keep when the job's main model changes to `model`: `level` if it accepts it, else the nearest lower
 *  level it accepts, else off. */
export function nearestLevel(model: AiModel | undefined, level: AiThinkingLevel): AiThinkingLevel {
  const lower = acceptedLevels(model).filter(name => rank(name) <= rank(level));
  return lower.length ? lower[lower.length - 1] : 'off';
}

/** The level the server really sends `model` when its job is set to `level` (ai_profile.effective_level): like
 *  nearestLevel, except that a model that always thinks never gets off; it gets its lowest level. */
export function sentLevel(model: AiModel | undefined, level: AiThinkingLevel): AiThinkingLevel {
  const nearest = nearestLevel(model, level);
  const lowest = acceptedLevels(model)[0];
  return nearest === 'off' && model?.profile?.thinking?.always && lowest ? lowest : nearest;
}

/** The model's time in Test, in ms, at the level it is sent for `level`; null at off or when that level has no time. */
export function thinkingMs(model: AiModel | undefined, level: AiThinkingLevel): number | null {
  const sent = sentLevel(model, level);
  const ms = sent === 'off' ? undefined : model?.profile?.thinking?.ms[sent];
  return typeof ms === 'number' ? ms : null;
}

/** True when that time is over what one call of the job may take. */
export function tooSlow(job: AiJob, model: AiModel | undefined, level: AiThinkingLevel): boolean {
  const ms = thinkingMs(model, level);
  return ms !== null && ms > JOB_CALL_LIMIT_MS[job];
}

/** What the job's Thinking list offers: off, each level its main model accepts, and the job's own level even when
 *  that model doesn't accept it (saved with another main model, or before a new Test), so the list shows what is saved. */
export function offeredLevels(main: AiModel | undefined, level: AiThinkingLevel): AiThinkingLevel[] {
  const accepted = acceptedLevels(main);
  return THINKING_CHOICES.filter(name => name === 'off' || name === level || accepted.includes(name));
}
```

- [ ] **Step 5: Run the unit tests and the type check, and see them pass**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus/frontend_source && node --experimental-strip-types tests/ai-thinking.unit.ts; echo "exit=$?"
npx tsc -b && echo TSC_OK
```

Expected: `ℹ pass 7`, `ℹ fail 0`, `exit=0`, then `TSC_OK` (the card still compiles: it copies each job choice with
`...jobs[job]`, so the new `thinking` field travels with it).

- [ ] **Step 6: Commit Part A**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && git add frontend_source/src/lib/ai-thinking.ts frontend_source/tests/ai-thinking.unit.ts frontend_source/src/lib/ai-connections-api.ts && git commit -F - <<'EOF'
AI card: thinking level types and rules (spec D6)

A job's thinking level and a model's thinking profile in the card's types, and
lib/ai-thinking.ts: the levels a model accepts, the level the server really sends
it (as ai_profile.effective_level), its Test time at that level, and each job's
per-call limit. Node unit tests in tests/ai-thinking.unit.ts.

Co-Authored-By: <the model that wrote the commit> <noreply@anthropic.com>
EOF
git log --oneline -1 && git status --short
```

Replace `<the model that wrote the commit>` with your own model name (global constraints), for example
`Claude Opus 5.5 (1M context)`. Expected: one new commit, a clean tree.

#### Part B: the card

- [ ] **Step 7: Write the failing Playwright tests**

In `frontend_source/tests/ai-connections.spec.ts`:

Edit S1 (lines 15-16, `startingSettings()`; every job now carries its level, as the server sends it), old_string:

```ts
    jobs: { reply: { primary_ref: 11, fallback_ref: 12 }, safety: { primary_ref: 11, fallback_ref: 12 },
      after_chat: { primary_ref: 11, fallback_ref: 12 } },
```

new_string:

```ts
    jobs: { reply: { primary_ref: 11, fallback_ref: 12, thinking: 'off' }, safety: { primary_ref: 11, fallback_ref: 12, thinking: 'off' },
      after_chat: { primary_ref: 11, fallback_ref: 12, thinking: 'off' } },
```

Edit S2 (line 135; after Task 6: 158; the card now sends each job's level), old_string:

```ts
    .toEqual({ primary_ref: 21, fallback_ref: 12 });
```

new_string:

```ts
    .toEqual({ primary_ref: 21, fallback_ref: 12, thinking: 'off' });
```

Edit S3, **only when Step 0's second `grep -c` printed `1`** (Task 6's keyless-OpenRouter fixture; without a level its
settings would no longer load), old_string:

```ts
  settings.jobs.reply = { primary_ref: 21, fallback_ref: null };
```

new_string:

```ts
  settings.jobs.reply = { primary_ref: 21, fallback_ref: null, thinking: 'off' };
```

Then check that no job fixture is left without a level:

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus/frontend_source && grep -nE "primary_ref: [0-9]+, fallback_ref: [0-9a-z]+ \}" tests/ai-connections.spec.ts
```

Expected: no output.

Edit S4: add the new tests right after the test "a job choice not yet saved survives adding another model" (lines
197-198; after Task 6: 220-221). The new tests use only `startingSettings`, `seedSession`, `mockApi`, `openSettings`
and `card`, which exist at `37ba84e`. They set `mock.settings` after `mockApi()` returns: the mock reads it at every
request, so no new `mockApi` option is needed. Model refs 41-44 don't collide with the refs other tests use (11, 12,
21, 31). old_string:

```ts
  await expect(main).toHaveValue('12');
});
```

new_string:

```ts
  await expect(main).toHaveValue('12');
});

// Thinking levels (spec D6). A profile as Test saves it: the levels the model accepts and its time at each, in ms.
const thinkingProfile = (thinking: unknown) => ({ token_param: 'max_completion_tokens', temperature: true,
  reasoning_off: { reasoning_effort: 'none' }, thinking });

/** The starting card plus an OpenAI connection with three tested models: gpt-6-luna (low, medium, high), gpt-5.4-mini
 *  (low only) and gpt-6-astra, which refuses to stop thinking, so its "off" is its lowest level. */
function thinkingSettings() {
  const settings: any = startingSettings();
  settings.connections.push({ connection_id: 2, name: 'OpenAI', kind: 'openai', base_url: 'https://api.openai.com/v1',
    key_last4: '9876', key_readable: true, has_key: true, service_name: 'OpenAI' });
  const openai = (model_ref: number, model_id: string, thinking: unknown) => ({
    model_ref, connection_id: 2, model_id, company: 'OpenAI', region: 'USA', retention: '30 days',
    profile: thinkingProfile(thinking), tested: true, copied_from_env: false, tested_at: '2026-10-11T09:00:00+00:00',
    test_ms: 900, last_error: null,
  });
  settings.models.push(
    openai(41, 'gpt-6-luna', { param: 'reasoning_effort', levels: ['low', 'medium', 'high'],
      ms: { low: 1500, medium: 2300, high: 7200 }, always: false, temperature: false }),
    openai(42, 'gpt-5.4-mini', { param: 'reasoning_effort', levels: ['low'], ms: { low: 1900 }, always: false, temperature: false }),
    openai(43, 'gpt-6-astra', { param: 'reasoning_effort', levels: ['low', 'medium', 'high'],
      ms: { low: 2700, medium: 5200, high: 8800 }, always: true, temperature: false }),
  );
  settings.jobs.reply = { primary_ref: 41, fallback_ref: 12, thinking: 'off' };
  settings.jobs.safety = { primary_ref: 43, fallback_ref: 12, thinking: 'off' };
  return settings;
}

/** The mock on thinkingSettings(), changed by `change` first. The mock reads its settings at every request. */
async function mockThinking(page: Page, change?: (settings: any) => void) {
  const mock = await mockApi(page);
  const settings = thinkingSettings();
  change?.(settings);
  mock.settings = settings;
  return mock;
}

const TOO_SLOW = 'Thinking at this level took longer than this job allows; Reachy may use its fixed line.';

test('a job offers off and the levels its main model accepts, with its time and a warning over the limit', async ({ page }) => {
  await seedSession(page);
  const mock = await mockThinking(page);
  await openSettings(page);
  const reply = card(page).getByRole('group', { name: "Reachy's replies", exact: true });
  const thinking = reply.getByLabel("Reachy's replies: Thinking");
  await expect(thinking.locator('option')).toHaveText(['Off', 'Low', 'Medium', 'High']);
  await expect(thinking).toHaveValue('off');
  await expect(reply).not.toContainText('Main model took');   // off: the model is not asked to think
  await thinking.selectOption('medium');
  await expect(reply).toContainText('Main model took 2.3 s in the test.');
  await expect(reply.getByText(TOO_SLOW)).toHaveCount(0);   // one reply call may take 6 s
  await thinking.selectOption('high');
  await expect(reply).toContainText('Main model took 7.2 s in the test.');
  await expect(reply.getByRole('alert').filter({ hasText: TOO_SLOW })).toBeVisible();
  // a main model that can't think offers only off
  await expect(card(page).getByLabel('After-chat summary and memory: Thinking').locator('option')).toHaveText(['Off']);
  await card(page).getByRole('button', { name: 'Save jobs' }).click();
  await expect.poll(() => mock.requests.find(r => r.path === '/api/ai/jobs')?.body).toEqual({
    reply: { primary_ref: 41, fallback_ref: 12, thinking: 'high' },
    safety: { primary_ref: 43, fallback_ref: 12, thinking: 'off' },
    after_chat: { primary_ref: 11, fallback_ref: 12, thinking: 'off' },
  });
  await expect(card(page).getByRole('status')).toHaveText('Saved.');
});

test('a main model that always thinks gets its lowest level at off, and that time counts', async ({ page }) => {
  await seedSession(page);
  await mockThinking(page);
  await openSettings(page);
  const safety = card(page).getByRole('group', { name: 'Safety check', exact: true });
  await expect(safety.getByLabel('Safety check: Thinking')).toHaveValue('off');
  // its lowest level took 2.7 s in Test; one safety check may take 2.5 s
  await expect(safety).toContainText('The main model uses Low for this job.');
  await expect(safety).toContainText('Main model took 2.7 s in the test.');
  await expect(safety.getByRole('alert').filter({ hasText: TOO_SLOW })).toBeVisible();
});

test('changing the main model keeps the nearest level the new one accepts', async ({ page }) => {
  await seedSession(page);
  await mockThinking(page);
  await openSettings(page);
  const reply = card(page).getByRole('group', { name: "Reachy's replies", exact: true });
  const thinking = reply.getByLabel("Reachy's replies: Thinking");
  const main = reply.getByLabel("Reachy's replies: Main model");
  await thinking.selectOption('high');
  await main.selectOption({ label: 'OpenAI · gpt-5.4-mini' });   // accepts low only
  await expect(thinking).toHaveValue('low');
  await expect(thinking.locator('option')).toHaveText(['Off', 'Low']);
  await expect(reply).toContainText('Main model took 1.9 s in the test.');
  await main.selectOption({ label: 'OpenRouter · google/gemini-2.5-flash' });   // can't think
  await expect(thinking).toHaveValue('off');
  await expect(thinking.locator('option')).toHaveText(['Off']);
});

test('a saved level its main model does not accept stays shown, with the level that model gets', async ({ page }) => {
  await seedSession(page);
  await mockThinking(page, settings => { settings.jobs.reply = { primary_ref: 42, fallback_ref: 12, thinking: 'high' }; });
  await openSettings(page);
  const reply = card(page).getByRole('group', { name: "Reachy's replies", exact: true });
  const thinking = reply.getByLabel("Reachy's replies: Thinking");
  await expect(thinking).toHaveValue('high');
  await expect(thinking.locator('option')).toHaveText(['Off', 'Low', 'High']);
  await expect(reply).toContainText('The main model uses Low for this job.');
  await expect(reply).toContainText('Main model took 1.9 s in the test.');   // timed at low, the level it gets
});

test('each tested model says whether it can think, in both languages', async ({ page }) => {
  await seedSession(page);
  await mockThinking(page);
  await page.goto('/settings');   // the app starts in Traditional Chinese
  const luna = card(page).getByRole('group', { name: 'OpenAI · gpt-6-luna', exact: true });
  const gemini = card(page).getByRole('group', { name: 'OpenRouter · google/gemini-2.5-flash-lite', exact: true });
  await expect(luna).toContainText('可思考：低・中・高');
  await expect(gemini).toContainText('不可思考');
  await expect(card(page).getByLabel('Reachy 的回答: 思考等級').locator('option')).toHaveText(['關閉', '低', '中', '高']);
  await expect(card(page).getByRole('group', { name: '安全檢查', exact: true }))
    .toContainText('此思考等級花的時間超過這項工作的上限；Reachy 可能會說固定句子。');
  await page.getByRole('button', { name: '中文' }).first().click();
  await expect(luna).toContainText('Can think: low · medium · high');
  await expect(card(page).getByRole('group', { name: 'OpenAI · gpt-5.4-mini', exact: true })).toContainText('Can think: low');
  await expect(gemini).toContainText("Can't think");
});

test('Test says the thinking levels are tested too, then the row says what the model can do', async ({ page }) => {
  await seedSession(page);
  const mock = await mockThinking(page, settings => {
    settings.models.push({ model_ref: 44, connection_id: 2, model_id: 'gpt-4.1-mini', company: 'OpenAI', region: 'USA',
      retention: '30 days', profile: null, tested: false, copied_from_env: false, tested_at: null, test_ms: null, last_error: null });
  });
  let answer: () => void = () => {};
  const answered = new Promise<void>(resolve => { answer = resolve; });
  // Registered after mockApi, so it answers this model's Test first, and only once the test lets it.
  await page.route('**/api/ai/models/44/test', async route => {
    await answered;
    const model: any = mock.settings.models.find((m: any) => m.model_ref === 44);
    Object.assign(model, { profile: thinkingProfile({ param: null, levels: [], ms: {}, always: false, temperature: false }),
      tested: true, tested_at: '2026-10-11T10:00:00+00:00', test_ms: 700 });
    await route.fulfill({ json: { ok: true, ms: 700, sample: '您好！', profile: model.profile, error: null, settings: mock.settings } });
  });
  await openSettings(page);
  const row = card(page).getByRole('group', { name: 'OpenAI · gpt-4.1-mini', exact: true });
  await expect(row).toContainText('Not tested');
  await expect(row).not.toContainText('think');   // untested: nothing is known yet
  await row.getByRole('button', { name: 'Test' }).click();
  await expect(row).toContainText('Testing, including thinking levels…');
  answer();
  await expect(row).toContainText('0.7 s');
  await expect(row).toContainText("Can't think");
});

test('a thinking level not yet saved survives adding another model', async ({ page }) => {
  await seedSession(page);
  await mockThinking(page);
  await openSettings(page);
  const thinking = card(page).getByLabel("Reachy's replies: Thinking");
  await thinking.selectOption('medium');
  await card(page).getByRole('button', { name: 'Add model' }).click();
  await card(page).getByLabel('Connection', { exact: true }).selectOption({ label: 'OpenAI' });
  await card(page).getByLabel('Model name').fill('gpt-4.1-mini');
  await card(page).getByRole('button', { name: 'Add', exact: true }).click();
  await expect(card(page).getByRole('group', { name: 'OpenAI · gpt-4.1-mini' })).toBeVisible();
  await expect(thinking).toHaveValue('medium');
});

test('a model whose thinking used the whole budget says so, in both languages', async ({ page }) => {
  await seedSession(page);
  // Task 8: an empty answer cut off with every token spent on thinking is saved as the model's last_error.
  await mockThinking(page, settings => { settings.models.find((m: any) => m.model_ref === 41).last_error = 'thinking_budget'; });
  await page.goto('/settings');   // the app starts in Traditional Chinese
  const luna = card(page).getByRole('group', { name: 'OpenAI · gpt-6-luna', exact: true });
  await expect(luna).toContainText('上次呼叫失敗：思考用完了全部的額度，所以沒有回答。');
  await page.getByRole('button', { name: '中文' }).first().click();
  await expect(luna).toContainText('Last call failed: Thinking used the whole budget, so no answer came back.');
  await expect(luna).not.toContainText('thinking_budget');
});

const brokenThinking: [string, (settings: any) => void][] = [
  ['a job thinking level the card does not know', settings => { settings.jobs.reply.thinking = 'max'; }],
  ['a job without a thinking level', settings => { delete settings.jobs.after_chat.thinking; }],
  ["a model's thinking levels that are not a list", settings => { settings.models[2].profile.thinking.levels = 'low'; }],
];
for (const [what, breakIt] of brokenThinking) {
  test(`${what} is a load failure, not a crash`, async ({ page }) => {
    const settings = thinkingSettings();
    breakIt(settings);
    await seedSession(page);
    await mockApi(page, { aiBody: settings });
    await openSettings(page);
    await expect(card(page)).toContainText('Could not load the AI settings.');
    await expect(page.locator('section[aria-labelledby="reachy-title"]')).toBeVisible();
  });
}
```

- [ ] **Step 8: Build and run them, and see the new ones fail**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus/frontend_source && npx tsc -b && npm run build 2>&1 | grep -E "✓ built in|error"
npx playwright test --config "$S/pw-frontend.config.mjs" tests/ai-connections.spec.ts 2>&1 | grep -E "✘|^ +[0-9]+ (passed|failed|flaky)"
```

Expected: a `✓ built in ...` line and no `error` line. Exactly the 11 new tests fail (the Thinking lists, the "Can think"
lines and the Testing status don't exist yet, so the locators time out; the `thinking_budget` row shows the raw code;
the three bad answers still load, so "Could not load the AI settings." never shows):
- a job offers off and the levels its main model accepts, with its time and a warning over the limit
- a main model that always thinks gets its lowest level at off, and that time counts
- changing the main model keeps the nearest level the new one accepts
- a saved level its main model does not accept stays shown, with the level that model gets
- each tested model says whether it can think, in both languages
- Test says the thinking levels are tested too, then the row says what the model can do
- a thinking level not yet saved survives adding another model
- a model whose thinking used the whole budget says so, in both languages (it reads `上次呼叫失敗：thinking_budget`)
- a job thinking level the card does not know is a load failure, not a crash
- a job without a thinking level is a load failure, not a crash
- a model's thinking levels that are not a list is a load failure, not a crash

Every older test passes, including the one with Edit S2's new expectation (the card already passes the server's
`thinking` through unchanged). Expected: `11 failed` with `13 passed` (8 old + Task 6's 5). Measured in planning,
before the `thinking_budget` test was added: `10 failed` with `13 passed`, in about 2.5 minutes (two tests wait for
the 30 s test timeout).

- [ ] **Step 9: Check the settings answer for thinking (`ai-connections-api.ts`)**

Edit A1 (line 1), old_string:

```ts
import { ApiError, call } from './reachy-api';
```

new_string:

```ts
import { isThinkingChoice } from './ai-thinking';
import { ApiError, call } from './reachy-api';
```

Edit A4 (line 31; after Task 6: 33), old_string:

```ts
const isNumber = (value: unknown) => typeof value === 'number' && Number.isFinite(value);
```

new_string:

```ts
const isNumber = (value: unknown) => typeof value === 'number' && Number.isFinite(value);
/** No `thinking` (a profile saved before thinking levels) is fine; a present one needs every part the card reads. */
const isThinking = (value: unknown) => value === undefined || value === null || (isObject(value)
  && (value.param === null || typeof value.param === 'string') && Array.isArray(value.levels)
  && value.levels.every(level => typeof level === 'string') && isObject(value.ms) && Object.values(value.ms).every(isNumber)
  && typeof value.always === 'boolean');
const hasThinking = (choice: unknown) => isObject(choice) && isThinkingChoice(choice.thinking);
```

Edit A5 (line 38; after Task 6: 42; the models line of `isAiSettings`, which Task 6 doesn't change), old_string:

```ts
    && value.models.every(m => isObject(m) && isNumber(m.model_ref) && isNumber(m.connection_id) && typeof m.model_id === 'string')
```

new_string:

```ts
    && value.models.every(m => isObject(m) && isNumber(m.model_ref) && isNumber(m.connection_id) && typeof m.model_id === 'string'
      && (m.profile === null || (isObject(m.profile) && isThinking(m.profile.thinking))))
    && AI_JOBS.every(job => hasThinking(jobs[job]))
```

For reference, the whole `isAiSettings` after Task 6 and this step (the `jobs_usable` parts are Task 6's, as in both
of its drafts; without Task 6 the function is the same without them):

```ts
/** True only when every part the card reads has the shape it expects. */
function isAiSettings(value: unknown): value is AiSettings {
  if (!isObject(value) || !Array.isArray(value.connections) || !Array.isArray(value.models) || !isObject(value.jobs)
    || !isObject(value.jobs_usable)) return false;
  const jobs = value.jobs;
  const usable = value.jobs_usable;
  return value.connections.every(c => isObject(c) && isNumber(c.connection_id) && typeof c.name === 'string' && typeof c.kind === 'string')
    && value.models.every(m => isObject(m) && isNumber(m.model_ref) && isNumber(m.connection_id) && typeof m.model_id === 'string'
      && (m.profile === null || (isObject(m.profile) && isThinking(m.profile.thinking))))
    && AI_JOBS.every(job => hasThinking(jobs[job]))
    && AI_JOBS.every(job => {
      const choice = jobs[job];
      return isObject(choice) && isNumber(choice.primary_ref) && (choice.fallback_ref === null || isNumber(choice.fallback_ref))
        && typeof usable[job] === 'boolean';
    });
}
```

- [ ] **Step 10: Add the words, en and zh-TW (`i18n.ts`)**

The level names in the list are capitalised in English; the "Can think" line uses the lower-case names joined by
" · " (U+00B7 with spaces) in English and "・" (U+30FB, no spaces) in zh-TW, exactly as the spec writes them. The
warning is the spec's exact text in both languages.

Edit I1 (en, line 59), old_string:

```ts
        onlyTested: 'Only tested models can be chosen.',
```

new_string:

```ts
        onlyTested: 'Only tested models can be chosen.',
        thinking: 'Thinking',
        levelNames: { off: 'Off', low: 'Low', medium: 'Medium', high: 'High' },
        levelShort: { low: 'low', medium: 'medium', high: 'high' },
        levelSeparator: ' · ',
        canThink: 'Can think: {{levels}}',
        cantThink: "Can't think",
        testingThinking: 'Testing, including thinking levels…',
        thinkingSent: 'The main model uses {{level}} for this job.',
        thinkingTime: 'Main model took {{seconds}} s in the test.',
        thinkingTooSlow: 'Thinking at this level took longer than this job allows; Reachy may use its fixed line.',
```

Edit I2 (zh-TW, line 739; after Task 6: 742), old_string:

```ts
        onlyTested: '只有測試過的模型可以指派工作。',
```

new_string:

```ts
        onlyTested: '只有測試過的模型可以指派工作。',
        thinking: '思考等級',
        levelNames: { off: '關閉', low: '低', medium: '中', high: '高' },
        levelShort: { low: '低', medium: '中', high: '高' },
        levelSeparator: '・',
        canThink: '可思考：{{levels}}',
        cantThink: '不可思考',
        testingThinking: '測試中，也會測試思考等級…',
        thinkingSent: '主要模型在這項工作會使用「{{level}}」。',
        thinkingTime: '主要模型測試時花了 {{seconds}} 秒。',
        thinkingTooSlow: '此思考等級花的時間超過這項工作的上限；Reachy 可能會說固定句子。',
```

Edit I3 (en, Task 6's `modelErrors` line; Task 8's `thinking_budget` gets the card's words, spec D5), old_string:

```ts
        modelErrors: { not_chat_model: "Voice-only model (like GPT-Live or a TTS model): it can't write Reachy's text replies." },
```

new_string:

```ts
        modelErrors: {
          not_chat_model: "Voice-only model (like GPT-Live or a TTS model): it can't write Reachy's text replies.",
          thinking_budget: 'Thinking used the whole budget, so no answer came back.',
        },
```

Edit I4 (zh-TW, Task 6's `modelErrors` line), old_string:

```ts
        modelErrors: { not_chat_model: '語音專用模型（例如 GPT-Live 或 TTS）：無法撰寫 Reachy 的文字回覆。' },
```

new_string:

```ts
        modelErrors: {
          not_chat_model: '語音專用模型（例如 GPT-Live 或 TTS）：無法撰寫 Reachy 的文字回覆。',
          thinking_budget: '思考用完了全部的額度，所以沒有回答。',
        },
```

- [ ] **Step 11: Check the new strings' characters**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus/frontend_source && python - <<'EOF'
text = open("src/i18n.ts", encoding="utf-8").read()
for needle in ["levelSeparator: ' \u00b7 ',", "levelSeparator: '\u30fb',", "canThink: '可思考：{{levels}}',",
               "testingThinking: 'Testing, including thinking levels\u2026',",
               "thinkingTooSlow: '此思考等級花的時間超過這項工作的上限；Reachy 可能會說固定句子。',",
               "thinking_budget: 'Thinking used the whole budget, so no answer came back.',",
               "thinking_budget: '思考用完了全部的額度，所以沒有回答。',"]:
    print(text.count(needle), needle)
EOF
```

Expected: each line starts with `1`.

- [ ] **Step 12: The card (`AiConnectionsCard.tsx`)**

Edit C1 (lines 8-9, imports), old_string:

```tsx
  type AiJob, type AiJobChoice, type AiKind, type AiModel, type AiSettings, type AiTestResult,
} from '../lib/ai-connections-api';
```

new_string:

```tsx
  type AiJob, type AiJobChoice, type AiKind, type AiModel, type AiSettings, type AiTestResult, type AiThinkingLevel,
} from '../lib/ai-connections-api';
import { acceptedLevels, nearestLevel, offeredLevels, sentLevel, thinkingMs, tooSlow } from '../lib/ai-thinking';
```

Edit C8 (Task 6's list of codes a model's Test result or last call can carry; `thinking_budget` is Task 8's),
old_string:

```tsx
const MODEL_ERRORS = ['not_chat_model'];
```

new_string:

```tsx
const MODEL_ERRORS = ['not_chat_model', 'thinking_budget'];
```

Edit C2 (lines 131-132; after Task 6: 137-138; the first line of `status`: while Test runs, the row says so),
old_string:

```tsx
  const status = (model: AiModel) => {
    const result = results[model.model_ref];
```

new_string:

```tsx
  const status = (model: AiModel) => {
    if (testing === model.model_ref) return <span className="text-gray-500">{t('aiConnections.testingThinking')}</span>;
    const result = results[model.model_ref];
```

Edit C3 (lines 142-143; after Task 6: 148-149; the end of `status`). It adds three helpers after `status`: `canThink`
for the model rows, `withMainModel` for a new main model, and `thinkingControls` for the job rows. old_string:

```tsx
    return <span className="text-gray-500">{t('aiConnections.notTested')}</span>;
  };
```

new_string:

```tsx
    return <span className="text-gray-500">{t('aiConnections.notTested')}</span>;
  };

  /** "Can think: low · medium · high" or "Can't think", from what the model's Test found. */
  const canThink = (model: AiModel) => {
    const levels = acceptedLevels(model);
    return levels.length
      ? t('aiConnections.canThink', { levels: levels.map(level => t(`aiConnections.levelShort.${level}`)).join(t('aiConnections.levelSeparator')) })
      : t('aiConnections.cantThink');
  };

  /** The job's choice with another main model. Its thinking level stays if the new model accepts it, else it becomes
   *  the nearest lower level that model accepts, else off. */
  const withMainModel = (job: AiJob, primary_ref: number): AiJobChoice => {
    const main = settings.models.find(model => model.model_ref === primary_ref);
    return { ...jobs[job], primary_ref, thinking: nearestLevel(main, jobs[job].thinking) };
  };

  /** A job's Thinking list (off, and the levels its main model accepts), the level that model really gets when it
   *  differs, its time at that level in Test, and a warning when that time is over what one call of the job may take. */
  const thinkingControls = (job: AiJob) => {
    const choice = jobs[job];
    const main = settings.models.find(model => model.model_ref === choice.primary_ref);
    const sent = sentLevel(main, choice.thinking);
    const ms = thinkingMs(main, choice.thinking);
    const notes: string[] = [];
    if (sent !== choice.thinking) notes.push(t('aiConnections.thinkingSent', { level: t(`aiConnections.levelNames.${sent}`) }));
    if (ms !== null) notes.push(t('aiConnections.thinkingTime', { seconds: (ms / 1000).toFixed(1) }));
    return (
      <>
        <label className="block text-xs text-gray-600 sm:col-start-2">{t('aiConnections.thinking')}
          <select className={field} aria-label={`${t(`aiConnections.jobNames.${job}`)}: ${t('aiConnections.thinking')}`} value={choice.thinking}
            onChange={event => {
              setJobsSaved(false);
              setJobs({ ...jobs, [job]: { ...choice, thinking: event.target.value as AiThinkingLevel } });
            }}>
            {offeredLevels(main, choice.thinking).map(level => <option key={level} value={level}>{t(`aiConnections.levelNames.${level}`)}</option>)}
          </select>
        </label>
        {notes.length > 0 && (
          <p className="text-xs text-gray-500 sm:pb-2">{notes.map(note => <span key={note} className="block">{note}</span>)}</p>
        )}
        {tooSlow(job, main, choice.thinking) && (
          <p role="alert" className={`${warning} sm:col-span-3`}><TriangleAlert className="inline w-4 h-4" /> {t('aiConnections.thinkingTooSlow')}</p>
        )}
      </>
    );
  };
```

Edit C4 (line 252; after Task 6: 258; the "Can think" line under a tested model's status), old_string:

```tsx
                <div className="text-xs break-words">{status(model)}</div>
```

new_string:

```tsx
                <div className="text-xs break-words">{status(model)}</div>
                {model.profile && <div className="text-xs text-gray-500">{canThink(model)}</div>}
```

Edit C5 (line 346; after Task 6: 353; inside the main model list's `onChange`, only the `setJobs` call changes, so the
line keeps its indentation), old_string:

```tsx
setJobs({ ...jobs, [job]: { ...jobs[job], primary_ref: Number(event.target.value) } });
```

new_string:

```tsx
setJobs({ ...jobs, [job]: withMainModel(job, Number(event.target.value)) });
```

Edit C6 or C6b (Step 0's last `grep -n` says which): the Thinking list and its notes go right after the backup list's
`</label>`, inside the same grid `<div>` as the main and backup lists, before Task 6's "no usable model" warning.

Edit C6 (layout at `37ba84e`, lines 357-360), old_string:

```tsx
                <option value="">{t('aiConnections.noBackup')}</option>
                {tested.map(model => <option key={model.model_ref} value={model.model_ref}>{label(model)}</option>)}
              </select>
            </label>
```

new_string:

```tsx
                <option value="">{t('aiConnections.noBackup')}</option>
                {tested.map(model => <option key={model.model_ref} value={model.model_ref}>{label(model)}</option>)}
              </select>
            </label>
            {thinkingControls(job)}
```

Edit C6b (Task 6's latest draft, lines 364-367), old_string:

```tsx
                  <option value="">{t('aiConnections.noBackup')}</option>
                  {tested.map(model => <option key={model.model_ref} value={model.model_ref}>{label(model)}</option>)}
                </select>
              </label>
```

new_string:

```tsx
                  <option value="">{t('aiConnections.noBackup')}</option>
                  {tested.map(model => <option key={model.model_ref} value={model.model_ref}>{label(model)}</option>)}
                </select>
              </label>
              {thinkingControls(job)}
```

If neither matches (Task 6 laid the row out another way), add the one line `{thinkingControls(job)}` right after the
`</label>` that closes the backup list, indented like that `</label>`, inside the grid `<div>` that holds the main
and backup lists.

Edit C7, **only when Step 0's first `grep -c` printed `0`** (each job row needs a group named after its job; the tests
find the row by it), old_string:

```tsx
          <div key={job} className="grid gap-2 sm:grid-cols-3 sm:items-end">
```

new_string:

```tsx
          <div key={job} role="group" aria-label={t(`aiConnections.jobNames.${job}`)} className="grid gap-2 sm:grid-cols-3 sm:items-end">
```

For reference, the job rows after Task 6's latest draft and this step (the grid stays 3 columns on wider screens: the
Thinking list sits under the main model list, the notes under the backup list, the warnings span the row; the
`jobs_usable` warning is Task 6's):

```tsx
        {AI_JOBS.map(job => (
          <div key={job} role="group" aria-label={t(`aiConnections.jobNames.${job}`)} className="space-y-2">
            <div className="grid gap-2 sm:grid-cols-3 sm:items-end">
              <span className="text-sm text-gray-900 sm:pb-2">{t(`aiConnections.jobNames.${job}`)}</span>
              <label className="block text-xs text-gray-600">{t('aiConnections.main')}
                <select className={field} aria-label={`${t(`aiConnections.jobNames.${job}`)}: ${t('aiConnections.main')}`} value={jobs[job].primary_ref}
                  onChange={event => {
                    setJobsSaved(false);
                    setJobs({ ...jobs, [job]: withMainModel(job, Number(event.target.value)) });
                  }}>
                  {tested.map(model => <option key={model.model_ref} value={model.model_ref}>{label(model)}</option>)}
                </select>
              </label>
              <label className="block text-xs text-gray-600">{t('aiConnections.backup')}
                <select className={field} aria-label={`${t(`aiConnections.jobNames.${job}`)}: ${t('aiConnections.backup')}`} value={jobs[job].fallback_ref ?? ''}
                  onChange={event => {
                    setJobsSaved(false);
                    setJobs({ ...jobs, [job]: { ...jobs[job], fallback_ref: event.target.value ? Number(event.target.value) : null } });
                  }}>
                  <option value="">{t('aiConnections.noBackup')}</option>
                  {tested.map(model => <option key={model.model_ref} value={model.model_ref}>{label(model)}</option>)}
                </select>
              </label>
              {thinkingControls(job)}
            </div>
            {/* The saved setup, as the server judged it; `?.`: change answers are not shape-checked like the first load. */}
            {settings.jobs_usable?.[job] === false && (
              <p role="alert" className={warning}>
                <TriangleAlert className="inline w-4 h-4" /> {t('aiConnections.jobUnusable')}
                {job !== 'reply' && <span className="block mt-1">{t(`aiConnections.jobUnusableEffect.${job}`)}</span>}
              </p>
            )}
          </div>
        ))}
```

- [ ] **Step 13: Type check, build, lint, line endings**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus/frontend_source && npx tsc -b && echo TSC_OK && npm run build 2>&1 | grep -E "✓ built in|error"
npx eslint src/components/AiConnectionsCard.tsx src/lib/ai-connections-api.ts src/lib/ai-thinking.ts && echo ESLINT_OK
python - <<'EOF'
from pathlib import Path
for name in ["src/components/AiConnectionsCard.tsx", "src/lib/ai-connections-api.ts", "src/i18n.ts", "tests/ai-connections.spec.ts"]:
    data = Path(name).read_bytes()
    print(name, "CRLF", data.count(b"\r\n"), "LF", data.count(b"\n"))
EOF
cd .. && git diff --stat
```

Expected: `TSC_OK`; a `✓ built in ...` line and no `error` line; `ESLINT_OK` (no new lint errors; the 20 old ones are in other files);
`AiConnectionsCard.tsx` has `CRLF 0`, and in each other file the CRLF count equals the LF count; `git diff --stat`
lists only the four files.

- [ ] **Step 14: Run the card's tests and see them pass**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus/frontend_source && npx playwright test --config "$S/pw-frontend.config.mjs" tests/ai-connections.spec.ts 2>&1 | grep -E "✘|^ +[0-9]+ (passed|failed|flaky)"
```

Expected: no `✘`, and `passed` is Step 8's passed count plus 11: `24 passed` (8 old + 5 from Task 6 + 11 new).
Measured in planning, before the `thinking_budget` test was added: `23 passed` with either Task 6 draft.

- [ ] **Step 15: Run the whole frontend suite**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus/frontend_source && for f in tests/*.unit.ts; do node --experimental-strip-types "$f" > /dev/null 2>&1 && echo "ok $f" || echo "FAILED $f"; done
npx playwright test --config "$S/pw-frontend.config.mjs" 2>&1 | grep -E "✘|^ +[0-9]+ (passed|failed|flaky)"
```

Expected: `ok` for `tests/ai-thinking.unit.ts`, `tests/doses.unit.ts` and `tests/scan.unit.ts`. Playwright: every test
passes except `scan.spec.ts:148 › a course that already ended starts unticked, and is saved only with a new last day`,
which already fails on an unchanged copy of `37ba84e` (checked 2026-10-11: it waits for
「最後服藥日已經過了。請修改或清除日期後再加入。」; the Scan page is not touched here).
Expected: `70 passed, 1 failed` (that one): 48 at `37ba84e`, plus Task 1's 3 (`robot-settings-page.spec.ts`), Task
3's 2, Task 6's 6 and this task's 11. Measured in planning without Tasks 1 and 3 and without the `thinking_budget`
test: `64 passed` with Task 6's latest draft. Any other failure is this task's to fix.

- [ ] **Step 16: Look at the card at 390 and 1280 px**

Create (or overwrite) `$S/t9_shots.mjs` with exactly the following. It serves the build from Step 13 with
`vite preview` on port 4193, mocks the API, and saves the card in zh-TW and en at both widths:

```js
// Task 9: screenshots of the AI connections card (job rows with Thinking, model rows with "Can think") at 390 and
// 1280 px, on the built app with a mocked API. A scratch tool, never committed.
//   node t9_shots.mjs <frontend_source folder, already built> <folder for the PNGs>
import { createRequire } from 'node:module';
import { execFileSync, spawn } from 'node:child_process';
import { mkdirSync } from 'node:fs';

const [root, out] = process.argv.slice(2);
const { chromium } = createRequire(`${root}/package.json`)('playwright');
const PORT = 4193;
const BASE = `http://localhost:${PORT}`;

const profile = thinking => ({ token_param: 'max_completion_tokens', temperature: true,
  reasoning_off: { reasoning_effort: 'none' }, thinking });
const model = (model_ref, connection_id, model_id, thinking) => ({
  model_ref, connection_id, model_id, company: connection_id === 1 ? 'Google' : 'OpenAI', region: 'USA',
  retention: '30 days', profile: profile(thinking), tested: true, copied_from_env: false,
  tested_at: '2026-10-11T09:00:00+00:00', test_ms: 900, last_error: null,
});
const settings = {
  connections: [
    { connection_id: 1, name: 'OpenRouter', kind: 'openrouter', base_url: 'https://openrouter.ai/api/v1', key_last4: 'ab12',
      key_readable: true, has_key: true, service_name: 'OpenRouter' },
    { connection_id: 2, name: 'OpenAI', kind: 'openai', base_url: 'https://api.openai.com/v1', key_last4: '9876',
      key_readable: true, has_key: true, service_name: 'OpenAI' },
  ],
  models: [
    model(12, 1, 'google/gemini-2.5-flash', undefined),
    model(41, 2, 'gpt-6-luna', { param: 'reasoning_effort', levels: ['low', 'medium', 'high'],
      ms: { low: 1500, medium: 2300, high: 7200 }, always: false, temperature: false }),
    model(43, 2, 'gpt-6-astra', { param: 'reasoning_effort', levels: ['low', 'medium', 'high'],
      ms: { low: 2700, medium: 5200, high: 8800 }, always: true, temperature: false }),
  ],
  jobs: { reply: { primary_ref: 41, fallback_ref: 12, thinking: 'high' },
    safety: { primary_ref: 43, fallback_ref: 12, thinking: 'off' },
    after_chat: { primary_ref: 41, fallback_ref: 12, thinking: 'medium' } },
  jobs_usable: { reply: true, safety: true, after_chat: true },
  seeded_at: null, weak_secret: false, no_key_in_use: false,
};

async function waitForServer() {
  for (let i = 0; i < 60; i += 1) {
    try { if ((await fetch(BASE)).ok) return; } catch { /* not up yet */ }
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw new Error(`vite preview did not answer on ${BASE}`);
}

mkdirSync(out, { recursive: true });
const server = spawn(`npx vite preview --port ${PORT} --strictPort`, { cwd: root, shell: true });
try {
  await waitForServer();
  const browser = await chromium.launch();
  for (const width of [390, 1280]) {
    for (const lang of ['zh-TW', 'en']) {
      const page = await browser.newPage({ viewport: { width, height: 900 } });
      await page.addInitScript(() => {
        for (const key of ['face_auth_session', 'onboarding_complete', 'onboarding_face_done']) localStorage.setItem(key, 'true');
        localStorage.setItem('face_auth_user', JSON.stringify({ name: 'AI Test', u_id: 7, loginAt: new Date().toISOString() }));
        localStorage.setItem('face_auth_token', 'test-face-token');
      });
      await page.route('**/api/**', route => {
        const path = new URL(route.request().url()).pathname;
        if (path === '/api/consent/status') {
          return route.fulfill({ json: { terms_version: '2026-10', core_current: true, robot_current: false,
            scopes: { core: { granted: true, terms_version: '2026-10', kind: 'core' } } } });
        }
        if (path === '/api/reachy/status') return route.fulfill({ json: { paired: false } });
        if (path === '/api/notify/settings') return route.fulfill({ json: {} });
        if (path === '/api/ai/settings') return route.fulfill({ json: settings });
        return route.fulfill({ json: [] });
      });
      await page.goto(`${BASE}/settings`);
      // the app starts in Traditional Chinese; its language button switches to English
      if (lang === 'en') await page.getByRole('button', { name: '中文' }).first().click();
      const card = page.locator('section[aria-labelledby="ai-title"]');
      await card.locator('select').first().waitFor();
      // the app's fixed header and bottom bar would cover parts of a tall element screenshot
      await page.addStyleTag({ content: '.fixed, .sticky { position: static !important; }' });
      const file = `${out}/ai-card-thinking-${width}-${lang}.png`;
      await card.screenshot({ path: file });
      console.log(`saved ${file}`);
      await page.close();
    }
  }
  await browser.close();
} finally {
  execFileSync('taskkill', ['/pid', String(server.pid), '/T', '/F'], { stdio: 'ignore' });
}
```

```bash
node "$S/t9_shots.mjs" C:/Reachy_Mini/repo/MedAiCarePlus/frontend_source "$S/t9shots"
```

Expected: four `saved .../ai-card-thinking-<390|1280>-<zh-TW|en>.png` lines. Open each PNG with the Read tool and
check:
- the reply job shows High, "Main model took 7.2 s in the test." and the yellow warning;
- the safety job shows Off (關閉), "The main model uses Low for this job." on its own line, then the 2.7 s line, and
  the warning;
- the after-chat job shows Medium and 2.3 s without a warning;
- each model row shows its "Can think" line (zh-TW 「可思考：低・中・高」, gemini 「不可思考」);
- at 390 px everything stacks in one column with nothing cut off; at 1280 px the Thinking list is under the main model
  list and the time under the backup list.

- [ ] **Step 17: Commit Part B**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && git add frontend_source/src/lib/ai-connections-api.ts frontend_source/src/components/AiConnectionsCard.tsx frontend_source/src/i18n.ts frontend_source/tests/ai-connections.spec.ts && git commit -F - <<'EOF'
AI card: a thinking level per job, and what each model can do (spec D6)

Each job row gets a Thinking list (Off, and the levels its main model accepts),
the main model's Test time at the level it really gets, and a warning when that
time is over the job's per-call limit (reply 6 s, safety 2.5 s, after-chat 12 s).
Model rows say "Can think: low · medium · high" or "Can't think"; Test says it
tests the thinking levels too; a last call that ended in thinking_budget says
"Thinking used the whole budget" instead of the code. The level is saved with Save jobs and kept, like
the model choices, across other changes. A settings answer with a malformed job
level or thinking profile is a load failure. Words in en and zh-TW; Playwright
tests for all of it.

Co-Authored-By: <the model that wrote the commit> <noreply@anthropic.com>
EOF
git log --oneline -2 && git status --short
```

Replace `<the model that wrote the commit>` with your own model name. Expected: one new commit, a clean tree.

#### NOTES

**Assumptions:**
- Task 6 runs first and changes the same four files. Two of its drafts were in the scratchpad: `t6-changed/` (17:22)
  and `t06fe/` (18:03). Between them it renamed its fixture (`keylessOpenRouter` to `keylessOpenRouterSettings`),
  dropped the `start` option of `mockApi`, renamed `testErrors` to `modelErrors`, and wrapped each job row's lists in
  one more `<div>`. This task touches none of Task 6's lines. The places that depend on Task 6's final layout are
  conditional (C6 or C6b, C7, S3), and Step 0 says which apply. C5 changes only the inside of one line, so it works at
  any indentation.
- The backend sends each job's `thinking` in every card answer (the backend task's prototype in scratchpad
  `t8proto/app/services/ai_settings.py:200`: `"thinking": snapshot.thinking.get(job, "off")`) and saves the
  profile's `thinking` as `ai_profile.detect_thinking` returns it (prototype `t7work/ai_profile.new.py:250-287`;
  `CANT_THINK = {"param": None, "levels": [], "ms": {}, "always": False, "temperature": False}` at line 37). The card's
  `sentLevel` mirrors that prototype's `effective_level` (lines 115-128): no `param` or no accepted level gives off;
  else the nearest lower accepted level; below the lowest, an `always` model gets its lowest level.
- An `always` model's lowest level can be in `levels` without a time in `ms` (the prototype keeps it even when its
  probe didn't answer, lines 282-284). The card then shows no time and no warning for it (unit test "the time shown
  is the time at the level really sent").

**Contract:** no deviation. The contract's inline types get names: `AiThinking` (exactly the contract's `thinking?`
shape) and `AiThinkingLevel` (`'off'|'low'|'medium'|'high'`). `settings.jobs_usable` is left to Task 6, which adds it
(if Task 6 did not, Task 10's check for `jobs_usable` in `ai-connections-api.ts` fails). New: `src/lib/ai-thinking.ts`
holds the rules so Node can unit-test them. It imports only types from `ai-connections-api.ts`, and
`ai-connections-api.ts` imports `isThinkingChoice` from it: a type-only cycle, which TypeScript and Vite accept
(checked: `tsc -b`, `vite build` and the Node tests all pass).

**Choices the spec leaves open (each covered by a test):**
- The saved level stays in the list even when the main model doesn't accept it (after a new Test, or saved with
  another main model), so the list never shows a level other than the saved one; the note says which level the main
  model really gets.
- Picking another main model moves the level down to the nearest one the new model accepts (else Off). The backup
  model gets the server's nearest-lower rule from that level.
- The time and the warning use the level the main model really gets, so an `always` model (GPT-6 Astra, 6.1 Sol)
  shows its lowest level's time even at Off.
- The limits are fixed in `ai-thinking.ts` from `conversation.py:49` (`CALL_TIMEOUT = 6`), `:59`
  (`RISK_CALL_TIMEOUT = 2.5`) and `:54` (`SUMMARY_CALL_TIMEOUT = 12`); a time equal to the limit is not a warning. The
  after-chat memory call allows 20 s (`memory.py:344`, `AFTER_CHAT_CALL_TIMEOUT`), but the spec says 12 s and the
  plain summary call has 12 s, so the card warns from 12 s. If those constants change, change `JOB_CALL_LIMIT_MS` too.
- The check of the settings answer is strict, as the card's existing rule is ("true only when every part the card
  reads has the shape it expects"): a job without a valid `thinking` makes the card show "Could not load the AI
  settings." while the rest of Settings keeps working. This is also what a server without the backend thinking task
  would cause.

**Risks:**
- If the backend sends a level name other than off/low/medium/high (for example `xhigh`), the card refuses the whole
  answer. The contract's CHECK and `Literal` rule that out.
- The Test status text shows for the whole Test request; the card can't tell the base test from the thinking probes
  (one request). That is what D2 asks ("a short 'testing thinking levels' status").
- `scan.spec.ts:148` fails before and after this task (see Step 15); don't count it against this task.

**Verified (read or run in this session, at commit `37ba84e`):**
- `AiConnectionsCard.tsx` (LF, 372 lines): imports 5-9; `status` 131-143; model row status line 252; job rows 339-362
  (row `<div>` 340, main `onChange` 344-347, backup label 351-360); `run` 60-75 uses `mergeJobs` unless `savesJobs`;
  Save jobs button 364-365.
- `ai-connections-api.ts` (CRLF, 89 lines): `AiProfile` 10, `AiJobChoice` 16, `isNumber` 31, `isAiSettings` 34-43,
  `mergeJobs` 56-67 (keeps the whole current choice), `saveJobs` 89 (sends the jobs as they are).
- `i18n.ts` (CRLF): en `aiConnections` 13-76 (`onlyTested` 59), zh-TW 693-756 (`onlyTested` 739); init 1361-1377
  (`lng: 'zh-TW'` 1366, `escapeValue: false` 1369).
- `tests/ai-connections.spec.ts` (CRLF, 198 lines): `startingSettings` 6-19, `mockApi` 29-72 (the route reads
  `mock.settings` at each request: 44, 66-67), POST models gives ref 21 (52-56), Save jobs expectation 134-135, last
  test 185-198.
- `api_ai.py:60-68` (`JobChoice`, `Jobs`), `api_ai.py:271-285` (`PUT /jobs`); `ai_settings.py:180-195` (`public`).
- The spec's separators: "Can think: low · medium · high" uses U+00B7; 「可思考：低・中・高」 uses U+FF1A and U+30FB.
- Rehearsal (scratch copies `$S/t9run-base`, `$S/t9run-with6` and `$S/t9run-with06`, each with a `node_modules`
  junction to the repo's; script `$S/t9_rehearse.sh`, edits applied by `$S/t9_edits.py` from the same text as this
  plan): every `old_string` occurs once, at a line start, where its step says it applies; Step 2 failed with
  `ERR_MODULE_NOT_FOUND`; Step 5 gave 7 passed and a clean `tsc -b`; Step 8 gave exactly the 10 new tests failing;
  Step 13 gave a clean `tsc -b`, build and eslint, CRLF intact; Step 14 gave 23 passed with either Task 6 draft and 18
  on `37ba84e` alone; Step 15 gave 58, 63 or 64 passed plus only the old `scan.spec.ts:148` failure, which also fails on the
  unchanged copy; Step 16's screenshots were read at both widths and in both languages.

---

### Task 10: Docs (CLAUDE.md, robot app README), then the deploy and the live checks

Spec section 6 (Part A: docs; Part C: the vault and the flaw list), plus the deploy of spec A4 and the live and robot
checks of spec section 5 (Part B). No code and no code tests change. Runs **after Tasks 1-9 are committed**, because
the docs name what those tasks built and the checks run it. The docs check is a small script that (a) finds every
name the docs use in the code and (b) finds the new text in the docs and checks the line endings.

Part A is Steps 1-7, Part B Steps 8-13, Part C Step 14. Part B uses real keys, the running stack and the real robot,
and parts of it need the user (a person in front of the robot, a logged-in browser): ask on Telegram before those
steps, and report any step that could not run as skipped, never as passed.

**Files:**
- Create (outside the repo, never committed): `C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/task10_check.py`
  (Part A) and `.../scratchpad/task10_live.py` (Part B).
- Outside the repo, not committed (Part C): the vault `C:\Reachy_Mini\knowledge\reachy_mini_brain` and
  `C:\Reachy_Mini\repo\flaw.md`.
- Modify: `CLAUDE.md` (CRLF line endings, one paragraph per line, no wrapping). Line numbers as of commit `37ba84e`:
  - line 89, end of the "Stream stalls of ~1 s" bullet in "Reachy robot (Phase 2)": insert 5 bullets after it (Edit A).
  - line 112, the "Model calls." bullet in "Reachy check-in conversations": two changes inside the line (Edits B1, B2),
    then 2 bullets inserted after it (Edit C).
  - line 290, Key Env Vars table, the `LLM_FALLBACK_MODEL` row: insert an `OPENROUTER_API_KEY` row before it (Edit D).
- Modify: `reachy_app/README.md` (CRLF, wrapped at about 118 characters):
  - after line 77 (`While a reminder runs, the settings page also shows the live **Camera landmarks** rate.`): new section
    "## When the camera stops" (Edit E).
  - line 81, first line of "## Status": insert the 0.6.0 line before it (Edit F).
- Test: none in the repo. Verification is `task10_check.py code` (names exist in the code), `task10_check.py docs`
  (the docs carry them, CRLF intact), and a read-back of every changed section.

Every edit is an exact string replacement with the Edit tool. Each `old_string` below was checked to occur exactly once
in its file at `37ba84e`. Tasks 1-9 don't touch these two files; Step 1 confirms that.

**Interfaces:**
- Consumes (names the docs quote; all from the interface contract and Tasks 1-9):
  - Robot app: `bridge/camera_watch.py` `CameraWatchdog` (`attach()`, `detach()`, `frame()`, `tick()`, `state`,
    `snapshot()` -> `{"state", "fps", "since", "media"}`), `CAMERA_STALL_SECONDS = 2.0`,
    `CAMERA_RECOVER_INTERVAL = 5.0`, `CAMERA_GIVE_UP_SECONDS = 15.0`, module logger; `ReachyRobot.reopen_camera()`
    -> `"acquired" | "reopened" | "failed: <short reason>"`; `ReachyRobot.media_status()` -> `dict | None`
    (`GET http://127.0.0.1:8000/api/media/status`); `MonitorStream.camera`; `bridge/session.py` ending `SEARCHING`
    with `not_found` and `{"reason": "camera_unavailable"}`; `BridgeService._restart_lock`; version `0.6.0`.
  - Server: `api_device.CameraReport`, `HeartbeatPayload.camera` stored as `status_detail["camera"]`;
    `/api/reachy/status` `camera`; `MonitorSession.started_mode`, `monitor_service.openvino_only(state)`,
    `monitor_service._log_identity(...)`; `ai_settings.job_usable(snapshot, job)`, `public()["jobs_usable"]`,
    `Target.thinking`; `ai_store.fill_openrouter_keys_from_env`; `ai_profile.NOT_CHAT_MODEL`, `THINKING_LEVELS`,
    `ALLOWANCE`, `effective_level`, `allowance`, `detect_thinking`, `request_body(..., level=)`, profile key
    `thinking: {param, levels, ms, always, temperature}`; conversation metrics `reasoning_tokens`, `level`, error
    `thinking_budget`; `PUT /api/ai/jobs` `thinking`; `ai_job.thinking`.
  - Frontend strings (Tasks 3, 6 and 9): "Robot camera not working" / 「機器人鏡頭無法使用」, "No usable model: this job
    can't run.", "Can think" / "Can't think".
- Produces: nothing for later tasks (last task).

- [ ] **Step 1: Write the check script and confirm every name exists in the code**

Create `C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/task10_check.py`
with exactly this content (it is a scratch tool; never add it to the repo):

```python
"""Task 10 check. `code`: every name the new docs use exists in the code (Tasks 1-9). `docs`: the docs carry them,
the replaced sentence is gone, and both files still have CRLF line endings only.

    python task10_check.py code|docs [repo root]
"""
import sys
from pathlib import Path

ROOT = Path(sys.argv[2] if len(sys.argv) > 2 else r"C:/Reachy_Mini/repo/MedAiCarePlus")

# (file, needles); a tuple needle passes when any one of its forms is present.
CODE = [
    ("reachy_app/medcare_reachy/bridge/camera_watch.py", [
        "class CameraWatchdog", "CAMERA_STALL_SECONDS = 2.0", "CAMERA_RECOVER_INTERVAL = 5.0",
        "CAMERA_GIVE_UP_SECONDS = 15.0", "getLogger(__name__)", "def attach(", "def detach(", "def frame(", "def tick(", "def snapshot(",
        '"idle"', '"ok"', '"stalled"', '"recovering"', '"unavailable"']),
    ("reachy_app/medcare_reachy/bridge/media.py", [
        "def reopen_camera(", "def media_status(", '"acquired"', '"reopened"', "failed: ",
        '"127.0.0.1"', "DAEMON_PORT = 8000", "/api/media/status"]),
    ("reachy_app/medcare_reachy/bridge/runner.py", [
        "CameraWatchdog", "self.camera", ".camera.frame(", ".camera.tick", ".camera.snapshot()"]),
    ("reachy_app/medcare_reachy/bridge/session.py", ["camera_unavailable", ".camera.state"]),
    ("reachy_app/medcare_reachy/service.py", ["_restart_lock"]),
    ("reachy_app/pyproject.toml", ['version = "0.6.0"']),
    ("reachy_app/medcare_reachy/bridge/__init__.py", ['__version__ = "0.6.0"']),
    ("app/routers/api_device.py", ["class CameraReport(BaseModel)", "camera: CameraReport | None = None"]),
    ("app/routers/api_reachy.py", ['"camera"']),
    ("app/services/monitor_service.py", [
        "started_mode", "def openvino_only(", "def _log_identity(", '"openvino_only"', '"with_mediapipe"']),
    ("app/services/ai_settings.py", ["def job_usable(", '"jobs_usable"', "thinking"]),
    ("app/services/ai_store.py", ["async def fill_openrouter_keys_from_env("]),
    ("app/main.py", ["fill_openrouter_keys_from_env(", "job_usable("]),
    ("app/services/ai_profile.py", [
        'NOT_CHAT_MODEL = "not_chat_model"', 'THINKING_LEVELS = ("low", "medium", "high")',
        'ALLOWANCE = {"low": 800, "medium": 1600, "high": 3000}', "def effective_level(", "def allowance(",
        "def detect_thinking(", '"openrouter_effort"', 'level: str = "off"']),
    ("app/services/conversation.py", ['"reasoning_tokens"', '"thinking_budget"', "allowance("]),
    ("app/routers/api_ai.py", ["thinking", ("NOT_CHAT_MODEL", "not_chat_model")]),
    ("sql/init.sql", ["ADD COLUMN IF NOT EXISTS thinking VARCHAR(10) NOT NULL DEFAULT 'off'"]),
    ("frontend_source/src/i18n.ts", [
        "Robot camera not working", "機器人鏡頭無法使用", "No usable model: this job can't run.",
        "沒有可用的模型：這項工作無法執行。", "Can think", ("Can't think", "Can\\'t think", "Can’t think"),
        "可思考", "不可思考"]),
    ("frontend_source/src/lib/reachy-api.ts", ["RobotCamera"]),
    ("frontend_source/src/lib/ai-connections-api.ts", ["jobs_usable", "thinking"]),
]

CLAUDE_MD = [
    "**Robot app 0.6.0: the camera watchdog**", "`CameraWatchdog`", "`medcare_reachy.bridge.camera_watch`", "`CAMERA_STALL_SECONDS` (2 s)",
    "`CAMERA_RECOVER_INTERVAL` (5 s)", "`CAMERA_GIVE_UP_SECONDS` (15 s)", "`ReachyRobot.reopen_camera()`",
    "`ReachyRobot.media_status()`", '{"reason": "camera_unavailable"}', "`_restart_lock`",
    "**The TCU companion takes the camera away.**", 'media_backend="no_media"', "`monitor_service.openvino_only`",
    "`MonitorSession.started_mode`", "`_log_identity`", "`CameraReport`", 'status_detail["camera"]',
    "Robot camera not working", "**Deploying robot app 0.6.0.**", "`curl -s -X POST localhost:8000/api/apps/restart-current-app`", "**Thinking level per job**",
    "`ai_profile.detect_thinking`", "`ai_profile.effective_level`", "`ai_profile.ALLOWANCE`", "`Target.thinking`",
    "`thinking_budget`", "`reasoning_tokens`", "**AI card checks**", "`ai_settings.job_usable`", "`jobs_usable`",
    "`ai_store.fill_openrouter_keys_from_env`", "`not_chat_model`", "`ai_profile.NOT_CHAT_MODEL`",
    "except that `OPENROUTER_API_KEY` still fills", "the thinking levels it accepts) is its saved *profile*",
    "| `OPENROUTER_API_KEY` |"]
README = ["## When the camera stops", "- **0.6.0**:", "`camera_unavailable`", "reachy-ai-companion",
          'media_backend="no_media"', "/api/media/acquire", "Robot camera not working"]
GONE = [("CLAUDE.md", "after that those lines are ignored (edit them on the card);")]


def text(path: str) -> str:
    file = ROOT / path
    return file.read_text(encoding="utf-8") if file.is_file() else ""


def missing(path: str, needles) -> list[str]:
    body, out = text(path), []
    for needle in needles:
        forms = needle if isinstance(needle, tuple) else (needle,)
        if not any(form in body for form in forms):
            out.append(f"MISSING {path}: {' | '.join(forms)}")
    return out


def soft() -> list[str]:
    """Clauses the docs drop when the code doesn't do them (Task 10 Step 1 says which sentence)."""
    notes = []
    if text("reachy_app/medcare_reachy/bridge/runner.py").count("_slow_camera_logged = False") < 2:
        notes.append("SOFT runner.py never resets _slow_camera_logged after a recovery: drop clause S1")
    if "disabled = true" not in text("reachy_app/medcare_reachy/static/main.js"):
        notes.append("SOFT static/main.js never disables its buttons: drop clause S2")
    return notes


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "code":
        problems = [line for path, needles in CODE for line in missing(path, needles)]
        count = sum(len(needles) for _, needles in CODE)
        for note in soft():
            print(note)
    elif mode == "docs":
        problems = missing("CLAUDE.md", CLAUDE_MD) + missing("reachy_app/README.md", README)
        problems += [f"STILL THERE {path}: {old}" for path, old in GONE if old in text(path)]
        for path in ("CLAUDE.md", "reachy_app/README.md"):
            data = (ROOT / path).read_bytes()
            crlf, lf = data.count(b"\r\n"), data.count(b"\n")
            if crlf != lf:
                problems.append(f"LINE ENDINGS {path}: {lf - crlf} bare LF line(s)")
        count = len(CLAUDE_MD) + len(README) + len(GONE) + 2
    else:
        print(__doc__)
        return 2
    for line in problems:
        print(line)
    print(f"{mode}: {count - len(problems)} of {count} checks pass" if problems else f"{mode}: all {count} checks pass")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
```

Run (Git Bash):

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && git status --short && git log --oneline -12 && git diff 37ba84e --stat -- CLAUDE.md reachy_app/README.md
.venv/Scripts/python.exe "C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/task10_check.py" code; echo "exit=$?"
```

Expected: a clean working tree; Tasks 1-9's commits in the log; no diff in `CLAUDE.md` or `reachy_app/README.md`
since `37ba84e` (if there is one, re-check that each `old_string` below still occurs exactly once with
`grep -cF -- '<old_string>' <file>`); `code: all 72 checks pass`, `exit=0`, and no `SOFT` line.

When a line says `MISSING`, the code uses another name than the contract. Read that file, find the real name, and use
the real name in the doc text of Steps 3-4 (the code wins). Also change the needle in the script's `CLAUDE_MD` list
when that name is one of its entries, and say so in the commit message body. Never change code in this task.

When a line says `SOFT ... clause S1`: in Edit A, delete the words `, and the slow-camera warning (below 12 fps) can
fire again` (the sentence then ends `...with the stall's length.`). When it says `SOFT ... clause S2`: in Edit A,
delete the words `, and the settings page disables Save and Restart while a request is pending` (the sentence then ends
`...never leave two bridge threads running.`). (S2 checks for the text `disabled = true` in `static/main.js`. If the
buttons are disabled another way, for example `.disabled = pending`, read `static/main.js` and keep the clause when
Save and Restart really are disabled while a request is pending.)

- [ ] **Step 2: Run the docs check and watch it fail**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && .venv/Scripts/python.exe "C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/task10_check.py" docs; echo "exit=$?"
```

Expected: FAIL, `exit=1`, with `MISSING CLAUDE.md: ...` lines for the new names, `MISSING reachy_app/README.md: ...`
lines, and `STILL THERE CLAUDE.md: after that those lines are ignored (edit them on the card);`. No `LINE ENDINGS`
line (both files are all CRLF now).

- [ ] **Step 3: Edit CLAUDE.md**

Use the Edit tool on `C:/Reachy_Mini/repo/MedAiCarePlus/CLAUDE.md` (Read it first). Each `old_string` is one whole
line or part of one line; each `new_string` keeps the old text and adds to it. The two-space indent of sub-bullets
matches the file's other nested bullets.

**Edit A** (line 89: the camera watchdog, the TCU companion clash, the search identity rule, the camera state on the
card, and the 0.6.0 deploy).

old_string:

```text
The server logs any frame it held up over 0.25 s (`monitor frame N of session ... took`).
```

new_string:

```text
The server logs any frame it held up over 0.25 s (`monitor frame N of session ... took`).
- **Robot app 0.6.0: the camera watchdog** (`bridge/camera_watch.py`, `CameraWatchdog`). Before 0.6.0, a camera that stopped gave one stale frame and then `None` forever; the app logged nothing and searched for the full 10 minutes (`SEARCH_TIMEOUT`).
  - While a monitor session is attached, the stream (`MonitorStream.camera`, `bridge/runner.py`) calls the watchdog's `frame()` for every new frame and `tick()` when none came. Only a frame the SDK returns counts. A slow camera (5 fps) is not a stall: only a 2 s gap is.
  - **Stall:** no new frame for `CAMERA_STALL_SECONDS` (2 s). The app logs one ERROR line per stall, with the daemon's `GET /api/media/status` answer (`ReachyRobot.media_status()`).
  - **Recovery**, at most once per `CAMERA_RECOVER_INTERVAL` (5 s): `ReachyRobot.reopen_camera()` asks the daemon for the media back when it says the media is released or unavailable (result `acquired`), else reopens the app's own camera reader (`reopened`). Either way the SDK builds the app's media link again, and that link also carries the speaker and the microphone: after a recovery on the real robot, check that Reachy still speaks and hears. A failed attempt (`failed: <reason>`) is logged and never stops the stream or the session.
  - **Recovered:** the first new frame after a stall logs one INFO line with the stall's length, and the slow-camera warning (below 12 fps) can fire again.
  - **Give up:** no frame `CAMERA_GIVE_UP_SECONDS` (15 s) after the stall began gives the state `unavailable`. A search (`SEARCHING`) then ends as `not_found` with detail `{"reason": "camera_unavailable"}` (the not_found path also adds `outcomes`), and Reachy goes to sleep as for any `not_found`. `camera_unavailable` never records or skips a dose by itself: for a dose task the server's missed-dose path decides, as for any `not_found`. `unavailable` outlasts the search, so the heartbeats and the card keep reporting it, until the next session starts and tries the camera afresh.
  - The watchdog's log lines come from the logger `medcare_reachy.bridge.camera_watch`.
  - **Restarts:** `BridgeService.restart()` runs under `_restart_lock`, so two overlapping restarts (two quick Saves) never leave two bridge threads running, and the settings page disables Save and Restart while a request is pending.
- **The TCU companion takes the camera away.** TCU's Reachy AI Companion (the `reachy-mini-tcu` project, systemd unit `reachy-ai-companion`) connects with `media_backend="no_media"`. In reachy_mini 1.11.0 (`ReachyMini._configure_mediamanager`) such a connection makes the daemon release the camera and audio for direct use, and nothing asks for them back, so MedCare Reachy's camera reader (GStreamer `unixfdsrc`) stops. Don't run both: on our robot the companion is disabled at boot (`sudo systemctl disable --now reachy-ai-companion`). Both also serve their page on port 8042. On the robot, `curl -s localhost:8000/api/media/status` shows `"released": true` when a program took the media, and `curl -s -X POST localhost:8000/api/media/acquire` gives it back; during a session the 0.6.0 watchdog does that itself.
- **Finding the patient before talking: OpenVINO alone** (`monitor_service.openvino_only`). A robot session that *started* in `observe` mode (the search before a check-in or before prompting a dose) verifies the patient with the OpenVINO match alone: exactly one face labelled as the patient, fewer than 4 OpenVINO faces (`saturated` false), and 2 hits in a row whose boxes overlap by at least 0.20. It needs no MediaPipe face box on the same frame. Every other session keeps the full rule, which also needs a MediaPipe face overlapping the match by at least 0.20 and fewer than 4 MediaPipe faces: dose sessions, browser sessions, and a dose session that switched to `observe` after recording. `MonitorSession.started_mode` keeps the mode a session started in, because `mode` changes after a record. Why: the server's MediaPipe short-range face detector misses faces under about 50 px wide in the robot's 480x360 frame, so the robot could not verify a patient further than about 1.35 m, while dashboard login (OpenVINO alone) worked. A search session still never records a dose, and the dose prompt still waits for verification in that session.
  - **Identity log** (`_log_identity`): for robot sessions, one INFO line at every `identity_status` change, else at most one per 5 s per session: session id, status, rule (`openvino_only` or `with_mediapipe`), best patient distance, patient box width in pixels, and the OpenVINO and MediaPipe face counts. No images, and no labels other than patient/other.
- **Robot camera state on the Reachy card.** From robot app 0.6.0, every heartbeat carries `camera` (`CameraWatchdog.snapshot()`): `state` (`idle`, `ok`, `stalled`, `recovering` or `unavailable`), `fps`, `since` (when the state last changed) and `media` (the daemon's media status, or null). `idle` means no session is attached, so 0 fps is normal then. The server validates it (`CameraReport` in `api_device.py`; an unknown state is a 422), stores it as `reachy_device.status_detail["camera"]`, and `/api/reachy/status` returns it as `camera`. The card shows "Robot camera not working" (「機器人鏡頭無法使用」) while the robot is online and the state is `stalled`, `recovering` or `unavailable`, and the camera fps while a session runs. Robots older than 0.6.0 send no `camera`, and the card then shows nothing new. A task's `camera_unavailable` detail is stored with the task like any status detail.
- **Deploying robot app 0.6.0.** From `reachy_app/`: `python tools/deploy_to_robot.py --host <the robot's address>` (user `pollen`; the SSH password comes from `REACHY_SSH_PASSWORD`, or it asks). It backs up the installed app, uploads the package, installs faster-whisper and Whisper Base, and renders changed clips. On 11 Oct 2026 the robot ran 0.5.4 with an older SenseVoice `voice.py`; the upload replaces it with the repo's Whisper Base version. Then restart the app through the daemon, on the robot: `curl -s -X POST localhost:8000/api/apps/restart-current-app` (when the app isn't running: `curl -s -X POST localhost:8000/api/apps/start-app/medcare_reachy`). Heartbeats then report `bridge_version` 0.6.0. Robot checks: one real check-in (frames at the camera rate, the patient verified, the chat runs), then a camera failure: `curl -s -X POST localhost:8000/api/media/release` during a search must end in a recovery, or in `camera_unavailable` within 15 s with the camera warning on the card.
```

**Edit B1** (line 112: the profile now also holds the thinking levels).

old_string:

```text
Each model's request shape (`max_tokens` vs `max_completion_tokens`, temperature, reasoning off) is its saved *profile*, found by the card's Test button (`ai_profile.py`).
```

new_string:

```text
Each model's request shape (`max_tokens` vs `max_completion_tokens`, temperature, reasoning off, the thinking levels it accepts) is its saved *profile*, found by the card's Test button (`ai_profile.py`).
```

**Edit B2** (line 112: `.env`'s OpenRouter key is no longer ignored after the first start).

old_string:

```text
are copied in (`ai_store.seed_from_env`); after that those lines are ignored (edit them on the card);
```

new_string:

```text
are copied in (`ai_store.seed_from_env`); after that those lines are ignored (edit them on the card), except that `OPENROUTER_API_KEY` still fills any OpenRouter connection without a key at every start (see AI card checks below);
```

**Edit C** (after line 112: thinking per job, and the AI card checks).

old_string:

```text
Tests patch `conversation._http_session`, not `requests.post`.
```

new_string:

```text
Tests patch `conversation._http_session`, not `requests.post`.
- **Thinking level per job** (Oct 2026; spec `docs/superpowers/specs/2026-10-11-robot-camera-identity-thinking-design.md`). Each job has a level: `off` (the default), `low`, `medium` or `high` (`ai_job.thinking`; the job row's "Thinking" select on the card; `PUT /api/ai/jobs` takes `thinking` per job, and anything else is a 422). `off` sends exactly the request from before (the profile's `reasoning_off` included), so nothing changes until someone picks a level.
  - **What a model can do** is found by Test and saved in its profile as `thinking: {param, levels, ms, always, temperature}`. The probes run in `ai_profile.detect_thinking`, apart from the base test, so a probe that fails or times out never costs the model its base profile. OpenAI-style connections: one request with an invalid `reasoning_effort` whose 400 lists "Supported values are: ...", else one probe per level; a model that refuses `reasoning_effort` altogether can't think. OpenRouter: the model's public `GET {base_url}/models` entry (`reasoning.supported_efforts`, `mandatory`). Each listed level then gets one timed probe (`ms`); a level that comes back empty or cut off is not offered. So Test costs a few more calls and seconds. A profile without `thinking` (an older Test) means "can't think".
  - **Requests** (`ai_profile.request_body(..., level=)`): OpenAI-style gets `reasoning_effort: <level>` and **no `temperature`** (with thinking on, these models refuse it). OpenRouter gets `reasoning: {"effort": <level>, "exclude": true}`, plus `temperature` only when the model's entry lists it (`thinking.temperature`). Memory extraction's `temperature=0` is dropped too when thinking is on.
  - **Main and backup can differ:** each model gets the job's level if it accepts it, else the nearest lower level it accepts, else `off` (`ai_profile.effective_level`; `Target.thinking` is the level actually sent). A model that refuses `reasoning_effort: none` (GPT-6 Astra, 6.1 Sol) has `thinking.always`: its `off` is its lowest level, never "no setting", which would leave it thinking at its default level inside a 200-token cap.
  - **Token allowances:** a call's cap is the job's base cap (reply 200, risk 32, summary 400, memory 1000) plus `ai_profile.ALLOWANCE` for the level sent: low +800, medium +1600, high +3000 (an `always` model gets its lowest level's allowance even at `off`). Timeouts and deadlines don't change, so the card shows the main model's measured time at the chosen level and warns when it is over the job's per-call limit (reply 6 s, safety 2.5 s, after-chat 12 s): Reachy may then say its fixed line.
  - **Measuring:** each attempt's metrics add `reasoning_tokens` (from `usage.completion_tokens_details`) and `level`. An empty answer cut off by the length limit whose reasoning tokens used every completion token is the error `thinking_budget` ("thinking used the whole budget"), in the attempt and in the model's `last_error`.
  - Each model row on the card says "Can think: low · medium · high" or "Can't think". Thinking doesn't change the notice fills (same models, same companies).
- **AI card checks** (Oct 2026).
  - **A job with no usable model.** A model is usable for a job when it has a saved profile and its connection has a key that can be read (`ai_settings.job_usable`). `public()` adds `jobs_usable`, and the card warns under each job without a usable main or backup model: "No usable model: this job can't run.", plus, for `safety`, that Reachy's model safety check is off, and for `after_chat`, that summaries and the summary safety check are off. At startup the server logs one WARNING per such job. The older `no_key_in_use` warning (no job has a key) stays.
  - **`.env`'s OpenRouter key fills keyless OpenRouter connections at every start** (`ai_store.fill_openrouter_keys_from_env`, in the lifespan after `seed_from_env`, in one `change()`). Only connections of kind `openrouter` with no key get it, encrypted as usual; a connection that has a key is never touched, and the key is never logged. The card stays the place to change keys. Why: the first start had copied an empty key from `.env`, so safety layers 2 and 3 never ran, and the card didn't warn, because its warning fires only when no job has a key.
  - **Voice-only models.** A Test whose 404 or 400 says the model is not a chat model ("not a chat model", or "not supported in the v1/chat/completions endpoint") gives the error `not_chat_model` (`ai_profile.NOT_CHAT_MODEL`, also stored as `last_error`), and the card says it is a voice-only model that can't write Reachy's text replies. gpt-live-1 is one: it works only over OpenAI's Live WebSocket. Live voice check-ins with GPT-Live, with the camera kept on during the chat, are Phase 2 of that spec (their own spec after a measurement spike; not built).
```

**Edit D** (line 290: the Key Env Vars table gets the OpenRouter key).

old_string:

```text
| `LLM_FALLBACK_MODEL` | Copied into the AI connections card on the first start, then ignored (edit models on the card) | `google/gemini-2.5-flash` |
```

new_string:

```text
| `OPENROUTER_API_KEY` | OpenRouter key. Copied into the AI connections card's OpenRouter connection on the first start; at every start it also fills any OpenRouter connection that has no key (never one that has). Secret, `.env` only | empty |
| `LLM_FALLBACK_MODEL` | Copied into the AI connections card on the first start, then ignored (edit models on the card) | `google/gemini-2.5-flash` |
```

- [ ] **Step 4: Edit reachy_app/README.md**

Use the Edit tool on `C:/Reachy_Mini/repo/MedAiCarePlus/reachy_app/README.md` (Read it first). This file is also the
Hugging Face space's page, so it stays in plain words for robot owners.

**Edit E** (after line 77: what happens when the camera stops, and the TCU companion).

old_string:

```text
While a reminder runs, the settings page also shows the live **Camera landmarks** rate.
```

new_string:

```text
While a reminder runs, the settings page also shows the live **Camera landmarks** rate.

## When the camera stops

From 0.6.0 the app watches its camera while it looks for the patient or watches a dose:

- **No new frame for 2 seconds** is a stall. The app logs an error with the robot daemon's media status and tries to
  get the camera back, at most once every 5 seconds: when the daemon says the camera was released, the app asks for it
  back; otherwise it reopens its own camera feed. A slow camera is not a stall; only a 2-second gap is.
- **Still no frame 15 seconds after the stall began:** the app ends the search with the reason `camera_unavailable`
  and puts the robot to sleep. The server then handles the reminder as for a patient it could not find; the camera
  alone never records or skips a dose.
- The heartbeat tells the server the camera state, and the web app's Reachy card shows "Robot camera not working".

**Don't run TCU's Reachy AI Companion at the same time.** It connects to the robot without media
(`media_backend="no_media"`), and Reachy Mini's SDK 1.11.0 then makes the daemon release the camera and audio. This
app's camera feed stops, and nothing asks for the camera back. Disable the companion at boot over SSH
(`sudo systemctl disable --now reachy-ai-companion`); it also uses this app's port, 8042. To check the camera over
SSH: `curl -s localhost:8000/api/media/status` shows `"released": true` when another program took it, and
`curl -s -X POST localhost:8000/api/media/acquire` gives it back.
```

**Edit F** (line 81: the 0.6.0 status line, first in the list).

old_string:

```text
- **0.3.0**: emotion is scored on the robot too; snapshots to the server are identity-only, 2 a second (was 5).
```

new_string:

```text
- **0.6.0**: a camera watchdog (above): a stalled camera is recovered, or the search ends with `camera_unavailable`
  within 15 s instead of searching for 10 minutes. The heartbeat reports the camera state. Two quick restarts from the
  settings page no longer start two copies of the app's worker.
- **0.3.0**: emotion is scored on the robot too; snapshots to the server are identity-only, 2 a second (was 5).
```

- [ ] **Step 4b: Add `camera` to the docs that list the heartbeat fields (controller ruling R2)**

Run `grep -rn "missing_clips" docs` from the repo root. Today it finds `docs/ARCHITECTURE.md`, `docs/DATA_FLOW.md`,
`docs/MODELS.md`, `docs/NEW_LAPTOP_SETUP.md` and `docs/reachy-data-transfer/02-data-contracts-and-failure-modes.md`.
In each place that LISTS the heartbeat's fields (not every mention of `missing_clips`), add the new optional field in
that document's own style, for example: "`camera` (robot app 0.6.0 and later: `{state, fps, since, media}`; state is
idle, ok, stalled, recovering or unavailable)". Leave places that only mention one field alone. Keep each file's
line endings. Add every changed file to Step 7's `git add`.

- [ ] **Step 5: Run the docs check and watch it pass**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && .venv/Scripts/python.exe "C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/task10_check.py" docs; echo "exit=$?"
.venv/Scripts/python.exe "C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/task10_check.py" code; echo "exit=$?"
file CLAUDE.md reachy_app/README.md
```

Expected: `docs: all 46 checks pass`, `exit=0`; `code: all 72 checks pass`, `exit=0`; `file` prints
`with CRLF line terminators` for both and no "mixed" wording.

When the docs check prints `LINE ENDINGS <file>: N bare LF line(s)` (the Edit tool wrote `\n` inside a CRLF file),
make that file all-CRLF again, then rerun the check:

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && .venv/Scripts/python.exe -c "import sys; p=sys.argv[1]; d=open(p,'rb').read().replace(b'\r\n',b'\n').replace(b'\n',b'\r\n'); open(p,'wb').write(d)" CLAUDE.md
```

(use `reachy_app/README.md` as the argument for the README). This is safe: both files were all-CRLF before the edits.

- [ ] **Step 6: Read back every changed section and check each name**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && git diff --stat && git diff -U0 CLAUDE.md reachy_app/README.md | sed 's/\r$//'
```

Expected: `2 files changed, 48 insertions(+), 1 deletion(-)`, only `CLAUDE.md` and `reachy_app/README.md`.
CLAUDE.md: 26 insertions and 1 deletion (25 new lines, plus the line 112 bullet changed in place). README.md: 22
insertions. Dropping clause S1 or S2 shortens a line and doesn't change these counts.

Read the diff line by line and tick each against the contract and Tasks 1-9 (open the code file when unsure):
- Robot app: `CameraWatchdog`, `bridge/camera_watch.py`, `frame()`, `tick()`, `snapshot()` and its four keys, the
  five states, `CAMERA_STALL_SECONDS` 2 s, `CAMERA_RECOVER_INTERVAL` 5 s, `CAMERA_GIVE_UP_SECONDS` 15 s,
  `ReachyRobot.reopen_camera()` and its three results, `ReachyRobot.media_status()`, `MonitorStream.camera`,
  `{"reason": "camera_unavailable"}` in `SEARCHING`, `_restart_lock`, `0.6.0`. In `reachy_app/medcare_reachy/bridge/media.py`,
  read `reopen_camera()`: the docs say `acquired` follows a released or unavailable daemon status and `reopened`
  otherwise. If Task 2 decided differently, change Edit A's Recovery sentence and README Edit E's first bullet to
  say what the code does.
- Server: `MonitorSession.started_mode`, `monitor_service.openvino_only`, `_log_identity`, the rule names
  `openvino_only` / `with_mediapipe`, `CameraReport`, `status_detail["camera"]`, `/api/reachy/status` `camera`.
- AI: `ai_job.thinking`, `PUT /api/ai/jobs` `thinking`, `ai_profile.detect_thinking`, the profile's
  `thinking: {param, levels, ms, always, temperature}`, `request_body(..., level=)`, `effective_level`,
  `Target.thinking`, `ALLOWANCE` 800/1600/3000, `reasoning_tokens`, `level`, `thinking_budget`,
  `ai_settings.job_usable`, `jobs_usable`, `ai_store.fill_openrouter_keys_from_env`, `ai_profile.NOT_CHAT_MODEL` /
  `not_chat_model`.
- UI words: "Robot camera not working", 「機器人鏡頭無法使用」, "No usable model: this job can't run.", "Can think",
  "Can't think": copy them from `frontend_source/src/i18n.ts` if the wording there differs.
- Numbers from the existing code that the text repeats: base caps reply 200 (`conversation.py` `REPLY_MAX_TOKENS`),
  risk 32 (`RISK_MAX_TOKENS`), summary 400 (`SUMMARY_MAX_TOKENS`), memory 1000 (`memory.py`, `max_tokens=1000`);
  per-call limits 6 s (`CALL_TIMEOUT`), 2.5 s (`RISK_CALL_TIMEOUT`), 12 s (`SUMMARY_CALL_TIMEOUT`);
  `SEARCH_TIMEOUT = 600.0` in `bridge/session.py`.

Any fix goes back through the Edit tool, then rerun Step 5.

- [ ] **Step 7: Commit**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && git add CLAUDE.md reachy_app/README.md && git commit -F - <<'EOF'
Docs: camera watchdog, search identity rule, thinking per job, AI card checks (robot app 0.6.0)

CLAUDE.md: the robot app's camera watchdog and restart lock, the TCU companion's
camera release, the OpenVINO-only rule for the search before talking and its log,
the camera state on the Reachy card, deploying 0.6.0, thinking per job with token
allowances and the temperature rule, the per-job usability warning, the OpenRouter
key fill from .env and the not_chat_model Test result.
reachy_app/README.md: 0.6.0 status line, what happens when the camera stops, and
why the TCU companion must not run at the same time.

Co-Authored-By: <the model that wrote the commit> <noreply@anthropic.com>
EOF
git log --oneline -1 && git status --short
```

Replace `<the model that wrote the commit>` with your own model name (global constraints), for example
`Claude Opus 5.5 (1M context)`. Expected: one new commit, a clean working tree.

#### Part B: rebuild, live checks on this PC, deploy 0.6.0 and the robot checks (spec A4, section 5)

**Controller ruling R3: Parts B and C are run by the controller after the final whole-branch review, not by the Task 10 implementer.** The implementer does Part A (Steps 1-7, including 4b) and stops.

Nothing in Part B changes a repo file. `S` is the scratchpad
(`C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad`).
The running stack is the `medcareai2` compose project in `C:/Reachy_Mini/repo/MedAiCarePlus` (app on
http://localhost:8080). The robot is at `192.168.137.235`, user `pollen`. The robot's SSH password goes only into the
command line as `REACHY_SSH_PASSWORD=<robot SSH password>` (the value the orchestrator gives); never into a file, a log
line or a commit.

- [ ] **Step 8: Rebuild the stack and check the start (C1, C2, D1)**

Create `$S/task10_live.py` with exactly (a scratch tool, never committed; it prints no key):

```python
"""Task 10 live checks, run inside the app container. Prints no key.

    python /tmp/task10_live.py view               # connections (key or not), jobs_usable, jobs, each model's thinking
    python /tmp/task10_live.py reply [model_id]   # one real reply at "low" (the reply job's main model, or model_id)
"""
import asyncio
import dataclasses
import sys

from app import database
from app.services import ai_settings, ai_store, conversation


async def main(mode: str, model_id: str | None) -> None:
    await database.init_pool()   # applies sql/init.sql, as every app start does (idempotent)
    try:
        async with database.get_pool().acquire() as conn:
            loaded = await ai_store.load(conn)
        if mode == "view":
            view = ai_settings.public(loaded)
            for c in view["connections"]:
                print("connection", c["connection_id"], c["name"], c["kind"], "has_key", c["has_key"],
                      "readable", c["key_readable"])
            print("jobs_usable", view["jobs_usable"])
            print("jobs", view["jobs"])
            for m in view["models"]:
                print("model", m["model_ref"], m["model_id"], "tested", m["tested"], "test_ms", m["test_ms"],
                      "last_error", m["last_error"], "thinking", (m["profile"] or {}).get("thinking"))
            return
        # In memory only: the saved jobs and levels stay as the user set them.
        jobs = dict(loaded.jobs)
        if model_id:
            ref = next(ref for ref, model in loaded.models.items() if model.model_id == model_id)
            jobs["reply"] = (ref, None)
        ai_settings.set_current(dataclasses.replace(loaded, jobs=jobs, thinking={**loaded.thinking, "reply": "low"}))
        text, metrics = await conversation.reply_with_metrics([{"role": "patient", "text": "我今天去公園散步了"}],
                                                             "zh-TW")
        await asyncio.sleep(0.5)   # let the model's last-call note finish before the loop closes
        print("reply:", text)
        print("fallback_used:", metrics["fallback_used"], "llm_ms:", metrics["llm_ms"])
        for attempt in metrics["attempts"]:
            print({key: attempt.get(key) for key in ("model_requested", "status", "level", "tokens",
                                                      "reasoning_tokens", "finish_reason", "usable", "error", "ms")})
    finally:
        await database.close_pool()


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "view", sys.argv[2] if len(sys.argv) > 2 else None))
```

Then (Git Bash):

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && git status --short && docker compose up -d --build app
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/health
```

Repeat the `curl` every few seconds until it prints `200` (the first start after a build warms the models). Then:

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus
docker compose logs app --since 15m 2>&1 | grep -E "filled the key of|has no usable model|Traceback"
docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT job, thinking FROM ai_job ORDER BY job"'
docker compose cp "C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/task10_live.py" app:/tmp/task10_live.py
MSYS_NO_PATHCONV=1 docker compose exec -T -w /app app python /tmp/task10_live.py view
```

Expected (spec §5, live check 3):
- `git status` clean; the build ends with the `app` container running.
- The log has `filled the key of N OpenRouter connection(s) from .env` once when an OpenRouter connection had no key
  and `.env` has `OPENROUTER_API_KEY` (nothing when every OpenRouter connection already had one), no `has no usable
  model` WARNING, and no `Traceback`.
- `ai_job` has three rows, each `thinking` = `off`.
- `view`: every `openrouter` connection `has_key True`; `jobs_usable {'reply': True, 'safety': True, 'after_chat':
  True}`; each job entry has `'thinking': 'off'`.

If a job is still not usable, read why from `view` (no key, or an untested model) and tell the user: the key or the
model choice is theirs to fix on the card.

- [ ] **Step 9: Test three models on the card and save their thinking levels (spec §5, live check 1; costs a few
  cents)**

In the user's browser (claude-in-chrome; the patient account must be logged in, else ask the user to log in), open
http://localhost:8080/settings, go to the AI connections card and press **Test** on `gpt-5.4-mini` and `gpt-6-luna`
(OpenAI connection) and on one OpenRouter model in use (for example `google/gemini-2.5-flash`). A model not yet on the
card is added first with "Add model". If there is no OpenAI connection with a key, stop and ask the user: the key is
theirs.

Expected while each Test runs: the row says "Testing, including thinking levels…" / 「測試中，也會測試思考等級…」. After
it: its time, and "Can think: low · medium · high" for `gpt-5.4-mini` and `gpt-6-luna` (or the levels they really
accept), "Can't think" for `google/gemini-2.5-flash` (its OpenRouter entry lists no efforts). Take a 1280 px screenshot
of the card into `$S/t10-shots/`. Then confirm what was saved:

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && MSYS_NO_PATHCONV=1 docker compose exec -T -w /app app python /tmp/task10_live.py view
```

Expected: those three models `tested True`, `last_error None`, and `thinking` = `{'param': 'reasoning_effort', 'levels':
[...], 'ms': {...}, 'always': False, 'temperature': False}` for the two OpenAI models (with `ms` for each level),
`{'param': None, 'levels': [], ...}` for the OpenRouter one.

- [ ] **Step 10: One reply at `low` through the real stack (spec §5, live check 2)**

The level is set in memory only, so the user's saved setup is not changed. Use the reply job's main model when it can
think (`view` shows its `thinking.levels`), else name `gpt-5.4-mini`:

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && MSYS_NO_PATHCONV=1 docker compose exec -T -w /app app python /tmp/task10_live.py reply gpt-5.4-mini
```

Expected: a short Traditional Chinese reply (not the fixed fallback line), `fallback_used: False`, and the first
attempt `{'model_requested': 'gpt-5.4-mini', 'status': 200, 'level': 'low', 'reasoning_tokens': <a number>,
'usable': True, ...}` with `ms` under the reply's 6 s per-call limit. If it fails with `error 'thinking_budget'`,
report it: the allowance (+800) is too small for that model at `low`.

- [ ] **Step 11: Deploy robot app 0.6.0 (spec A4)**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus/reachy_app && REACHY_SSH_PASSWORD=<robot SSH password> ../.venv/Scripts/python.exe tools/deploy_to_robot.py --host 192.168.137.235
```

Expected: the tool backs up the installed app, uploads the package, installs faster-whisper and Whisper Base, renders
changed clips, and ends without a failure line. Then restart the app through the daemon and check what runs:

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && REACHY_HOST=192.168.137.235 REACHY_SSH_PASSWORD=<robot SSH password> .venv/Scripts/python.exe "C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/robot_ssh.py" \
  "/venvs/apps_venv/bin/python -c 'import medcare_reachy.bridge as b; print(b.__version__)'" \
  "grep -c SenseVoice /venvs/apps_venv/lib/python3.12/site-packages/medcare_reachy/bridge/voice.py" \
  "curl -s -X POST localhost:8000/api/apps/restart-current-app" \
  "curl -s localhost:8000/api/media/status"
```

Expected: `0.6.0`; `0` for SenseVoice (the older SenseVoice `voice.py` is gone, F15; `grep -c` exits 1 on no match,
which is fine); the restart answers; media status `{"available":true,"released":false,"no_media":false}`. If the app
is not running after the restart, start it: `curl -s -X POST localhost:8000/api/apps/start-app/medcare_reachy`.
About 30 s later, on this PC:

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "SELECT last_seen_at, status_detail FROM reachy_device WHERE revoked_at IS NULL"'
```

Expected: `last_seen_at` within the last minute, and `status_detail` with `"bridge_version": "0.6.0"` and
`"camera": {"state": "idle", ...}`.

- [ ] **Step 12: Robot check 1: one real check-in (spec §5; needs the patient in front of the robot)**

Ask the user on Telegram to sit in front of the robot, then start a check-in with the Reachy card's "Talk to Reachy
now" button (`POST /api/reachy/checkin`). Pass when: the card (reload `/settings`) shows the "Robot camera" row at the
camera's rate (about 10 fps) while the session runs and no camera warning; the server logs the patient verified with
the search rule; the chat runs (Reachy answers the patient). Logs:

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && docker compose logs app --since 15m 2>&1 | grep "identity of session"
REACHY_HOST=192.168.137.235 REACHY_SSH_PASSWORD=<robot SSH password> .venv/Scripts/python.exe "C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/robot_ssh.py" \
  "sudo journalctl -u reachy-mini-daemon --since -15min --no-pager | grep -E 'camera (stalled|recovery|recovered|unavailable)'"
```

Expected: a line `identity of session ...: status=verified rule=openvino_only ...`; no camera line on the robot (the
app runs under `reachy-mini-daemon`, so its log is in that unit's journal; if nothing of the app is there at all,
look with `sudo journalctl --since -15min --no-pager | grep -i medcare`).

- [ ] **Step 13: Robot check 2: the camera fails during a search (spec §5; review focus 1 and 3)**

Start another check-in with nobody in front of the robot, so it keeps searching. During the search, release the
daemon's media, then follow the logs for about 30 s:

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && REACHY_HOST=192.168.137.235 REACHY_SSH_PASSWORD=<robot SSH password> .venv/Scripts/python.exe "C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/robot_ssh.py" \
  "curl -s -X POST localhost:8000/api/media/release"
```

Then (after about 30 s):

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && REACHY_HOST=192.168.137.235 REACHY_SSH_PASSWORD=<robot SSH password> .venv/Scripts/python.exe "C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/robot_ssh.py" \
  "sudo journalctl -u reachy-mini-daemon --since -5min --no-pager | grep -E 'camera (stalled|recovery|recovered|unavailable)'" \
  "curl -s localhost:8000/api/media/status"
docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "SELECT reason, status, detail FROM reachy_task ORDER BY created_at DESC LIMIT 1"'
```

Pass when either:
- **recovered:** `camera stalled: no new frame for 2.x s ...` with `'released': True`, then `camera recovery: acquired
  with the SDK's release_media() and acquire_media() (... media backend MediaBackend.LOCAL)` and `camera recovered:
  ...`; media status `released` false again; the search goes on; and the next clip Reachy plays is heard (the media
  link also carries the speaker and microphone, review focus 1); or
- **gave up:** `camera unavailable: ...` within about 17 s of the last frame, and the task row `checkin | not_found |
  {"reason": "camera_unavailable", "outcomes": {}}`; the robot goes to sleep without an extra sentence.

In both cases, reload `/settings` while the camera is not working: the Reachy card shows "Robot camera not working" /
「機器人鏡頭無法使用」. Take a 390 px and a 1280 px screenshot into `$S/t10-shots/`. Afterwards make sure the robot has its
media: if `curl -s localhost:8000/api/media/status` still says `"released":true`, run `curl -s -X POST
localhost:8000/api/media/acquire` on the robot. A failed check is reported with its log lines, not retried blindly
(spec review focus 1: a wrong recovery can kill the app's audio).

#### Part C: the vault and the flaw list (spec section 6, last line)

- [ ] **Step 14: Update the vault and `flaw.md` (outside the repo, not committed)**

- The vault `C:\Reachy_Mini\knowledge\reachy_mini_brain` uses its own template: read its `HOME.md` first and follow it
  (append-only log, `BUG-NN` notes). Record what Phase 1 changed and the results of Steps 8-13 (pass, fail or skipped,
  with the measured numbers: the models' thinking times, the reply's time at `low`, the robot's camera fps and the
  recovery outcome).
- `C:\Reachy_Mini\repo\flaw.md`: add "Part 2: flaws found after the changes", one entry per flaw found in Steps 8-13 or
  in review (what, evidence, severity), or the line "none found" with the date.

#### NOTES

**Assumptions**
- Tasks 1-9 are committed before this task and use the contract's names. Step 1's `code` check proves it and says
  what to do for each mismatch (the code wins; never change code here).
- The UI words quoted in the docs are the spec's (sections B3, C1, D6). Step 1 checks they are in `i18n.ts`.
- The relayed user request ("add option 1 to phase 2") is read as the spec's Phase 2 line (spec line 4: GPT-Live
  check-ins with the camera kept on during the chat). The docs mention it in one sentence (Edit C, voice-only models)
  and build nothing for it.

**Deviation from the contract:** none. Two additions beyond spec section 6's list, both needed for the docs to stay
true:
- Edit B2 and Edit D: CLAUDE.md:112 said that after the first start `OPENROUTER_API_KEY` is ignored. The key fill (C2)
  makes that false, so the sentence gets an exception, and the Key Env Vars table (which had no `OPENROUTER_API_KEY`
  row) gets one.
- Edit A's deploy bullet (the brief asked for "how to deploy it").

**Risks for other tasks, found while reading (please check the Task 2 and Task 3 drafts):**
(Cross-check: items 1 and 3 are resolved in Task 2. Its `reopen_camera()` calls `release_media()` then
`acquire_media()`, and its tests use a fake whose `acquire_media()` does nothing unless released first;
`AppClient.heartbeat(payload, camera=None)` gains the keyword and `heartbeat_once` passes the snapshot.)
1. **`ReachyMini.acquire_media()` alone cannot get the camera back from the TCU companion.** In SDK 1.11.0 on the robot
   (`/venvs/apps_venv/lib/python3.12/site-packages/reachy_mini/reachy_mini.py:222-239`), `acquire_media()` starts with
   `if not self._media_released: return`. That flag is set only when *this* `ReachyMini` object called
   `release_media()`. When another program (the companion) made the daemon release the media, MedCare's object still
   has `_media_released == False`, so `acquire_media()` does nothing and the camera stays dead. A working path is
   `release_media()` then `acquire_media()` on the app's object (release is idempotent on the daemon side and sets the
   flag; acquire then POSTs `/api/media/acquire` through `mini.client.acquire_media()`, `io/ws_client.py:310`, and
   rebuilds the media manager), or `mini.client.acquire_media()` followed by a rebuild. Spec A2 step 1 says "through
   the SDK's own `acquire_media()`", which taken literally fails in exactly the case that caused the bug. Task 2's test
   should use a fake whose `acquire_media()` is a no-op unless `release_media()` was called first.
2. Both recovery paths rebuild the whole media manager (`self.media_manager = self._configure_mediamanager(...)`,
   reachy_mini.py:236), which also carries the speaker and microphone. The docs tell the reader to check speech after
   a recovery (spec review focus 1).
3. The contract says `AppClient.heartbeat(...)` gains a keyword `camera`, but today it is
   `heartbeat(self, payload: dict)` (`reachy_app/medcare_reachy/bridge/app_client.py:171`) and `Runner.heartbeat_once`
   builds the dict (`runner.py:463-465`). The docs don't depend on which form Task 2 picks.
4. The AI card's note `aiConnections.seeded` (`frontend_source/src/i18n.ts:19`, zh-TW :699) says the LLM lines in
   `.env` no longer apply; the key fill makes that slightly untrue for `OPENROUTER_API_KEY`. Not changed by any task (spec
   C4 names only `reachy.checkinsHelp`); listed as unresolved in the cross-check.
5. Left stale on purpose (outside spec section 6): `reachy_app/README.md` still describes on-robot vision as the
   default and says "No microphone in this version" (lines 18-20, 46), and has no status lines for 0.4.0-0.5.4;
   `docs/ARCHITECTURE.md:341-343`, `docs/DATA_FLOW.md:1002` and `docs/MODELS.md:698` say 0.5.4 is the code's version;
   CLAUDE.md:127 and :137 call 0.5.3 and 0.5.4 "not yet deployed", although the robot ran 0.5.4 (with a SenseVoice
   `voice.py`) on 11 Oct 2026. The new deploy bullet states the fact.

**Facts verified (read-only)**
- CLAUDE.md and reachy_app/README.md are UTF-8 with CRLF (`file`). Every `old_string` above occurs exactly once at
  `37ba84e` (`grep -cF`).
- Identity rule today: `app/services/monitor_service.py:519-523` (one patient label, OpenVINO `saturated` and `error`
  checks), :528-532 (a MediaPipe face overlapping by >= 0.20 and fewer than 4 MediaPipe faces), :534-542 (consecutive
  boxes overlap >= 0.20, verified at 2 hits); `MonitorSession.mode` defaults to "dose" (:138) and becomes "observe"
  after a record (:588-590).
- Token caps and limits: `app/services/conversation.py:49` `CALL_TIMEOUT = 6`, :51 `REPLY_MAX_TOKENS = 200`, :52
  `SUMMARY_MAX_TOKENS = 400`, :54 `SUMMARY_CALL_TIMEOUT = 12`, :56 `RISK_MAX_TOKENS = 32`, :59
  `RISK_CALL_TIMEOUT = 2.5`; `app/services/memory.py:378` `max_tokens=1000, temperature=0`.
- `config.OPENROUTER_API_KEY` (`app/config.py:95`, default ""); `ai_store.seed_from_env` copies it once
  (`app/services/ai_store.py:45-72`); lifespan order `app/main.py:39-41`.
- Routers: `/api/ai` (`api_ai.py:24`), `/api/reachy` (`api_reachy.py:15`), `/api/device` (`api_device.py:30`).
- Robot app: `SEARCH_TIMEOUT = 600.0` (`bridge/session.py:34`), slow-camera warning `_slow_camera_logged`
  (`bridge/runner.py:116`, :284-290), `BridgeService.restart()` with no lock today (`service.py:124-126`), version
  pins `reachy_app/pyproject.toml:8` and `reachy_app/medcare_reachy/bridge/__init__.py:3` (both "0.5.4"; no other
  pin in code), the app's page on port 8042 (`medcare_reachy/main.py:19`).
- Deploy tool: `reachy_app/tools/deploy_to_robot.py` (`--host`, default `reachy-mini.local`; user `pollen`;
  `REACHY_SSH_PASSWORD`; backup, upload, `pip install 'faster-whisper>=1.1,<2'` at :346, Whisper Base download at
  :353-356; its own hint `curl -X POST localhost:8000/api/apps/start-app/medcare_reachy` at :304).
- On the robot (read-only SSH, 11 Oct 2026): reachy_mini 1.11.0; `ReachyMini.release_media`/`acquire_media` at
  reachy_mini.py:203-239; `no_media` makes the SDK call `release_media()` (reachy_mini.py:349-357); daemon routes
  `POST /api/media/release`, `POST /api/media/acquire`, `GET /api/media/status` -> `{"available", "released",
  "no_media"}` (`daemon/app/routers/media.py:41-62`); `POST /api/apps/restart-current-app` and
  `/api/apps/start-app/{app_name}` (`daemon/app/routers/apps.py:132,156`); `media/status` answered
  `{"available":true,"released":false,"no_media":false}`; unit `reachy-ai-companion.service` is disabled and runs
  `app.py --port 8042`; installed MedCare Reachy is 0.5.4 and its `voice.py` mentions SenseVoice 7 times.
- TCU companion connects with `media_backend="no_media"`
  (`C:/Reachy_Mini/repo/reachy-mini-tcu/companion/robot/reachy_controller.py:177-180`).
- The check script ran against today's tree: `code` fails on every new name (Tasks 1-9 not in yet) and `docs` fails
  with the expected MISSING and STILL THERE lines, so both modes can fail.

---

### Task 11: The keyword safety check catches 「活著沒什麼意思」

Added on the user's request (2026-10-11, flaw F21). Not in the spec's sections A-D; the user approved it as part of
Phase 1 on Telegram.

**Files:**
- Modify: `app/services/conversation.py:104` (one pattern in `RISK_PATTERNS["self_harm"]`)
- Test: `tests/test_conversation.py:66-78` (`test_explicit_self_harm_statements_match`) and the everyday-phrases list
  of `test_everyday_sentences_raise_no_alert` (`tests/test_conversation.py:92-105`)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: nothing new; `conversation.screen(text) -> str | None` keeps its signature.

**Why:** `screen()` flags 「活著沒有意思」 and 「活著有什麼意思」, but returns `None` for 「活著沒什麼意思」 and
「我覺得活著沒什麼意思」 (checked in the running app container on 2026-10-11). The pattern
`活著還?(沒有?|有什麼)(意思|意義)` needs 沒有? or 有什麼, and 沒什麼 fits neither. During an OpenRouter outage
the keyword list is the only safety check.

- [ ] **Step 1: Write the failing tests**

In `tests/test_conversation.py`, add three phrases to the `test_explicit_self_harm_statements_match` list, right
after `"活著有什麼意思",`. Replace line 74 (one line today):

```python
    "我恨不得去死", "讓我死了吧", "让我死了吧", "死了比較好", "我不如死掉", "活著有什麼意思", "我想自我了斷", "我想尋短",
```

with these three lines:

```python
    "我恨不得去死", "讓我死了吧", "让我死了吧", "死了比較好", "我不如死掉", "活著有什麼意思",
    "活著沒什麼意思", "我覺得活著沒什麼意思", "活着没什么意义",
    "我想自我了斷", "我想尋短",
```

(Keep the rest of the list exactly as it is; only the middle line's phrases are new. 「活着没什么意义」 is the
Simplified form the robot's speech-to-text writes; `screen()` converts it to Traditional first.)

In the list of `test_everyday_sentences_raise_no_alert` (line 92; it contains `"我不想活在過去"` at line 96), add one control phrase that has 沒什麼意思 but not 活著, right after `"我不想活在過去",`:

```python
    "我不想活在過去",
    "這個節目沒什麼意思",
```

- [ ] **Step 2: Run the tests to see the new self-harm cases fail**

Run (from `C:/Reachy_Mini/repo/MedAiCarePlus`):
`.venv/Scripts/python.exe -m pytest tests/test_conversation.py -q -p no:cacheprovider -k "self_harm_statements_match or everyday_sentences"`
Expected: 3 FAILED (`活著沒什麼意思`, `我覺得活著沒什麼意思`, `活着没什么意义`: `assert None == 'self_harm'`);
the control phrase passes already.

- [ ] **Step 3: Widen the pattern**

In `app/services/conversation.py`, replace this line (line 104 at `37ba84e`; Task 8 added two lines above it, so it is
line 106 after Task 8: find it by its text, it occurs once):

```python
        r"活著還?(沒有?|有什麼)(意思|意義)",
```

with:

```python
        r"活著還?(沒有?|沒什麼|有什麼)(意思|意義)",                  # 活著沒什麼意思 too
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_conversation.py -q -p no:cacheprovider`
Expected: all pass except the known BUG-37 test `test_a_broken_background_never_costs_the_reply`, if it is in this
file's run (it fails before this change too).

- [ ] **Step 5: Run the whole backend suite**

Run: `.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider`
Expected: the count before this task plus the 4 new cases (`1727 passed` after Tasks 1-10: 1723 after Task 8, plus
4); only the known BUG-37 failure (and, rarely, a BUG-39 timing flake that passes on rerun).

- [ ] **Step 6: Commit**

```bash
git add app/services/conversation.py tests/test_conversation.py
git commit -m "Safety keywords: 活著沒什麼意思 counts as self-harm" -m "Co-Authored-By: <the model that wrote the commit> <noreply@anthropic.com>"
```

- [ ] **Step 7: Make it live (only when Task 10's Part B already rebuilt the stack)**

Task 10's Step 8 rebuilds the running app from the commits made by then. If it ran before this task, rebuild once more
so the running app has the new pattern, then check it in the container:

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus && docker compose up -d --build app
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/health
MSYS_NO_PATHCONV=1 docker compose exec -T -w /app app python -c "from app.services import conversation; print(conversation.screen('我覺得活著沒什麼意思'))"
```

(The escapes spell 我覺得活著沒什麼意思, so the Windows console's code page can't garble the phrase.) Repeat the
`curl` until it prints `200`. Expected last line: `self_harm`. If this task runs before Task 10, skip this
step: Task 10's Step 8 rebuild includes it.

#### NOTES
- Verified: `app/services/conversation.py:104` holds the pattern; `tests/test_conversation.py:74` holds
  "活著有什麼意思" in the explicit list. `screen()` converts Simplified to Traditional before matching (CLAUDE.md,
  "Safety: three layers", item 1).
- Risk: 沒什麼意思 without 活著 stays unmatched by design (the control phrase pins that).

---

