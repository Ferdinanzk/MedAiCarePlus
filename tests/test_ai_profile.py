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
