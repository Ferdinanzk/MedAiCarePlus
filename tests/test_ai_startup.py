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
