"""Web search for the live agent's search_web tool (14 Oct 2026).

Pollen's official conversation app gives its model a `search_web(query, max_results)` function that returns a short
list of results (title, snippet, url) from DuckDuckGo through a public Hugging Face Space. This server runs the same
kind of function tool, but the search itself is OpenAI's hosted web search through the Responses API, with the live
voice connection's key (ai_settings.live_target): the patient's words already go to OpenAI for the chat, so no new
service sees them, nothing new is installed, and the answer comes back as a few plain sentences Reachy can read aloud
plus the pages it came from. `search()` is blocking (requests) and is run in a thread by the registry; the answer is
cleaned of markdown and citation markers (measured: the model wrote **bold** and ([site](url)) although asked not to).
"""

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from app import config

log = logging.getLogger(__name__)

RESPONSES_URL = "https://api.openai.com/v1/responses"
TIMEOUT = 20.0
MAX_RESULTS = 6
MAX_ANSWER = 600
INSTRUCTIONS = {
    "zh-TW": ("你是一個居家陪伴機器人的搜尋助手。請先用 web search 工具搜尋這個問題，再根據搜尋結果用繁體中文回答。"
              "只用純文字：不要 markdown、不要標題、不要條列、不要網址、不要引用標記。最多四個適合唸給長者聽的短句，"
              "先講最重要的事實，和日期有關的要說日期。找不到相關資料就用一句話說找不到。"),
    "en": ("You are the search helper of a home companion robot. First search the web for the question with the web "
           "search tool, then answer from what you found, in English. Plain text only: no markdown, no headings, no "
           "bullet lists, no URLs, no citation markers. At most four short sentences that can be read aloud to an "
           "older adult: the key facts first, with dates when they matter. If nothing relevant was found, say so in "
           "one sentence."),
}
_CITATION = re.compile(r"\s*\(\[[^\]]*\]\([^)]*\)\)|\s*\[[^\]]*\]\((?:https?://)[^)]*\)|\s*\(https?://[^)]*\)|\[\d+\]|【[^】]*】")
_URL = re.compile(r"https?://\S+")
_MARKDOWN = re.compile(r"[*_#`>]+")


def clean(text: str) -> str:
    """The answer as Reachy can say it: no citation parentheticals, URLs or markdown, single spaces."""
    text = _CITATION.sub("", text or "")
    text = _URL.sub("", text)
    text = _MARKDOWN.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n+\s*", " ", text)
    text = re.sub(r"\s+([。，、！？；：,.!?;:])", r"\1", text)   # a removed citation leaves no space before the stop
    return text.strip()[:MAX_ANSWER]


def parse(body: dict) -> dict:
    """What the Responses answer holds: the cleaned answer text, the sources it cited, and the queries searched."""
    answer, sources, queries, searched = "", [], [], False
    seen = set()
    for item in body.get("output") or []:
        kind = item.get("type")
        if kind == "web_search_call":
            searched = True
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            for query in action.get("queries") or ([action["query"]] if action.get("query") else []):
                queries.append(str(query))
        elif kind == "message":
            for part in item.get("content") or []:
                if part.get("type") in ("output_text", "text"):
                    answer += str(part.get("text") or "")
                    for note in part.get("annotations") or []:
                        url = note.get("url")
                        if note.get("type") == "url_citation" and url and url not in seen:
                            seen.add(url)
                            sources.append({"title": str(note.get("title") or "")[:120], "url": url})
    return {"answer": clean(answer), "sources": sources[:MAX_RESULTS], "queries": queries[:MAX_RESULTS],
            "searched": searched}


def search(query: str, language: str, api_key: str, model: str | None = None, *, timeout: float = TIMEOUT) -> dict:
    """One web search, answered in `language`. Never raises: status ok (with answer, sources, queries, searched_at),
    or failed / timeout with a reason. The key never gets logged."""
    query = str(query or "").strip()[:300]
    lang = "zh-TW" if language == "zh-TW" else "en"
    body = {"model": model or config.LIVE_BACKEND_MODEL, "store": False, "instructions": INSTRUCTIONS[lang],
            "input": query,
            "tools": [{"type": "web_search", "search_context_size": "low",
                       "user_location": {"type": "approximate", "country": "TW", "timezone": config.MEDCARE_TIMEZONE}}],
            "tool_choice": "auto"}
    started = datetime.now(ZoneInfo(config.MEDCARE_TIMEZONE))
    try:
        response = requests.post(RESPONSES_URL, timeout=timeout, headers={"Authorization": f"Bearer {api_key}"},
                                 json=body)
    except requests.Timeout:
        log.warning("web search timed out after %.0f s", timeout)
        return {"status": "timeout", "query": query}
    except requests.RequestException as error:
        log.warning("web search failed (%s)", type(error).__name__)
        return {"status": "failed", "query": query, "reason": type(error).__name__}
    if response.status_code != 200:
        log.warning("web search: the Responses API answered %s", response.status_code)
        return {"status": "failed", "query": query, "reason": f"http_{response.status_code}"}
    try:
        parsed = parse(response.json())
    except ValueError:
        return {"status": "failed", "query": query, "reason": "bad_json"}
    if not parsed["answer"]:
        return {"status": "failed", "query": query, "reason": "no_answer", "searched": parsed["searched"]}
    log.info("[TOOL] search_web: %d source(s), %d quer%s, %s", len(parsed["sources"]), len(parsed["queries"]),
             "y" if len(parsed["queries"]) == 1 else "ies", "searched" if parsed["searched"] else "answered without searching")
    return {"status": "ok", "query": query, "searched_at": started.isoformat(timespec="seconds"), **parsed}
