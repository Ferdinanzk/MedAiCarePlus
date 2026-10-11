"""Request profiles: the settings each model accepts, found from the services' own 400 messages, and the thinking
levels each model accepts.

The first three messages were measured with OpenAI on 2026-10-10 (spec §7); the reasoning_effort value refusals
LUNA_REFUSES_MAX and ASTRA_REFUSES_NONE on 2026-10-11. ASTRA_REFUSES_MAX is the same shape, constructed for the
test. The OpenRouter entries are cut from its public model list, fetched 2026-10-11."""

import copy

import pytest

from app.services import ai_profile

MAX_TOKENS_REFUSED = ("Unsupported parameter: 'max_tokens' is not supported with this model. "
                      "Use 'max_completion_tokens' instead.")
TEMPERATURE_REFUSED = ("Unsupported value: 'temperature' does not support 0.7 with this model. "
                       "Only the default (1) value is supported.")
EFFORT_UNKNOWN = "Unrecognized request argument supplied: reasoning_effort"
LUNA_REFUSES_MAX = ("Unsupported value: 'reasoning_effort' does not support 'max' with this model. "
                    "Supported values are: 'none', 'low', 'medium', 'high', and 'xhigh'.")
ASTRA_REFUSES_NONE = ("Unsupported value: 'reasoning_effort' does not support 'none' with this model. "
                      "Supported values are: 'low', 'medium', 'high', and 'xhigh'.")
ASTRA_REFUSES_MAX = ("Unsupported value: 'reasoning_effort' does not support 'max' with this model. "
                     "Supported values are: 'low', 'medium', 'high', and 'xhigh'.")


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


def test_a_model_that_refuses_none_always_thinks_at_its_lowest_level():
    """GPT-6 Astra and 6.1 Sol (spec F14): never "no setting", which leaves the model thinking at its default level
    inside the reply's 200 tokens. Its "off" is low, without temperature, tried with low's allowance."""
    send, sent = _service((lambda p: p["reasoning_off"] == {"reasoning_effort": "none"}, _bad(ASTRA_REFUSES_NONE)))
    result = ai_profile.detect(send, "openai")
    assert [p["reasoning_off"] for p in sent] == [{"reasoning_effort": "none"}, {"reasoning_effort": "low"}]
    assert result["ok"] and result["profile"] == {
        "token_param": "max_completion_tokens", "temperature": False, "reasoning_off": {"reasoning_effort": "low"},
        "thinking": {"param": "reasoning_effort", "levels": ["low"], "ms": {}, "always": True, "temperature": False}}
    assert ai_profile.probe_body(result["profile"], "gpt-6-astra") == {
        "model": "gpt-6-astra", "messages": ai_profile.TEST_MESSAGES, "max_completion_tokens": 1000,
        "reasoning_effort": "low"}


def test_without_a_list_none_then_minimal_refused_moves_to_low_never_to_no_setting():
    refused = "Unsupported value: 'reasoning_effort' does not support '{}' with this model."
    send, sent = _service(
        (lambda p: p["reasoning_off"] == {"reasoning_effort": "none"}, _bad(refused.format("none"))),
        (lambda p: p["reasoning_off"] == {"reasoning_effort": "minimal"}, _bad(refused.format("minimal"))))
    result = ai_profile.detect(send, "openai")
    assert [p["reasoning_off"] for p in sent] == [{"reasoning_effort": "none"}, {"reasoning_effort": "minimal"},
                                                  {"reasoning_effort": "low"}]
    assert result["ok"] and result["profile"]["thinking"]["always"] is True


def test_a_model_that_lists_minimal_turns_thinking_down_to_minimal():
    message = ("Unsupported value: 'reasoning_effort' does not support 'none' with this model. "
               "Supported values are: 'minimal', 'low', 'medium', and 'high'.")
    send, _ = _service((lambda p: p["reasoning_off"] == {"reasoning_effort": "none"}, _bad(message)))
    profile = ai_profile.detect(send, "openai")["profile"]
    assert profile["reasoning_off"] == {"reasoning_effort": "minimal"} and "thinking" not in profile


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


