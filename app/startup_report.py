"""[STARTUP] diagnostics (14 Oct 2026): what the server found as it came up, with the real reason when a stage failed.

After a laptop reboot the stack often came up looking fine while a feature was quietly off: the live voice model's
Check cleared (check-ins silently became text chats), the LINE tunnel down (no videos), the device port bound to a
stale IP (a "healthy" container the robot could not reach), postgres not ready yet (one crash and a restart). Each
stage now logs one line, `[STARTUP] <stage>: OK ...` or `[STARTUP] <stage>: FAILED: <reason>`, the lifespan marks them
as it goes (app/main.py), and `GET /health` carries the same report (`startup`) for the host's boot script
(scripts/medcare-boot.ps1), which checks the things only the host can see (the port actually bound, the robot).

`finish()` runs after the servers are up: the network probes (api.openai.com, api.line.me: reachable, and the clock
skew from their Date header), the LINE public link, and, when the live voice model has a key but no passing Check
(saving the AI card's live section clears it; a Check pressed while offline fails), the Check itself, retried a few
times so a Wi-Fi that comes up after the stack does not leave GPT-Live off until someone presses the button.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from app import config
from app.services import ai_settings

log = logging.getLogger("app.startup")

STAGES = ("environment", "database", "ai_settings", "gpt_live", "models", "scheduler", "outbox", "line", "reminders",
          "device_api", "network", "clock")
PROBE_TIMEOUT = 4.0
PROBES = {"openai": "https://api.openai.com/v1/models", "line": "https://api.line.me/v2/bot/info"}
CLOCK_SKEW_WARN_SECONDS = 60.0
LIVE_CHECK_ATTEMPTS = 4
LIVE_CHECK_RETRY_SECONDS = 20.0
SECRET_NAMES = ("SECRET_KEY", "LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET", "GEMINI_API_KEY",
                "OPENROUTER_API_KEY", "RISK_CLASSIFIER_API_KEY", "BACKUP_PASSPHRASE")
SWITCHES = ("APP_ENV", "REACHY_FEATURE_ENABLED", "LIVE_VOICE_TEST", "LIVE_TOOLS", "LIVE_VISION_CLOUD",
            "LIVE_BACKEND_MODEL", "MEDCARE_TIMEZONE", "WEATHER_ENABLED", "PUBLIC_BASE_URL", "MED_CLIP_DIR",
            "DOSE_VIDEO_DIR")
DEFAULT_SECRET = "change-me-in-production-32chars!!"

_report: dict[str, dict] = {}
_started_at: float | None = None
_tasks: set = set()


def begin() -> None:
    global _started_at
    _report.clear()
    _started_at = time.monotonic()
    log.info("[STARTUP] starting (%s)", datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"))


def mark(stage: str, ok: bool, detail: str, *, pending: bool = False, warn: bool = False) -> None:
    """Record one stage and log it: OK at INFO, FAILED at ERROR (WARNING for a degraded feature the server runs
    without, `warn`); `pending`: a background step will settle it (the live Check)."""
    _report[stage] = {"ok": bool(ok), "detail": detail, "pending": bool(pending),
                      "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if ok:
        log.info("[STARTUP] %s: OK %s", stage, detail)
    elif warn or pending:
        log.warning("[STARTUP] %s: DEGRADED: %s", stage, detail)
    else:
        log.error("[STARTUP] %s: FAILED: %s", stage, detail)


def environment(problems: list[str]) -> str:
    """The switches that decide what runs and which secrets are set (names only, never values)."""
    switches = ", ".join(f"{name}={getattr(config, name, '')}" for name in SWITCHES)
    secrets = ", ".join(f"{name}={'set' if str(getattr(config, name, '') or '').strip() else 'EMPTY'}"
                        for name in SECRET_NAMES)
    notes = []
    if str(getattr(config, "SECRET_KEY", "")).strip() == DEFAULT_SECRET:
        notes.append("SECRET_KEY is the default (fine for dev; the AI card's keys are encrypted with it: changing it "
                     "later makes them unreadable)")
    if problems:
        notes.append("startup checks: " + "; ".join(problems))
    return f"{switches}; secrets: {secrets}" + (f"; notes: {' | '.join(notes)}" if notes else "")


def ai_jobs(snapshot, unusable: list[str]) -> str:
    usable = [job for job in ai_settings.JOBS if job not in unusable]
    parts = []
    for job in ai_settings.JOBS:
        model = ai_settings.main_model_id(job) or "-"
        parts.append(f"{job}={model}{'' if job in usable else ' (NO USABLE MODEL)'}")
    detail = ", ".join(parts)
    if unusable:
        detail += "; a job without a usable model gives Reachy's fixed line / no safety model check / no summaries: " \
                  "choose a tested model whose connection has a key on the AI connections card"
    return detail


def gpt_live(snapshot) -> tuple[str, str]:
    """The live voice model's state: ("ok", detail) or (why not, detail). "no_check" and "check_failed" are settled by
    the automatic Check in finish()."""
    if not config.LIVE_VOICE_TEST:
        return "off", "LIVE_VOICE_TEST is off: check-ins use the text chat"
    live = snapshot.live
    if live is None:
        return "no_model", "no live model saved on the AI connections card: check-ins use the text chat"
    connection = snapshot.connections.get(live.connection_id)
    if connection is None:
        return "no_connection", f"the live model's connection {live.connection_id} is gone: check-ins use the text chat"
    if connection.kind != "openai":
        return "wrong_kind", f"the live model's connection is {connection.kind}, not OpenAI: check-ins use the text chat"
    if not connection.key_readable:
        return "key_unreadable", ("the OpenAI connection's key cannot be decrypted (SECRET_KEY changed since it was "
                                  "saved?): enter the key again on the AI connections card; check-ins use the text chat")
    if not connection.api_key:
        return "no_key", "the OpenAI connection has no key: check-ins use the text chat"
    if live.last_error:
        return "check_failed", (f"the last Check failed ({live.last_error}); check-ins use the text chat until a Check "
                                f"passes: one runs now")
    if live.tested_at is None:
        return "no_check", ("no passing Check saved (saving the AI card's live section clears it); check-ins use the "
                            "text chat until a Check passes: one runs now")
    when = live.tested_at.astimezone().isoformat(timespec="seconds") if live.tested_at else "?"
    tools = f", tools {'on' if config.LIVE_TOOLS else 'off'} (backend {config.LIVE_BACKEND_MODEL})"
    return "ok", f"model {live.model_id}, voice {live.voice}, Check passed at {when} ({live.test_ms} ms){tools}"


def probe(url: str) -> dict:
    """One HTTPS request (any status proves the host answers), its time, and the server's Date header."""
    began = time.monotonic()
    try:
        response = requests.head(url, timeout=PROBE_TIMEOUT, allow_redirects=False)
    except requests.RequestException as error:
        return {"ok": False, "error": f"{type(error).__name__}: {str(error)[:120]}"}
    return {"ok": True, "status": response.status_code, "ms": round((time.monotonic() - began) * 1000),
            "date": response.headers.get("Date")}


