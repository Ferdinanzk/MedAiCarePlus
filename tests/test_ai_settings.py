"""The AI setup in use: from .env until the card's tables are loaded, then the main and backup model per job."""

import dataclasses
import json
from datetime import datetime, timezone

import pytest

from app import config
from app.services import ai_profile, ai_settings


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(ai_settings, "_snapshot", None)
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-v1-envkey1234")
    monkeypatch.setattr(config, "LLM_MODEL", "google/gemini-2.5-flash-lite")
    monkeypatch.setattr(config, "LLM_FALLBACK_MODEL", "google/gemini-2.5-flash")
    monkeypatch.setattr(config, "LLM_SERVICE", "OpenRouter")
    monkeypatch.setattr(config, "LLM_PROVIDER", "Google")
    monkeypatch.setattr(config, "LLM_PROVIDER_REGION", "global")
    monkeypatch.setattr(config, "LLM_RETENTION", "not kept")


def _card_snapshot():
    openai = ai_settings.Connection(1, "OpenAI", "openai", ai_profile.BASE_URLS["openai"], "sk-proj-secret-abcd",
                                    "abcd", True, "OpenAI")
    openrouter = ai_settings.Connection(2, "OpenRouter", "openrouter", ai_profile.BASE_URLS["openrouter"], "", None,
                                        True, "OpenRouter")
    models = {11: ai_settings.Model(11, 1, "gpt-5.4-mini", "OpenAI", "USA", "30 days",
                                    ai_profile.starting_profile("openai")),
              21: ai_settings.Model(21, 2, "google/gemini-2.5-flash", "Google", "global", "not kept",
                                    ai_profile.starting_profile("openrouter"))}
    jobs = {"reply": (11, 21), "safety": (21, None), "after_chat": (21, None)}
    return ai_settings.Snapshot({1: openai, 2: openrouter}, models, jobs)


def test_before_loading_every_job_uses_the_env_setup():
    for job in ai_settings.JOBS:
        targets = ai_settings.job_targets(job)
        assert [t.model_id for t in targets] == ["google/gemini-2.5-flash-lite", "google/gemini-2.5-flash"]
        assert {t.url for t in targets} == {"https://openrouter.ai/api/v1/chat/completions"}
        assert {t.api_key for t in targets} == {"sk-or-v1-envkey1234"}
        assert targets[0].profile == ai_profile.starting_profile("openrouter")
    assert ai_settings.main_model_id("reply") == "google/gemini-2.5-flash-lite"


def test_without_llm_model_the_fallback_is_tried_twice_as_before(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODEL", "")
    assert [t.model_id for t in ai_settings.job_targets("reply")] == ["google/gemini-2.5-flash"] * 2


def test_an_env_target_calls_openrouter_as_before():
    target = ai_settings.env_target("x/model")
    assert (target.url, target.api_key, target.kind, target.model_id) == (
        "https://openrouter.ai/api/v1/chat/completions", "sk-or-v1-envkey1234", "openrouter", "x/model")


def test_a_loaded_snapshot_replaces_the_env_setup(monkeypatch):
    monkeypatch.setattr(ai_settings, "_snapshot", _card_snapshot())
    reply = ai_settings.job_targets("reply")
    assert [(t.connection.name, t.model_id) for t in reply] == [("OpenAI", "gpt-5.4-mini"),
                                                                 ("OpenRouter", "google/gemini-2.5-flash")]
    assert reply[0].url == "https://api.openai.com/v1/chat/completions"
    assert [t.model_id for t in ai_settings.job_targets("safety")] == ["google/gemini-2.5-flash"]   # no backup: once
    assert ai_settings.current() is ai_settings.loaded()


def test_notice_fills_name_each_service_and_company_once_in_job_order():
    assert ai_settings.notice_fills(_card_snapshot()) == {
        "LLM_SERVICE": "OpenAI / OpenRouter", "LLM_PROVIDER": "OpenAI / Google",
        "LLM_PROVIDER_REGION": "USA / global", "LLM_RETENTION": "30 days / not kept"}


def test_one_missing_detail_leaves_that_fill_empty_so_the_notice_shows_its_placeholder():
    snapshot = _card_snapshot()
    snapshot = ai_settings.with_model(snapshot, dataclasses.replace(snapshot.models[11], region=""))
    fills = ai_settings.notice_fills(snapshot)
    assert fills["LLM_PROVIDER_REGION"] == "" and fills["LLM_PROVIDER"] == "OpenAI / Google"


def test_problems_name_what_the_robot_feature_needs():
    assert ai_settings.problems(_card_snapshot()) == []
    snapshot = _card_snapshot()
    snapshot = ai_settings.with_model(snapshot, dataclasses.replace(snapshot.models[21], model_id="openrouter/free",
                                                                    retention=""))
    found = ai_settings.problems(snapshot)
    assert any("openrouter/free" in problem for problem in found)
    assert any("retention" in problem for problem in found)


def test_the_public_view_never_carries_a_key():
    view = ai_settings.public(_card_snapshot())
    assert "sk-proj-secret-abcd" not in json.dumps(view)
    assert view["connections"][0]["key_last4"] == "abcd" and view["connections"][1]["has_key"] is False
    assert view["jobs"]["safety"] == {"primary_ref": 21, "fallback_ref": None, "thinking": "off"}
    assert view["no_key_in_use"] is False
    assert view["models"][1]["copied_from_env"] is True and view["models"][1]["tested"] is True


def test_no_key_in_use_when_no_model_in_use_has_one():
    snapshot = dataclasses.replace(_card_snapshot(), jobs={job: (21, None) for job in ai_settings.JOBS})
    assert ai_settings.public(snapshot)["no_key_in_use"] is True


def test_a_key_is_never_in_a_repr():   # a log line or a traceback showing these must not print the plain key
    connection = dataclasses.replace(_card_snapshot().connections[1], api_key="AIzaReprProbe-77")
    snapshot = dataclasses.replace(_card_snapshot(), connections={**_card_snapshot().connections, 1: connection})
    for value in (connection, ai_settings.job_targets("reply", snapshot)[0], snapshot):
        assert "AIzaReprProbe-77" not in repr(value)
    assert connection.api_key == "AIzaReprProbe-77"


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


# ── Live voice model (spec 2026-10-12 E2) ──

CHECKED = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)


