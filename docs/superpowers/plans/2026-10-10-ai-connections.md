# AI Connections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Settings card where any logged-in account adds AI services (OpenRouter, OpenAI, any OpenAI-compatible service) with encrypted keys, adds and tests models, and picks the main and backup model for Reachy's replies, the safety check and the after-chat summary. No service or model is fixed in code.

**Architecture:**
- Three new tables hold the connections, models and jobs. One in-memory snapshot (`ai_settings`) is loaded at startup and replaced after every change (`ai_store.change`), so model calls never query the database.
- `conversation._post` sends each request through a `Target` (a connection plus a model) and shapes it with the model's saved profile (`ai_profile`). The Test button finds the profile by reading each service's 400 messages.
- Until the snapshot is loaded, and in the unit tests, the snapshot is built from `.env` exactly as the app worked before.

**Tech Stack:** Python 3.11, FastAPI, asyncpg (raw SQL), `cryptography` Fernet + HKDF, requests, pytest; React 19 + TypeScript + Tailwind, react-i18next, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-10-ai-connections-design.md` (approved 2026-10-10). Read it before starting.

**Refinements to the spec, decided while planning (all small):**
- **R1.** "Env mode": with no snapshot loaded, `ai_settings.current()` builds one from `.env` on every call. The existing conversation tests therefore keep patching `config.*`, and only two fake signatures change (spec §13 expected the tests to set a snapshot).
- **R2.** The Test button allows up to 200 tokens (spec §7 said 60). It is the real reply budget (`REPLY_MAX_TOKENS`); a model that cannot answer within it cannot serve replies.
- **R3.** Every write route returns the whole card (`ai_settings.public` + `weak_secret`), and DELETE returns 200 with it. Spec §10 listed single objects and 204. One source of truth on the client.
- **R4.** Extra error codes: `invalid_kind`, `invalid_name`, `no_key`, `already_exists`, `remote_models_unavailable`.
- **R5.** A notice fill is empty (so the notice keeps its placeholder and shows as incomplete) when **any** model in use lacks that detail. Otherwise one model's missing region would silently disappear behind another's (spec §9 is about empty values; this makes it strict).
- **R6.** `conversation.OPENROUTER_URL` is removed; nothing uses it after the change.

## Global Constraints

- New dependency `cryptography==50.0.1` in `requirements.txt`. 50.0.2's changelog was checked on 2026-10-10: it only rebuilds wheels with OpenSSL 4.0.3 and adds free-threading wheels, so stay on 50.0.1.
- Fernet key = urlsafe-base64(HKDF-SHA256(`SECRET_KEY`, salt=None, info=`b"medaicareplus ai-connection keys"`, length=32)).
- Base URLs: OpenRouter `https://openrouter.ai/api/v1`, OpenAI `https://api.openai.com/v1`. Calls go to `{base_url}/chat/completions`, and the model list to `{base_url}/models`.
- **A plain API key never appears in any API response, log line, error message or test output.** Responses carry `key_last4` only, and errors pass through `ai_crypto.redact`.
- Every new route depends on `get_consented_user` (spec decision 3). `tests/test_consent_enforcement.py` checks this.
- `consent_service.is_current` is unchanged; consents are never re-asked (spec decision 4).
- The keyword screen (`conversation.screen()`) still runs before any model call; nothing here touches it.
- Deadlines, timeouts, token limits and `accept` checks in `conversation.py` are unchanged.
- Backend test command, from `C:\Reachy_Mini\repo\MedAiCarePlus`: `.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider`. **Baseline on 2026-10-10: 1500 passed, 1 failed.** The known pre-existing failure is `tests/test_conversation.py::test_a_broken_background_never_costs_the_reply`: commit c84c0d6 added `CONVERSATION_EXAMPLES` to the fallback prompt but not to this test. Leave it alone and report it.
- Frontend gate, from `frontend_source`: `npx tsc -b` and `npm run build`. Add no new `eslint` errors (20 exist).
- Write files with LF line endings. Match the style of the file being edited: comment density, naming, one-line docstrings that say *why*.
- Commits go on branch `ai-connections` (already created; the spec is commit 0e891d1). Git identity is set for this repository only (kuoyuming / ss248@gms.tcu.edu.tw). End every commit message with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`. **Never push.**

## Review Focus

These five inputs are implied by the spec but not covered by its listed tests, and are the most likely to bite a real person. Each has a test in the task named.

1. **A key pasted from a phone with spaces or a trailing newline** is stored trimmed. Otherwise every call gets a 401 the person cannot see. (Task 6: `test_a_new_openai_connection_keeps_its_key_on_the_server`)
2. **A full address ending in `/chat/completions`, or with a trailing `/`, pasted for an "Other" service**, is cleaned to the base. Otherwise calls go to `/chat/completions/chat/completions` and get 404. (Task 6: `test_an_other_service_address_is_cleaned`)
3. **A name or model name of only spaces** is refused with `invalid_name`. It is never stored as an empty name. (Task 6: `test_refused_connections_give_the_cards_codes`)
4. **A connection whose model is only a job's *backup*** still counts as in use and cannot be deleted. Deleting it would leave a job pointing at nothing. (Task 5: `test_what_a_job_uses_cannot_be_deleted`; Task 6: `test_a_connection_used_only_as_a_backup_is_still_in_use`)
5. **`SECRET_KEY` changed after keys were saved.** Startup still works, that connection shows "enter the key again", Test refuses with `key_unreadable`, and replies use the other models or the fixed line. (Task 5: `test_load_reads_keys_and_profiles`; Task 6: `test_a_key_that_cannot_be_read_is_not_tested`)

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `app/services/ai_crypto.py` | new | Encrypt and decrypt saved keys, `last4`, `weak_secret`, `redact`. |
| `app/services/ai_profile.py` | new | A model's request profile: starting point per kind, `adjust` from a 400 message, `detect`, `request_body`, `headers`. Pure, no network. |
| `app/services/ai_settings.py` | new | The snapshot (dataclasses), env mode, `job_targets`, notice fills, `problems`, `public` view. Pure, no database. |
| `app/services/ai_store.py` | new | SQL: `load`, `seed_from_env`, `change()` (transaction + reload + notice registration), writes, `note_call`. |
| `app/routers/api_ai.py` | new | `/api/ai/*` routes; validation and error codes; the Test button's sender. |
| `sql/init.sql` | modify | Three tables. |
| `app/services/conversation.py` | modify | `_post` through a `Target`; `_complete(job=…)`; `_reason(…, job)`; `_note_call`. |
| `app/services/legal_service.py` | modify | `fills()` takes the LLM values from the loaded snapshot. |
| `app/routers/api_device.py` | modify | The conversation's `model` column comes from the reply job. |
| `app/startup_checks.py` | modify | LLM checks move to `check_ai_settings(snapshot)`. |
| `app/main.py` | modify | Lifespan seed, load and check; router include. |
| `requirements.txt` | modify | `cryptography==50.0.1`. |
| `frontend_source/src/lib/ai-api.ts` | new | Types and calls. |
| `frontend_source/src/components/AiConnectionsCard.tsx` | new | The card. |
| `frontend_source/src/pages/Settings.tsx` | modify | Render the card under `ReachyCard`. |
| `frontend_source/src/i18n.ts` | modify | `aiConnections` in `en` and `zh-TW`. |
| `frontend_source/tests/ai-connections.spec.ts` | new | Playwright, API mocked. |
| `CLAUDE.md`, `.env.example` | modify | Say where the LLM settings live now. |
| Tests: `tests/test_ai_crypto.py`, `test_ai_profile.py`, `test_ai_settings.py`, `test_ai_calls.py`, `test_ai_store.py`, `test_api_ai.py` | new | One per unit. |
| `tests/test_conversation.py`, `tests/test_startup_checks.py` | modify | Two fake signatures; the moved LLM checks. |

---

### Task 1: Key encryption (`ai_crypto`)

**Files:**
- Create: `app/services/ai_crypto.py`
- Modify: `requirements.txt` (add one line at the end)
- Test: `tests/test_ai_crypto.py`

**Interfaces:**
- Consumes: `config.SECRET_KEY`.
- Produces:
  - `encrypt(api_key: str, secret: str | None = None) -> str`
  - `decrypt(token: str, secret: str | None = None) -> str | None`
  - `last4(api_key: str) -> str`
  - `weak_secret() -> bool`
  - `redact(text: str) -> str`
  - `DEFAULT_SECRET: str`

- [ ] **Step 1: Write the failing tests**

`tests/test_ai_crypto.py`:

```python
"""Saved API keys: encrypted with a key derived from SECRET_KEY, unreadable under another one, never echoed."""

from app import config
from app.services import ai_crypto


def test_a_key_round_trips_under_the_same_secret():
    token = ai_crypto.encrypt("sk-test-abcd1234", secret="secret-one")
    assert token != "sk-test-abcd1234" and "abcd1234" not in token
    assert ai_crypto.decrypt(token, secret="secret-one") == "sk-test-abcd1234"


def test_a_key_saved_under_another_secret_cannot_be_read():
    token = ai_crypto.encrypt("sk-test-abcd1234", secret="secret-one")
    assert ai_crypto.decrypt(token, secret="secret-two") is None
    assert ai_crypto.decrypt("not-a-token", secret="secret-one") is None


def test_the_secret_comes_from_config_by_default(monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "from-config")
    assert ai_crypto.decrypt(ai_crypto.encrypt("sk-x-123456"), secret="from-config") == "sk-x-123456"


def test_last4_and_weak_secret(monkeypatch):
    assert ai_crypto.last4("sk-or-v1-0123456789wxyz") == "wxyz"
    monkeypatch.setattr(config, "SECRET_KEY", ai_crypto.DEFAULT_SECRET)
    assert ai_crypto.weak_secret()
    monkeypatch.setattr(config, "SECRET_KEY", "a-real-random-secret")
    assert not ai_crypto.weak_secret()


def test_redact_removes_key_shapes_from_error_text():
    cleaned = ai_crypto.redact("Incorrect API key provided: sk-proj-AbCdEf123456. Sent Bearer sk-or-v1-zzzzzzzz")
    assert "AbCdEf123456" not in cleaned and "zzzzzzzz" not in cleaned
    assert cleaned.startswith("Incorrect API key provided: [key removed]")
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ai_crypto.py -q -p no:cacheprovider`
Expected: `ModuleNotFoundError: No module named 'app.services.ai_crypto'` (or ImportError).

- [ ] **Step 3: Add the dependency and the module**

Append to `requirements.txt`:

```text
cryptography==50.0.1
```

Install it into the test venv: `uv pip install --python .venv/Scripts/python.exe cryptography==50.0.1`. It is already installed there on 2026-10-10; the command is a no-op then.

`app/services/ai_crypto.py`:

```python
"""API keys saved on the AI connections card: encrypted at rest with a key derived from SECRET_KEY.

A key never goes back to the browser (only its last 4 characters) and never into logs or error text (redact).
A key saved under another SECRET_KEY cannot be read (decrypt returns None); the card then asks for it again.
"""

import base64
import re

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app import config

DEFAULT_SECRET = "change-me-in-production-32chars!!"
HKDF_INFO = b"medaicareplus ai-connection keys"
# What an API key or an Authorization header looks like inside a service's error text.
_KEY_SHAPES = re.compile(r"sk-[A-Za-z0-9_-]{6,}|Bearer\s+\S+", re.IGNORECASE)


def _fernet(secret: str | None) -> Fernet:
    # A key derived for this purpose only: the face-token signing secret is never used as an encryption key.
    secret = config.SECRET_KEY if secret is None else secret
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=HKDF_INFO).derive(secret.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt(api_key: str, secret: str | None = None) -> str:
    return _fernet(secret).encrypt(api_key.encode("utf-8")).decode("ascii")


def decrypt(token: str, secret: str | None = None) -> str | None:
    """The key, or None when this SECRET_KEY cannot read it."""
    try:
        return _fernet(secret).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def last4(api_key: str) -> str:
    return api_key[-4:]


def weak_secret() -> bool:
    """True while SECRET_KEY is empty or the committed default: the encryption then protects nothing."""
    return config.SECRET_KEY.strip() in ("", DEFAULT_SECRET)


def redact(text: str) -> str:
    return _KEY_SHAPES.sub("[key removed]", text)
```

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ai_crypto.py -q -p no:cacheprovider`
Expected: `5 passed`.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt app/services/ai_crypto.py tests/test_ai_crypto.py
git commit -m "AI connections: encrypt saved API keys (Fernet, key derived from SECRET_KEY)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Request profiles and the Test detection (`ai_profile`)

**Files:**
- Create: `app/services/ai_profile.py`
- Test: `tests/test_ai_profile.py`

**Interfaces:**
- Consumes: `ai_crypto.redact`.
- Produces:
  - Constants: `KINDS`, `BASE_URLS`, `TEST_MESSAGES`, `TEST_MAX_TOKENS = 200`, `TEST_TIMEOUT = 15`, `MAX_TRIES = 5`.
  - `starting_profile(kind: str) -> dict`
  - `adjust(profile: dict, message: str) -> dict | None`
  - `request_body(profile: dict, model_id: str, messages: list[dict], max_tokens: int, temperature: float, extras: dict | None = None) -> dict`
  - `headers(kind: str, api_key: str) -> dict`
  - `detect(send, kind: str) -> dict`. `send(profile) -> (status: int, body: dict | str, ms: int)`, where status 0 means no answer. Returns `{"ok", "profile", "ms", "sample", "error"}`.

- [ ] **Step 1: Write the failing tests**

`tests/test_ai_profile.py`:

```python
"""Request profiles: the settings each model accepts, found from the services' own 400 messages.

The first three messages were measured with OpenAI on 2026-10-10 (spec §7). The reasoning_effort value message is
the same shape, constructed for the test."""

import copy

import pytest

from app.services import ai_profile

MAX_TOKENS_REFUSED = ("Unsupported parameter: 'max_tokens' is not supported with this model. "
                      "Use 'max_completion_tokens' instead.")
TEMPERATURE_REFUSED = ("Unsupported value: 'temperature' does not support 0.7 with this model. "
                       "Only the default (1) value is supported.")
EFFORT_UNKNOWN = "Unrecognized request argument supplied: reasoning_effort"
EFFORT_VALUE_REFUSED = ("Unsupported value: 'reasoning_effort' does not support 'none' with this model. "
                        "Supported values are: 'low', 'medium', and 'high'.")


def _ok(text="您好！"):
    return 200, {"choices": [{"finish_reason": "stop", "message": {"content": text}}]}, 640


def _bad(message, status=400):
    return status, {"error": {"message": message}}, 120


def _service(*rules):
    """A fake model: rules are (refuses(profile), response); the first rule that refuses answers, else 200."""
    sent = []

    def send(profile):
        sent.append(copy.deepcopy(profile))
        for refuses, response in rules:
            if refuses(profile):
                return response
        return _ok()
    return send, sent


def test_gpt_6_luna_keeps_reasoning_off_but_drops_temperature():
    send, sent = _service((lambda p: p["temperature"], _bad(TEMPERATURE_REFUSED)))
    result = ai_profile.detect(send, "openai")
    assert result["ok"] and result["profile"] == {"token_param": "max_completion_tokens", "temperature": False,
                                                  "reasoning_off": {"reasoning_effort": "none"}}
    assert len(sent) == 2 and result["ms"] == 640 and result["sample"] == "您好！" and result["error"] is None


def test_gpt_4_1_mini_refuses_reasoning_effort():
    send, _ = _service((lambda p: p["reasoning_off"], _bad(EFFORT_UNKNOWN)))
    result = ai_profile.detect(send, "openai")
    assert result["profile"] == {"token_param": "max_completion_tokens", "temperature": True, "reasoning_off": None}


def test_a_model_that_refuses_max_tokens_moves_to_max_completion_tokens():
    send, _ = _service((lambda p: p["token_param"] == "max_tokens", _bad(MAX_TOKENS_REFUSED)))
    result = ai_profile.detect(send, "openrouter")
    assert result["profile"] == {"token_param": "max_completion_tokens", "temperature": True,
                                 "reasoning_off": {"reasoning": {"enabled": False}}}


def test_reasoning_effort_none_refused_tries_minimal_then_drops_it():
    send, sent = _service(
        (lambda p: p["reasoning_off"] == {"reasoning_effort": "none"}, _bad(EFFORT_VALUE_REFUSED)),
        (lambda p: p["reasoning_off"] == {"reasoning_effort": "minimal"},
         _bad(EFFORT_VALUE_REFUSED.replace("'none'", "'minimal'"))))
    result = ai_profile.detect(send, "openai")
    assert [p["reasoning_off"] for p in sent] == [{"reasoning_effort": "none"}, {"reasoning_effort": "minimal"}, None]
    assert result["ok"] and result["profile"]["reasoning_off"] is None


@pytest.mark.parametrize("response, words", [
    (_bad("Incorrect API key provided: sk-proj-AbCdEf123456.", 401), "HTTP 401"),
    (_bad("The model `gpt-9` does not exist", 404), "does not exist"),
    (_bad("Rate limit reached", 429), "HTTP 429"),
    (_bad("Something new we do not understand"), "HTTP 400: Something new"),
    ((0, {"error": {"message": "No answer: ReadTimeout"}}, 15000), "No answer: ReadTimeout"),
])
def test_other_failures_save_nothing_and_show_the_services_words(response, words):
    send, sent = _service((lambda p: True, response))
    result = ai_profile.detect(send, "openai")
    assert result["ok"] is False and result["profile"] is None and words in result["error"]
    assert "AbCdEf123456" not in result["error"] and len(sent) == 1


@pytest.mark.parametrize("choice", [{"finish_reason": "stop", "message": {"content": "  "}},
                                    {"finish_reason": "length", "message": {"content": "您"}}])
def test_an_empty_or_cut_off_answer_is_not_a_pass(choice):
    send, _ = _service((lambda p: True, (200, {"choices": [choice]}, 300)))
    assert ai_profile.detect(send, "openai")["ok"] is False


def test_detection_gives_up_after_five_tries():
    send, sent = _service(
        (lambda p: p["token_param"] == "max_tokens", _bad(MAX_TOKENS_REFUSED)),
        (lambda p: p["token_param"] == "max_completion_tokens",
         _bad("Unrecognized request argument supplied: max_completion_tokens")))
    result = ai_profile.detect(send, "openai")
    assert result["ok"] is False and len(sent) == ai_profile.MAX_TRIES and result["error"].startswith("Gave up")


def test_the_request_follows_the_profile():
    luna = {"token_param": "max_completion_tokens", "temperature": False, "reasoning_off": {"reasoning_effort": "none"}}
    body = ai_profile.request_body(luna, "gpt-6-luna", [{"role": "user", "content": "hi"}], 200, 0.7)
    assert body == {"model": "gpt-6-luna", "messages": [{"role": "user", "content": "hi"}],
                    "max_completion_tokens": 200, "reasoning_effort": "none"}


def test_the_openrouter_starting_request_is_todays_request():
    body = ai_profile.request_body(ai_profile.starting_profile("openrouter"), "google/gemini-2.5-flash-lite", [],
                                   200, 0.7, {"provider": {"only": ["x"]}})
    assert body == {"model": "google/gemini-2.5-flash-lite", "messages": [], "max_tokens": 200, "temperature": 0.7,
                    "reasoning": {"enabled": False}, "provider": {"only": ["x"]}}


def test_only_openrouter_gets_the_app_title_header():
    assert ai_profile.headers("openrouter", "k")["X-Title"] == "MedAiCarePlus Reachy check-in"
    assert ai_profile.headers("openai", "k") == {"Authorization": "Bearer k", "Content-Type": "application/json"}


def test_a_starting_profile_is_a_fresh_copy():
    profile = ai_profile.starting_profile("openai")
    profile["reasoning_off"]["reasoning_effort"] = "high"
    assert ai_profile.starting_profile("openai")["reasoning_off"] == {"reasoning_effort": "none"}
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ai_profile.py -q -p no:cacheprovider`
Expected: ImportError for `app.services.ai_profile`.

- [ ] **Step 3: Write the module**

`app/services/ai_profile.py`:

```python
"""How to ask one model: the request settings it accepts (its profile), found by trying, and the request they give.

Models differ even within one service (measured 2026-10-10 with OpenAI): gpt-6-luna and gpt-5.4-mini refuse
max_tokens and want max_completion_tokens; gpt-6-luna refuses any temperature but the default; gpt-4.1-mini refuses
reasoning_effort. So no request is fixed in code: the Test button starts from the service's usual request, reads
each 400 message, changes the one setting it names, and the card saves what worked.
"""

import copy
import re

from app.services.ai_crypto import redact

KINDS = ("openrouter", "openai", "openai_compatible")
BASE_URLS = {"openrouter": "https://openrouter.ai/api/v1", "openai": "https://api.openai.com/v1"}
_OPENAI_STYLE = {"token_param": "max_completion_tokens", "temperature": True,
                 "reasoning_off": {"reasoning_effort": "none"}}
# OpenRouter's is the request conversation._post sent before profiles, so models copied from .env keep working.
STARTING = {"openrouter": {"token_param": "max_tokens", "temperature": True,
                           "reasoning_off": {"reasoning": {"enabled": False}}},
            "openai": _OPENAI_STYLE, "openai_compatible": _OPENAI_STYLE}
MAX_TRIES = 5
TEST_MESSAGES = [{"role": "user", "content": "請用一句很短的繁體中文跟我打招呼。"}]
TEST_MAX_TOKENS = 200   # conversation.REPLY_MAX_TOKENS: a model that cannot answer within it cannot serve replies
TEST_TIMEOUT = 15
_SETTINGS = ("max_completion_tokens", "max_tokens", "reasoning_effort", "reasoning", "temperature")


def starting_profile(kind: str) -> dict:
    return copy.deepcopy(STARTING[kind])


def _named_setting(message: str) -> str | None:
    """The request setting a 400 message is about: the one it names first ("Use 'max_completion_tokens' instead"
    comes after the refused max_tokens)."""
    found = [(match.start(), name) for name in _SETTINGS
             for match in [re.search(rf"\b{name}\b", message)] if match]
    return min(found)[1] if found else None


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
        # A refused value ("does not support 'none'") is worth one try at minimal; an unknown setting is dropped.
        value_refused = "does not support" in message.lower() or "unsupported value" in message.lower()
        new["reasoning_off"] = ({"reasoning_effort": "minimal"}
                                if value_refused and sent_reasoning["reasoning_effort"] == "none" else None)
    elif setting == "reasoning" and "reasoning" in sent_reasoning:
        new["reasoning_off"] = None
    else:
        return None
    return new


def request_body(profile: dict, model_id: str, messages: list[dict], max_tokens: int, temperature: float,
                 extras: dict | None = None) -> dict:
    body = {"model": model_id, "messages": messages, profile["token_param"]: max_tokens}
    if profile["temperature"]:
        body["temperature"] = temperature
    body.update(copy.deepcopy(profile["reasoning_off"] or {}))
    body.update(extras or {})
    return body


def headers(kind: str, api_key: str) -> dict:
    sent = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if kind == "openrouter":
        sent["X-Title"] = "MedAiCarePlus Reachy check-in"
    return sent


def _error_message(body) -> str:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(error, str):
            return error
    return str(body)[:300]


def _failed(ms, error: str) -> dict:
    return {"ok": False, "profile": None, "ms": ms, "sample": None, "error": redact(error)[:300]}


def detect(send, kind: str) -> dict:
    """Find the profile a model accepts. send(profile) -> (HTTP status, parsed body or text, ms) does one request;
    status 0 means no answer came back.

    Returns {"ok", "profile", "ms", "sample", "error"}: on success the profile to save, the successful request's
    time and the answer's first 40 characters; otherwise the service's own words (cut to 300), and nothing to save."""
    profile = starting_profile(kind)
    body, ms = None, None
    for _ in range(MAX_TRIES):
        status, body, ms = send(profile)
        if status == 200:
            choices = (body.get("choices") if isinstance(body, dict) else None) or []
            content = (choices[0].get("message") or {}).get("content") if choices else None
            if not isinstance(content, str) or not content.strip():
                return _failed(ms, "The model answered with no text.")
            if choices[0].get("finish_reason") == "length":
                return _failed(ms, "The answer was cut off by the length limit.")
            return {"ok": True, "profile": profile, "ms": ms, "sample": content.strip()[:40], "error": None}
        message = _error_message(body)
        changed = adjust(profile, message) if status == 400 else None
        if changed is None:
            return _failed(ms, f"HTTP {status}: {message}" if status else message)
        profile = changed
    return _failed(ms, f"Gave up after {MAX_TRIES} tries: {_error_message(body)}")
```

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ai_profile.py tests/test_ai_crypto.py -q -p no:cacheprovider`
Expected: `21 passed` (16 in `test_ai_profile.py` with its parametrized cases, plus 5).

- [ ] **Step 5: Commit**

```bash
git add app/services/ai_profile.py tests/test_ai_profile.py
git commit -m "AI connections: request profiles found from each service's 400 messages" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: The settings snapshot (`ai_settings`)

**Files:**
- Create: `app/services/ai_settings.py`
- Test: `tests/test_ai_settings.py`

**Interfaces:**
- Consumes: `ai_profile.BASE_URLS`, `ai_profile.starting_profile`; `config.OPENROUTER_API_KEY`, `LLM_MODEL`, `LLM_FALLBACK_MODEL`, `LLM_SERVICE`, `LLM_PROVIDER`, `LLM_PROVIDER_REGION`, `LLM_RETENTION`.
- Produces:
  - `JOBS = ("reply", "safety", "after_chat")`, `ENV_CONNECTION_ID = 0`, `ENV_PRIMARY_REF = -1`, `ENV_FALLBACK_REF = -2`.
  - Frozen dataclasses:
    - `Connection(connection_id, name, kind, base_url, api_key, key_last4, key_readable, service_name)`
    - `Model(model_ref, connection_id, model_id, company="", region="", retention="", profile=None, tested_at=None, test_ms=None, last_error=None)`
    - `Target(connection, model)`, with properties `model_id`, `api_key`, `kind`, `url`, `profile`
    - `Snapshot(connections: dict[int, Connection], models: dict[int, Model], jobs: dict[str, tuple[int, int | None]], seeded_at=None)`
  - Functions:
    - `set_current(snapshot | None)`, `loaded() -> Snapshot | None`, `current() -> Snapshot`
    - `env_snapshot()`, `env_target(model_id) -> Target`
    - `job_targets(job, snapshot=None) -> list[Target]`, `main_model_id(job) -> str | None`
    - `notice_fills(snapshot) -> dict[str, str]`, `problems(snapshot) -> list[str]`
    - `with_model(snapshot, model) -> Snapshot`, `public(snapshot) -> dict`

- [ ] **Step 1: Write the failing tests**

`tests/test_ai_settings.py`:

```python
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
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ai_settings.py -q -p no:cacheprovider`
Expected: ImportError for `app.services.ai_settings`.

- [ ] **Step 3: Write the module**

`app/services/ai_settings.py`:

```python
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


@dataclass(frozen=True)
class Connection:
    connection_id: int
    name: str
    kind: str
    base_url: str
    api_key: str            # decrypted; "" when there is none or it cannot be read
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
    """One model to call, with the connection it goes through."""
    connection: Connection
    model: Model

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
class Snapshot:
    connections: dict[int, Connection]
    models: dict[int, Model]
    jobs: dict[str, tuple[int, int | None]]     # job -> (main model_ref, backup model_ref or None)
    seeded_at: datetime | None = None


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
    """The job's main model, then its backup (none when unset); the same model twice is tried twice."""
    snapshot = snapshot or current()
    targets = []
    for ref in snapshot.jobs.get(job, ()):
        model = snapshot.models.get(ref) if ref is not None else None
        if model is not None:
            targets.append(Target(snapshot.connections[model.connection_id], model))
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


def with_model(snapshot: Snapshot, model: Model) -> Snapshot:
    return dataclasses.replace(snapshot, models={**snapshot.models, model.model_ref: model})


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
    }
```

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ai_settings.py -q -p no:cacheprovider`
Expected: `9 passed`.

- [ ] **Step 5: Commit**

```bash
git add app/services/ai_settings.py tests/test_ai_settings.py
git commit -m "AI connections: the settings snapshot, with .env as the setup until the card's tables load" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Model calls go through the jobs (`conversation`, `legal_service`, `api_device`)

**Files:**
- Modify: `app/services/conversation.py`: docstring lines 1, 15-22; imports lines 31-40; `OPENROUTER_URL` line 44; `_post` 338-365; `_complete` 372-415; `_reason` 422-432; `complete_with_reason` 435-443; `reply_with_metrics` 499; `classify_risk` 580-581; `summarize` 651-655
- Modify: `app/services/legal_service.py:13` (import) and `fills()` lines 61-70
- Modify: `app/routers/api_device.py:22` (import list) and line 336
- Modify: `tests/test_conversation.py` lines 119 and 607 (two fake signatures)
- Create: `app/services/ai_store.py`, as a stub holding only `note_call` (Task 5 fills in the rest)
- Test: `tests/test_ai_calls.py`

**Interfaces:**
- Consumes:
  - `ai_settings.job_targets`, `env_target`, `Target`, `main_model_id`, `loaded`, `notice_fills`
  - `ai_profile.request_body`, `headers`
  - `ai_crypto.redact`
  - `ai_store.note_call(model_ref: int, error: str | None) -> None` (async)
- Produces:
  - `conversation._post(messages, max_tokens, model=None, timeout=CALL_TIMEOUT, info=None, temperature=0.7, target=None)`. `target` is the new **7th positional** parameter.
  - `conversation._complete(messages, max_tokens, deadline_seconds, call_timeout, accept, temperature=0.7, *, job: str)`.
  - Each attempt dict gains the key `"connection"`.
  - `conversation._reason(result, metrics, job)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_ai_calls.py`:

```python
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
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ai_calls.py -q -p no:cacheprovider`
Expected: failures (ImportError for `ai_store`, then `_post` rejecting the 7th argument).

- [ ] **Step 3: Create the `ai_store` stub with `note_call`**

`app/services/ai_store.py`. Task 5 adds the rest of this module.

```python
"""The AI connections tables (sql/init.sql): reading them into the snapshot, copying .env in on the first start, and
the writes the card makes. Every change runs inside change(), which reloads the snapshot and registers the
re-worded notices in the same transaction (consent rows reference legal_document by hash)."""

import dataclasses
import logging

from app.database import get_pool
from app.services import ai_settings

log = logging.getLogger(__name__)


async def note_call(model_ref: int, error: str | None) -> None:
    """A real call's outcome for the card ("last call failed"): written only when it changes, and kept in the
    snapshot so the next call compares against it. Never raises: it must not cost a reply."""
    try:
        async with get_pool().acquire() as conn:
            await conn.execute("UPDATE ai_model SET last_error = $2 WHERE model_ref = $1 "
                               "AND last_error IS DISTINCT FROM $2", model_ref, error)
        snapshot = ai_settings.loaded()
        if snapshot is not None and model_ref in snapshot.models:
            ai_settings.set_current(ai_settings.with_model(
                snapshot, dataclasses.replace(snapshot.models[model_ref], last_error=error)))
    except Exception:
        log.exception("could not record the last call of AI model %s", model_ref)
```

- [ ] **Step 4: Change `conversation.py`**

4a. Module docstring.
- Line 1 becomes: `"""Reachy check-in conversations: replies from an LLM (the AI connections card; OpenRouter by default), safety screening, summaries.`
- Lines 15-16: replace "Layers 2 and 3 depend on OpenRouter, so during an outage" with "Layers 2 and 3 depend on the model services, so during an outage".
- Lines 20-22 become:

```text
If no model has a key, is rate-limited or returns nothing, the robot still answers with a fixed line, so a
demo never stalls. A reply tries the reply job's main model, then its backup (ai_settings), inside one deadline
(config.LLM_DEADLINE_SECONDS), and reports how long each attempt took so slow turns can be traced.
```

4b. Imports. Replace `from app.services import context_info, outbox` with:

```python
from app.services import ai_crypto, ai_profile, ai_settings, ai_store, context_info, outbox
```

Delete line 44 (`OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"`). Then run `grep -rn OPENROUTER_URL app tests` and expect no output.

4c. Replace `_post` (lines 338-365) with:

```python
def _post(messages: list[dict], max_tokens: int, model: str | None = None, timeout: float = CALL_TIMEOUT,
          info: dict | None = None, temperature: float = 0.7,
          target: ai_settings.Target | None = None) -> str | None:
    """One model call through target's connection, shaped by its model's profile (ai_profile); without a target,
    OpenRouter with the .env key as before the AI connections card. `info`, when given, receives status,
    model_served, finish_reason and tokens. temperature: 0 for extraction (memory facts), 0.7 for everything else
    (left out for a model that accepts only its default)."""
    info = {} if info is None else info
    target = target or ai_settings.env_target(model or config.LLM_FALLBACK_MODEL)
    provider = _provider() if target.kind == "openrouter" else None
    # The profile turns reasoning off: free reasoning models otherwise spend every token thinking and send nothing.
    payload = ai_profile.request_body(target.profile, target.model_id, messages, max_tokens, temperature,
                                      {"provider": provider} if provider else None)
    response = _http_session().post(target.url, timeout=timeout,
                                    headers=ai_profile.headers(target.kind, target.api_key), json=payload)
    info["status"] = response.status_code
    if response.status_code in (429, 500, 502, 503):
        raise RuntimeError(f"HTTP {response.status_code}")
    response.raise_for_status()
    body = response.json()
    choices = body.get("choices") or []
    info.update(model_served=body.get("model"), tokens=(body.get("usage") or {}).get("completion_tokens"),
                finish_reason=choices[0].get("finish_reason") if choices else None)
    if not choices or choices[0].get("finish_reason") == "length":
        return None   # cut off mid-sentence (often a reasoning model that ran out of tokens): never speak half a reply
    content = (choices[0].get("message") or {}).get("content")
    return content.strip() if isinstance(content, str) and content.strip() else None
```

4d. Replace `_complete` (lines 372-415) with the version below, and add `_note_call` plus `_call_error` above it:

```python
_NOTE_TASKS: set[asyncio.Task] = set()


def _call_error(status, problem) -> str:
    if problem is not None:
        return ai_crypto.redact(str(problem))[:300]
    return "No answer in time" if status == "timeout" else "No usable answer"


def _note_call(target: ai_settings.Target, error: str | None) -> None:
    """Keep the card's "last call failed" current: a failure's reason, cleared by the next success. Only for models
    saved on the card (the .env ones have no row), only when it changes, and never in the reply's way."""
    if target.model.model_ref <= 0 or target.model.last_error == error:
        return
    task = asyncio.get_running_loop().create_task(ai_store.note_call(target.model.model_ref, error))
    _NOTE_TASKS.add(task)
    task.add_done_callback(_NOTE_TASKS.discard)


async def _complete(messages: list[dict], max_tokens: int, deadline_seconds: float, call_timeout: float,
                    accept, temperature: float = 0.7, *, job: str) -> tuple[object | None, dict]:
    """The job's main model, then its backup (ai_settings), never past `deadline_seconds` in total.

    accept(answer) turns an answer into the result, or returns None when it is unusable (the next model is tried).
    Returns (result or None, {"llm_ms", "attempts"}). Each attempt records connection, model_requested,
    model_served, ms, status (the HTTP status, or "timeout" / "error" when none came back), finish_reason, tokens
    and usable. A model whose connection has no readable key is skipped.
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
        if result is None:
            log.warning("%s %s gave no usable answer (status %s, finish %s): %s", target.connection.name,
                        target.model_id, status, info.get("finish_reason"), problem or "unusable text")
        attempts.append({"connection": target.connection.name, "model_requested": target.model_id,
                         "model_served": info.get("model_served"), "ms": _ms_since(call_started),
                         "status": status, "finish_reason": info.get("finish_reason"),
                         "tokens": info.get("tokens"), "usable": result is not None})
        _note_call(target, None if result is not None else _call_error(status, problem))
        if result is not None:
            break
    return result, {"llm_ms": _ms_since(started), "attempts": attempts}
```

4e. Replace `_reason` (lines 422-432) with:

```python
def _reason(result, metrics: dict, job: str) -> str:
    """Why a job's model chain (_complete) gave no result: ok (it did), no_key, empty (a model answered, but nothing
    usable), rate_limited (no answer, and a 429 among the failures) or unavailable."""
    if result is not None:
        return "ok"
    if not any(target.api_key for target in ai_settings.job_targets(job)):
        return "no_key"
    statuses = [attempt["status"] for attempt in metrics["attempts"]]
    if any(not _failed_call(status) for status in statuses):
        return "empty"
    return "rate_limited" if 429 in statuses else "unavailable"
```

4f. Pass the jobs to the callers:
- `complete_with_reason`, lines 441-443:

```python
    result, metrics = await _complete(messages, max_tokens, deadline, call_timeout,
                                      accept or (lambda answer: answer or None), temperature, job="after_chat")
    return result, _reason(result, metrics, "after_chat")
```

- `reply_with_metrics`, line 499:

```python
    answer, metrics = await _complete(messages, REPLY_MAX_TOKENS, config.LLM_DEADLINE_SECONDS, CALL_TIMEOUT, accept,
                                      job="reply")
```

- `classify_risk`, lines 580-581:

```python
        result, metrics = await _complete(_risk_messages(history, language), RISK_MAX_TOKENS, deadline,
                                          call_timeout, accept, job="safety")
```

- `summarize`, lines 651-655:

```python
    result, metrics = await _complete([{"role": "system", "content": SUMMARY_PROMPT[language]},
                                       {"role": "user", "content": transcript}],
                                      SUMMARY_MAX_TOKENS, SUMMARY_DEADLINE_SECONDS, SUMMARY_CALL_TIMEOUT, accept,
                                      job="after_chat")
    if info is not None:
        info.update(reason=_reason(result, metrics, "after_chat"), attempts=metrics["attempts"])
```

4g. `legal_service.py`. Change line 13 to `from app import config` + `from app.services import ai_settings` (two lines). Replace `fills()` with:

```python
def fills() -> dict[str, str]:
    values = {
        "OPERATOR_NAME": config.OPERATOR_NAME,
        "OPERATOR_CONTACT": config.OPERATOR_CONTACT,
        "TUNNEL_PROVIDER": config.TUNNEL_PROVIDER,
        "LLM_SERVICE": config.LLM_SERVICE,
        "LLM_PROVIDER": config.LLM_PROVIDER,
        "LLM_PROVIDER_REGION": config.LLM_PROVIDER_REGION,
        "LLM_RETENTION": config.LLM_RETENTION,
    }
    snapshot = ai_settings.loaded()
    if snapshot is not None:   # the AI connections card names the services in use (spec 2026-10-10 §9)
        values.update(ai_settings.notice_fills(snapshot))
    return values
```

4h. `api_device.py`:
- Add `ai_settings` to the `from app.services import (...)` list on line 22, keeping it alphabetical: `(after_chat, ai_settings, consent_service, ...`.
- On line 336, replace `config.LLM_MODEL or config.LLM_FALLBACK_MODEL` with `ai_settings.main_model_id("reply")`.

4i. `tests/test_conversation.py`. The two fakes of `_post` gain the new parameter.
- Line 119 becomes `    def post(messages, max_tokens, model=None, timeout=conversation.CALL_TIMEOUT, info=None, temperature=0.7, target=None):`
- Line 607 becomes `    def post(messages, max_tokens, model=None, timeout=conversation.CALL_TIMEOUT, info=None, temperature=0.7, target=None):`
- The other fakes take `*args` and need no change.

- [ ] **Step 5: Run the new tests and the whole suite**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ai_calls.py -q -p no:cacheprovider`
Expected: `8 passed`.

Run: `.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider`
Expected: everything passes except the known `test_a_broken_background_never_costs_the_reply`. The count rises by the new tests (Tasks 1-4).

- [ ] **Step 6: Commit**

```bash
git add app/services/conversation.py app/services/legal_service.py app/services/ai_store.py app/routers/api_device.py tests/test_ai_calls.py tests/test_conversation.py
git commit -m "AI connections: model calls go through each job's main and backup model" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Tables, loading, the first start, and startup checks (`ai_store`, `init.sql`, lifespan)

**Files:**
- Modify: `sql/init.sql` (append at the end)
- Modify: `app/services/ai_store.py` (add to the Task 4 stub)
- Modify: `app/main.py`: lines 23-24 (imports) and 33-36 (lifespan)
- Modify: `app/startup_checks.py`: lines 20-27, plus a new function
- Modify: `tests/test_startup_checks.py`: replace `test_robot_enabled_requires_voice_and_risk_configuration`; add tests
- Test: `tests/test_ai_store.py`

**Interfaces:**
- Consumes: `ai_crypto.encrypt/decrypt/last4`, `ai_profile.BASE_URLS/starting_profile`, `ai_settings.*`, `legal_service.load_documents/register_documents`, `database.get_pool`.
- Produces:
  - `ai_store.InUse(Exception)`
  - `load(conn) -> Snapshot`
  - `seed_from_env(conn) -> bool`
  - `change(conn)` (an async context manager)
  - `add_connection(conn, *, name, kind, base_url, api_key, service_name) -> int`
  - `update_connection(conn, connection_id, *, name=None, base_url=None, api_key=None, service_name=None)`
  - `delete_connection(conn, connection_id)`
  - `add_model(conn, *, connection_id, model_id, company, region, retention, profile=None) -> int`
  - `update_model(conn, model_ref, *, company, region, retention)`
  - `delete_model(conn, model_ref)`
  - `save_test(conn, model_ref, result)`
  - `set_jobs(conn, jobs: dict[str, tuple[int, int | None]])`
  - `startup_checks.check_ai_settings(snapshot, env=None) -> list[str]`

- [ ] **Step 1: Write the failing tests**

`tests/test_ai_store.py`:

```python
"""The AI connections tables: read into the snapshot, copied from .env on the first start, changed in one
transaction together with the re-worded notices, and refusing to drop what a job uses."""

import asyncio
import json
import sys
import types

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.services import ai_crypto, ai_profile, ai_settings, ai_store, legal_service


class Conn:
    """Answers fetch by table name and fetchval from a queue; keeps every statement with its arguments."""

    def __init__(self, tables=None, values=()):
        self.tables, self.values, self.sql = tables or {}, list(values), []

    async def fetch(self, query, *args):
        self.sql.append((query, args))
        return next((rows for table, rows in self.tables.items() if f"FROM {table}" in query), [])

    async def fetchval(self, query, *args):
        self.sql.append((query, args))
        return self.values.pop(0) if self.values else None

    async def execute(self, query, *args):
        self.sql.append((query, args))

    def transaction(self):
        sql = self.sql

        class Transaction:
            async def __aenter__(self):
                sql.append("BEGIN")

            async def __aexit__(self, exc_type, *rest):
                sql.append("ROLLBACK" if exc_type else "COMMIT")
                return False
        return Transaction()

    def statements(self, prefix):
        return [args for entry in self.sql if isinstance(entry, tuple)
                for query, args in [entry] if query.startswith(prefix)]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(ai_settings, "_snapshot", None)
    monkeypatch.setattr(config, "SECRET_KEY", "the-secret")


def _env(monkeypatch, model="google/gemini-2.5-flash-lite", fallback="google/gemini-2.5-flash"):
    for key, value in {"OPENROUTER_API_KEY": "sk-or-v1-env-4321", "LLM_MODEL": model, "LLM_FALLBACK_MODEL": fallback,
                       "LLM_SERVICE": "OpenRouter", "LLM_PROVIDER": "Google", "LLM_PROVIDER_REGION": "global",
                       "LLM_RETENTION": "not kept"}.items():
        monkeypatch.setattr(config, key, value)


def test_load_reads_keys_and_profiles():
    rows = {
        "ai_connection": [
            {"connection_id": 1, "name": "OpenAI", "kind": "openai", "base_url": ai_profile.BASE_URLS["openai"],
             "api_key_enc": ai_crypto.encrypt("sk-proj-k-1234"), "api_key_last4": "1234", "service_name": "OpenAI"},
            {"connection_id": 2, "name": "Old", "kind": "openai", "base_url": ai_profile.BASE_URLS["openai"],
             "api_key_enc": ai_crypto.encrypt("sk-proj-k-5678", secret="an-older-secret"), "api_key_last4": "5678",
             "service_name": "OpenAI"},
            {"connection_id": 3, "name": "Empty", "kind": "openrouter", "base_url": ai_profile.BASE_URLS["openrouter"],
             "api_key_enc": None, "api_key_last4": None, "service_name": "OpenRouter"}],
        "ai_model": [{"model_ref": 11, "connection_id": 1, "model_id": "gpt-5.4-mini", "company": "OpenAI",
                      "region": "USA", "retention": "30 days", "tested_at": None, "test_ms": None, "last_error": None,
                      "profile": json.dumps({"token_param": "max_completion_tokens", "temperature": True,
                                             "reasoning_off": None})}],
        "ai_job": [{"job": "reply", "primary_ref": 11, "fallback_ref": None}]}
    snapshot = asyncio.run(ai_store.load(Conn(rows)))
    assert snapshot.connections[1].api_key == "sk-proj-k-1234" and snapshot.connections[1].key_readable
    # SECRET_KEY changed since this key was saved: kept, unreadable, and startup goes on (review focus 5)
    assert snapshot.connections[2].api_key == "" and snapshot.connections[2].key_readable is False
    assert snapshot.connections[3].api_key == "" and snapshot.connections[3].key_readable is True
    assert snapshot.models[11].profile["token_param"] == "max_completion_tokens"
    assert snapshot.jobs == {"reply": (11, None)}


def test_the_first_start_copies_the_env_setup(monkeypatch):
    _env(monkeypatch)
    conn = Conn(values=[False, 1, 11, 12])   # the tables are empty, then the new ids
    assert asyncio.run(ai_store.seed_from_env(conn)) is True
    (connection,) = conn.statements("INSERT INTO ai_connection")
    assert connection[:3] == ("OpenRouter", "openrouter", "https://openrouter.ai/api/v1")
    assert ai_crypto.decrypt(connection[3]) == "sk-or-v1-env-4321" and connection[4] == "4321"
    models = conn.statements("INSERT INTO ai_model")
    assert [model[1] for model in models] == ["google/gemini-2.5-flash-lite", "google/gemini-2.5-flash"]
    assert models[0][2:5] == ("Google", "global", "not kept")
    assert json.loads(models[0][5]) == ai_profile.starting_profile("openrouter")
    assert conn.statements("INSERT INTO ai_job") == [(job, 11, 12) for job in ai_settings.JOBS]
    assert conn.statements("INSERT INTO ops_state") == [()]
    assert conn.sql[0] == "BEGIN" and conn.sql[-1] == "COMMIT"


def test_equal_env_models_make_one_model_tried_twice(monkeypatch):
    _env(monkeypatch, model="google/gemini-2.5-flash", fallback="google/gemini-2.5-flash")
    conn = Conn(values=[False, 1, 11])
    asyncio.run(ai_store.seed_from_env(conn))
    assert len(conn.statements("INSERT INTO ai_model")) == 1
    assert conn.statements("INSERT INTO ai_job") == [(job, 11, 11) for job in ai_settings.JOBS]


def test_a_second_start_copies_nothing(monkeypatch):
    _env(monkeypatch)
    conn = Conn(values=[True])
    assert asyncio.run(ai_store.seed_from_env(conn)) is False
    assert conn.statements("INSERT") == []


def test_a_change_reloads_the_snapshot_and_registers_the_notices(monkeypatch):
    new = ai_settings.Snapshot({}, {}, {}, None)
    registered = []

    async def load(conn):
        return new

    async def register(conn):
        registered.append(legal_service.load_documents.cache_info().currsize)

    monkeypatch.setattr(ai_store, "load", load)
    monkeypatch.setattr(legal_service, "register_documents", register)
    conn = Conn()

    async def run():
        async with ai_store.change(conn):
            await conn.execute("UPDATE ai_model SET region = $2 WHERE model_ref = $1", 11, "USA")

    asyncio.run(run())
    assert ai_settings.loaded() is new and registered == [0]   # the notices were re-rendered for the new setup
    assert conn.sql[0] == "BEGIN" and conn.sql[-1] == "COMMIT"


def test_a_failed_change_keeps_the_previous_snapshot(monkeypatch):
    previous = ai_settings.Snapshot({}, {}, {"reply": (1, None)}, None)
    monkeypatch.setattr(ai_settings, "_snapshot", previous)
    conn = Conn()

    async def run():
        async with ai_store.change(conn):
            raise ai_store.InUse()

    with pytest.raises(ai_store.InUse):
        asyncio.run(run())
    assert ai_settings.loaded() is previous and conn.sql[-1] == "ROLLBACK"


def test_what_a_job_uses_cannot_be_deleted():
    with pytest.raises(ai_store.InUse):
        asyncio.run(ai_store.delete_model(Conn(values=[True]), 21))
    conn = Conn(values=[True])
    with pytest.raises(ai_store.InUse):
        asyncio.run(ai_store.delete_connection(conn, 2))
    assert "fallback_ref" in conn.sql[0][0]   # a backup counts as in use too (review focus 4)
    conn = Conn(values=[False])
    asyncio.run(ai_store.delete_connection(conn, 2))
    assert [query.split(" WHERE")[0] for query, _ in conn.sql[1:]] == ["DELETE FROM ai_model",
                                                                        "DELETE FROM ai_connection"]


def test_an_update_sets_only_the_given_fields_and_encrypts_a_new_key():
    conn = Conn()
    asyncio.run(ai_store.update_connection(conn, 2, api_key="sk-proj-new-7777"))
    ((query, args),) = conn.sql
    assert query.startswith("UPDATE ai_connection SET api_key_enc = $2, api_key_last4 = $3,")
    assert args[0] == 2 and ai_crypto.decrypt(args[1]) == "sk-proj-new-7777" and args[2] == "7777"


def test_a_call_result_is_written_and_kept_in_memory(monkeypatch):
    conn = Conn()

    class Pool:
        def acquire(self):
            class Ctx:
                async def __aenter__(self):
                    return conn

                async def __aexit__(self, *exc):
                    return False
            return Ctx()

    monkeypatch.setattr(ai_store, "get_pool", lambda: Pool())
    monkeypatch.setattr(ai_settings, "_snapshot",
                        ai_settings.Snapshot({}, {11: ai_settings.Model(11, 1, "gpt-5.4-mini")}, {}, None))
    asyncio.run(ai_store.note_call(11, "HTTP 503"))
    assert ai_settings.current().models[11].last_error == "HTTP 503"
    assert "last_error IS DISTINCT FROM" in conn.sql[0][0]


def test_a_note_never_raises_without_a_database(monkeypatch):
    def no_pool():
        raise RuntimeError("Database pool not initialised")

    monkeypatch.setattr(ai_store, "get_pool", no_pool)
    asyncio.run(ai_store.note_call(11, "HTTP 503"))
```

Append to `tests/test_startup_checks.py`, replacing `test_robot_enabled_requires_voice_and_risk_configuration` (the whole function, lines 51-61):

```python
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
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ai_store.py tests/test_startup_checks.py -q -p no:cacheprovider`
Expected: AttributeErrors (`ai_store.load`, `check_ai_settings`), and the old LLM expectations failing.

- [ ] **Step 3: Add the tables**

Append to `sql/init.sql`:

```sql

-- AI connections card (spec 2026-10-10-ai-connections-design.md): services with encrypted keys, their models with
-- the request profile the Test button found, and the main and backup model of each job. No ON DELETE CASCADE:
-- what a job uses cannot be deleted (409 in_use).
CREATE TABLE IF NOT EXISTS ai_connection (
    connection_id  BIGSERIAL PRIMARY KEY,
    name           VARCHAR(60)  NOT NULL UNIQUE,
    kind           VARCHAR(20)  NOT NULL CHECK (kind IN ('openrouter','openai','openai_compatible')),
    base_url       VARCHAR(300) NOT NULL,
    api_key_enc    TEXT,                     -- Fernet token; NULL = no key yet
    api_key_last4  VARCHAR(4),
    service_name   VARCHAR(80)  NOT NULL,    -- {LLM_SERVICE} in the notices
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS ai_model (
    model_ref      BIGSERIAL PRIMARY KEY,
    connection_id  BIGINT NOT NULL REFERENCES ai_connection(connection_id),
    model_id       VARCHAR(200) NOT NULL,    -- as the service names it, e.g. gpt-5.4-mini
    company        VARCHAR(80)  NOT NULL DEFAULT '',   -- {LLM_PROVIDER}
    region         VARCHAR(80)  NOT NULL DEFAULT '',   -- {LLM_PROVIDER_REGION}
    retention      VARCHAR(200) NOT NULL DEFAULT '',   -- {LLM_RETENTION}
    profile        JSONB,                    -- request settings (ai_profile); NULL = not tested
    tested_at      TIMESTAMPTZ,
    test_ms        INTEGER,
    last_error     TEXT,                     -- last failed real call or test; NULL after a success
    UNIQUE (connection_id, model_id)
);
CREATE TABLE IF NOT EXISTS ai_job (
    job            VARCHAR(20) PRIMARY KEY CHECK (job IN ('reply','safety','after_chat')),
    primary_ref    BIGINT NOT NULL REFERENCES ai_model(model_ref),
    fallback_ref   BIGINT REFERENCES ai_model(model_ref),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

- [ ] **Step 4: Complete `ai_store.py`**

Replace the stub's imports with the lines below, and add everything after `log = …` above `note_call`:

```python
import dataclasses
import json
import logging
from contextlib import asynccontextmanager

from app import config
from app.database import get_pool
from app.services import ai_crypto, ai_profile, ai_settings, legal_service

log = logging.getLogger(__name__)
_IN_USE = "SELECT EXISTS (SELECT 1 FROM ai_job j JOIN ai_model m ON m.model_ref IN (j.primary_ref, j.fallback_ref) "


class InUse(Exception):
    """A job still uses this connection or model, as its main model or its backup."""


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


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
    jobs = {row["job"]: (row["primary_ref"], row["fallback_ref"])
            for row in await conn.fetch("SELECT job, primary_ref, fallback_ref FROM ai_job")}
    seeded_at = await conn.fetchval("SELECT updated_at FROM ops_state WHERE key = 'ai_seeded_from_env'")
    return ai_settings.Snapshot(connections, models, jobs, seeded_at)


async def seed_from_env(conn) -> bool:
    """Copy the .env setup into empty tables (spec §6), so day one works exactly as before; True when it did. The
    table is locked in one transaction, so two starts cannot both copy."""
    async with conn.transaction():
        await conn.execute("LOCK TABLE ai_connection IN EXCLUSIVE MODE")
        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM ai_connection)"):
            return False
        connection_id = await add_connection(
            conn, name="OpenRouter", kind="openrouter", base_url=ai_profile.BASE_URLS["openrouter"],
            api_key=config.OPENROUTER_API_KEY.strip(), service_name=config.LLM_SERVICE or "OpenRouter")
        fallback = config.LLM_FALLBACK_MODEL
        main = config.LLM_MODEL or fallback
        refs = {}
        for model_id in dict.fromkeys([main, fallback]):
            refs[model_id] = await add_model(
                conn, connection_id=connection_id, model_id=model_id, company=config.LLM_PROVIDER,
                region=config.LLM_PROVIDER_REGION, retention=config.LLM_RETENTION,
                profile=ai_profile.starting_profile("openrouter"))
        for job in ai_settings.JOBS:
            await conn.execute("INSERT INTO ai_job (job, primary_ref, fallback_ref) VALUES ($1, $2, $3)",
                               job, refs[main], refs[fallback])
        await conn.execute("INSERT INTO ops_state (key) VALUES ('ai_seeded_from_env') ON CONFLICT (key) DO NOTHING")
    return True


@asynccontextmanager
async def change(conn):
    """One change on the card: the caller's writes, then the new snapshot and the re-worded notices registered, all in
    one transaction (consent rows reference legal_document by hash). On any failure the previous snapshot stays."""
    previous = ai_settings.loaded()
    try:
        async with conn.transaction():
            yield
            ai_settings.set_current(await load(conn))
            legal_service.load_documents.cache_clear()
            await legal_service.register_documents(conn)
    except BaseException:
        ai_settings.set_current(previous)
        legal_service.load_documents.cache_clear()
        raise


async def add_connection(conn, *, name: str, kind: str, base_url: str, api_key: str, service_name: str) -> int:
    return await conn.fetchval(
        "INSERT INTO ai_connection (name, kind, base_url, api_key_enc, api_key_last4, service_name) "
        "VALUES ($1, $2, $3, $4, $5, $6) RETURNING connection_id",
        name, kind, base_url, ai_crypto.encrypt(api_key) if api_key else None,
        ai_crypto.last4(api_key) if api_key else None, service_name)


async def update_connection(conn, connection_id: int, *, name: str | None = None, base_url: str | None = None,
                            api_key: str | None = None, service_name: str | None = None) -> None:
    fields = {"name": name, "base_url": base_url, "service_name": service_name}
    if api_key:
        fields.update(api_key_enc=ai_crypto.encrypt(api_key), api_key_last4=ai_crypto.last4(api_key))
    sets = [(column, value) for column, value in fields.items() if value is not None]
    if not sets:
        return
    assignments = ", ".join(f"{column} = ${index}" for index, (column, _) in enumerate(sets, start=2))
    await conn.execute(f"UPDATE ai_connection SET {assignments}, updated_at = NOW() WHERE connection_id = $1",
                       connection_id, *[value for _, value in sets])


async def delete_connection(conn, connection_id: int) -> None:
    if await conn.fetchval(_IN_USE + "WHERE m.connection_id = $1)", connection_id):
        raise InUse()
    await conn.execute("DELETE FROM ai_model WHERE connection_id = $1", connection_id)
    await conn.execute("DELETE FROM ai_connection WHERE connection_id = $1", connection_id)


async def add_model(conn, *, connection_id: int, model_id: str, company: str, region: str, retention: str,
                    profile: dict | None = None) -> int:
    return await conn.fetchval(
        "INSERT INTO ai_model (connection_id, model_id, company, region, retention, profile) "
        "VALUES ($1, $2, $3, $4, $5, $6::jsonb) RETURNING model_ref",
        connection_id, model_id, company, region, retention, json.dumps(profile) if profile is not None else None)


async def update_model(conn, model_ref: int, *, company: str, region: str, retention: str) -> None:
    await conn.execute("UPDATE ai_model SET company = $2, region = $3, retention = $4 WHERE model_ref = $1",
                       model_ref, company, region, retention)


async def delete_model(conn, model_ref: int) -> None:
    if await conn.fetchval(_IN_USE + "WHERE m.model_ref = $1)", model_ref):
        raise InUse()
    await conn.execute("DELETE FROM ai_model WHERE model_ref = $1", model_ref)


async def save_test(conn, model_ref: int, result: dict) -> None:
    """The Test button's outcome: a found profile and its time, or the reason it failed (the profile stays)."""
    if result["ok"]:
        await conn.execute("UPDATE ai_model SET profile = $2::jsonb, tested_at = NOW(), test_ms = $3, "
                           "last_error = NULL WHERE model_ref = $1", model_ref, json.dumps(result["profile"]),
                           result["ms"])
    else:
        await conn.execute("UPDATE ai_model SET last_error = $2 WHERE model_ref = $1", model_ref, result["error"])


async def set_jobs(conn, jobs: dict[str, tuple[int, int | None]]) -> None:
    for job, (primary_ref, fallback_ref) in jobs.items():
        await conn.execute(
            "INSERT INTO ai_job (job, primary_ref, fallback_ref) VALUES ($1, $2, $3) ON CONFLICT (job) DO UPDATE "
            "SET primary_ref = EXCLUDED.primary_ref, fallback_ref = EXCLUDED.fallback_ref, updated_at = NOW()",
            job, primary_ref, fallback_ref)
```

Keep `note_call` from Task 4 below these functions. Its `dataclasses` import is now in the shared import block.

- [ ] **Step 5: Change the startup checks and the lifespan**

`app/startup_checks.py`, lines 20-27 become:

```python
    if str(value("REACHY_FEATURE_ENABLED")).strip().lower() in ("1", "true", "yes"):
        # The models and the companies the notices name are checked on the AI connections card's settings
        # (check_ai_settings), once the database is up.
        required += ["RISK_CLASSIFIER_API_KEY"]
```

Add after `run_startup_checks`:

```python
def check_ai_settings(snapshot, env: Mapping[str, str] | None = None) -> list[str]:
    """The robot feature's model rules (LLM_* in .env before the AI connections card) on the settings in use:
    warn in dev, stop in prod."""
    def value(key, default=""):
        return env.get(key, default) if env is not None else getattr(config, key, default)

    if str(value("REACHY_FEATURE_ENABLED")).strip().lower() not in ("1", "true", "yes"):
        return []
    problems = ai_settings.problems(snapshot)
    for problem in problems:
        logger.warning("Startup check: %s", problem)
    if problems and str(value("APP_ENV", "dev")).strip().lower() == "prod":
        raise SystemExit(1)
    return problems
```

Also add `from app.services import ai_settings` under `from app import config` in `startup_checks.py`.

`app/main.py`:
- Line 24 becomes `from app.startup_checks import run_startup_checks, check_ai_settings, check_restore_state`.
- Add `from app.services import ai_settings, ai_store` after line 23.
- The lifespan block (lines 33-36) becomes:

```python
    await init_pool()
    async with get_pool().acquire() as conn:
        await check_restore_state(conn)
        await ai_store.seed_from_env(conn)
        ai_settings.set_current(await ai_store.load(conn))
        check_ai_settings(ai_settings.current())
        await register_documents(conn)   # after the AI settings: the notices name the services in use
```

- [ ] **Step 6: Run the tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ai_store.py tests/test_startup_checks.py -q -p no:cacheprovider`
Expected: all pass.

Run: `.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider`
Expected: only the known pre-existing failure.

- [ ] **Step 7: Check the SQL against a real PostgreSQL**

The fake connection cannot catch SQL mistakes, so run the store against a throwaway PostgreSQL 16, the image the stack uses.

```bash
docker run -d --name ai-store-check -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16-alpine
until docker exec ai-store-check pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
```

Write `C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/check_ai_store_pg.py`:

```python
"""Throwaway check of ai_store's SQL on a real PostgreSQL (not committed)."""
import asyncio
import os
import sys

os.environ["DATABASE_URL"] = "postgresql://postgres:test@localhost:55432/postgres"
sys.path.insert(0, r"C:\Reachy_Mini\repo\MedAiCarePlus")

from app import database
from app.services import ai_settings, ai_store, legal_service

PROFILE = {"token_param": "max_completion_tokens", "temperature": True, "reasoning_off": None}


async def main():
    await database.init_pool()   # applies sql/init.sql, twice-safe
    async with database.get_pool().acquire() as conn:
        assert await ai_store.seed_from_env(conn) is True
        assert await ai_store.seed_from_env(conn) is False
        ai_settings.set_current(await ai_store.load(conn))
        print("seeded jobs:", ai_settings.current().jobs, "at", ai_settings.current().seeded_at)
        async with ai_store.change(conn):
            cid = await ai_store.add_connection(conn, name="OpenAI", kind="openai",
                                                base_url="https://api.openai.com/v1",
                                                api_key="sk-proj-pgcheck-1234", service_name="OpenAI")
        assert ai_settings.current().connections[cid].api_key == "sk-proj-pgcheck-1234"
        raw = await conn.fetchval("SELECT api_key_enc FROM ai_connection WHERE connection_id = $1", cid)
        assert "pgcheck" not in raw
        async with ai_store.change(conn):
            ref = await ai_store.add_model(conn, connection_id=cid, model_id="gpt-5.4-mini", company="OpenAI",
                                           region="USA", retention="30 days")
        async with ai_store.change(conn):
            await ai_store.save_test(conn, ref, {"ok": True, "profile": PROFILE, "ms": 800})
        jobs = dict(ai_settings.current().jobs)
        jobs["reply"] = (ref, jobs["reply"][1])
        async with ai_store.change(conn):
            await ai_store.set_jobs(conn, jobs)
        try:
            async with ai_store.change(conn):
                await ai_store.delete_connection(conn, cid)
            raise SystemExit("delete of a used connection was allowed")
        except ai_store.InUse:
            print("in use: refused")
        robot = legal_service.get_document("robot", "en")
        registered = await conn.fetchval(
            "SELECT count(*) FROM legal_document WHERE kind = 'robot' AND sha256 = $1", robot.sha256)
        print("robot notice names OpenAI:", "OpenAI" in str(robot.document), "| registered:", registered)
        await ai_store.note_call(ref, "HTTP 503")
        print("last_error:", await conn.fetchval("SELECT last_error FROM ai_model WHERE model_ref = $1", ref))
        print("profile:", ai_settings.current().models[ref].profile)
    await database.close_pool()


asyncio.run(main())
```

Run, from `C:\Reachy_Mini\repo\MedAiCarePlus`:
`.venv/Scripts/python.exe C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/check_ai_store_pg.py`

Expected output:
- `seeded jobs:` with all three jobs on two refs, and a timestamp
- `in use: refused`
- `robot notice names OpenAI: True | registered: 1`
- `last_error: HTTP 503`
- `profile: {'token_param': 'max_completion_tokens', ...}`

Then: `docker rm -f ai-store-check`.

- [ ] **Step 8: Commit**

```bash
git add sql/init.sql app/services/ai_store.py app/startup_checks.py app/main.py tests/test_ai_store.py tests/test_startup_checks.py
git commit -m "AI connections: tables, first-start copy of .env, and the model checks on the loaded settings" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: The card's API (`api_ai`)

**Files:**
- Create: `app/routers/api_ai.py`
- Modify: `app/main.py`: line 19 (`from app.routers import api_dose_video` becomes `from app.routers import api_ai, api_dose_video`), plus an `include_router` after line 122
- Test: `tests/test_api_ai.py`

**Interfaces:**
- Consumes:
  - `ai_settings.current/public/with_model/problems/JOBS/ENV_CONNECTION_ID/Target`
  - `ai_store.change/add_connection/update_connection/delete_connection/add_model/update_model/delete_model/save_test/set_jobs/InUse`
  - `ai_profile.KINDS/BASE_URLS/TEST_*/request_body/headers/detect`
  - `ai_crypto.weak_secret`, `conversation._provider()`
- Produces: the routes in spec §10. Every success returns the card: `ai_settings.public(...)` plus `weak_secret` (R3). The Test route returns `{ok, ms, sample, profile, error, settings}`.

- [ ] **Step 1: Write the failing tests**

`tests/test_api_ai.py`:

```python
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
        self.calls, self.next_id = [], 100

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


