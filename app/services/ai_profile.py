"""How to ask one model: the request settings it accepts (its profile), found by trying, and the request they give.

Models differ even within one service (measured 2026-10-10 with OpenAI): gpt-6-luna and gpt-5.4-mini refuse
max_tokens and want max_completion_tokens; gpt-6-luna refuses a temperature other than the default unless reasoning is turned off
(reasoning_effort none); gpt-4.1-mini refuses
reasoning_effort. So no request is fixed in code: the Test button starts from the service's usual request, reads
each 400 message, changes the one setting it names, and the card saves what worked.

Thinking (spec 2026-10-11 D2): after that, detect_thinking finds the levels (low, medium, high) the model accepts and
times each one; the profile saves them as "thinking". A model that refuses reasoning_effort "none" (GPT-6 Astra,
GPT-6.1 Sol) always thinks: its "off" is its lowest level, sent with that level's token allowance.
"""

import copy
import re
import time

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
TEST_TEMPERATURE = 0.7
TEST_TIMEOUT = 15
_SETTINGS = ("max_completion_tokens", "max_tokens", "reasoning_effort", "reasoning", "temperature")
# The Test result's error for a model that can't write chat replies (a voice-only model such as gpt-live-1, or a TTS
# model). OpenAI answers /chat/completions for one with HTTP 404: "This is not a chat model and thus not supported in
# the v1/chat/completions endpoint. Did you mean to use v1/completions?"
NOT_CHAT_MODEL = "not_chat_model"
_NOT_CHAT = re.compile(r"not a chat model|not supported in the v1/chat/completions endpoint", re.IGNORECASE)
THINKING_LEVELS = ("low", "medium", "high")
ALLOWANCE = {"low": 800, "medium": 1600, "high": 3000}   # tokens added to a call's cap at the level sent (spec D4)
THINKING_SECONDS = 40   # Test starts no thinking probe after this; a level not tried is not offered
CANT_THINK = {"param": None, "levels": [], "ms": {}, "always": False, "temperature": False}
_OFF_ORDER = ("none", "minimal") + THINKING_LEVELS   # reasoning_effort values for "off", the least thinking first
_NEXT_OFF = {"none": "minimal", "minimal": "low"}     # tried in turn when a refusal lists no accepted values


def starting_profile(kind: str) -> dict:
    return copy.deepcopy(STARTING[kind])


def _named_setting(message: str) -> str | None:
    """The request setting a 400 message is about: the one it names first ("Use 'max_completion_tokens' instead"
    comes after the refused max_tokens)."""
    found = [(match.start(), name) for name in _SETTINGS
             for match in [re.search(rf"\b{name}\b", message, re.IGNORECASE)] if match]
    return min(found)[1] if found else None


def _value_refused(message: str) -> bool:
    """The 400 refuses a value we sent ("does not support 'none'"), not a setting the model doesn't know."""
    lowered = message.lower()
    return "does not support" in lowered or "unsupported value" in lowered


def _supported_values(message: str) -> list[str] | None:
    """The values a refusal lists, in its order: "... Supported values are: 'none', 'low', 'medium', 'high', and
    'xhigh'." (OpenAI, measured 2026-10-11; not a documented format). None when it lists none."""
    match = re.search(r"supported values are:?(.*)", message, re.IGNORECASE | re.DOTALL)
    values = re.findall(r"'([A-Za-z_]+)'", match.group(1)) if match else []
    return [value.lower() for value in values] or None


def _off_value(supported: list[str] | None, refused: str) -> tuple[str, bool] | None:
    """The reasoning_effort to send as "off" after `refused` was refused, and whether the model then always thinks:
    none or minimal when listed, else the lowest listed level. Without a list, the next value up, as far as low.
    None when nothing is left. Never "no setting": that leaves the model thinking at its default level inside the
    reply's 200 tokens (spec F14)."""
    if supported is None:
        value = _NEXT_OFF.get(refused)
        return (value, value in THINKING_LEVELS) if value else None
    for value in _OFF_ORDER:
        if value in supported and value != refused:
            return value, value in THINKING_LEVELS
    return None


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
        if not _value_refused(message):
            new["reasoning_off"] = None   # a setting it doesn't know ("Unrecognized request argument"): it can't think
        else:
            off = _off_value(_supported_values(message), sent_reasoning["reasoning_effort"])
            if off is None:
                return None
            value, always = off
            new["reasoning_off"] = {"reasoning_effort": value}
            if always:
                # It can't stop thinking (GPT-6 Astra, 6.1 Sol): "off" is its lowest level, without the temperature
                # a thinking model refuses, and with that level's allowance (so Test tries it with that budget).
                new["temperature"] = False
                new["thinking"] = {"param": "reasoning_effort", "levels": [value], "ms": {}, "always": True,
                                   "temperature": False}
    elif setting == "reasoning" and sent_reasoning:
        # "Reasoning is mandatory for this endpoint": whichever form of reasoning-off we sent, stop sending it.
        new["reasoning_off"] = None
    else:
        return None
    return new


