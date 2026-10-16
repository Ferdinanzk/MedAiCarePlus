"""Requests whose tool must run whatever GPT-Live decides, for the live relay's backstop (live_session.py).

GPT-Live alone decides whether to hand a request to the agent's tools, and on 14 Oct 2026 it did not always:
「你可以看到我嗎」 got 「我現在看不到你」 (0 of 3 probe sessions called the camera with the old instructions), and
「跳舞給我看」 got 「好，我來跳」 with no dance (1 of 3 with the new ones). A clear request to dance, to stop dancing or
to look must not depend on that, so the relay reads the patient's words itself, as it does for risk and body
language, and runs the tool when GPT-Live has not asked for it in time.

- `detect(text, dancing)` -> "dance", "stop_dance", "look" or None: plain request forms only, in Traditional Chinese
  (the transcript may come in either script) or English. Talking about dancing or about something seen
  (「我年輕的時候很喜歡跳舞」, 「我昨天看到一隻狗」), a question about someone else (「他會跳舞嗎」) and a compliment
  (「你跳舞跳得很好」) are no request. "Stop" alone counts only while a dance runs, and 「再跳一次」 only after a dance.
- `TOOLS` names the delegated calls that satisfy an intent (GPT-Live asked for one of them itself).
"""

import re

from app.services.conversation import _traditional

INTENTS = ("stop_dance", "dance", "look", "time", "search")
TOOLS = {"dance": ("dance",), "stop_dance": ("stop_dance",),
         "look": ("describe_view", "camera", "identify_medicine", "verify_medication"),
         "time": ("get_current_time",), "search": ("search_web",)}
# A search request about these is another tool's (medicine, weather, time, reminders), never a web search
NOT_SEARCH = re.compile(r"藥|药|天氣|天气|幾點|几点|時間|时间|提醒|家人|吃飯|medicine|medication|pill|weather|time|remind|family")