def test_the_test_button_saves_what_the_model_accepted(api, monkeypatch):
    sent = []

    def post(url, json, timeout, headers):
        sent.append({"url": url, "json": json, "headers": headers})
        if "temperature" in json:
            return Reply(400, {"error": {"message": "Unsupported value: 'temperature' does not support 0.7 with "
                                                    "this model. Only the default (1) value is supported."}})
        return Reply(200, {"choices": [{"finish_reason": "stop", "message": {"content": "您好！"}}]})

    monkeypatch.setattr(api_ai.requests, "post", post)
    response = api.client.post("/api/ai/models/13/test")
    body = response.json()
    assert body["ok"] is True and body["profile"]["temperature"] is False and body["sample"] == "您好！"
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
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api_ai.py -q -p no:cacheprovider`
Expected: ImportError for `app.routers.api_ai`.

- [ ] **Step 3: Write the router**

`app/routers/api_ai.py`:

```python
"""The AI connections card (spec docs/superpowers/specs/2026-10-10-ai-connections-design.md): services with their
keys, models with the request profile the Test button finds, and the main and backup model of each job.

Every route needs a consented user; any account may change the setup (spec decision 3). Keys never leave the
server: answers carry only their last 4 characters. Every change answers with the whole card."""

import asyncio
import dataclasses
import re
import time

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import config
from app.database import get_pool
from app.dependencies import get_consented_user
from app.services import ai_crypto, ai_profile, ai_settings, ai_store, conversation

