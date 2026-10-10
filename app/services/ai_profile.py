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