REASONING_MANDATORY = "Reasoning is mandatory for this endpoint and cannot be disabled."


@pytest.mark.parametrize("kind", ["openrouter", "openai_compatible"])
def test_a_mandatory_reasoning_model_gets_reasoning_off_dropped(kind):
    send, sent = _service((lambda p: p["reasoning_off"] is not None, _bad(REASONING_MANDATORY)))
    result = ai_profile.detect(send, kind)
    assert [p["reasoning_off"] for p in sent][-1] is None and len(sent) == 2
    assert result["ok"] and result["profile"]["reasoning_off"] is None


@pytest.mark.parametrize("body", [{"choices": [{"message": "hi"}]}, {"choices": ["x"]}, {"choices": "x"},
                                  {"choices": [{"message": ["hi"]}]}])
def test_a_200_with_an_unexpected_shape_is_a_failure_not_an_exception(body):
    send, _ = _service((lambda p: True, (200, body, 100)))
    result = ai_profile.detect(send, "openai")
    assert result["ok"] is False and result["error"] == "The model answered with no text."


NOT_CHAT = ("This is not a chat model and thus not supported in the v1/chat/completions endpoint. "
            "Did you mean to use v1/completions?")   # OpenAI's real 404 for a voice-only model (gpt-live-1)


@pytest.mark.parametrize("response", [
    _bad(NOT_CHAT, 404),
    _bad(NOT_CHAT, 400),
    _bad("THIS IS NOT A CHAT MODEL.", 404),
    _bad("gpt-x-tts is not supported in the v1/chat/completions endpoint.", 404),
])
def test_a_voice_only_model_gets_the_not_chat_model_code(response):
    send, sent = _service((lambda p: True, response))
    result = ai_profile.detect(send, "openai")
    assert result == {"ok": False, "profile": None, "ms": 120, "sample": None, "error": "not_chat_model"}
    assert ai_profile.NOT_CHAT_MODEL == "not_chat_model" and len(sent) == 1   # no setting can fix it: one request


@pytest.mark.parametrize("response, error", [
    (_bad(NOT_CHAT, 500), f"HTTP 500: {NOT_CHAT}"),   # only a 404 or a 400 says so
    (_bad("The model `gpt-9` does not exist", 404), "HTTP 404: The model `gpt-9` does not exist"),
])
def test_other_failures_keep_the_services_words(response, error):
    send, _ = _service((lambda p: True, response))
    assert ai_profile.detect(send, "openai")["error"] == error


# Thinking levels (spec 2026-10-11 D2): what each model accepts, found by Test.

OPENAI_BASE = {"token_param": "max_completion_tokens", "temperature": True,
               "reasoning_off": {"reasoning_effort": "none"}}
LEVEL_MS = {"low": 1250, "medium": 1640, "high": 1984}
OR_OPENAI_PARAMS = ["include_reasoning", "max_completion_tokens", "max_tokens", "reasoning", "reasoning_effort",
                    "response_format", "seed", "structured_outputs", "tool_choice", "tools", "verbosity"]
OR_LUNA = {"id": "openai/gpt-6-luna", "supported_parameters": OR_OPENAI_PARAMS,
           "reasoning": {"mandatory": False, "default_enabled": True, "default_effort": "medium",
                         "supported_efforts": ["max", "xhigh", "high", "medium", "low", "none"]}}
OR_SOL = {"id": "openai/gpt-6.1-sol", "supported_parameters": OR_OPENAI_PARAMS,
          "reasoning": {"mandatory": True, "default_enabled": True, "default_effort": "medium",
                        "supported_efforts": ["max", "xhigh", "high", "medium", "low"]}}