router = APIRouter(prefix="/api/ai", tags=["ai-connections"])
REMOTE_MODELS_LIMIT = 2000
REMOTE_MODELS_TIMEOUT = 10
_BASE_URL = re.compile(r"^https?://[^\s/]+(/\S*)?$", re.IGNORECASE)


class NewConnection(BaseModel):
    name: str = Field(max_length=60)
    kind: str
    base_url: str | None = Field(default=None, max_length=300)
    api_key: str | None = Field(default=None, max_length=500)
    service_name: str = Field(max_length=80)


class ConnectionChange(BaseModel):
    name: str | None = Field(default=None, max_length=60)
    base_url: str | None = Field(default=None, max_length=300)
    api_key: str | None = Field(default=None, max_length=500)
    service_name: str | None = Field(default=None, max_length=80)


class NewModel(BaseModel):
    connection_id: int
    model_id: str = Field(max_length=200)
    company: str = Field(default="", max_length=80)
    region: str = Field(default="", max_length=80)
    retention: str = Field(default="", max_length=200)


class ModelChange(BaseModel):
    company: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    retention: str | None = Field(default=None, max_length=200)


class JobChoice(BaseModel):
    primary_ref: int
    fallback_ref: int | None = None


class Jobs(BaseModel):
    reply: JobChoice
    safety: JobChoice
    after_chat: JobChoice