def _live_snapshot(**changes):
    """_card_snapshot plus a checked live model on the OpenAI connection (1); `changes` edit the live row."""
    live = ai_settings.LiveSetting(1, "gpt-live-1", "marin", CHECKED, 1180, None)
    return dataclasses.replace(_card_snapshot(), live=dataclasses.replace(live, **changes))


def test_a_checked_live_model_on_an_openai_connection_is_the_live_target(monkeypatch):
    monkeypatch.setattr(config, "LIVE_VOICE_TEST", True)
    target = ai_settings.live_target(_live_snapshot(voice="cedar"))
    assert (target.api_key, target.model_id, target.voice) == ("sk-proj-secret-abcd", "gpt-live-1", "cedar")
    assert "sk-proj-secret-abcd" not in repr(target)   # a log line or a traceback never prints the key
    monkeypatch.setattr(ai_settings, "_snapshot", _live_snapshot())
    assert ai_settings.live_target() == ai_settings.LiveTarget("sk-proj-secret-abcd", "gpt-live-1", "marin")


@pytest.mark.parametrize("change", [
    {"tested_at": None},            # never checked, or saved again since the last Check
    {"last_error": "live_auth"},    # the last Check failed
    {"connection_id": 2},           # an OpenRouter connection
    {"connection_id": 9},           # a connection that is gone
    {"connection_id": None},
])
def test_a_live_model_is_usable_only_when_checked_on_an_openai_connection(monkeypatch, change):
    monkeypatch.setattr(config, "LIVE_VOICE_TEST", True)
    assert ai_settings.live_target(_live_snapshot(**change)) is None


def test_no_live_target_without_the_test_switch_a_saved_model_or_a_readable_key(monkeypatch):
    monkeypatch.setattr(config, "LIVE_VOICE_TEST", False)
    assert ai_settings.live_target(_live_snapshot()) is None
    monkeypatch.setattr(config, "LIVE_VOICE_TEST", True)
    assert ai_settings.live_target(_card_snapshot()) is None   # no live model saved
    assert ai_settings.live_target() is None                   # the .env setup has none
    snapshot = _live_snapshot()
    unreadable = dataclasses.replace(snapshot.connections[1], api_key="", key_readable=False)
    snapshot = dataclasses.replace(snapshot, connections={**snapshot.connections, 1: unreadable})
    assert ai_settings.live_target(snapshot) is None


def test_the_public_view_shows_the_live_model_without_its_key(monkeypatch):
    monkeypatch.setattr(config, "LIVE_VOICE_TEST", True)
    view = ai_settings.public(_live_snapshot())
    assert view["live"] == {"connection_id": 1, "model_id": "gpt-live-1", "voice": "marin",
                            "tested_at": CHECKED.isoformat(), "test_ms": 1180, "last_error": None, "usable": True}
    assert view["live_voice_test"] is True and "sk-proj-secret-abcd" not in json.dumps(view)
    monkeypatch.setattr(config, "LIVE_VOICE_TEST", False)
    view = ai_settings.public(_live_snapshot())
    assert view["live_voice_test"] is False and view["live"]["usable"] is False
    assert ai_settings.public(_card_snapshot())["live"] is None   # nothing saved yet
