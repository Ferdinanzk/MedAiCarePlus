"""The live agent's search_web tool (services/web_search.py): OpenAI's hosted web search through the Responses API,
answered as plain sentences with the sources; the request shape, the cleaning of the answer, and the failures."""

import json

import pytest
import requests

from app.services import web_search

BODY = {"output": [
    {"type": "reasoning", "summary": []},
    {"type": "web_search_call", "status": "completed", "action": {"type": "search", "queries": ["Reachy Mini news 2026"]}},
    {"type": "message", "content": [{"type": "output_text",
                                     "text": "**Reachy Mini** 推出了應用商店 ([hf.co](https://huggingface.co/blog?utm_source=openai))。\n\n"
                                             "現在有超過200個應用程式 [1]。詳見 https://example.com/page 。",
                                     "annotations": [{"type": "url_citation", "url": "https://huggingface.co/blog",
                                                      "title": "Hugging Face blog", "start_index": 0, "end_index": 5},
                                                     {"type": "url_citation", "url": "https://huggingface.co/blog",
                                                      "title": "dup", "start_index": 0, "end_index": 5}]}]},
]}


class Response:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def test_the_answer_is_cleaned_of_markdown_citations_and_urls():
    parsed = web_search.parse(BODY)
    assert parsed["answer"] == "Reachy Mini 推出了應用商店。 現在有超過200個應用程式。詳見。"
    assert parsed["sources"] == [{"title": "Hugging Face blog", "url": "https://huggingface.co/blog"}]
    assert parsed["queries"] == ["Reachy Mini news 2026"] and parsed["searched"]


def test_search_posts_the_hosted_tool_with_the_live_key_and_returns_the_answer(monkeypatch):
    calls = []

    def post(url, timeout, headers, json):
        calls.append((url, timeout, headers, json))
        return Response(200, BODY)

    monkeypatch.setattr(web_search.requests, "post", post)
    result = web_search.search("Reachy Mini 最新消息", "zh-TW", "sk-live", "backend-x")
    url, timeout, headers, body = calls[0]
    assert url == web_search.RESPONSES_URL and timeout == web_search.TIMEOUT
    assert headers == {"Authorization": "Bearer sk-live"} and body["model"] == "backend-x" and body["store"] is False
    assert body["tools"][0]["type"] == "web_search" and body["input"] == "Reachy Mini 最新消息"
    assert body["instructions"] == web_search.INSTRUCTIONS["zh-TW"]
    assert result["status"] == "ok" and result["searched"] and result["query"] == "Reachy Mini 最新消息"
    assert result["answer"].startswith("Reachy Mini 推出了應用商店") and result["searched_at"]
    assert "sk-live" not in json.dumps(result)


@pytest.mark.parametrize("post, status, reason", [
    (lambda *a, **k: Response(500, {}), "failed", "http_500"),
    (lambda *a, **k: Response(200, ValueError("bad")), "failed", "bad_json"),
    (lambda *a, **k: Response(200, {"output": []}), "failed", "no_answer"),
    (lambda *a, **k: (_ for _ in ()).throw(requests.Timeout()), "timeout", None),
    (lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError()), "failed", "ConnectionError"),
])
def test_failures_never_raise(monkeypatch, post, status, reason):
    monkeypatch.setattr(web_search.requests, "post", post)
    result = web_search.search("x", "en", "sk-live")
    assert result["status"] == status and result.get("reason") == reason and result["query"] == "x"


def test_the_query_is_trimmed_and_the_english_instructions_used(monkeypatch):
    seen = {}
    monkeypatch.setattr(web_search.requests, "post", lambda url, timeout, headers, json: seen.update(json) or Response(200, BODY))
    web_search.search("  " + "a" * 400, "en", "k")
    assert len(seen["input"]) == 300 and seen["instructions"] == web_search.INSTRUCTIONS["en"]