def _card() -> dict:
    return {**ai_settings.public(ai_settings.current()), "weak_secret": ai_crypto.weak_secret()}


def _name(value: str) -> str:
    value = value.strip()
    if not value:
        raise HTTPException(400, "invalid_name")
    return value


def _base_url(kind: str, base_url: str | None) -> str:
    """The fixed address of a known service, or a cleaned "Other" one: no trailing / and no /chat/completions
    (a pasted full address would otherwise be called as /chat/completions/chat/completions)."""
    if kind not in ai_profile.KINDS:
        raise HTTPException(400, "invalid_kind")
    if kind != "openai_compatible":
        return ai_profile.BASE_URLS[kind]
    url = re.sub(r"/chat/completions/?$", "", (base_url or "").strip(), flags=re.IGNORECASE).rstrip("/")
    if not _BASE_URL.match(url):
        raise HTTPException(400, "invalid_base_url")
    return url


def _connection(connection_id: int) -> ai_settings.Connection:
    connection = ai_settings.current().connections.get(connection_id)
    if connection is None or connection_id == ai_settings.ENV_CONNECTION_ID:
        raise HTTPException(404, "not_found")
    return connection


def _model(model_ref: int) -> ai_settings.Model:
    model = ai_settings.current().models.get(model_ref)
    if model is None or model_ref <= 0:
        raise HTTPException(404, "not_found")
    return model


