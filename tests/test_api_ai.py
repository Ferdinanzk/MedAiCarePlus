"""The AI connections card's API: keys never leave the server, refused changes give the card's codes, and the Test
button saves what the service accepted."""

import dataclasses
import json
import sys
import types
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.dependencies import get_current_user
from app.routers import api_ai
from app.services import ai_profile, ai_settings, ai_store, consent_service

OPENROUTER_KEY = "sk-or-v1-routerSECRET-ab12"
NEW_KEY = "sk-proj-newSECRET-9876"


def _snapshot():
    openrouter = ai_settings.Connection(1, "OpenRouter", "openrouter", ai_profile.BASE_URLS["openrouter"],
                                        OPENROUTER_KEY, "ab12", True, "OpenRouter")
    lite = ai_settings.Model(11, 1, "google/gemini-2.5-flash-lite", "Google", "global", "not kept",
                             ai_profile.starting_profile("openrouter"))
    flash = ai_settings.Model(12, 1, "google/gemini-2.5-flash", "Google", "global", "not kept",
                              ai_profile.starting_profile("openrouter"))
    spare = ai_settings.Model(13, 1, "meta/llama-4", "Meta", "", "", None)
    return ai_settings.Snapshot({1: openrouter}, {11: lite, 12: flash, 13: spare},
                                {"reply": (11, 12), "safety": (11, None), "after_chat": (11, None)})


class FakeStore:
    """ai_store without a database: every write edits the snapshot the way load() would read it back."""
    InUse = ai_store.InUse

    def __init__(self):
        self.calls, self.next_id, self.saved_tests = [], 100, []

    @asynccontextmanager
    async def change(self, conn):
        yield

    def _put(self, **changes):
        ai_settings.set_current(dataclasses.replace(ai_settings.current(), **changes))

    def _uses(self, refs):
        return any(ref in refs for pair in ai_settings.current().jobs.values() for ref in pair)

    async def add_connection(self, conn, *, name, kind, base_url, api_key, service_name):
        self.calls.append(("add_connection", name, kind, base_url, api_key, service_name))
        self.next_id += 1
        connection = ai_settings.Connection(self.next_id, name, kind, base_url, api_key,
                                            api_key[-4:] if api_key else None, True, service_name)
        self._put(connections={**ai_settings.current().connections, self.next_id: connection})
        return self.next_id

    async def update_connection(self, conn, connection_id, *, name=None, base_url=None, api_key=None,
                                service_name=None):
        self.calls.append(("update_connection", connection_id, name, base_url, api_key, service_name))

    async def delete_connection(self, conn, connection_id):
        snapshot = ai_settings.current()
        if self._uses({m.model_ref for m in snapshot.models.values() if m.connection_id == connection_id}):
            raise ai_store.InUse()
        self.calls.append(("delete_connection", connection_id))

    async def add_model(self, conn, *, connection_id, model_id, company, region, retention, profile=None):
        self.calls.append(("add_model", connection_id, model_id, company, region, retention))
        self.next_id += 1
        model = ai_settings.Model(self.next_id, connection_id, model_id, company, region, retention, profile)
        self._put(models={**ai_settings.current().models, self.next_id: model})
        return self.next_id

    async def update_model(self, conn, model_ref, *, company, region, retention):
        self.calls.append(("update_model", model_ref, company, region, retention))

    async def delete_model(self, conn, model_ref):
        if self._uses({model_ref}):
            raise ai_store.InUse()
        self.calls.append(("delete_model", model_ref))

    async def save_test(self, conn, model_ref, result):
        self.calls.append(("save_test", model_ref, result["ok"], result["profile"]))
        self.saved_tests.append(result)

    async def set_jobs(self, conn, jobs):
        self.calls.append(("set_jobs", jobs))
        self._put(jobs=dict(jobs))


class Pool:
    def acquire(self):
        class Ctx:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *exc):
                return False
        return Ctx()


class Reply:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


class TextReply:
    """An error page that is not JSON."""

    def __init__(self, status, text):
        self.status_code, self.text = status, text

    def json(self):
        raise ValueError("not JSON")