def clock_skew(date_header: str | None) -> float | None:
    """Seconds this process's clock is ahead of the server's Date header (None when unreadable)."""
    if not date_header:
        return None
    try:
        theirs = parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return None
    if theirs.tzinfo is None:
        theirs = theirs.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - theirs).total_seconds(), 1)


async def network_and_clock() -> None:
    results = await asyncio.gather(*(asyncio.to_thread(probe, url) for url in PROBES.values()))
    found = dict(zip(PROBES, results))
    down = [name for name, result in found.items() if not result["ok"]]
    detail = ", ".join(f"{name} {'reachable' if r['ok'] else 'UNREACHABLE (' + r['error'] + ')'}"
                       + (f" in {r['ms']} ms" if r["ok"] else "") for name, r in found.items())
    mark("network", not down, detail + ("; GPT-Live, the helper, web search and LINE need these" if down else ""),
         warn=True)
    skews = [clock_skew(r.get("date")) for r in found.values() if r["ok"]]
    skews = [s for s in skews if s is not None]
    if not skews:
        mark("clock", False, "no server answered, the clock could not be compared", warn=True)
        return
    skew = max(skews, key=abs)
    ok = abs(skew) <= CLOCK_SKEW_WARN_SECONDS
    mark("clock", ok, f"{skew:+.1f} s against the internet (local {datetime.now().isoformat(timespec='seconds')})"
         + ("" if ok else "; TLS, tokens and the schedule go wrong with a skewed clock: restart Docker Desktop / "
                          "check the Windows clock"), warn=True)


