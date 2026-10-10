"""The AI setup in use: from .env until the card's tables are loaded, then the main and backup model per job."""

import dataclasses
import json

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
    assert view["jobs"]["safety"] == {"primary_ref": 21, "fallback_ref": None}
    assert view["no_key_in_use"] is False
    assert view["models"][1]["copied_from_env"] is True and view["models"][1]["tested"] is True


def test_no_key_in_use_when_no_model_in_use_has_one():
    snapshot = dataclasses.replace(_card_snapshot(), jobs={job: (21, None) for job in ai_settings.JOBS})
    assert ai_settings.public(snapshot)["no_key_in_use"] is True