OR_STEP = {"id": "stepfun/step-5-preview",
           "supported_parameters": ["frequency_penalty", "include_reasoning", "logprobs", "max_tokens", "reasoning",
                                    "reasoning_effort", "response_format", "stop", "structured_outputs", "temperature",
                                    "tools", "top_logprobs", "top_p"],
           "reasoning": {"mandatory": True, "default_effort": "medium", "supported_efforts": ["high", "medium", "low"]}}
OR_GEMINI_25 = {"id": "google/gemini-2.5-flash", "reasoning": {"mandatory": False},
                "supported_parameters": ["include_reasoning", "max_tokens", "reasoning", "response_format", "seed",
                                         "stop", "structured_outputs", "temperature", "tool_choice", "tools", "top_p"]}


def _thinker(max_answer=None, refused=(), cut_off=(), model_id="gpt-6-luna"):
    """A fake thinking model for detect_thinking, answering the bodies Test really sends (ai_profile.probe_body).
    max_answer: the answer to reasoning_effort "max" (default: accepted); refused: levels answered with a 400;
    cut_off: levels whose thinking used the whole budget (no text, finish_reason length)."""
    sent = []

    def send(profile, level="off"):
        body = ai_profile.probe_body(profile, model_id, level)
        sent.append(body)
        effort = body.get("reasoning_effort") or (body.get("reasoning") or {}).get("effort")
        if effort == "max":
            return max_answer or _ok()
        if effort in refused:
            return _bad(f"Unsupported value: 'reasoning_effort' does not support '{effort}' with this model.")
        if effort in cut_off:
            return 200, {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}, 2100
        return 200, {"choices": [{"finish_reason": "stop", "message": {"content": "您好！"}}]}, LEVEL_MS[effort]
    return send, sent


def _can(levels, always=False, param="reasoning_effort", temperature=False):
    return {**OPENAI_BASE, "thinking": {"param": param, "levels": levels, "ms": {}, "always": always,
                                        "temperature": temperature}}


@pytest.mark.parametrize("message, values", [
    (LUNA_REFUSES_MAX, ["none", "low", "medium", "high", "xhigh"]),
    (ASTRA_REFUSES_NONE, ["low", "medium", "high", "xhigh"]),
    (EFFORT_UNKNOWN, None),
    ("Unsupported value: 'reasoning_effort' does not support 'max' with this model.", None),
])
def test_the_accepted_values_are_read_from_the_refusal(message, values):
    assert ai_profile._supported_values(message) == values


def test_one_refused_request_lists_the_levels_then_each_is_timed():
    send, sent = _thinker(max_answer=_bad(LUNA_REFUSES_MAX))
    thinking = ai_profile.detect_thinking(send, "openai", OPENAI_BASE)
    assert thinking == {"param": "reasoning_effort", "levels": ["low", "medium", "high"], "ms": LEVEL_MS,
                        "always": False, "temperature": False}
    assert sent[0] == {"model": "gpt-6-luna", "messages": ai_profile.TEST_MESSAGES, "max_completion_tokens": 200,
                       "reasoning_effort": "max"}
    assert [(body["reasoning_effort"], body["max_completion_tokens"]) for body in sent[1:]] == [
        ("low", 1000), ("medium", 1800), ("high", 3200)]
    assert not any("temperature" in body for body in sent)


def test_a_level_whose_thinking_uses_the_whole_budget_is_not_offered():
    send, _ = _thinker(max_answer=_bad(LUNA_REFUSES_MAX), cut_off={"high"})
    thinking = ai_profile.detect_thinking(send, "openai", OPENAI_BASE)
    assert thinking["levels"] == ["low", "medium"] and "high" not in thinking["ms"]


@pytest.mark.parametrize("max_answer", [None, _bad("Something new we do not understand"),
                                        _bad("Invalid value for 'reasoning_effort': 'max'."),
                                        (0, {"error": {"message": "No answer: ReadTimeout"}}, 15000)])