def effective_level(profile: dict | None, level: str) -> str:
    """The thinking level actually sent when a job asks for `level`: the nearest lower level the model accepts, else
    "off". A model that always thinks never gets "off": below its lowest level it gets that level. A profile without
    "thinking" (tested before thinking levels, or untested) can't think."""
    thinking = (profile or {}).get("thinking") or {}
    accepted = [name for name in THINKING_LEVELS if name in (thinking.get("levels") or [])]
    if not thinking.get("param") or not accepted:
        return "off"
    wanted = THINKING_LEVELS.index(level) if level in THINKING_LEVELS else -1
    lower = [name for name in accepted if THINKING_LEVELS.index(name) <= wanted]
    if lower:
        return lower[-1]
    return accepted[0] if thinking.get("always") else "off"


def allowance(profile: dict | None, level: str) -> int:
    """The tokens to add to a call's cap when its job asks for `level`: the allowance of the level actually sent
    (effective_level), 0 at "off"."""
    return ALLOWANCE.get(effective_level(profile, level), 0)


def request_body(profile: dict, model_id: str, messages: list[dict], max_tokens: int, temperature: float | None,
                 extras: dict | None = None, *, level: str = "off") -> dict:
    """The request for this model. At "off" (the default) it is the request from before thinking levels, with the
    profile's reasoning_off. At another level it sends the level the model accepts (effective_level): OpenAI's
    reasoning_effort without temperature, or OpenRouter's reasoning effort (hidden from the answer) with temperature
    only when the model's OpenRouter entry lists it. temperature None sends none. max_tokens is sent as given:
    allowance() says what to add for thinking."""
    sent = effective_level(profile, level)
    body = {"model": model_id, "messages": messages, profile["token_param"]: max_tokens}
    if sent == "off":
        if profile["temperature"] and temperature is not None:
            body["temperature"] = temperature
        body.update(copy.deepcopy(profile["reasoning_off"] or {}))
    elif profile["thinking"]["param"] == "openrouter_effort":
        if profile["thinking"].get("temperature") and temperature is not None:
            body["temperature"] = temperature
        body["reasoning"] = {"effort": sent, "exclude": True}
    else:
        body["reasoning_effort"] = sent
    body.update(extras or {})
    return body


def probe_body(profile: dict, model_id: str, level: str = "off", extras: dict | None = None) -> dict:
    """Test's request at `level`: the short greeting with the reply's budget plus the allowance of the level sent (a
    model that always thinks is tried with the budget its replies get)."""
    return request_body(profile, model_id, TEST_MESSAGES, TEST_MAX_TOKENS + allowance(profile, level),
                        TEST_TEMPERATURE, extras, level=level)


def headers(kind: str, api_key: str) -> dict:
    sent = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if kind == "openrouter":
        sent["X-Title"] = "MedAiCarePlus Reachy check-in"
    return sent


def _error_message(body) -> str:
    """The service's message in an error answer. Not cut: callers redact first, then cut (ai_crypto.redact)."""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(error, str):
            return error
    return str(body)


def _not_chat_model(status: int, message: str) -> bool:
    """A 404 (or 400) that says the model is not a chat model: no request setting can fix that."""
    return status in (400, 404) and bool(_NOT_CHAT.search(message))


def _failed(ms, error: str, secret: str) -> dict:
    return {"ok": False, "profile": None, "ms": ms, "sample": None, "error": redact(error, secret)[:300]}


def detect(send, kind: str, secret: str = "") -> dict:
    """Find the profile a model accepts. send(profile) -> (HTTP status, parsed body or text, ms) does one request;
    status 0 means no answer came back. secret: the key sent, removed from any error text that echoes it.

    Returns {"ok", "profile", "ms", "sample", "error"}: on success the profile to save, the successful request's
    time and the answer's first 40 characters; otherwise the service's own words (cut to 300), and nothing to save."""
    profile = starting_profile(kind)
    body, ms = None, None
    for _ in range(MAX_TRIES):
        status, body, ms = send(profile)
        if status == 200:
            choices = (body.get("choices") if isinstance(body, dict) else None) or []
            choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
            message_part = choice.get("message")
            content = message_part.get("content") if isinstance(message_part, dict) else None
            if not isinstance(content, str) or not content.strip():
                return _failed(ms, "The model answered with no text.", secret)
            if choice.get("finish_reason") == "length":
                return _failed(ms, "The answer was cut off by the length limit.", secret)
            return {"ok": True, "profile": profile, "ms": ms, "sample": content.strip()[:40], "error": None}
        message = _error_message(body)
        if _not_chat_model(status, message):
            return _failed(ms, NOT_CHAT_MODEL, secret)
        changed = adjust(profile, message) if status == 400 else None
        if changed is None:
            return _failed(ms, f"HTTP {status}: {message}" if status else message, secret)
        profile = changed
    return _failed(ms, f"Gave up after {MAX_TRIES} tries: {_error_message(body)}", secret)