_YOU = r"(你|妳|您|reachy|日希|瑞奇|瑞希)"
_DANCE = r"跳(個|一個|一支|一段|一下|支|段)?舞"
_MODAL = r"(可以|能|會|能不能|可不可以|會不會|要不要)"
_SEE = r"(看到|看得到|看的到|看不到|看見|看得見|看的見|看不見|看清楚)"
_ASK = r"(嗎|麼|呢|嘛|沒|不)"
_START = r"(?:^|(?<=\s))"   # a clause's start: the forms without a subject (「可以跳舞嗎」), never 「他會跳舞嗎」
# Patterns on the compact text (Traditional, lower case, no spaces or punctuation) and on the spaced text (punctuation
# as spaces, so a clause starts after a space); a form without a subject must start its clause.
PATTERNS = {
    "stop_dance": (re.compile(r"(停止|不要再?|別再?|不用再?)跳舞|跳舞停"), re.compile(r"(?<![a-z])stop dancing")),
    "dance": (
        re.compile(_YOU + _MODAL + r"?(幫我|給我|為我|來)?" + _DANCE + r"(?!的|得|了|過|跳)"),  # 你可以跳舞 (not 你跳舞跳得好)
        re.compile(_START + r"(要不要|能不能|可不可以|會不會)(幫我|給我|為我)?" + _DANCE),   # 要不要跳個舞
        re.compile(_START + r"(可以|能|會)(幫我|給我|為我)?" + _DANCE + _ASK),              # 可以跳舞嗎
        re.compile(_DANCE + r"給(我|我們)看"),                                        # 跳舞給我看
        re.compile(r"再跳一(支|段|個)舞|再來一(支|段|個)舞"),                             # 再跳一支舞
        re.compile(r"^(來|請|快)?" + _DANCE + r"(吧|啦|嘛|喔|哦)?$"),                    # 跳舞 / 跳舞吧, the whole turn
        re.compile(r"(?<![a-z])(can|could|will|would) you (please )?dance(?![a-z])"),
        re.compile(r"(?<![a-z])dance for (me|us)(?![a-z])"),
        re.compile(r"(?<![a-z])(do|show me) a (little )?dance(?![a-z])"),
        re.compile(r"(?<![a-z])dance again(?![a-z])"),
        re.compile(r"^(please )?dance( for me| now| again| please)?$"),
    ),
    "look": (
        re.compile(_YOU + r"(有沒有|可以|能|能不能|可不可以|有)?" + _SEE + r"(我|我們|這個|這裡|那個|什麼)"),  # 你看得到我
        re.compile(_START + r"(可以|能|有沒有)?" + _SEE + r"我" + _ASK),                # 看得到我嗎
        re.compile(_YOU + r"看(一下|一看|看)(我|這個|這裡|我的)"),                          # 你看看我
        re.compile(_START + r"看(一下|一看|看)(我|這個|這裡)"),                           # 看看我 (not 我看看我的)
        re.compile(_YOU + r"看我(穿|拿|手上|今天|的)"),                                    # 你看我穿什麼
        re.compile(r"(?<![a-z])(can|could) you see (me|us|this|that)(?! (i|you) mean)(?![a-z])"),
        re.compile(r"(?<![a-z])do you see (me|us|this|that)(?![a-z])"),
        re.compile(r"(?<![a-z])what (do|can) you see(?![a-z])"),
        re.compile(r"(?<![a-z])look at (me|this|that)(?![a-z])"),
    ),
    "time": (
        re.compile(r"現在(是)?(幾點|幾點鐘|什麼時間|什麼時候)|幾點(了|鐘了|鐘)$|幾點(了|鐘)[嗎呢啊呀]?$|(現在|今天)(的)?(時間|日期)[是呢]?"
                   r"|今天(是)?(幾號|幾月幾號|星期幾|禮拜幾|什麼日子)|(現在|今天)(幾號|星期幾|禮拜幾)"),
        re.compile(r"(?<![a-z])what('s| is) the (time|date)(?![a-z])|(?<![a-z])what time is it(?![a-z])"
                   r"|(?<![a-z])what day (is it|is today|of the week)(?![a-z])|(?<![a-z])what('s| is) today'?s date(?![a-z])"
                   r"|(?<![a-z])(do you )?know what time it is(?![a-z])|(?<![a-z])tell me the time(?![a-z])"),
    ),
    "search": (
        re.compile(r"(上網|網路上|網上|用網路)(幫我)?(查|搜尋|搜|找)(?!到)|(幫我|請你|可以|能不能|可不可以)(上網)?(查一下|查查|搜尋|搜一下|找一下|查)"
                   r"|(搜尋|查詢|查一下)(一下)?(關於)?|最新(的)?.{0,6}?(新聞|消息|資訊|動態|發展)"
                   r"|(今天|最近|這幾天).{0,8}?(有什麼|發生了?什麼)(新聞|事|大事|消息)|(新聞|消息)(是什麼|有哪些|怎麼樣)"),
        re.compile(r"(?<![a-z])search (the web|online|the internet|google|for)(?![a-z])|(?<![a-z])look (it |that |this )?up(?![a-z])"
                   r"|(?<![a-z])google (it|that|this|for)?(?![a-z])|(?<![a-z])(latest|newest|recent|today'?s) (\w+ ){0,3}(news|headlines|updates?|developments?)(?![a-z])"
                   r"|(?<![a-z])what('s| is) (new|happening|going on) (in|with|about)(?![a-z])|(?<![a-z])what happened (today|recently|this week) (in|with|to)(?![a-z])"
                   r"|(?<![a-z])(any |the )?news (about|on|from)(?![a-z])"),
    ),
}
# 「再跳一次」 by itself (also "jump again") is a dance only after one in this chat (the relay asks the registry)
AGAIN = re.compile(r"再跳一?(次|下|遍)|再來一(次|遍)")
# "Stop" by itself is a stop only while a dance runs (the relay asks the tool registry)
STOP_WORDS = (re.compile(r"^(停|停下|停下來|停止|好了|夠了|不要跳了|別跳了|不用跳了)(吧|啦|了)?$"),
              re.compile(r"^(ok |okay )?(stop|enough|that's enough)( it| now| please)?$"))
_PUNCTUATION = re.compile(r"(?:[^\w\s]|_)+")


def _prepare(text: str) -> tuple[str, str]:
    """(compact, spaced): the words in Traditional Chinese, lower case; compact without spaces or punctuation for
    the Chinese patterns, spaced (punctuation as spaces) for the English ones."""
    converted = _traditional(text).lower().replace("’", "'")
    compact = re.sub(r"\s+", "", _PUNCTUATION.sub("", converted.replace("'", "")))
    spaced = re.sub(r"\s+", " ", re.sub(r"[^\w\s']+", " ", converted)).strip()
    return compact, spaced


def detect(text: str, dancing: bool = False, danced: bool = False) -> str | None:
    """The tool request in the patient's words: "stop_dance", "dance", "look", or None. `dancing`: a dance runs now,
    so a bare "stop" (停, 夠了, stop) is a stop too; `danced`: this chat had a dance, so 「再跳一次」 is another."""
    if not text or not text.strip():
        return None
    compact, spaced = _prepare(text)
    if dancing and (STOP_WORDS[0].search(compact) or STOP_WORDS[1].search(spaced)):
        return "stop_dance"
    for intent in INTENTS:
        if intent == "search" and (NOT_SEARCH.search(compact) or NOT_SEARCH.search(spaced)):
            continue   # 「幫我查一下我今天要吃什麼藥」 is the medicine tool's, 「查一下天氣」 the weather's
        if any(pattern.search(compact) or pattern.search(spaced) for pattern in PATTERNS[intent]):
            return intent
    if danced and AGAIN.search(compact):
        return "dance"
    return None