async def public_link() -> None:
    from app.services import dose_video
    try:
        base = await dose_video.public_base_url()
    except Exception as error:
        base = None
        reason = f"{type(error).__name__}"
    else:
        reason = "PUBLIC_BASE_URL unset and the LINE tunnel is down (scripts/line-tunnel.ps1): family get no videos"
    stage = _report.get("line", {})
    detail = stage.get("detail", "").split(";")[0]   # the token line, without an earlier public-link verdict
    if base:
        mark("line", stage.get("ok", True), f"{detail}; public link {base}")
    else:
        mark("line", False, f"{detail}; no public link: {reason}", warn=True)


async def auto_check_live(snapshot) -> None:
    """A Check for the live voice model that has a key but no passing Check, retried over about a minute (the Wi-Fi
    may come up after the stack); the result is saved as the AI card's Check button saves it."""
    from app.database import get_pool
    from app.services import ai_store, live_client
    live = snapshot.live
    connection = snapshot.connections.get(live.connection_id)
    for attempt in range(1, LIVE_CHECK_ATTEMPTS + 1):
        ok, ms, error = await live_client.check(connection.api_key, live.model_id, live.voice)
        if ok or error == "live_auth" or attempt == LIVE_CHECK_ATTEMPTS:
            break
        log.warning("[STARTUP] gpt_live: Check attempt %d/%d failed (%s); retrying in %.0f s", attempt,
                    LIVE_CHECK_ATTEMPTS, error, LIVE_CHECK_RETRY_SECONDS)
        await asyncio.sleep(LIVE_CHECK_RETRY_SECONDS)
    now = ai_settings.current().live
    if now is None or (now.connection_id, now.model_id, now.voice) != (live.connection_id, live.model_id, live.voice):
        mark("gpt_live", False, "the live model was changed while its Check ran; press Check on the AI card", warn=True)
        return
    async with get_pool().acquire() as conn:
        async with ai_store.change(conn):
            await ai_store.save_live_check(conn, ok, ms, error)
    if ok:
        mark("gpt_live", True, f"model {live.model_id}, voice {live.voice}, Check passed now ({ms} ms); live voice "
                               f"check-ins are on")
    else:
        why = {"live_auth": "the OpenAI key was refused (401/403): check it on the AI connections card"}.get(
            error, f"GPT-Live could not be reached ({error}): check the internet connection")
        mark("gpt_live", False, f"the Check failed: {why}; check-ins use the text chat", warn=True)


async def finish(snapshot, *, live_status: str) -> None:
    """After the servers are up: the probes, the public link, the automatic Check, then the summary line."""
    try:
        await network_and_clock()
    except Exception:
        log.exception("[STARTUP] network: the probes failed")
    try:
        await public_link()
    except Exception:
        log.exception("[STARTUP] line: the public link check failed")
    if live_status in ("no_check", "check_failed"):
        try:
            await auto_check_live(snapshot)
        except Exception as error:
            mark("gpt_live", False, f"the automatic Check failed: {type(error).__name__}: {error}", warn=True)
    log_summary()


def finish_in_background(snapshot, *, live_status: str) -> None:
    task = asyncio.get_running_loop().create_task(finish(snapshot, live_status=live_status), name="startup-report")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def refresh_line() -> None:
    """GET /health: the LINE tunnel is started after the server (scripts/medcare-boot.ps1), so a degraded `line` stage
    is checked again (dose_video.public_base_url caches its answer, so this is cheap)."""
    stage = _report.get("line")
    if stage is not None and not stage["ok"] and "no public link" in stage["detail"]:
        try:
            await public_link()
        except Exception:
            pass


def summary() -> dict:
    """For GET /health: ready (every stage that must hold does), the degraded stages, and each stage's line."""
    stages = {stage: {"ok": r["ok"], "detail": r["detail"], "pending": r.get("pending", False)}
              for stage, r in _report.items()}
    degraded = [stage for stage, r in _report.items() if not r["ok"]]
    pending = [stage for stage, r in _report.items() if r.get("pending")]
    return {"ready": "database" in _report and _report["database"]["ok"], "degraded": degraded, "pending": pending,
            "seconds": round(time.monotonic() - _started_at, 1) if _started_at else None, "stages": stages}


def log_summary() -> None:
    report = summary()
    if report["degraded"]:
        log.warning("[STARTUP] ready with degraded features: %s (%.1f s)", ", ".join(report["degraded"]),
                    report["seconds"] or 0)
    else:
        log.info("[STARTUP] ready: every stage OK (%.1f s)", report["seconds"] or 0)