def test_without_a_list_each_level_is_tried(max_answer):
    send, sent = _thinker(max_answer=max_answer, refused={"high"})
    thinking = ai_profile.detect_thinking(send, "openai_compatible", OPENAI_BASE)
    assert thinking["levels"] == ["low", "medium"] and len(sent) == 4


def test_a_model_that_does_not_know_reasoning_effort_cannot_think():
    send, sent = _thinker(max_answer=_bad(EFFORT_UNKNOWN))
    thinking = ai_profile.detect_thinking(send, "openai", {**OPENAI_BASE, "reasoning_off": None})
    assert thinking == ai_profile.CANT_THINK and len(sent) == 1


def test_an_always_thinking_model_keeps_always_and_the_level_its_base_test_passed():
    send, _ = _service((lambda p: p["reasoning_off"] == {"reasoning_effort": "none"}, _bad(ASTRA_REFUSES_NONE)))
    profile = ai_profile.detect(send, "openai")["profile"]
    thinker, _ = _thinker(max_answer=_bad(ASTRA_REFUSES_MAX), model_id="gpt-6-astra")
    thinking = ai_profile.detect_thinking(thinker, "openai", profile)
    assert thinking == {"param": "reasoning_effort", "levels": ["low", "medium", "high"], "ms": LEVEL_MS,
                        "always": True, "temperature": False}
    flaky, _ = _thinker(max_answer=_bad(ASTRA_REFUSES_MAX), cut_off={"low"}, model_id="gpt-6-astra")
    assert ai_profile.detect_thinking(flaky, "openai", profile)["levels"] == ["low", "medium", "high"]


def test_openrouter_levels_come_from_its_public_model_list():
    send, sent = _thinker(model_id="openai/gpt-6-luna")
    thinking = ai_profile.detect_thinking(send, "openrouter", ai_profile.starting_profile("openrouter"),
                                          lambda: OR_LUNA)
    assert thinking == {"param": "openrouter_effort", "levels": ["low", "medium", "high"], "ms": LEVEL_MS,
                        "always": False, "temperature": False}
    assert [(body["reasoning"], body["max_tokens"]) for body in sent] == [
        ({"effort": "low", "exclude": True}, 1000), ({"effort": "medium", "exclude": True}, 1800),
        ({"effort": "high", "exclude": True}, 3200)]
    assert not any("temperature" in body for body in sent)   # and no "max" request: the list says it all


def test_a_mandatory_openrouter_model_always_thinks_and_keeps_temperature_only_when_listed():
    send, sent = _thinker(model_id="stepfun/step-5-preview")
    thinking = ai_profile.detect_thinking(send, "openrouter", ai_profile.starting_profile("openrouter"),
                                          lambda: OR_STEP)
    assert (thinking["always"], thinking["temperature"]) == (True, True)
    assert not any("temperature" in body for body in sent)   # the probes never carry it
    sol, _ = _thinker(model_id="openai/gpt-6.1-sol")
    thinking = ai_profile.detect_thinking(sol, "openrouter", ai_profile.starting_profile("openrouter"), lambda: OR_SOL)
    assert (thinking["always"], thinking["temperature"]) == (True, False)


@pytest.mark.parametrize("entry", [None, OR_GEMINI_25, {"id": "openai/gpt-4.1-mini", "reasoning": None}])
def test_an_openrouter_model_without_effort_levels_cannot_think(entry):
    send, sent = _thinker()
    thinking = ai_profile.detect_thinking(send, "openrouter", ai_profile.starting_profile("openrouter"),
                                          lambda: entry)
    assert thinking == ai_profile.CANT_THINK and sent == []