def _guard(snapshot: ai_settings.Snapshot) -> None:
    """With the robot feature in prod, refuse a setup the next start would refuse (spec §10)."""
    if config.REACHY_FEATURE_ENABLED and config.APP_ENV == "prod" and ai_settings.problems(snapshot):
        raise HTTPException(400, "notice_incomplete")


@router.get("/settings")
async def settings(user: dict = Depends(get_consented_user)):
    return _card()


@router.post("/connections")
async def add_connection(body: NewConnection, user: dict = Depends(get_consented_user)):
    name, service_name = _name(body.name), _name(body.service_name)
    base_url = _base_url(body.kind, body.base_url)
    if any(c.name == name for c in ai_settings.current().connections.values()):
        raise HTTPException(409, "already_exists")
    async with get_pool().acquire() as conn:
        async with ai_store.change(conn):
            await ai_store.add_connection(conn, name=name, kind=body.kind, base_url=base_url,
                                          api_key=(body.api_key or "").strip(), service_name=service_name)
    return _card()


@router.patch("/connections/{connection_id}")
async def update_connection(connection_id: int, body: ConnectionChange, user: dict = Depends(get_consented_user)):
    connection = _connection(connection_id)
    name = _name(body.name) if body.name is not None else None
    if name and any(c.name == name and c.connection_id != connection_id
                    for c in ai_settings.current().connections.values()):
        raise HTTPException(409, "already_exists")
    base_url = None
    if body.base_url is not None:
        if connection.kind != "openai_compatible":
            raise HTTPException(400, "invalid_base_url")
        base_url = _base_url(connection.kind, body.base_url)
    async with get_pool().acquire() as conn:
        async with ai_store.change(conn):
            await ai_store.update_connection(
                conn, connection_id, name=name, base_url=base_url, api_key=(body.api_key or "").strip() or None,
                service_name=_name(body.service_name) if body.service_name is not None else None)
    return _card()


@router.delete("/connections/{connection_id}")
async def delete_connection(connection_id: int, user: dict = Depends(get_consented_user)):
    _connection(connection_id)
    try:
        async with get_pool().acquire() as conn:
            async with ai_store.change(conn):
                await ai_store.delete_connection(conn, connection_id)
    except ai_store.InUse:
        raise HTTPException(409, "in_use") from None
    return _card()


@router.get("/connections/{connection_id}/remote-models")
async def remote_models(connection_id: int, user: dict = Depends(get_consented_user)):
    connection = _connection(connection_id)

    def fetch() -> list[str]:
        response = requests.get(f"{connection.base_url}/models", timeout=REMOTE_MODELS_TIMEOUT,
                                headers=ai_profile.headers(connection.kind, connection.api_key)
                                if connection.api_key else {})
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        data = response.json().get("data") or []
        return sorted({item["id"] for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)})

    try:
        models = await asyncio.to_thread(fetch)
    except Exception:
        raise HTTPException(502, "remote_models_unavailable") from None
    return {"models": models[:REMOTE_MODELS_LIMIT]}


@router.post("/models")
async def add_model(body: NewModel, user: dict = Depends(get_consented_user)):
    model_id = _name(body.model_id)
    _connection(body.connection_id)
    if any(m.connection_id == body.connection_id and m.model_id == model_id
           for m in ai_settings.current().models.values()):
        raise HTTPException(409, "already_exists")
    async with get_pool().acquire() as conn:
        async with ai_store.change(conn):
            await ai_store.add_model(conn, connection_id=body.connection_id, model_id=model_id,
                                     company=body.company.strip(), region=body.region.strip(),
                                     retention=body.retention.strip())
    return _card()


@router.patch("/models/{model_ref}")
async def update_model(model_ref: int, body: ModelChange, user: dict = Depends(get_consented_user)):
    changed = dataclasses.replace(_model(model_ref), **{key: value.strip() for key, value in body.model_dump().items()
                                                        if value is not None})
    _guard(ai_settings.with_model(ai_settings.current(), changed))
    async with get_pool().acquire() as conn:
        async with ai_store.change(conn):
            await ai_store.update_model(conn, model_ref, company=changed.company, region=changed.region,
                                        retention=changed.retention)
    return _card()


@router.delete("/models/{model_ref}")
async def delete_model(model_ref: int, user: dict = Depends(get_consented_user)):
    _model(model_ref)
    try:
        async with get_pool().acquire() as conn:
            async with ai_store.change(conn):
                await ai_store.delete_model(conn, model_ref)
    except ai_store.InUse:
        raise HTTPException(409, "in_use") from None
    return _card()


@router.post("/models/{model_ref}/test")
async def test_model(model_ref: int, user: dict = Depends(get_consented_user)):
    """Find the request settings this model accepts (ai_profile.detect) and save them, or why it failed."""
    model = _model(model_ref)
    connection = ai_settings.current().connections[model.connection_id]
    if not connection.key_readable:
        raise HTTPException(400, "key_unreadable")
    if not connection.api_key:
        raise HTTPException(400, "no_key")
    target = ai_settings.Target(connection, model)
    provider = conversation._provider() if connection.kind == "openrouter" else None   # as real calls are routed

    def send(profile: dict):
        body = ai_profile.request_body(profile, model.model_id, ai_profile.TEST_MESSAGES, ai_profile.TEST_MAX_TOKENS,
                                       0.7, {"provider": provider} if provider else None)
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
            parsed = response.text[:300]
        return response.status_code, parsed, ms

    result = await asyncio.to_thread(ai_profile.detect, send, connection.kind)
    async with get_pool().acquire() as conn:
        async with ai_store.change(conn):
            await ai_store.save_test(conn, model_ref, result)
    return {**result, "settings": _card()}


@router.put("/jobs")
async def set_jobs(body: Jobs, user: dict = Depends(get_consented_user)):
    snapshot = ai_settings.current()
    chosen = {job: (getattr(body, job).primary_ref, getattr(body, job).fallback_ref) for job in ai_settings.JOBS}
    for ref in (ref for refs in chosen.values() for ref in refs if ref is not None):
        model = snapshot.models.get(ref)
        if model is None:
            raise HTTPException(404, "not_found")
        if model.profile is None:
            raise HTTPException(400, "untested_model")
    _guard(dataclasses.replace(snapshot, jobs=chosen))
    async with get_pool().acquire() as conn:
        async with ai_store.change(conn):
            await ai_store.set_jobs(conn, chosen)
    return _card()
```

`app/main.py`: change line 19 to `from app.routers import api_ai, api_dose_video`, and after line 122 add:

```python
app.include_router(api_ai.router,            tags=["ai-connections"])
```

- [ ] **Step 4: Run the tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api_ai.py tests/test_consent_enforcement.py tests/test_port_isolation.py -q -p no:cacheprovider`
Expected: all pass. The consent test proves every new route needs consent, and port isolation proves `/api/ai` is not served on 8001.

Run: `.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider`
Expected: only the known pre-existing failure.

- [ ] **Step 5: Commit**