@pytest.fixture
def api(monkeypatch):
    from app.main import app

    async def get_state(u_id):
        return {"core": {"granted": True, "terms_version": config.TERMS_VERSION}}

    store = FakeStore()
    monkeypatch.setattr(ai_settings, "_snapshot", _snapshot())
    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(api_ai, "ai_store", store)
    monkeypatch.setattr(api_ai, "get_pool", lambda: Pool())
    monkeypatch.setattr(config, "OPENROUTER_PROVIDER_ONLY", "")
    monkeypatch.setattr(config, "OPENROUTER_DATA_COLLECTION", "")
    monkeypatch.setattr(app, "dependency_overrides", {get_current_user: lambda: {"u_id": 1, "name": "A"}})
    return types.SimpleNamespace(client=TestClient(app), store=store)


def test_the_card_never_carries_a_key(api, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "change-me-in-production-32chars!!")
    response = api.client.get("/api/ai/settings")
    body = response.json()
    assert "routerSECRET" not in response.text and body["connections"][0]["key_last4"] == "ab12"
    assert body["weak_secret"] is True and body["no_key_in_use"] is False


def test_a_new_openai_connection_keeps_its_key_on_the_server(api):
    response = api.client.post("/api/ai/connections", json={
        "name": " OpenAI ", "kind": "openai", "api_key": f"  {NEW_KEY}\n", "service_name": "OpenAI"})
    assert response.status_code == 200 and "newSECRET" not in response.text
    # a key pasted with spaces or a newline is stored trimmed (review focus 1)
    assert api.store.calls[-1] == ("add_connection", "OpenAI", "openai", "https://api.openai.com/v1", NEW_KEY, "OpenAI")
    assert [c["key_last4"] for c in response.json()["connections"]] == ["ab12", "9876"]


@pytest.mark.parametrize("base_url, stored", [
    ("http://host.docker.internal:11434/v1/", "http://host.docker.internal:11434/v1"),
    ("https://llm.example.org/v1/chat/completions", "https://llm.example.org/v1"),
])
def test_an_other_service_address_is_cleaned(api, base_url, stored):   # review focus 2
    response = api.client.post("/api/ai/connections", json={
        "name": "Local", "kind": "openai_compatible", "base_url": base_url, "service_name": "Local"})
    assert response.status_code == 200 and api.store.calls[-1][3] == stored


@pytest.mark.parametrize("body, status, code", [
    ({"name": "X", "kind": "openai_compatible", "base_url": "ftp://x", "service_name": "X"}, 400, "invalid_base_url"),
    ({"name": "X", "kind": "openai_compatible", "service_name": "X"}, 400, "invalid_base_url"),
    ({"name": "X", "kind": "gemini", "service_name": "X"}, 400, "invalid_kind"),
    ({"name": "   ", "kind": "openai", "service_name": "X"}, 400, "invalid_name"),   # review focus 3
    ({"name": "X", "kind": "openai", "service_name": "  "}, 400, "invalid_name"),
    ({"name": "OpenRouter", "kind": "openai", "service_name": "X"}, 409, "already_exists"),
])
def test_refused_connections_give_the_cards_codes(api, body, status, code):
    response = api.client.post("/api/ai/connections", json=body)
    assert (response.status_code, response.json()["detail"]) == (status, code)


def test_a_blank_model_name_is_refused(api):
    response = api.client.post("/api/ai/models", json={"connection_id": 1, "model_id": "  "})
    assert response.json() == {"detail": "invalid_name"}


def test_a_connection_used_only_as_a_backup_is_still_in_use(api, monkeypatch):   # review focus 4
    other = ai_settings.Connection(2, "Backup", "openai", ai_profile.BASE_URLS["openai"], "sk-x-1111", "1111", True,
                                   "OpenAI")
    backup = ai_settings.Model(21, 2, "gpt-4.1-mini", "OpenAI", "USA", "30 days", ai_profile.starting_profile("openai"))
    snapshot = _snapshot()
    monkeypatch.setattr(ai_settings, "_snapshot", dataclasses.replace(
        snapshot, connections={**snapshot.connections, 2: other}, models={**snapshot.models, 21: backup},
        jobs={**snapshot.jobs, "safety": (11, 21)}))
    assert api.client.delete("/api/ai/connections/2").json() == {"detail": "in_use"}
    assert api.client.delete("/api/ai/models/21").status_code == 409
    assert api.client.delete("/api/ai/models/13").status_code == 200