def test_openrouter_efforts_listed_as_null_mean_every_level_is_tried():
    """OpenRouter's docs: supported_efforts null means every effort is accepted (no model in the list did so on
    2026-10-11); the probes still decide."""
    send, sent = _thinker(refused={"high"}, model_id="x/any")
    entry = {"id": "x/any", "reasoning": {"mandatory": False, "supported_efforts": None}}
    thinking = ai_profile.detect_thinking(send, "openrouter", ai_profile.starting_profile("openrouter"),
                                          lambda: entry)
    assert thinking["levels"] == ["low", "medium"] and len(sent) == 3


def test_no_probe_starts_after_the_time_budget():
    ticks = iter([0, 0, ai_profile.THINKING_SECONDS + 1])
    send, sent = _thinker(max_answer=_bad(LUNA_REFUSES_MAX))
    thinking = ai_profile.detect_thinking(send, "openai", OPENAI_BASE, clock=lambda: next(ticks))
    assert thinking["levels"] == ["low"] and len(sent) == 2


@pytest.mark.parametrize("levels, always, asked, sent", [
    (["low", "medium", "high"], False, "medium", "medium"),
    (["low", "high"], False, "medium", "low"),
    (["medium", "high"], False, "low", "off"),
    (["low", "medium", "high"], False, "off", "off"),
    (["low", "medium", "high"], True, "off", "low"),
    (["medium", "high"], True, "low", "medium"),
    ([], False, "high", "off"),
])
def test_a_model_gets_the_nearest_lower_level_it_accepts(levels, always, asked, sent):
    assert ai_profile.effective_level(_can(levels, always), asked) == sent


def test_profiles_without_thinking_cannot_think():
    assert ai_profile.effective_level(OPENAI_BASE, "high") == "off"
    assert ai_profile.effective_level(None, "low") == "off"
    assert ai_profile.effective_level({**OPENAI_BASE, "thinking": ai_profile.CANT_THINK}, "low") == "off"
    assert ai_profile.allowance(None, "high") == 0


@pytest.mark.parametrize("profile, asked, extra", [
    (_can(["low", "medium"]), "high", 1600),
    (_can(["low", "medium", "high"]), "off", 0),
    (_can(["low", "medium", "high"], always=True), "off", 800),
    (_can(["high"]), "medium", 0),
])
def test_the_allowance_is_for_the_level_actually_sent(profile, asked, extra):
    assert ai_profile.allowance(profile, asked) == extra


def test_a_thinking_level_is_sent_without_temperature_or_reasoning_off():
    body = ai_profile.request_body(_can(["low", "medium"]), "gpt-6-luna", [], 1800, 0.7, level="high")
    assert body == {"model": "gpt-6-luna", "messages": [], "max_completion_tokens": 1800, "reasoning_effort": "medium"}
    assert ai_profile.request_body(_can(["low"]), "gpt-6-luna", [], 200, 0.7) == {
        "model": "gpt-6-luna", "messages": [], "max_completion_tokens": 200, "temperature": 0.7,
        "reasoning_effort": "none"}   # "off" is today's request


def test_openrouter_thinking_is_hidden_and_keeps_temperature_only_when_listed():
    profile = {**ai_profile.starting_profile("openrouter"),
               "thinking": {"param": "openrouter_effort", "levels": ["low", "medium", "high"], "ms": {},
                            "always": False, "temperature": True}}
    body = ai_profile.request_body(profile, "stepfun/step-5-preview", [], 1800, 0.7, {"provider": {"only": ["x"]}},
                                   level="medium")
    assert body == {"model": "stepfun/step-5-preview", "messages": [], "max_tokens": 1800, "temperature": 0.7,
                    "reasoning": {"effort": "medium", "exclude": True}, "provider": {"only": ["x"]}}
    assert "temperature" not in ai_profile.request_body(profile, "stepfun/step-5-preview", [], 1800, None,
                                                        level="medium")   # a caller's None sends none
    profile["thinking"]["temperature"] = False
    assert "temperature" not in ai_profile.request_body(profile, "openai/gpt-6-luna", [], 1800, 0.7, level="medium")