```bash
git add app/routers/api_ai.py app/main.py tests/test_api_ai.py
git commit -m "AI connections: the card's API (keys stay on the server, Test saves the profile)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: The Settings card (frontend)

**Files:**
- Create: `frontend_source/src/lib/ai-api.ts`
- Create: `frontend_source/src/components/AiConnectionsCard.tsx`
- Modify: `frontend_source/src/pages/Settings.tsx`: import near line 4, and `<AiConnectionsCard />` right after `<ReachyCard />` (line 181)
- Modify: `frontend_source/src/i18n.ts`: insert `aiConnections` right **before** the line `      reachy: {` in `en.translation` (line 13) and in `'zh-TW'.translation` (line 629; recheck the number after the first insert)
- Test: `frontend_source/tests/ai-connections.spec.ts`

**Interfaces:**
- Consumes: the Task 6 routes; `call` and `ApiError` from `src/lib/reachy-api.ts`.
- Produces: the default export `AiConnectionsCard`, with the section `aria-labelledby="ai-title"`. Rows are `role="group"`, named `"<connection name>"` or `"<connection> · <model id>"`.

- [ ] **Step 1: Install and check the toolchain**

Run, in `frontend_source`:
- `npm ci`
- `npx playwright install chromium`
- `node -e "const l=require('lucide-react'); console.log(['Brain','CheckCircle2','Loader2','Lock','Plus','Trash2','TriangleAlert','XCircle'].map(n=>n+':'+!!l[n]).join(' '))"`

Expected: every icon `:true`. All eight are already used in `src/`.

- [ ] **Step 2: Write the failing Playwright spec**

`frontend_source/tests/ai-connections.spec.ts`:

```ts
import { expect, test, type Page } from '@playwright/test';

const OPENAI_KEY = 'sk-proj-PLAYWRIGHT-secret-9876';
const openrouterProfile = { token_param: 'max_tokens', temperature: true, reasoning_off: { reasoning: { enabled: false } } };

function startingSettings() {
  const copied = (model_ref: number, model_id: string) => ({
    model_ref, connection_id: 1, model_id, company: 'Google', region: 'global', retention: 'not kept',
    profile: openrouterProfile, tested: true, copied_from_env: true, tested_at: null, test_ms: null, last_error: null,
  });
  return {
    connections: [{ connection_id: 1, name: 'OpenRouter', kind: 'openrouter', base_url: 'https://openrouter.ai/api/v1',
      key_last4: 'ab12', key_readable: true, has_key: true, service_name: 'OpenRouter' }],
    models: [copied(11, 'google/gemini-2.5-flash-lite'), copied(12, 'google/gemini-2.5-flash')],
    jobs: { reply: { primary_ref: 11, fallback_ref: 12 }, safety: { primary_ref: 11, fallback_ref: 12 },
      after_chat: { primary_ref: 11, fallback_ref: 12 } },
    seeded_at: '2026-10-10T12:00:00+00:00', weak_secret: true, no_key_in_use: false,
  };
}

async function seedSession(page: Page) {
  await page.addInitScript(() => {
    for (const key of ['face_auth_session', 'onboarding_complete', 'onboarding_face_done']) localStorage.setItem(key, 'true');
    localStorage.setItem('face_auth_user', JSON.stringify({ name: 'AI Test', u_id: 7, loginAt: new Date().toISOString() }));
    localStorage.setItem('face_auth_token', 'test-face-token');
  });
}

async function mockApi(page: Page, options: { aiDown?: boolean } = {}) {
  const mock = { settings: startingSettings(), requests: [] as { method: string; path: string; body: any }[] };
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    const body = request.postData() ? request.postDataJSON() : null;
    if (path.startsWith('/api/ai/')) mock.requests.push({ method, path, body });
    if (path === '/api/consent/status') {
      return route.fulfill({ json: { terms_version: '2026-10', core_current: true, robot_current: false,
        scopes: { core: { granted: true, terms_version: '2026-10', kind: 'core' } } } });
    }
    if (path === '/api/reachy/status') return route.fulfill({ json: { paired: false } });
    if (path === '/api/notify/settings') return route.fulfill({ json: {} });
    if (path === '/api/ai/settings') {
      return options.aiDown ? route.fulfill({ status: 500, json: { detail: 'boom' } }) : route.fulfill({ json: mock.settings });
    }
    if (path === '/api/ai/connections' && method === 'POST') {
      mock.settings.connections.push({ connection_id: 2, name: body.name, kind: body.kind, base_url: 'https://api.openai.com/v1',
        key_last4: body.api_key.slice(-4), key_readable: true, has_key: true, service_name: body.service_name });
      return route.fulfill({ json: mock.settings });
    }
    if (path === '/api/ai/connections/1' && method === 'DELETE') return route.fulfill({ status: 409, json: { detail: 'in_use' } });
    if (path === '/api/ai/models' && method === 'POST') {
      mock.settings.models.push({ model_ref: 21, connection_id: body.connection_id, model_id: body.model_id, company: body.company,
        region: body.region, retention: body.retention, profile: null as any, tested: false, copied_from_env: false,
        tested_at: null, test_ms: null, last_error: null });
      return route.fulfill({ json: mock.settings });
    }
    if (path === '/api/ai/models/21/test') {
      const model: any = mock.settings.models.find(m => m.model_ref === 21);
      Object.assign(model, { profile: { token_param: 'max_completion_tokens', temperature: true, reasoning_off: { reasoning_effort: 'none' } },
        tested: true, tested_at: '2026-10-10T13:00:00+00:00', test_ms: 812 });
      return route.fulfill({ json: { ok: true, ms: 812, sample: '您好！很高興見到您。', profile: model.profile, error: null, settings: mock.settings } });
    }
    if (path === '/api/ai/jobs' && method === 'PUT') {
      mock.settings.jobs = body;
      return route.fulfill({ json: mock.settings });
    }
    return route.fulfill({ json: [] });
  });
  return mock;
}

const card = (page: Page) => page.locator('section[aria-labelledby="ai-title"]');

async function addOpenAi(page: Page) {
  await card(page).getByRole('button', { name: 'Add connection' }).click();
  await card(page).getByLabel('API key').fill(OPENAI_KEY);
  await card(page).getByRole('button', { name: 'Save', exact: true }).click();
  // exact: the model rows' names ("OpenAI · …") contain the connection's name
  await expect(card(page).getByRole('group', { name: 'OpenAI', exact: true })).toContainText('Key ••••9876');
}

test('keys show only their last 4 characters, with the default-secret warning', async ({ page }) => {
  await seedSession(page);
  await mockApi(page);
  await page.goto('/settings');
  await expect(card(page).getByRole('group', { name: 'OpenRouter', exact: true })).toContainText('Key ••••ab12');
  await expect(card(page).getByText(/SECRET_KEY/)).toBeVisible();
  await expect(card(page).getByRole('group', { name: 'OpenRouter · google/gemini-2.5-flash-lite', exact: true }))
    .toContainText('Copied from .env');
});

test('a new OpenAI key is sent once and never shown again', async ({ page }) => {
  await seedSession(page);
  const mock = await mockApi(page);
  await page.goto('/settings');
  await addOpenAi(page);
  expect(mock.requests.find(r => r.path === '/api/ai/connections')?.body).toMatchObject(
    { kind: 'openai', name: 'OpenAI', api_key: OPENAI_KEY, service_name: 'OpenAI' });
  await expect(page.locator('body')).not.toContainText('PLAYWRIGHT-secret');
  await expect(card(page).getByLabel('API key')).toHaveCount(0);
});

test('a tested model can be given a job, and the card sends the chosen jobs', async ({ page }) => {
  await seedSession(page);
  const mock = await mockApi(page);
  await page.goto('/settings');
  await addOpenAi(page);
  await card(page).getByRole('button', { name: 'Add model' }).click();
  await card(page).getByLabel('Connection', { exact: true }).selectOption({ label: 'OpenAI' });
  await card(page).getByLabel('Model name').fill('gpt-5.4-mini');
  await card(page).getByLabel('Company behind the model').fill('OpenAI');
  await card(page).getByLabel('Region').fill('USA');
  await card(page).getByLabel('How long it keeps data').fill('30 days');
  await card(page).getByRole('button', { name: 'Add', exact: true }).click();
  const row = card(page).getByRole('group', { name: 'OpenAI · gpt-5.4-mini' });
  await expect(row).toContainText('Not tested');
  const main = card(page).getByLabel("Reachy's replies: Main model");
  await expect(main.locator('option', { hasText: 'gpt-5.4-mini' })).toHaveCount(0);
  await row.getByRole('button', { name: 'Test' }).click();
  await expect(row).toContainText('0.8 s');
  await main.selectOption({ label: 'OpenAI · gpt-5.4-mini' });
  await card(page).getByRole('button', { name: 'Save jobs' }).click();
  await expect.poll(() => mock.requests.find(r => r.path === '/api/ai/jobs')?.body?.reply)
    .toEqual({ primary_ref: 21, fallback_ref: 12 });
});

test("a refused change shows the card's own words", async ({ page }) => {
  await seedSession(page);
  await mockApi(page);
  await page.goto('/settings');
  await card(page).getByRole('button', { name: 'Remove OpenRouter' }).click();
  // the default-secret warning is an alert too, so pick the refusal by its words
  await expect(card(page).getByRole('alert').filter({ hasText: 'A job still uses this' })).toBeVisible();
});

test('the rest of Settings keeps working when the AI settings cannot load', async ({ page }) => {
  await seedSession(page);
  await mockApi(page, { aiDown: true });
  await page.goto('/settings');
  await expect(card(page)).toContainText('Could not load the AI settings.');
  await expect(page.locator('section[aria-labelledby="reachy-title"]')).toBeVisible();
});
```

Write the throwaway Playwright config (outside the repo; it has no imports, so it resolves from anywhere):
`C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/pw-frontend.config.mjs`

```js
export default {
  testDir: 'C:/Reachy_Mini/repo/MedAiCarePlus/frontend_source/tests',
  testMatch: ['*.spec.ts'],
  timeout: 30000,
  workers: 1,
  reporter: 'list',
  use: { baseURL: 'http://localhost:4180', viewport: { width: 393, height: 851 } },
  webServer: {
    command: 'npx vite preview --port 4180 --strictPort',
    cwd: 'C:/Reachy_Mini/repo/MedAiCarePlus/frontend_source',
    url: 'http://localhost:4180',
    reuseExistingServer: false,
    timeout: 60000,
  },
};
```

Run, in `frontend_source`: `npm run build && npx playwright test -c C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/pw-frontend.config.mjs ai-connections`
Expected: the 5 new tests fail (no card yet).

- [ ] **Step 3: Write `src/lib/ai-api.ts`**

```ts
import { ApiError, call } from './reachy-api';

export type AiKind = 'openrouter' | 'openai' | 'openai_compatible';
export type AiJob = 'reply' | 'safety' | 'after_chat';

export interface AiConnection {
  connection_id: number; name: string; kind: AiKind; base_url: string;
  key_last4: string | null; key_readable: boolean; has_key: boolean; service_name: string;
}
export interface AiProfile { token_param: string; temperature: boolean; reasoning_off: Record<string, unknown> | null }
export interface AiModel {
  model_ref: number; connection_id: number; model_id: string; company: string; region: string; retention: string;
  profile: AiProfile | null; tested: boolean; copied_from_env: boolean; tested_at: string | null;
  test_ms: number | null; last_error: string | null;
}
export interface AiJobChoice { primary_ref: number; fallback_ref: number | null }
export interface AiSettings {
  connections: AiConnection[]; models: AiModel[]; jobs: Record<AiJob, AiJobChoice>;
  seeded_at: string | null; weak_secret: boolean; no_key_in_use: boolean;
}
export interface AiTestResult { ok: boolean; ms: number | null; sample: string | null; profile: AiProfile | null; error: string | null }

export const AI_KINDS: AiKind[] = ['openrouter', 'openai', 'openai_compatible'];
export const AI_JOBS: AiJob[] = ['reply', 'safety', 'after_chat'];

const send = (method: string, body?: unknown): RequestInit =>
  body === undefined ? { method } : { method, body: JSON.stringify(body) };

/** The card; a malformed answer is a load failure, so one card can never take the Settings page down. */
export async function fetchAiSettings(): Promise<AiSettings> {
  const settings = await call<AiSettings>('/api/ai/settings', { cache: 'no-store' });
  if (!Array.isArray(settings?.connections) || !Array.isArray(settings?.models) || !settings?.jobs) {
    throw new ApiError(500, 'loadFailed', settings);
  }
  return settings;
}

export const addConnection = (body: { name: string; kind: AiKind; base_url?: string; api_key?: string; service_name: string }) =>
  call<AiSettings>('/api/ai/connections', send('POST', body));

export const updateConnection = (id: number, body: Partial<{ name: string; base_url: string; api_key: string; service_name: string }>) =>
  call<AiSettings>(`/api/ai/connections/${id}`, send('PATCH', body));

export const deleteConnection = (id: number) => call<AiSettings>(`/api/ai/connections/${id}`, send('DELETE'));

export const fetchRemoteModels = (id: number) => call<{ models: string[] }>(`/api/ai/connections/${id}/remote-models`);

export const addModel = (body: { connection_id: number; model_id: string; company: string; region: string; retention: string }) =>
  call<AiSettings>('/api/ai/models', send('POST', body));

export const updateModel = (ref: number, body: { company: string; region: string; retention: string }) =>
  call<AiSettings>(`/api/ai/models/${ref}`, send('PATCH', body));

export const deleteModel = (ref: number) => call<AiSettings>(`/api/ai/models/${ref}`, send('DELETE'));

export const testModel = (ref: number) => call<AiTestResult & { settings: AiSettings }>(`/api/ai/models/${ref}/test`, send('POST'));

export const saveJobs = (jobs: Record<AiJob, AiJobChoice>) => call<AiSettings>('/api/ai/jobs', send('PUT', jobs));
```

- [ ] **Step 4: Write `src/components/AiConnectionsCard.tsx`**

```tsx
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Brain, CheckCircle2, Loader2, Lock, Plus, Trash2, TriangleAlert, XCircle } from 'lucide-react';
import { ApiError } from '../lib/reachy-api';
import {
  AI_JOBS, AI_KINDS, addConnection, addModel, deleteConnection, deleteModel, fetchAiSettings, fetchRemoteModels,
  saveJobs, testModel, updateConnection, updateModel,
  type AiJob, type AiJobChoice, type AiKind, type AiModel, type AiSettings, type AiTestResult,
} from '../lib/ai-api';

/** Codes api_ai.py answers with; anything else shows the general message. */
const KNOWN_ERRORS = ['in_use', 'untested_model', 'not_found', 'invalid_base_url', 'invalid_name', 'invalid_kind',
  'key_unreadable', 'no_key', 'already_exists', 'notice_incomplete', 'remote_models_unavailable'];
const field = 'w-full px-3 py-2 border border-gray-200 rounded-xl text-sm';
const primary = 'px-3 py-2 rounded-xl text-sm font-medium bg-[#0057B8] text-white disabled:opacity-50';
const secondary = 'px-3 py-2 rounded-xl text-sm font-medium border border-gray-200 text-gray-700 disabled:opacity-50';
const warning = 'rounded-xl bg-amber-50 border border-amber-200 p-3 text-sm text-amber-900';

interface ConnectionDraft { kind: AiKind; name: string; base_url: string; api_key: string; service_name: string }
interface ModelDraft { connection_id: number; model_id: string; company: string; region: string; retention: string }
interface NoticeDraft { ref: number; company: string; region: string; retention: string }

const errorCode = (cause: unknown) => (cause instanceof ApiError && KNOWN_ERRORS.includes(cause.message) ? cause.message : 'failed');