def test_only_tested_models_can_be_given_a_job(api):
    jobs = {"reply": {"primary_ref": 13, "fallback_ref": 12}, "safety": {"primary_ref": 11},
            "after_chat": {"primary_ref": 11}}
    assert api.client.put("/api/ai/jobs", json=jobs).json() == {"detail": "untested_model"}
    jobs["reply"]["primary_ref"] = 99
    assert api.client.put("/api/ai/jobs", json=jobs).json() == {"detail": "not_found"}
    jobs["reply"] = {"primary_ref": 12, "fallback_ref": None}
    response = api.client.put("/api/ai/jobs", json=jobs)
    assert response.status_code == 200 and response.json()["jobs"]["reply"] == {"primary_ref": 12, "fallback_ref": None}
    assert api.store.calls[-1] == ("set_jobs", {"reply": (12, None), "safety": (11, None), "after_chat": (11, None)})


def test_in_prod_with_the_robot_the_models_in_use_need_full_notice_details(api, monkeypatch):
    monkeypatch.setattr(config, "REACHY_FEATURE_ENABLED", True)
    monkeypatch.setattr(config, "APP_ENV", "prod")
    jobs = {job: {"primary_ref": 11} for job in ai_settings.JOBS}
    assert api.client.put("/api/ai/jobs", json=jobs).status_code == 200
    assert api.client.patch("/api/ai/models/11", json={"region": " "}).json() == {"detail": "notice_incomplete"}
    assert api.client.patch("/api/ai/models/13", json={"region": " "}).status_code == 200   # not in use
    monkeypatch.setattr(config, "APP_ENV", "dev")
    assert api.client.patch("/api/ai/models/11", json={"region": " "}).status_code == 200


def test_in_prod_an_incomplete_setup_can_be_fixed_one_model_at_a_time(api, monkeypatch):
    """Only a change that adds a problem is refused: with two incomplete models in use, fixing either one is not
    refused because of the other (that would leave no way to fix them)."""
    monkeypatch.setattr(config, "REACHY_FEATURE_ENABLED", True)
    monkeypatch.setattr(config, "APP_ENV", "prod")
    snapshot = _snapshot()
    complete = ai_settings.Model(14, 1, "openai/gpt-4.1-mini", "OpenAI", "USA", "30 days",
                                 ai_profile.starting_profile("openrouter"))
    models = {**snapshot.models, 11: dataclasses.replace(snapshot.models[11], region=""),
              12: dataclasses.replace(snapshot.models[12], region=""), 14: complete}
    monkeypatch.setattr(ai_settings, "_snapshot", dataclasses.replace(
        snapshot, models=models, jobs={"reply": (11, 12), "safety": (14, None), "after_chat": (14, None)}))
    assert api.client.patch("/api/ai/models/11", json={"region": "global"}).status_code == 200
    assert api.client.patch("/api/ai/models/12", json={"region": "global"}).status_code == 200
    jobs = {"reply": {"primary_ref": 11, "fallback_ref": 12}, "safety": {"primary_ref": 14},
            "after_chat": {"primary_ref": 14}}
    assert api.client.put("/api/ai/jobs", json=jobs).status_code == 200   # saved as they are: nothing added
    assert api.client.patch("/api/ai/models/14", json={"region": " "}).json() == {"detail": "notice_incomplete"}
    jobs["safety"] = {"primary_ref": 11}   # an incomplete model given one more job adds a problem
    assert api.client.put("/api/ai/jobs", json=jobs).json() == {"detail": "notice_incomplete"}


