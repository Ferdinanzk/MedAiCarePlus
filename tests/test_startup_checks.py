import asyncio
import sys
import types

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config, startup_checks


def _configured():
    return {"APP_ENV": "prod", "SECRET_KEY": "unique-test-secret",
            "LINE_CHANNEL_SECRET": "line-secret", "OPERATOR_NAME": "Operator",
            "OPERATOR_CONTACT": "operator@example.test", "TUNNEL_PROVIDER": "Tunnel"}


def test_dev_returns_problems_and_logs_warnings(caplog):
    problems = startup_checks.run_startup_checks({"APP_ENV": "dev"})
    assert len(problems) == 5
    assert "SECRET_KEY" in problems[0]
    assert "LINE_CHANNEL_SECRET" in caplog.text


@pytest.mark.parametrize("key", ["SECRET_KEY", "LINE_CHANNEL_SECRET", "OPERATOR_NAME",
                                  "OPERATOR_CONTACT", "TUNNEL_PROVIDER"])
def test_prod_rejects_missing_required_configuration(key):
    env = _configured()
    env[key] = " "
    with pytest.raises(SystemExit) as exc:
        startup_checks.run_startup_checks(env)
    assert exc.value.code == 1


def test_prod_rejects_default_secret():
    env = _configured()
    env["SECRET_KEY"] = "change-me-in-production-32chars!!"
    with pytest.raises(SystemExit):
        startup_checks.run_startup_checks(env)


def test_robot_disabled_does_not_require_voice_configuration():
    assert startup_checks.run_startup_checks(_configured()) == []


@pytest.mark.parametrize("enabled", ["1", "true", "yes"])
def test_robot_enabled_requires_the_risk_key(enabled):
    """The models and the companies the notices name are checked on the AI settings (check_ai_settings)."""
    env = {**_configured(), "APP_ENV": "dev", "REACHY_FEATURE_ENABLED": enabled}
    assert [problem.split()[0] for problem in startup_checks.run_startup_checks(env)] == ["RISK_CLASSIFIER_API_KEY"]
    env.update(RISK_CLASSIFIER_API_KEY="configured", APP_ENV="prod")
    assert startup_checks.run_startup_checks(env) == []


def _ai_snapshot(model_id="google/gemini-2.5-flash", region="global"):
    from app.services import ai_profile, ai_settings
    connection = ai_settings.Connection(1, "OpenRouter", "openrouter", ai_profile.BASE_URLS["openrouter"], "k", "k",
                                        True, "OpenRouter")
    model = ai_settings.Model(11, 1, model_id, "Google", region, "not kept", ai_profile.starting_profile("openrouter"))
    return ai_settings.Snapshot({1: connection}, {11: model}, {job: (11, None) for job in ai_settings.JOBS})


def test_ai_settings_are_checked_only_with_the_robot():
    assert startup_checks.check_ai_settings(_ai_snapshot(region=""), {"REACHY_FEATURE_ENABLED": ""}) == []


def test_ai_settings_warn_in_dev_and_stop_prod(caplog):
    env = {"REACHY_FEATURE_ENABLED": "1", "APP_ENV": "dev"}
    assert startup_checks.check_ai_settings(_ai_snapshot(), env) == []
    found = startup_checks.check_ai_settings(_ai_snapshot(model_id="openrouter/free", region=""), env)
    assert found and "openrouter/free" in caplog.text
    with pytest.raises(SystemExit):
        startup_checks.check_ai_settings(_ai_snapshot(region=""), {**env, "APP_ENV": "prod"})


def test_once_copied_the_exit_says_where_the_settings_are_changed(caplog):
    import dataclasses
    from datetime import datetime, timezone
    snapshot = dataclasses.replace(_ai_snapshot(region=""), seeded_at=datetime(2026, 10, 10, 12, tzinfo=timezone.utc))
    with pytest.raises(SystemExit):
        startup_checks.check_ai_settings(snapshot, {"REACHY_FEATURE_ENABLED": "1", "APP_ENV": "prod"})
    assert "copied from .env on 2026-10-10" in caplog.text and "AI connections card" in caplog.text


def test_explicit_env_is_isolated_from_config(monkeypatch):
    for key, value in _configured().items():
        monkeypatch.setattr(config, key, value)
    monkeypatch.setattr(config, "REACHY_FEATURE_ENABLED", False)
    assert startup_checks.run_startup_checks() == []
    assert len(startup_checks.run_startup_checks({})) == 5


@pytest.mark.parametrize("marked", [True, False])
def test_restore_marker_blocks_startup_in_every_mode(monkeypatch, marked):
    class Connection:
        async def fetchval(self, query):
            assert "restore_in_progress" in query and "EXISTS" in query
            return marked

    monkeypatch.setattr(config, "APP_ENV", "dev")
    if marked:
        with pytest.raises(SystemExit) as exc:
            asyncio.run(startup_checks.check_restore_state(Connection()))
        assert exc.value.code == 1
    else:
        asyncio.run(startup_checks.check_restore_state(Connection()))