export default function AiConnectionsCard() {
  const { t } = useTranslation();
  const [settings, setSettings] = useState<AiSettings | null>(null);
  const [jobs, setJobs] = useState<Record<AiJob, AiJobChoice> | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState<number | null>(null);
  const [results, setResults] = useState<Record<number, AiTestResult>>({});
  const [connectionDraft, setConnectionDraft] = useState<ConnectionDraft | null>(null);
  const [modelDraft, setModelDraft] = useState<ModelDraft | null>(null);
  const [remoteModels, setRemoteModels] = useState<string[]>([]);
  const [keyDraft, setKeyDraft] = useState<{ id: number; key: string } | null>(null);
  const [noticeDraft, setNoticeDraft] = useState<NoticeDraft | null>(null);

  useEffect(() => {
    let active = true;
    fetchAiSettings().then(
      next => { if (active) { setSettings(next); setJobs(next.jobs); } },
      () => { if (active) setLoadFailed(true); },
    );
    return () => { active = false; };
  }, []);

  /** One change; the server answers with the whole card. `done` runs only after a success (e.g. closing a form). */
  const run = async (action: () => Promise<AiSettings>, done?: () => void) => {
    setBusy(true);
    setError('');
    try {
      const next = await action();
      setSettings(next);
      setJobs(next.jobs);
      done?.();
    } catch (cause) {
      setError(errorCode(cause));
    } finally {
      setBusy(false);
    }
  };

  const runTest = async (ref: number) => {
    setTesting(ref);
    setError('');
    try {
      const { settings: next, ...result } = await testModel(ref);
      setResults(previous => ({ ...previous, [ref]: result }));
      setSettings(next);
    } catch (cause) {
      setError(errorCode(cause));
    } finally {
      setTesting(null);
    }
  };

  const loadRemoteModels = async (id: number) => {
    setBusy(true);
    setError('');
    try {
      setRemoteModels((await fetchRemoteModels(id)).models);
    } catch (cause) {
      setError(errorCode(cause));
    } finally {
      setBusy(false);
    }
  };

  const heading = (
    <div className="flex items-center gap-3">
      <div className="w-10 h-10 rounded-xl bg-blue-50 flex items-center justify-center"><Brain className="w-5 h-5 text-[#0057B8]" /></div>
      <div>
        <h2 id="ai-title" className="text-base font-semibold text-gray-900">{t('aiConnections.title')}</h2>
        <p className="text-xs text-gray-500">{t('aiConnections.subtitle')}</p>
      </div>
    </div>
  );

  if (!settings || !jobs) {
    return (
      <section className="bg-white rounded-2xl border border-gray-100 p-5 shadow-sm space-y-4" aria-labelledby="ai-title">
        {heading}
        {loadFailed
          ? <p className="text-sm text-red-700">{t('aiConnections.errors.loadFailed')}</p>
          : <Loader2 className="w-5 h-5 animate-spin text-gray-400" aria-label={t('aiConnections.loading')} />}
      </section>
    );
  }

  const connectionName = (id: number) => settings.connections.find(c => c.connection_id === id)?.name ?? '';
  const label = (model: AiModel) => `${connectionName(model.connection_id)} · ${model.model_id}`;
  const tested = settings.models.filter(model => model.tested);

  const status = (model: AiModel) => {
    const result = results[model.model_ref];
    if (result && !result.ok) return <span className="text-red-700"><XCircle className="inline w-4 h-4" /> {result.error}</span>;
    if (model.last_error) return <span className="text-red-700">{t('aiConnections.lastCallFailed', { error: model.last_error })}</span>;
    if (model.test_ms != null) {
      return (
        <span className="text-green-700"><CheckCircle2 className="inline w-4 h-4" /> {t('aiConnections.testOk', { seconds: (model.test_ms / 1000).toFixed(1) })}
          {result?.sample ? ` · ${result.sample}` : ''}</span>
      );
    }
    if (model.copied_from_env) return <span className="text-gray-500">{t('aiConnections.copiedFromEnv')}</span>;
    return <span className="text-gray-500">{t('aiConnections.notTested')}</span>;
  };

  return (
    <section className="bg-white rounded-2xl border border-gray-100 p-5 shadow-sm space-y-5" aria-labelledby="ai-title">
      {heading}

      {settings.weak_secret && <p role="alert" className={warning}><TriangleAlert className="inline w-4 h-4" /> {t('aiConnections.weakSecret')}</p>}
      {settings.no_key_in_use && <p role="alert" className={warning}>{t('aiConnections.noKeyInUse')}</p>}
      {settings.seeded_at && <p className="text-xs text-gray-500">{t('aiConnections.seeded', { date: new Date(settings.seeded_at).toLocaleDateString() })}</p>}
      {error && <p role="alert" className="rounded-xl bg-red-50 p-3 text-sm text-red-700">{t(`aiConnections.errors.${error}`)}</p>}

      <div className="space-y-2">
        <h3 className="text-sm font-semibold text-gray-900">{t('aiConnections.connections')}</h3>
        {settings.connections.map(connection => (
          <div key={connection.connection_id} role="group" aria-label={connection.name} className="rounded-xl border border-gray-100 p-3 space-y-2">
            <div className="flex items-center justify-between gap-2">
              <div className="text-sm min-w-0">
                <span className="font-medium text-gray-900">{connection.name}</span>
                <span className="text-gray-500"> · {t(`aiConnections.kinds.${connection.kind}`)}</span>
                <div className="text-xs text-gray-500"><Lock className="inline w-3 h-3" /> {
                  !connection.key_readable ? t('aiConnections.keyUnreadable')
                    : connection.key_last4 ? t('aiConnections.keyShown', { last4: connection.key_last4 }) : t('aiConnections.noKey')}</div>
              </div>
              <div className="flex gap-2 shrink-0">
                <button type="button" className={secondary} disabled={busy}
                  onClick={() => setKeyDraft({ id: connection.connection_id, key: '' })}>{t('aiConnections.replaceKey')}</button>
                <button type="button" className={secondary} disabled={busy} aria-label={t('aiConnections.removeNamed', { name: connection.name })}
                  onClick={() => void run(() => deleteConnection(connection.connection_id))}><Trash2 className="w-4 h-4" /></button>
              </div>
            </div>
            {keyDraft?.id === connection.connection_id && (
              <form className="flex gap-2" onSubmit={event => {
                event.preventDefault();
                const key = keyDraft.key.trim();
                if (key) void run(() => updateConnection(connection.connection_id, { api_key: key }), () => setKeyDraft(null));
              }}>
                <input type="password" autoComplete="off" className={field} aria-label={t('aiConnections.apiKey')} value={keyDraft.key}
                  onChange={event => setKeyDraft({ id: connection.connection_id, key: event.target.value })} />
                <button type="submit" className={primary} disabled={busy}>{t('aiConnections.save')}</button>
                <button type="button" className={secondary} onClick={() => setKeyDraft(null)}>{t('aiConnections.cancel')}</button>
              </form>
            )}
          </div>
        ))}
        {connectionDraft ? (
          <form className="rounded-xl border border-gray-100 p-3 space-y-2" onSubmit={event => {
            event.preventDefault();
            const draft = connectionDraft;
            void run(() => addConnection({ ...draft, base_url: draft.kind === 'openai_compatible' ? draft.base_url : undefined }),
              () => setConnectionDraft(null));
          }}>
            <label className="block text-xs text-gray-600">{t('aiConnections.kind')}
              <select className={field} value={connectionDraft.kind} onChange={event => {
                const kind = event.target.value as AiKind;
                const name = kind === 'openai_compatible' ? '' : t(`aiConnections.kinds.${kind}`);
                setConnectionDraft({ ...connectionDraft, kind, name, service_name: name });
              }}>
                {AI_KINDS.map(kind => <option key={kind} value={kind}>{t(`aiConnections.kinds.${kind}`)}</option>)}
              </select>
            </label>
            <label className="block text-xs text-gray-600">{t('aiConnections.name')}
              <input className={field} value={connectionDraft.name} onChange={event => setConnectionDraft({ ...connectionDraft, name: event.target.value })} />
            </label>
            {connectionDraft.kind === 'openai_compatible' && (
              <label className="block text-xs text-gray-600">{t('aiConnections.baseUrl')}
                <input className={field} inputMode="url" value={connectionDraft.base_url}
                  onChange={event => setConnectionDraft({ ...connectionDraft, base_url: event.target.value })} />
              </label>
            )}
            <label className="block text-xs text-gray-600">{t('aiConnections.apiKey')}
              <input type="password" autoComplete="off" className={field} value={connectionDraft.api_key}
                onChange={event => setConnectionDraft({ ...connectionDraft, api_key: event.target.value })} />
            </label>
            <label className="block text-xs text-gray-600">{t('aiConnections.serviceName')}
              <input className={field} value={connectionDraft.service_name}
                onChange={event => setConnectionDraft({ ...connectionDraft, service_name: event.target.value })} />
            </label>
            <div className="flex gap-2">
              <button type="submit" className={primary} disabled={busy}>{t('aiConnections.save')}</button>
              <button type="button" className={secondary} onClick={() => setConnectionDraft(null)}>{t('aiConnections.cancel')}</button>
            </div>
          </form>
        ) : (
          <button type="button" className={secondary} disabled={busy}
            onClick={() => setConnectionDraft({ kind: 'openai', name: t('aiConnections.kinds.openai'), base_url: '', api_key: '', service_name: t('aiConnections.kinds.openai') })}>
            <Plus className="inline w-4 h-4" /> {t('aiConnections.addConnection')}
          </button>
        )}
      </div>

      <div className="space-y-2">
        <h3 className="text-sm font-semibold text-gray-900">{t('aiConnections.models')}</h3>
        {settings.models.map(model => (
          <div key={model.model_ref} role="group" aria-label={label(model)} className="rounded-xl border border-gray-100 p-3 space-y-2">
            <div className="flex items-start justify-between gap-2">
              <div className="text-sm min-w-0">
                <div className="font-medium text-gray-900 break-all">{label(model)}</div>
                <div className="text-xs">{status(model)}</div>
                <div className="text-xs text-gray-500">{[model.company, model.region, model.retention].map(value => value || '—').join(' · ')}</div>
              </div>
              <div className="flex flex-wrap gap-2 justify-end shrink-0">
                <button type="button" className={secondary} disabled={busy || testing !== null} onClick={() => void runTest(model.model_ref)}>
                  {testing === model.model_ref ? <Loader2 className="w-4 h-4 animate-spin" aria-label={t('aiConnections.test')} /> : t('aiConnections.test')}
                </button>
                <button type="button" className={secondary} disabled={busy}
                  onClick={() => setNoticeDraft({ ref: model.model_ref, company: model.company, region: model.region, retention: model.retention })}>
                  {t('aiConnections.editNotice')}
                </button>
                <button type="button" className={secondary} disabled={busy} aria-label={t('aiConnections.removeNamed', { name: model.model_id })}
                  onClick={() => void run(() => deleteModel(model.model_ref))}><Trash2 className="w-4 h-4" /></button>
              </div>
            </div>
            {noticeDraft?.ref === model.model_ref && (
              <form className="space-y-2" onSubmit={event => {
                event.preventDefault();
                const { ref, ...details } = noticeDraft;
                void run(() => updateModel(ref, details), () => setNoticeDraft(null));
              }}>
                {(['company', 'region', 'retention'] as const).map(name => (
                  <label key={name} className="block text-xs text-gray-600">{t(`aiConnections.${name}`)}
                    <input className={field} value={noticeDraft[name]} onChange={event => setNoticeDraft({ ...noticeDraft, [name]: event.target.value })} />
                  </label>
                ))}
                <div className="flex gap-2">
                  <button type="submit" className={primary} disabled={busy}>{t('aiConnections.save')}</button>
                  <button type="button" className={secondary} onClick={() => setNoticeDraft(null)}>{t('aiConnections.cancel')}</button>
                </div>
              </form>
            )}
          </div>
        ))}
        {modelDraft ? (
          <form className="rounded-xl border border-gray-100 p-3 space-y-2" onSubmit={event => {
            event.preventDefault();
            const draft = modelDraft;
            void run(() => addModel(draft), () => setModelDraft(null));
          }}>
            <label className="block text-xs text-gray-600">{t('aiConnections.connection')}
              <select className={field} value={modelDraft.connection_id} onChange={event => {
                setRemoteModels([]);
                setModelDraft({ ...modelDraft, connection_id: Number(event.target.value) });
              }}>
                {settings.connections.map(connection => <option key={connection.connection_id} value={connection.connection_id}>{connection.name}</option>)}
              </select>
            </label>
            <label className="block text-xs text-gray-600">{t('aiConnections.modelId')}
              <input className={field} list="ai-remote-models" value={modelDraft.model_id}
                onChange={event => setModelDraft({ ...modelDraft, model_id: event.target.value })} />
            </label>
            <datalist id="ai-remote-models">{remoteModels.map(id => <option key={id} value={id} />)}</datalist>
            <button type="button" className={secondary} disabled={busy} onClick={() => void loadRemoteModels(modelDraft.connection_id)}>
              {t('aiConnections.loadList')}
            </button>
            {(['company', 'region', 'retention'] as const).map(name => (
              <label key={name} className="block text-xs text-gray-600">{t(`aiConnections.${name}`)}
                <input className={field} value={modelDraft[name]} onChange={event => setModelDraft({ ...modelDraft, [name]: event.target.value })} />
              </label>
            ))}
            <div className="flex gap-2">
              <button type="submit" className={primary} disabled={busy}>{t('aiConnections.add')}</button>
              <button type="button" className={secondary} onClick={() => setModelDraft(null)}>{t('aiConnections.cancel')}</button>
            </div>
          </form>
        ) : (
          <button type="button" className={secondary} disabled={busy || !settings.connections.length}
            onClick={() => {
              setRemoteModels([]);
              setModelDraft({ connection_id: settings.connections[0].connection_id, model_id: '', company: '', region: '', retention: '' });
            }}>
            <Plus className="inline w-4 h-4" /> {t('aiConnections.addModel')}
          </button>
        )}
      </div>

      <div className="space-y-2">
        <h3 className="text-sm font-semibold text-gray-900">{t('aiConnections.jobs')}</h3>
        <p className="text-xs text-gray-500">{t('aiConnections.onlyTested')}</p>
        {AI_JOBS.map(job => (
          <div key={job} className="grid gap-2 sm:grid-cols-3 sm:items-center">
            <span className="text-sm text-gray-900">{t(`aiConnections.jobNames.${job}`)}</span>
            <select className={field} aria-label={`${t(`aiConnections.jobNames.${job}`)}: ${t('aiConnections.main')}`} value={jobs[job].primary_ref}
              onChange={event => setJobs({ ...jobs, [job]: { ...jobs[job], primary_ref: Number(event.target.value) } })}>
              {tested.map(model => <option key={model.model_ref} value={model.model_ref}>{label(model)}</option>)}
            </select>
            <select className={field} aria-label={`${t(`aiConnections.jobNames.${job}`)}: ${t('aiConnections.backup')}`} value={jobs[job].fallback_ref ?? ''}
              onChange={event => setJobs({ ...jobs, [job]: { ...jobs[job], fallback_ref: event.target.value ? Number(event.target.value) : null } })}>
              <option value="">{t('aiConnections.noBackup')}</option>
              {tested.map(model => <option key={model.model_ref} value={model.model_ref}>{label(model)}</option>)}
            </select>
          </div>
        ))}
        <button type="button" className={primary} disabled={busy} onClick={() => void run(() => saveJobs(jobs))}>{t('aiConnections.saveJobs')}</button>
      </div>
    </section>
  );
}
```

- [ ] **Step 5: Add the translations and put the card on Settings**

In `src/i18n.ts`, insert this block right before `      reachy: {` inside `en.translation`:

```ts
      aiConnections: {
        title: 'AI connections',
        subtitle: 'Which AI service and model Reachy uses for each job',
        loading: 'Loading…',
        weakSecret: 'Keys are not really protected until SECRET_KEY in .env is changed from its default.',
        noKeyInUse: 'No model in use has an API key: Reachy will answer with its fixed line.',
        seeded: 'These settings were copied from .env on {{date}}. Edit them here; the LLM lines in .env no longer apply.',
        connections: 'Connections',
        addConnection: 'Add connection',
        kind: 'Type',
        kinds: { openrouter: 'OpenRouter', openai: 'OpenAI', openai_compatible: 'Other (OpenAI-compatible)' },
        name: 'Name',
        baseUrl: 'Web address (ending before /chat/completions)',
        apiKey: 'API key',
        serviceName: 'Service name in the notice',
        keyShown: 'Key ••••{{last4}}',
        noKey: 'No key',
        keyUnreadable: 'Enter the key again (it cannot be read since SECRET_KEY changed)',
        replaceKey: 'Replace key',
        save: 'Save',
        add: 'Add',
        cancel: 'Cancel',
        removeNamed: 'Remove {{name}}',
        models: 'Models',
        addModel: 'Add model',
        connection: 'Connection',
        modelId: 'Model name',
        loadList: "Load the service's model list",
        company: 'Company behind the model',
        region: 'Region',
        retention: 'How long it keeps data',
        editNotice: 'Notice details',
        test: 'Test',
        testOk: '{{seconds}} s',
        notTested: 'Not tested',
        copiedFromEnv: 'Copied from .env',
        lastCallFailed: 'Last call failed: {{error}}',
        jobs: 'Jobs',
        jobNames: { reply: "Reachy's replies", safety: 'Safety check', after_chat: 'After-chat summary and memory' },
        main: 'Main model',
        backup: 'Backup model',
        noBackup: 'No backup',
        saveJobs: 'Save jobs',
        onlyTested: 'Only tested models can be chosen.',
        errors: {
          loadFailed: 'Could not load the AI settings.',
          failed: 'That did not work. Please try again.',
          in_use: 'A job still uses this. Move the job to another model first.',
          untested_model: 'Test this model before giving it a job.',
          not_found: 'It no longer exists. Reload the page.',
          invalid_base_url: 'Enter a web address starting with http:// or https://.',
          invalid_name: 'A name cannot be only spaces.',
          invalid_kind: 'Choose a type.',
          key_unreadable: 'This key can no longer be read. Enter it again.',
          no_key: 'Add an API key to this connection first.',
          already_exists: 'That name is already used.',
          notice_incomplete: 'Fill in company, region and data retention for every model in use.',
          remote_models_unavailable: "Could not load the service's model list. Type the model name instead.",
        },
      },
```

And this block right before `      reachy: {` inside `'zh-TW'.translation`:

```ts
      aiConnections: {
        title: 'AI 連線',
        subtitle: 'Reachy 每項工作使用哪個 AI 服務與模型',
        loading: '載入中…',
        weakSecret: '.env 裡的 SECRET_KEY 仍是預設值，金鑰並未真正受到保護。請先更改 SECRET_KEY。',
        noKeyInUse: '使用中的模型都沒有 API 金鑰：Reachy 只會說固定的句子。',
        seeded: '這些設定於 {{date}} 從 .env 複製而來。請在這裡修改；.env 裡的 LLM 設定已不再使用。',
        connections: '連線',
        addConnection: '新增連線',
        kind: '類型',
        kinds: { openrouter: 'OpenRouter', openai: 'OpenAI', openai_compatible: '其他（相容 OpenAI）' },
        name: '名稱',
        baseUrl: '網址（到 /chat/completions 之前）',
        apiKey: 'API 金鑰',
        serviceName: '通知中顯示的服務名稱',
        keyShown: '金鑰 ••••{{last4}}',
        noKey: '沒有金鑰',
        keyUnreadable: '請重新輸入金鑰（SECRET_KEY 變更後已無法讀取）',
        replaceKey: '更換金鑰',
        save: '儲存',
        add: '新增',
        cancel: '取消',
        removeNamed: '移除 {{name}}',
        models: '模型',
        addModel: '新增模型',
        connection: '連線',
        modelId: '模型名稱',
        loadList: '載入服務的模型清單',
        company: '提供模型的公司',
        region: '地區',
        retention: '資料保存多久',
        editNotice: '通知資料',
        test: '測試',
        testOk: '{{seconds}} 秒',
        notTested: '尚未測試',
        copiedFromEnv: '從 .env 複製',
        lastCallFailed: '上次呼叫失敗：{{error}}',
        jobs: '工作',
        jobNames: { reply: 'Reachy 的回答', safety: '安全檢查', after_chat: '聊天後的摘要與記憶' },
        main: '主要模型',
        backup: '備用模型',
        noBackup: '不使用備用',
        saveJobs: '儲存工作設定',
        onlyTested: '只有測試過的模型可以指派工作。',
        errors: {
          loadFailed: '無法載入 AI 設定。',
          failed: '沒有成功，請再試一次。',
          in_use: '仍有工作在使用它。請先把該工作改用其他模型。',
          untested_model: '請先測試這個模型，再指派工作。',
          not_found: '它已不存在，請重新整理頁面。',
          invalid_base_url: '請輸入以 http:// 或 https:// 開頭的網址。',
          invalid_name: '名稱不能只有空白。',
          invalid_kind: '請選擇類型。',
          key_unreadable: '這個金鑰已無法讀取，請重新輸入。',
          no_key: '請先為這個連線加入 API 金鑰。',
          already_exists: '這個名稱已經使用過了。',
          notice_incomplete: '請為每個使用中的模型填寫公司、地區與資料保存期間。',
          remote_models_unavailable: '無法載入服務的模型清單，請直接輸入模型名稱。',
        },
      },
```

In `src/pages/Settings.tsx`, add `import AiConnectionsCard from '../components/AiConnectionsCard';` below the `ReachyCard` import (line 4). Change line 181 from `      <ReachyCard />` to:

```tsx
      <ReachyCard />

      <AiConnectionsCard />
```

- [ ] **Step 6: Run the gates and every spec**

Run, in `frontend_source`:
- `npx tsc -b`. Expected: no output, exit 0.
- `npm run build`. Expected: `✓ built in`.
- `npx eslint src/components/AiConnectionsCard.tsx src/lib/ai-api.ts`. Expected: no errors.
- `npx playwright test -c C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad/pw-frontend.config.mjs`. Expected: the 5 new tests pass, and `consent`, `overdose`, `reachy` and `scan` pass as before. They mock `/api/ai/settings` with `[]`, which the card shows as a quiet load failure, not an alert.

If an existing spec fails, first check whether it already fails on `ai-connections`'s parent commit before blaming the card. Run it there with `git stash`.

- [ ] **Step 7: Commit**

```bash
git add frontend_source/src/lib/ai-api.ts frontend_source/src/components/AiConnectionsCard.tsx frontend_source/src/pages/Settings.tsx frontend_source/src/i18n.ts frontend_source/tests/ai-connections.spec.ts
git commit -m "AI connections: the Settings card (connections, models with Test, jobs)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Docs, and a real run in Docker on this PC

**Files:**
- Modify: `CLAUDE.md`: the "Model calls" bullet under *Reachy check-in conversations*, plus the `LLM_FALLBACK_MODEL` and `LLM_DEADLINE_SECONDS` rows of *Key Env Vars*
- Modify: `.env.example`: the comment above `LLM_SERVICE=` (line 51) and above `OPENROUTER_API_KEY=` (line 56)

- [ ] **Step 1: Update the docs**

In `CLAUDE.md`, in *Reachy check-in conversations*, replace the first sentence of the "**Model calls.**" bullet ("`services/conversation.py` sends conversation replies, risk checks, summaries and memory extraction to Google Gemini through the OpenRouter API.") with:

```markdown
- **Model calls.** Replies, risk checks, summaries and memory extraction go to the models chosen on the Settings page's **AI connections** card (`api_ai.py`, `ai_settings.py`, `ai_store.py`; spec `docs/superpowers/specs/2026-10-10-ai-connections-design.md`): per job (`reply`, `safety`, `after_chat`) a main and an optional backup model, each through its connection (OpenRouter, OpenAI, or any OpenAI-compatible address). Keys are Fernet-encrypted with a key derived from `SECRET_KEY` and never sent to the browser. Each model's request shape (`max_tokens` vs `max_completion_tokens`, temperature, reasoning off) is its saved *profile*, found by the card's Test button (`ai_profile.py`). On the first start with empty tables, `.env`'s `OPENROUTER_API_KEY`, `LLM_MODEL`, `LLM_FALLBACK_MODEL` and `LLM_SERVICE`/`LLM_PROVIDER`/`LLM_PROVIDER_REGION`/`LLM_RETENTION` are copied in (`ai_store.seed_from_env`); after that `.env`'s LLM lines are ignored. Without a loaded snapshot (the unit tests) the `.env` setup is used.
```

Keep the rest of that bullet (the laptop's pinned models, the deadline, the per-thread session) as it is.

In *Key Env Vars*:
- Change the `LLM_FALLBACK_MODEL` row's purpose to: `Copied into the AI connections card on the first start, then ignored (edit models on the card)`.
- Leave `LLM_DEADLINE_SECONDS` as it is; it still applies.

In `.env.example`, add these two comment lines right above `LLM_SERVICE=OpenRouter` (line 52):

```text
# The LLM lines below (key, models, notice details) are copied into the Settings page's "AI connections" card on the
# first start with empty tables; after that edit them on the card. LLM_DEADLINE_SECONDS and OPENROUTER_* routing stay here.
```

Commit:

```bash
git add CLAUDE.md .env.example
git commit -m "Docs: LLM settings now live on the AI connections card" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 2: Start the stack (a local test database; nothing of the real laptop's)**

Run, in `C:\Reachy_Mini\repo\MedAiCarePlus`:

```bash
docker volume ls --format '{{.Name}}' | grep -i medcareai2 && echo "STOP: a medcareai2 volume exists; ask before using it" || echo "no volume: ok"
test -e .env && echo "STOP: .env exists; read it before touching it" || .venv/Scripts/python.exe -I -c "
import re, secrets
text = open('.env.example', encoding='utf-8').read()
text = re.sub(r'(?m)^SECRET_KEY=.*$', 'SECRET_KEY=' + secrets.token_urlsafe(48), text)
text = re.sub(r'(?m)^APP_ENV=.*$', 'APP_ENV=dev', text)
open('.env', 'w', encoding='utf-8', newline='\n').write(text)
print('wrote .env')"
grep -c '^SECRET_KEY=your-random' .env   # the .env.example placeholder; expect 0
docker compose up -d --build postgres app
until curl -sf http://localhost:8080/health >/dev/null; do sleep 3; done; curl -s http://localhost:8080/health
```

Expected: `no volume: ok`, `wrote .env`, `0`, and a `/health` JSON. `.env.example` has `SECRET_KEY=` on line 11 and `APP_ENV=dev` on line 40, checked on 2026-10-10.

Check the first-start copy. The database user and name come from `docker-compose.yml`: `medai` / `medcareai2`.
- `docker compose logs app | grep -i "startup check"` shows no AI error with the robot feature off.
- `docker compose exec -T postgres psql -U medai -d medcareai2 -tAc "SELECT name, kind, api_key_last4 FROM ai_connection"` gives `OpenRouter|openrouter|` (no key on this PC).

- [ ] **Step 3: Make a test account and add the OpenAI connection (the key never printed)**

```bash
cd /c/Reachy_Mini/repo/MedAiCarePlus
S=C:/Users/AIDL-14/AppData/Local/Temp/claude/C--Reachy-Mini-repo/1ff25281-c5da-4818-bbf1-9fd9035013bc/scratchpad
.venv/Scripts/python.exe -I - <<'EOF' > "$S/ai-test-token.txt"
import json, secrets, urllib.request
base = "http://localhost:8080"
core = json.load(urllib.request.urlopen(f"{base}/api/legal/current?kind=core&lang=en"))
body = {"name": "AI Card Test", "face_label": "ai-card-test", "email": "ai-card-test@example.test",
        "password": secrets.token_urlsafe(16),
        "consent": {"terms_version": core["terms_version"], "language": core["language"],
                    "document_sha256": core["sha256"], "scopes": {"core": True}}}
request = urllib.request.Request(f"{base}/api/auth/register", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(request))["token"])
EOF
TOKEN=$(cat "$S/ai-test-token.txt")
KEY=$(grep -oE 'sk-[A-Za-z0-9_-]+' /c/Reachy_Mini/key/access_key.txt | head -1)
curl -s -o /dev/null -w "add connection: %{http_code}\n" -X POST http://localhost:8080/api/ai/connections \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"name\":\"OpenAI\",\"kind\":\"openai\",\"api_key\":\"$KEY\",\"service_name\":\"OpenAI\"}"
for model in gpt-5.4-mini gpt-4.1-mini gpt-6-luna; do
  curl -s -o /dev/null -w "add $model: %{http_code}\n" -X POST http://localhost:8080/api/ai/models \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"connection_id\":2,\"model_id\":\"$model\",\"company\":\"OpenAI\",\"region\":\"USA\",\"retention\":\"up to 30 days\"}"
done
curl -s http://localhost:8080/api/ai/settings -H "Authorization: Bearer $TOKEN" | grep -c "${KEY:12:16}"   # expect 0
```

Expected: `add connection: 200`, three `add …: 200` lines, then `0`. If the OpenAI connection's id is not 2, read it from `/api/ai/settings` first. If registration needs fields this script lacks, read `api_auth.register` and adjust.

Check the key at rest:
`docker compose exec -T postgres psql -U medai -d medcareai2 -tAc "SELECT api_key_enc FROM ai_connection WHERE name='OpenAI'" | grep -c "${KEY:12:16}"`
Expected: `0`.

- [ ] **Step 4: Use the card in a real browser and take screenshots**

Use the Playwright MCP tools (load them with ToolSearch `select:mcp__plugin_playwright_playwright__browser_navigate,mcp__plugin_playwright_playwright__browser_evaluate,mcp__plugin_playwright_playwright__browser_click,mcp__plugin_playwright_playwright__browser_select_option,mcp__plugin_playwright_playwright__browser_snapshot,mcp__plugin_playwright_playwright__browser_take_screenshot,mcp__plugin_playwright_playwright__browser_resize,mcp__plugin_playwright_playwright__browser_wait_for`):

1. Navigate to `http://localhost:8080/login`. Use `browser_evaluate` to set `face_auth_token` to the contents of `ai-test-token.txt`, plus `face_auth_session`, `onboarding_complete` and `onboarding_face_done` = `'true'`, and `face_auth_user`, as `tests/reachy.spec.ts` seeds them. Then navigate to `/settings`.
2. In the AI connections card, press **Test** on `OpenAI · gpt-5.4-mini`, then on `gpt-4.1-mini` and `gpt-6-luna`. Each should show a time in seconds.
3. Choose `OpenAI · gpt-5.4-mini` as *Reachy's replies: Main model* and press **Save jobs**.
4. Resize to 390×844 and take a screenshot. Then resize to 1280×900 and take another. Save both to the scratchpad as `ai-card-390.png` and `ai-card-1280.png`.

Then compare the saved profiles with spec §7:
`curl -s http://localhost:8080/api/ai/settings -H "Authorization: Bearer $TOKEN" | .venv/Scripts/python.exe -I -c "import json,sys; [print(m['model_id'], m['profile'], m['test_ms']) for m in json.load(sys.stdin)['models'] if m['connection_id']==2]"`

Expected:
- `gpt-6-luna {'token_param': 'max_completion_tokens', 'temperature': False, 'reasoning_off': {'reasoning_effort': 'none'}}`
- `gpt-5.4-mini {'token_param': 'max_completion_tokens', 'temperature': True, 'reasoning_off': {'reasoning_effort': 'none'}}`
- `gpt-4.1-mini {'token_param': 'max_completion_tokens', 'temperature': True, 'reasoning_off': None}`

Any difference is a finding to report, not something to edit away.

- [ ] **Step 5: Check that a real reply now goes through OpenAI, and that the notice names it**

```bash
docker compose exec -T app python - <<'EOF'
import asyncio
from app import database
from app.services import ai_settings, ai_store, conversation

async def main():
    await database.init_pool()
    async with database.get_pool().acquire() as conn:
        ai_settings.set_current(await ai_store.load(conn))
    text, metrics = await conversation.reply_with_metrics([{"role": "patient", "text": "我今天去公園散步了"}], "zh-TW")
    print([(a["connection"], a["model_requested"], a["status"], a["usable"], a["ms"]) for a in metrics["attempts"]])
    print("reply characters:", len(text), "| fixed line used:", metrics["fallback_used"])
    await database.close_pool()

asyncio.run(main())
EOF
curl -s "http://localhost:8080/api/legal/current?kind=robot&lang=en" | grep -o "OpenAI / OpenRouter" | head -1
```

Expected:
- The first attempt is `('OpenAI', 'gpt-5.4-mini', 200, True, <ms>)`.
- `fixed line used: False`.
- The notice prints `OpenAI / OpenRouter`.

The OpenRouter backup has no key here, so it is skipped.

- [ ] **Step 6: Final gates, stop the stack, report**

- `.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider`. Expected: only the known pre-existing failure.
- In `frontend_source`: `npx tsc -b`, `npm run build` and the Playwright run from Task 7 Step 6. Expected: green.
- `docker compose down`. This keeps the test volume and `.env`, so the run can be repeated. Say so in the report; both are local to this PC and gitignored.
- `git status --short`. Expected: clean, apart from nothing. `.env` is ignored.
- Append the session's log entry to `C:\Reachy_Mini\knowledge\reachy_mini_brain\10-Log\2026-10-10.md` (or today's file), in the vault's *Did / Measured / Next* format, with LF endings.
- Send the Telegram report to chat 5855944177: what was built, the gate results, the measured profiles and reply time, the pre-existing failing test, and that nothing was pushed.
