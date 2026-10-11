"""The AI setup in use: connections (a service and its key), models, and the main and backup model of each job.

One snapshot per process, loaded at startup and replaced after every change on the card (ai_store.change), so a
model call never queries the database. Before it is loaded (and in tests) the snapshot comes from .env, exactly as
the app worked before the card: one OpenRouter connection, LLM_MODEL then LLM_FALLBACK_MODEL for every job.
"""

import dataclasses
from dataclasses import dataclass
from datetime import datetime

from app import config
from app.services import ai_profile

JOBS = ("reply", "safety", "after_chat")
ENV_CONNECTION_ID = 0           # the .env connection and its models have no database row
ENV_PRIMARY_REF, ENV_FALLBACK_REF = -1, -2
# The live voice model's voices (spec 2026-10-12 E2): the TCU companion's list. The card offers only these.
LIVE_VOICES = ("marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse")


@dataclass(frozen=True)
class Connection:
    connection_id: int
    name: str
    kind: str
    base_url: str
    api_key: str = dataclasses.field(repr=False)   # decrypted; "" when none or unreadable; never in a repr or log
    key_last4: str | None
    key_readable: bool
    service_name: str       # {LLM_SERVICE} in the notices


@dataclass(frozen=True)
class Model:
    model_ref: int
    connection_id: int
    model_id: str
    company: str = ""       # {LLM_PROVIDER}
    region: str = ""        # {LLM_PROVIDER_REGION}
    retention: str = ""     # {LLM_RETENTION}
    profile: dict | None = None
    tested_at: datetime | None = None
    test_ms: int | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class Target:
    """One model to call, with the connection it goes through and the thinking level to send it: the job's level,
    or the nearest lower one this model accepts (ai_profile.effective_level); "off" outside a job."""
    connection: Connection
    model: Model
    thinking: str = "off"

    @property
    def model_id(self) -> str:
        return self.model.model_id

    @property
    def api_key(self) -> str:
        return self.connection.api_key

    @property
    def kind(self) -> str:
        return self.connection.kind

    @property
    def url(self) -> str:
        return f"{self.connection.base_url}/chat/completions"

    @property
    def profile(self) -> dict:
        return self.model.profile or ai_profile.starting_profile(self.kind)


@dataclass(frozen=True)
class LiveSetting:
    """The card's live voice model (table ai_live, one row; spec 2026-10-12 E2): an OpenAI connection, the model id
    and the voice, with the last Check: its time (tested_at, test_ms) or its error code (last_error)."""
    connection_id: int | None
    model_id: str
    voice: str
    tested_at: datetime | None
    test_ms: int | None
    last_error: str | None


@dataclass(frozen=True)
class LiveTarget:
    """What the live relay opens a GPT-Live session with (live_target)."""
    api_key: str = dataclasses.field(repr=False)   # decrypted; never in a repr or log
    model_id: str
    voice: str


def _thinking_off() -> dict[str, str]:
    return {job: "off" for job in JOBS}


@dataclass(frozen=True)
class Snapshot:
    connections: dict[int, Connection]
    models: dict[int, Model]
    jobs: dict[str, tuple[int, int | None]]     # job -> (main model_ref, backup model_ref or None)
    seeded_at: datetime | None = None
    thinking: dict[str, str] = dataclasses.field(default_factory=_thinking_off)   # job -> level chosen on the card
    live: LiveSetting | None = None   # the live voice model (ai_live); None until one is saved


_snapshot: Snapshot | None = None


def set_current(snapshot: Snapshot | None) -> None:
    global _snapshot
    _snapshot = snapshot


def loaded() -> Snapshot | None:
    return _snapshot


def current() -> Snapshot:
    return _snapshot or env_snapshot()


def _env_connection() -> Connection:
    key = config.OPENROUTER_API_KEY
    return Connection(ENV_CONNECTION_ID, "OpenRouter", "openrouter", ai_profile.BASE_URLS["openrouter"], key,
                      key[-4:] if key else None, True, config.LLM_SERVICE or "OpenRouter")


def _env_model(ref: int, model_id: str) -> Model:
    return Model(ref, ENV_CONNECTION_ID, model_id, config.LLM_PROVIDER, config.LLM_PROVIDER_REGION,
                 config.LLM_RETENTION, ai_profile.starting_profile("openrouter"))


def env_snapshot() -> Snapshot:
    """The .env setup, read from config on every call (so a changed config, e.g. in a test, applies). With
    LLM_MODEL empty the fallback is tried twice, as conversation._complete did before the card."""
    fallback = config.LLM_FALLBACK_MODEL
    models = {ENV_PRIMARY_REF: _env_model(ENV_PRIMARY_REF, config.LLM_MODEL or fallback),
              ENV_FALLBACK_REF: _env_model(ENV_FALLBACK_REF, fallback)}
    return Snapshot({ENV_CONNECTION_ID: _env_connection()}, models,
                    {job: (ENV_PRIMARY_REF, ENV_FALLBACK_REF) for job in JOBS})