def _full_answer(body) -> bool:
    """A 200 body with text that was not cut off by the length limit (thinking can use up the whole budget)."""
    choices = (body.get("choices") if isinstance(body, dict) else None) or []
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message_part = choice.get("message")
    content = message_part.get("content") if isinstance(message_part, dict) else None
    return isinstance(content, str) and bool(content.strip()) and choice.get("finish_reason") != "length"


def _levels_to_try(send, profile: dict) -> list[str] | None:
    """OpenAI and compatible services: the levels worth a probe. One request asks for reasoning_effort "max" without
    temperature, and its refusal lists the values the model accepts. Without a list (an accepted "max", another
    error, no answer) all three are tried. None when the model doesn't know reasoning_effort ("Unrecognized request
    argument supplied: reasoning_effort"): it can't think."""
    asked = {**copy.deepcopy(profile), "temperature": False, "reasoning_off": {"reasoning_effort": "max"}}
    asked.pop("thinking", None)
    status, body, _ = send(asked)
    if status == 400:
        message = _error_message(body)
        listed = _supported_values(message)
        if listed is not None:
            return [level for level in THINKING_LEVELS if level in listed]
        if (_named_setting(message) == "reasoning_effort"
                and re.search(r"unrecognized request argument|unknown parameter", message, re.IGNORECASE)):
            return None
    return list(THINKING_LEVELS)


def _probe(send, profile: dict, param: str, level: str) -> int | None:
    """One short request at `level`, without temperature, with the reply's budget plus the level's allowance. Its
    time in ms, or None when the level is not offered: refused, no answer, empty, or cut off by the length limit."""
    trial = {**copy.deepcopy(profile), "thinking": {"param": param, "levels": [level], "ms": {}, "always": False,
                                                     "temperature": False}}
    status, body, ms = send(trial, level)
    return ms if status == 200 and _full_answer(body) else None


def detect_thinking(send, kind: str, profile: dict, openrouter_entry=None, clock=time.monotonic) -> dict:
    """The thinking levels a model accepts (spec D2), found by Test after detect() passed, and apart from it: the
    Test route saves the base profile whatever happens here. send(profile, level="off") is detect's send, with the
    level to ask for. openrouter_entry() gives the model's entry in OpenRouter's public model list (None: not
    listed); only OpenRouter connections call it, and it may raise.

    Returns the profile's "thinking": {"param": "reasoning_effort" | "openrouter_effort" | None, "levels": the levels
    offered, "ms": each level's time, "always": the model can't stop thinking, "temperature": a thinking request may
    carry temperature}; CANT_THINK when no level is offered. A level is offered only when its probe answered in
    full. No probe starts after THINKING_SECONDS."""
    started = clock()
    base = profile.get("thinking") or {}
    if kind == "openrouter":
        entry = openrouter_entry() if openrouter_entry else None
        entry = entry if isinstance(entry, dict) else {}
        reasoning = entry.get("reasoning") if isinstance(entry.get("reasoning"), dict) else {}
        efforts = reasoning.get("supported_efforts", [])
        if efforts is None:   # OpenRouter's docs: null means every effort is accepted; no key means none is
            efforts = THINKING_LEVELS
        parameters = entry.get("supported_parameters") if isinstance(entry.get("supported_parameters"), list) else []
        param, always = "openrouter_effort", bool(reasoning.get("mandatory"))
        temperature = "temperature" in parameters and bool(profile["temperature"])
        to_try = [level for level in THINKING_LEVELS if isinstance(efforts, (list, tuple)) and level in efforts]
    else:
        param, always, temperature = "reasoning_effort", bool(base.get("always")), False
        to_try = _levels_to_try(send, profile) or []
    levels, ms = [], {}
    for level in to_try:
        if clock() - started > THINKING_SECONDS:
            break
        took = _probe(send, profile, param, level)
        if took is not None:
            levels.append(level)
            ms[level] = took
    if always:
        # A model adjust() found always thinking passed the base test at its lowest level: that level stays.
        levels = [level for level in THINKING_LEVELS if level in levels or level in (base.get("levels") or [])]
    if not levels:
        return copy.deepcopy(CANT_THINK)
    return {"param": param, "levels": levels, "ms": ms, "always": always, "temperature": temperature}
