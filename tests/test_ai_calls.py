"""Model calls through the AI connections: the request each service gets, the job's main then backup model, the
notices naming the services in use, and failed calls noted for the card."""

import asyncio
import dataclasses
import json
from datetime import date

import pytest
import requests

from app import config
from app.services import ai_profile, ai_settings, ai_store, conversation, legal_service, memory

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


def _http_answer(status, body):
    """A real requests Response, so the note shows what the card gets from requests itself."""
    response = requests.models.Response()
    response.status_code = status
    response._content = (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
    response.url = "https://api.openai.com/v1/chat/completions"
    return response


MAX_TOKENS_REFUSED = ("Unsupported parameter: 'max_tokens' is not supported with this model. "
                      "Use 'max_completion_tokens' instead.")


def test_a_refused_request_notes_the_services_own_words(service, notes, monkeypatch):   # spec §12
    sent, answers = service
    monkeypatch.setattr(ai_settings, "_snapshot", _snapshot())
    answers["gpt-6-luna"] = _http_answer(400, {"error": {"message": MAX_TOKENS_REFUSED, "type": "invalid_request_error"}})
    text, metrics = _reply_after_notes()
    assert text == "您好！" and metrics["attempts"][0]["status"] == 400   # the backup answered
    assert notes[0] == (11, f"HTTP 400: {MAX_TOKENS_REFUSED}")


ECHOED_KEY = "AIzaEchoProbe-1234567890"


@pytest.mark.parametrize("body", [{"error": {"message": f"Invalid API key: {ECHOED_KEY}"}},
                                  "x" * 280 + ECHOED_KEY])   # an error page: the key straddles the 300-character cut
def test_a_key_the_service_echoes_never_reaches_the_note_or_the_log(service, notes, monkeypatch, caplog, body):
    sent, answers = service
    monkeypatch.setattr(ai_settings, "_snapshot", _snapshot(openai_key=ECHOED_KEY))
    answers["gpt-6-luna"] = _http_answer(401, body)
    with caplog.at_level("WARNING"):
        _reply_after_notes()
    (ref, note), *_ = notes
    assert ref == 11 and note.startswith("HTTP 401: ") and "gpt-6-luna" in caplog.text
    assert ECHOED_KEY[:8] not in note and ECHOED_KEY[:8] not in caplog.text


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
