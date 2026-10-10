"""Model calls through the AI connections: the request each service gets, the job's main then backup model, the
notices naming the services in use, and failed calls noted for the card."""

import asyncio
import dataclasses
import json

import pytest

from app import config
from app.services import ai_profile, ai_settings, ai_store, conversation, legal_service

LUNA = {"token_param": "max_completion_tokens", "temperature": False, "reasoning_off": {"reasoning_effort": "none"}}


class Response:
    def __init__(self, status=200, content="您好！"):
        self.status_code, self.content = status, content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return {"model": "served", "usage": {"completion_tokens": 5},
                "choices": [{"finish_reason": "stop", "message": {"content": self.content}}]}


@pytest.fixture
def service(monkeypatch):
    """A fake network: answers[model id] is a Response; every request is kept."""
    sent, answers = [], {}

    class Session:
        def post(self, url, timeout, headers, json):
            sent.append({"url": url, "headers": headers, "json": json})
            return answers.get(json["model"], Response())

    monkeypatch.setattr(conversation, "_http_session", lambda: Session())
    return sent, answers


@pytest.fixture
def notes(monkeypatch):
    found = []

    async def note_call(model_ref, error):
        found.append((model_ref, error))

    monkeypatch.setattr(ai_store, "note_call", note_call)
    return found


@pytest.fixture(autouse=True)
def fresh_notices():
    legal_service.load_documents.cache_clear()
    yield
    legal_service.load_documents.cache_clear()


def _snapshot(fallback=True, openai_key="sk-proj-test-key-9876"):
    openai = ai_settings.Connection(1, "OpenAI", "openai", ai_profile.BASE_URLS["openai"], openai_key,
                                    openai_key[-4:] if openai_key else None, True, "OpenAI")
    openrouter = ai_settings.Connection(2, "OpenRouter", "openrouter", ai_profile.BASE_URLS["openrouter"],
                                        "sk-or-v1-test-5555", "5555", True, "OpenRouter")
    models = {11: ai_settings.Model(11, 1, "gpt-6-luna", "OpenAI", "USA", "30 days", LUNA),
              21: ai_settings.Model(21, 2, "google/gemini-2.5-flash", "Google", "global", "not kept",
                                    ai_profile.starting_profile("openrouter"))}
    return ai_settings.Snapshot({1: openai, 2: openrouter}, models,
                                {job: (11, 21 if fallback else None) for job in ai_settings.JOBS})


def _reply_after_notes():
    async def run():
        result = await conversation.reply_with_metrics([{"role": "patient", "text": "我去散步了"}], "zh-TW")
        await asyncio.sleep(0)   # let the background notes run before the loop closes
        return result
    return asyncio.run(run())


def test_an_openai_model_gets_its_own_request(service, monkeypatch):
    sent, _ = service
    monkeypatch.setattr(config, "OPENROUTER_PROVIDER_ONLY", "deepinfra")
    target = ai_settings.job_targets("reply", _snapshot())[0]
    assert conversation._post([{"role": "user", "content": "嗨"}], 200, target.model_id, 3, None, 0.7,
                              target) == "您好！"
    (request,) = sent
    assert request["url"] == "https://api.openai.com/v1/chat/completions"
    assert request["headers"] == {"Authorization": "Bearer sk-proj-test-key-9876", "Content-Type": "application/json"}
    assert request["json"] == {"model": "gpt-6-luna", "messages": [{"role": "user", "content": "嗨"}],
                               "max_completion_tokens": 200, "reasoning_effort": "none"}


def test_the_reply_tries_the_main_model_then_the_backup(service, notes, monkeypatch):
    sent, answers = service
    monkeypatch.setattr(ai_settings, "_snapshot", _snapshot())
    answers["gpt-6-luna"] = Response(status=503)
    text, metrics = _reply_after_notes()
    assert text == "您好！"
    assert [request["url"] for request in sent] == ["https://api.openai.com/v1/chat/completions",
                                                    "https://openrouter.ai/api/v1/chat/completions"]
    assert [(a["connection"], a["model_requested"], a["usable"]) for a in metrics["attempts"]] == [
        ("OpenAI", "gpt-6-luna", False), ("OpenRouter", "google/gemini-2.5-flash", True)]
    assert sent[1]["json"]["reasoning"] == {"enabled": False}
    assert sent[1]["headers"]["X-Title"] == "MedAiCarePlus Reachy check-in"