def env_target(model_id: str) -> Target:
    """A call to an OpenRouter model as before the card (conversation._post without a target)."""
    return Target(_env_connection(), _env_model(ENV_PRIMARY_REF, model_id))


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


def main_model_id(job: str) -> str | None:
    targets = job_targets(job)
    return targets[0].model_id if targets else None


def _distinct(values) -> str:
    """Each value once, in order, joined by " / "; "" when any is missing, so the notice keeps its placeholder
    rather than naming only the companies that were filled in."""
    values = [value.strip() for value in values]
    if not values or any(not value for value in values):
        return ""
    return " / ".join(dict.fromkeys(values))


def notice_fills(snapshot: Snapshot) -> dict[str, str]:
    """{LLM_SERVICE}, {LLM_PROVIDER}, {LLM_PROVIDER_REGION} and {LLM_RETENTION} for the notices, from every job's
    main and backup model in job order (spec §9)."""
    targets = [target for job in JOBS for target in job_targets(job, snapshot)]
    return {"LLM_SERVICE": _distinct(t.connection.service_name for t in targets),
            "LLM_PROVIDER": _distinct(t.model.company for t in targets),
            "LLM_PROVIDER_REGION": _distinct(t.model.region for t in targets),
            "LLM_RETENTION": _distinct(t.model.retention for t in targets)}


def problems(snapshot: Snapshot) -> list[str]:
    """What the robot feature needs from every job's models (the .env startup checks before the card): a pinned
    model, not openrouter/free, with the company, region and retention the notices name."""
    found = []
    for job in JOBS:
        targets = job_targets(job, snapshot)
        if not targets:
            found.append(f"{job}: no model")
        for target in targets:
            name = f"{job}: {target.model_id}"
            if target.model_id.strip() in ("", "openrouter/free"):
                found.append(f"{name} must be a pinned model, not openrouter/free")
            for detail in ("company", "region", "retention"):
                if not getattr(target.model, detail).strip():
                    found.append(f"{name} needs its {detail} for the notice")
    return found


def job_usable(snapshot: Snapshot, job: str) -> bool:
    """The job has a model it can use (spec C1): its main or its backup model is tested (has a profile) and its
    connection has a key this SECRET_KEY can read. The card warns under a job without one, and the start logs it."""
    return any(target.model.profile is not None and target.connection.key_readable and bool(target.api_key)
               for target in job_targets(job, snapshot))


def live_target(snapshot: Snapshot | None = None) -> LiveTarget | None:
    """The live voice model a check-in may use (spec 2026-10-12 E1, E2), or None. Only while .env has
    LIVE_VOICE_TEST, and only for a saved model on an OpenAI connection whose key this SECRET_KEY can read, after a
    Check that passed (saving the model again clears its Check: ai_store.set_live)."""
    if not config.LIVE_VOICE_TEST:
        return None
    snapshot = snapshot or current()
    live = snapshot.live
    if live is None or live.tested_at is None or live.last_error is not None:
        return None
    connection = snapshot.connections.get(live.connection_id)
    if connection is None or connection.kind != "openai" or not connection.key_readable or not connection.api_key:
        return None
    return LiveTarget(connection.api_key, live.model_id, live.voice)


def with_model(snapshot: Snapshot, model: Model) -> Snapshot:
    return dataclasses.replace(snapshot, models={**snapshot.models, model.model_ref: model})


def _live_public(snapshot: Snapshot) -> dict | None:
    live = snapshot.live
    if live is None:
        return None
    return {"connection_id": live.connection_id, "model_id": live.model_id, "voice": live.voice,
            "tested_at": live.tested_at.isoformat() if live.tested_at else None, "test_ms": live.test_ms,
            "last_error": live.last_error, "usable": live_target(snapshot) is not None}


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
        "jobs": {job: {"primary_ref": refs[0], "fallback_ref": refs[1], "thinking": snapshot.thinking.get(job, "off")}
                 for job, refs in snapshot.jobs.items()},
        "seeded_at": snapshot.seeded_at.isoformat() if snapshot.seeded_at else None,
        "no_key_in_use": not any(target.api_key for target in in_use),
        "jobs_usable": {job: job_usable(snapshot, job) for job in JOBS},
        "live": _live_public(snapshot),
        "live_voice_test": config.LIVE_VOICE_TEST,   # the card shows its live section only then
    }