def test_the_test_button_saves_what_the_model_accepted(api, monkeypatch):
    sent = []

    def post(url, json, timeout, headers):
        sent.append({"url": url, "json": json, "headers": headers})
        if "temperature" in json:
            return Reply(400, {"error": {"message": "Unsupported value: 'temperature' does not support 0.7 with "
                                                    "this model. Only the default (1) value is supported."}})
        return Reply(200, {"choices": [{"finish_reason": "stop", "message": {"content": "您好！"}}]})

    monkeypatch.setattr(api_ai.requests, "post", post)
    monkeypatch.setattr(api_ai.requests, "get", lambda url, timeout: Reply(200, {"data": [{"id": "other/model"}]}))
    response = api.client.post("/api/ai/models/13/test")
    body = response.json()
    assert body["ok"] is True and body["profile"]["temperature"] is False and body["sample"] == "您好！"
    assert body["profile"]["thinking"] == ai_profile.CANT_THINK   # not in OpenRouter's list: it can't think
    assert "routerSECRET" not in response.text and "connections" in body["settings"]
    assert sent[0]["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert sent[0]["headers"]["Authorization"] == f"Bearer {OPENROUTER_KEY}"
    assert api.store.calls[-1] == ("save_test", 13, True, body["profile"])


def test_a_key_that_cannot_be_read_is_not_tested(api, monkeypatch):   # review focus 5
    snapshot = _snapshot()
    broken = dataclasses.replace(snapshot.connections[1], api_key="", key_readable=False)
    monkeypatch.setattr(ai_settings, "_snapshot", dataclasses.replace(snapshot, connections={1: broken}))
    assert api.client.post("/api/ai/models/11/test").json() == {"detail": "key_unreadable"}
    assert api.client.get("/api/ai/settings").json()["connections"][0]["key_readable"] is False


def test_the_services_model_list_is_sorted_ids(api, monkeypatch):
    def get(url, timeout, headers):
        assert url == "https://openrouter.ai/api/v1/models"
        return Reply(200, {"data": [{"id": "z/model"}, {"id": "a/model"}, {"name": "no id"}]})

    monkeypatch.setattr(api_ai.requests, "get", get)
    assert api.client.get("/api/ai/connections/1/remote-models").json() == {"models": ["a/model", "z/model"]}
    monkeypatch.setattr(api_ai.requests, "get", lambda url, timeout, headers: Reply(401, {"error": "no"}))
    assert api.client.get("/api/ai/connections/1/remote-models").json() == {"detail": "remote_models_unavailable"}


def test_unknown_ids_are_not_found(api):
    assert api.client.patch("/api/ai/connections/77", json={"name": "Y"}).status_code == 404
    assert api.client.post("/api/ai/models/77/test").status_code == 404


def test_a_refused_request_never_echoes_the_key(api):
    key = "sk-proj-LEAKPROBE-1234"
    response = api.client.post("/api/ai/connections", json={"name": "X", "api_key": key, "service_name": "X"})
    assert response.status_code == 422 and response.json() == {"detail": "invalid_request"}
    assert key not in response.text
    long_key = "k" * 600
    response = api.client.patch("/api/ai/connections/1", json={"api_key": long_key})
    assert response.status_code == 422 and response.json() == {"detail": "invalid_request"}
    assert long_key not in response.text


def test_other_routes_keep_the_default_422(api):
    response = api.client.post("/api/notify/generate-code", json={"contact_id": "not a number"})
    assert response.status_code == 422 and isinstance(response.json()["detail"], list)


def _other_service(monkeypatch, key="sk-local-5555"):
    snapshot = _snapshot()
    other = ai_settings.Connection(2, "Local", "openai_compatible", "https://llm.example.org/v1", key,
                                   key[-4:] if key else None, True, "Local")
    monkeypatch.setattr(ai_settings, "_snapshot",
                        dataclasses.replace(snapshot, connections={**snapshot.connections, 2: other}))


def test_a_new_address_needs_a_new_key_when_one_is_stored(api, monkeypatch):
    _other_service(monkeypatch)
    response = api.client.patch("/api/ai/connections/2", json={"base_url": "https://attacker.example/v1"})
    assert (response.status_code, response.json()) == (400, {"detail": "no_key"}) and api.store.calls == []
    response = api.client.patch("/api/ai/connections/2", json={"base_url": "https://attacker.example/v1",
                                                               "api_key": NEW_KEY})
    assert response.status_code == 200
    assert api.store.calls[-1] == ("update_connection", 2, None, "https://attacker.example/v1", NEW_KEY, None)
    response = api.client.patch("/api/ai/connections/2", json={"base_url": "https://llm.example.org/v1/"})
    assert response.status_code == 200 and api.store.calls[-1][3] == "https://llm.example.org/v1"


def test_a_new_address_without_a_stored_key_is_allowed(api, monkeypatch):
    _other_service(monkeypatch, key="")
    assert api.client.patch("/api/ai/connections/2", json={"base_url": "http://other.local/v1"}).status_code == 200


@pytest.mark.parametrize("base_url", ["https://user:secret@llm.example.org/v1", "https://token@llm.example.org",
                                      "http://a@b@llm.example.org/v1"])
def test_an_address_carrying_a_user_name_or_password_is_refused(api, monkeypatch, base_url):
    """It would show on the card in clear, outside the key's encryption."""
    response = api.client.post("/api/ai/connections", json={
        "name": "X", "kind": "openai_compatible", "base_url": base_url, "service_name": "X"})
    assert (response.status_code, response.json()) == (400, {"detail": "invalid_base_url"})
    _other_service(monkeypatch)
    response = api.client.patch("/api/ai/connections/2", json={"base_url": base_url, "api_key": NEW_KEY})
    assert (response.status_code, response.json()) == (400, {"detail": "invalid_base_url"})
    assert api.store.calls == []


def test_an_at_sign_in_the_path_is_still_an_address(api):
    response = api.client.post("/api/ai/connections", json={
        "name": "X", "kind": "openai_compatible", "base_url": "https://llm.example.org/v1/@team", "service_name": "X"})
    assert response.status_code == 200 and api.store.calls[-1][3] == "https://llm.example.org/v1/@team"


ECHOED_KEY = "AIzaEchoProbe-1234567890"


@pytest.mark.parametrize("answer", [
    Reply(401, {"error": {"message": f"Invalid proxy server token passed. Received API Key = {ECHOED_KEY}"}}),
    TextReply(401, "x" * 280 + ECHOED_KEY),   # an error page: the key straddles the 300-character cut
])
def test_a_key_the_service_echoes_never_reaches_the_card(api, monkeypatch, answer):
    """A key of any shape, not only sk-…: the Test answer and the saved last_error never carry any of it."""
    _other_service(monkeypatch, key=ECHOED_KEY)
    model = ai_settings.Model(31, 2, "local-model", "", "", "", None)
    monkeypatch.setattr(ai_settings, "_snapshot", ai_settings.with_model(ai_settings.current(), model))
    monkeypatch.setattr(api_ai.requests, "post", lambda url, json, timeout, headers: answer)
    response = api.client.post("/api/ai/models/31/test")
    assert response.status_code == 200 and response.json()["ok"] is False
    saved = api.store.saved_tests[-1]["error"]
    assert saved.startswith("HTTP 401: ") and response.json()["error"] == saved
    assert ECHOED_KEY[:8] not in response.text and ECHOED_KEY[:8] not in saved


@pytest.mark.parametrize("get", [
    lambda url, timeout, headers: Reply(200, ["not", "a", "dict"]),
    lambda url, timeout, headers: (_ for _ in ()).throw(api_ai.requests.ConnectionError("down")),
])
def test_a_failed_model_list_is_logged_without_the_key(api, monkeypatch, caplog, get):
    monkeypatch.setattr(api_ai.requests, "get", get)
    with caplog.at_level("WARNING", logger=api_ai.log.name):
        response = api.client.get("/api/ai/connections/1/remote-models")
    assert (response.status_code, response.json()) == (502, {"detail": "remote_models_unavailable"})
    assert "AI connection 1 failed" in caplog.text and "routerSECRET" not in caplog.text


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