def test_a_failed_call_is_noted_for_the_card_and_a_success_clears_it(service, notes, monkeypatch):
    sent, answers = service
    snapshot = _snapshot()
    snapshot = ai_settings.with_model(snapshot, dataclasses.replace(snapshot.models[21], last_error="HTTP 429"))
    monkeypatch.setattr(ai_settings, "_snapshot", snapshot)
    answers["gpt-6-luna"] = Response(status=503)
    _reply_after_notes()
    assert notes == [(11, "HTTP 503"), (21, None)]


def test_a_keyless_main_model_is_skipped_for_the_backup(service, notes, monkeypatch):
    sent, _ = service
    monkeypatch.setattr(ai_settings, "_snapshot", _snapshot(openai_key=""))
    text, metrics = _reply_after_notes()
    assert text == "您好！"
    assert [request["url"] for request in sent] == ["https://openrouter.ai/api/v1/chat/completions"]
    assert [(a["connection"], a["usable"]) for a in metrics["attempts"]] == [("OpenRouter", True)]


def test_an_unchanged_error_is_not_noted_again(service, notes, monkeypatch):
    sent, answers = service
    snapshot = _snapshot()
    snapshot = ai_settings.with_model(snapshot, dataclasses.replace(snapshot.models[11], last_error="HTTP 503"))
    monkeypatch.setattr(ai_settings, "_snapshot", snapshot)
    answers["gpt-6-luna"] = Response(status=503)
    _reply_after_notes()
    assert len(sent) == 2
    assert all(ref != 11 for ref, _ in notes)


def test_a_key_in_an_error_never_reaches_the_log_or_the_note(notes, monkeypatch, caplog):
    class Session:
        def post(self, url, timeout, headers, json):
            raise ValueError("Invalid header value 'Bearer sk-proj-LEAKED-1234'")

    monkeypatch.setattr(conversation, "_http_session", lambda: Session())
    monkeypatch.setattr(ai_settings, "_snapshot", _snapshot(fallback=False))
    with caplog.at_level("WARNING"):
        _reply_after_notes()
    assert "LEAKED" not in caplog.text and "[key removed]" in caplog.text
    assert notes and all("LEAKED" not in (error or "") for _, error in notes)
    assert any("[key removed]" in (error or "") for _, error in notes)


def test_env_models_are_never_noted(service, notes, monkeypatch):
    sent, answers = service
    monkeypatch.setattr(ai_settings, "_snapshot", None)
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-v1-env")
    monkeypatch.setattr(config, "LLM_MODEL", "a/model")
    answers["a/model"] = Response(status=503)
    _reply_after_notes()
    assert notes == [] and len(sent) == 2


def test_a_job_without_a_usable_key_makes_no_call(service, monkeypatch):
    sent, _ = service
    monkeypatch.setattr(ai_settings, "_snapshot", _snapshot(fallback=False, openai_key=""))
    assert asyncio.run(conversation.complete_with_reason([{"role": "user", "content": "x"}])) == (None, "no_key")
    assert sent == []


def test_the_conversation_row_names_the_reply_jobs_main_model(monkeypatch):
    monkeypatch.setattr(ai_settings, "_snapshot", _snapshot())
    assert ai_settings.main_model_id("reply") == "gpt-6-luna"


def test_the_notices_name_the_services_in_use(monkeypatch):
    monkeypatch.setattr(ai_settings, "_snapshot", None)
    monkeypatch.setattr(config, "LLM_SERVICE", "OpenRouter")
    assert legal_service.fills()["LLM_SERVICE"] == "OpenRouter"
    monkeypatch.setattr(ai_settings, "_snapshot", _snapshot())
    fills = legal_service.fills()
    assert fills["LLM_SERVICE"] == "OpenAI / OpenRouter" and fills["LLM_PROVIDER"] == "OpenAI / Google"
    assert fills["OPERATOR_NAME"] == config.OPERATOR_NAME


def test_a_new_service_rewords_the_notice_and_changes_its_hash(monkeypatch):
    monkeypatch.setattr(ai_settings, "_snapshot", None)
    before = legal_service.get_document("robot", "en")
    monkeypatch.setattr(ai_settings, "_snapshot", _snapshot())
    legal_service.load_documents.cache_clear()
    after = legal_service.get_document("robot", "en")
    assert after.sha256 != before.sha256
    assert "OpenAI" in json.dumps(after.document, ensure_ascii=False)
