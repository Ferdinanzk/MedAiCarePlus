"""Requests whose tool must run whatever GPT-Live decides, for the live relay's backstop (live_session.py).

GPT-Live alone decides whether to hand a request to the agent's tools, and on 14 Oct 2026 it did not always:
「你可以看到我嗎」 got 「我現在看不到你」 (0 of 3 probe sessions called the camera with the old instructions), and
「跳舞給我看」 got 「好，我來跳」 with no dance (1 of 3 with the new ones). A clear request to dance, to stop dancing or
to look must not depend on that, so the relay reads the patient's words itself, as it does for risk and body
language, and runs the tool when GPT-Live has not asked for it in time.

- `detect(text, dancing)` -> "dance", "stop_dance", "look", "time", "search", "took_medicine" or None: plain
  request forms only, in Traditional Chinese (the transcript may come in either script) or English. Talking about
  dancing or about something seen (「我年輕的時候很喜歡跳舞」, 「我昨天看到一隻狗」), a question about someone else
  (「他會跳舞嗎」) and a compliment (「你跳舞跳得很好」) are no request. 「再跳一次」 counts only after a dance.
- `stops(text, dancing, waiting, danced)`: 「不要跳舞了」 or "stop dancing" at any time, and while a dance runs or waits
  for the backstop a stop phrase anywhere in a clause of the words (「不要再跳了」, 「日希，停下來」, 「停，不要跳了」,
  "Reachy, stop"; agent tool fixes, Task 5), never "don't stop" or another kind of stop (「不要停」, 「不停」, 「停車」,
  「停電」, 「我不想停」). Each clause is read by itself, so a "don't" never reaches across a comma (「不要，停下來」 is a
  stop), and a stop phrase counts only after the last dance request of the words (「停一下，跳舞給我看」 asks for a
  dance; round 3 of 17 Oct 2026).
- "took_medicine" is the patient's own word that the medicine is taken (「我吃完藥了」, "I took my medicine"): GPT-Live
  praised it itself and it often never reached record_medication_taken (agent tool fixes, Task 4, Oct 2026). It is
  a closed whole-turn match (claims_intake, round 3): the turn, without the call name and the fillers, is nothing but
  one plain first-person finished intake that names the medicine, with one closing word after it at most (再見,
  謝謝, "thanks"). Anything else in the turn is GPT-Live's own delegation to answer.
- `TOOLS` names the delegated calls that satisfy an intent (GPT-Live asked for one of them itself).
"""

import functools
import re

from app.services.conversation import _traditional

INTENTS = ("stop_dance", "took_medicine", "dance", "look", "time", "search")
TOOLS = {"dance": ("dance",), "stop_dance": ("stop_dance",),
         "look": ("describe_view", "camera", "identify_medicine", "verify_medication"),
         "time": ("get_current_time",), "search": ("search_web",),
         # GPT-Live's backend took the claim: its completion flow's due check or record (reading the schedule records
         # nothing, so it no longer drops a claim nobody recorded; round 3)
         "took_medicine": ("check_medication_due", "record_medication_taken")}
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
        # (and DANCE_CLAUSE: 「雨停了，跳個舞吧」)
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
# A clause that is a dance request by itself, with a word that asks (「雨停了，跳個舞吧」, 「好，跳舞吧」, 「來跳舞」;
# round 3 of 17 Oct 2026): never a bare 跳舞 in a list (「跳舞、唱歌」)
DANCE_CLAUSE = re.compile(r"(來|請|快)" + _DANCE + r"(吧|啦|嘛|喔|哦)?|(來|請|快)?" + _DANCE + r"(吧|啦|嘛)")
# 「再跳一次」 by itself (also "jump again") is a dance only after one in this chat (the relay asks the registry), and so
# is the patient asking the dance to go on (「繼續跳」, 「算了，你繼續跳吧」, "keep dancing", "go on dancing"; round 3:
# after a stop, only a dance request ends its hold), never 「不要繼續跳了」 or "don't keep dancing"
AGAIN = (re.compile(r"再跳一?(次|下|遍)|再來一(次|遍)"
                    r"|(?<!不要)(?<!不用)(?<!不想)(?<![不別])(?<!不要再)(?<!別再)(?<!不再)繼續跳"),
         re.compile(r"(?<!don't )(?<!not )(?<![a-z'])(keep|go on|continue|carry on) dancing(?![a-z])"))
# While a dance may still run (the relay asks the tool registry and its own waiting request), a stop phrase anywhere in
# the patient's words is a stop (agent tool fixes, Task 5: 「不要再跳了」, 「日希，停下來」 and "Reachy, stop" were missed):
# 停 in any form (「停」, 「停下來」, 「停止」, 「好了，停」, 「你可以停下來嗎」, 「暫停」, 「你要不要停下來」), 「不要再跳了」,
# 「別跳了」, 「不跳了」, a clause that ends with 不要跳, 別跳 or 不要再跳 (and 了, 啦 or nothing: 「不要再跳啦」), "stop"
# ("please stop", "Reachy, stop"); 「夠了」, 「夠啦」 and "enough" only as a whole clause (「日希，夠了」). Each clause is
# read by itself (stops), so 「停，不要跳了」 and 「不要，停下來」 are stops
STOP_WORDS = (re.compile(r"停|(不要|別|不用|不)再?跳了|(不要|別|不用)再?(繼續)?跳(了|啦)?$|^((夠了|夠啦)(吧|啦)?)+$"),
              re.compile(r"(?<![a-z])stop(?![a-z])|^(ok |okay )?(enough|that's enough)( now| please)?$"))
# 「好了」 and 「好啦」 as a whole clause only while a dance actually runs (stops' `dancing`): while a dance request only
# waits for the backstop, 「好了，開始跳吧」 agrees to it (round 3; 「我吃好了」 is never a stop)
OKAY_WORDS = re.compile(r"^((好了|好啦|夠了|夠啦)(吧|啦)?)+$")
# ... but never "don't stop" or another kind of stop, removed from the clause before any stop pattern reads it (so
# 「不要停止跳舞」 and "don't stop dancing" are no stop either): 「不要停」, 「別停」, 「不停」, 「跳個不停」, 「停不下來」,
# 「停不了」, 「停車」, 「停電」, 「停藥」, 「我不想停」, "non-stop", "can't stop laughing", "bus stop" (「停不要跳了」 is a
# stop: only 「停不下」, 「停不了」, 「停不住」 and 「停不得」 say "can't stop"). Round 3: 要不要停 asks to stop (「你要不要停
# 下來」), and so do the complaints 還沒停 and 還不停 (「你怎麼還沒停」, 「你還不停」)
NOT_STOP = (re.compile(r"(?<!要)不要停|不用停|別停|(?<!還)沒有?停|不想停|(?<!還)不停"
                       r"|停(不[下了住得]|車|電|水|留|課|班|工|業|靠|經|藥|用|頓)"),
            re.compile(r"(?<![a-z])(don't|dont|do not|never|not|can't|cannot|couldn't|won't|wouldn't|didn't|non) stop"
                       r"(?![a-z])|(?<![a-z])stop (by|over)(?![a-z])|(?<![a-z])bus stop(?![a-z])"))
_PUNCTUATION = re.compile(r"(?:[^\w\s]|_)+")
# The patient's own intake, done (took_medicine): a CLOSED whole-turn match (round 3 of 17 Oct 2026). Three review
# rounds each found phrases an open-ended reader got wrong (conditions across a comma, other people, clock times such
# as 「我吃過藥了，八點吃的」, retractions, transcript fragments that dropped a real claim for good), and that set never
# ends. So a turn is a claim only when, once the call name and the fillers around each clause are dropped (CLAIM_LEAD,
# CLAIM_TRAIL), nothing is left but one plain first-person finished intake that names the medicine (CLAIM_ZH,
# CLAIM_EN), with at most one closing word after it (CLOSING_ZH, CLOSING_EN). Anything else in the turn (another
# clause, a time, a person, a question, a plan, a doubt) is no backstop claim, and GPT-Live's own delegation (the
# tools prompt) answers it: a false claim files a request for the current dose and makes overdose protection refuse
# the real one for up to 2 h, while a missed one only leaves GPT-Live's own call as the path.
CLAIM_LEAD = re.compile(r"^(?:\s*(?:日希|瑞希|瑞奇|嗯|喔|哦|欸|誒|啊|呀|好|對|那個|我跟你說|跟你說"
                        r"|(?:reachy|richy|rishi|um|uh|oh|okay|ok|well|so|yes|yeah)(?![a-z'])))+")
CLAIM_TRAIL = re.compile(r"(?:\s*(?:日希|瑞希|瑞奇|嗯|喔|哦|欸|誒|啊|呀"
                         r"|(?<![a-z'])(?:reachy|richy|rishi|um|uh|oh|okay|ok)))+$")
_PILLS = r"(藥|藥物|藥丸|藥片)"
_JUST = r"(已經|剛剛|剛才|剛|有)*"
_SINCE = r"(已經|剛剛|剛才|剛|有)?"
CLOSING_ZH = r"(再見|拜拜|謝謝|囉|啦)"
CLOSING_EN = r"(bye|goodbye|thanks|thank you)"
_CALL_NAME = re.compile(r"日希|瑞希|瑞奇|(?<![a-z'])(?:reachy|richy|rishi)(?![a-z'])")
# The Chinese forms (compact), each with what makes it done in its own words (None: the form says it): 我 with at
# most a "just" or "already" word before 吃 (我吃完藥了, 我已經吃藥了, 我剛剛吃藥了, 我吃了藥; never 「我有吃藥」, "I'm on
# medication", nor 「我吃藥囉」, "I'm taking it now"); no subject, with 完/過/好/掉 or 已經/剛 (吃完藥了; never 「吃藥了」,
# "medicine time!"); or the medicine first (藥吃了, 藥我吃過了, 我把藥吃完了; never 「把藥吃了」, an order, and 吃完了
# only after 我把: 「藥吃完了」 and 「我的藥吃完了」 say it ran out). No 今天的: 「我吃完今天的藥了」 can be every dose of
# the day
CLAIM_ZH = tuple((re.compile(r"(?P<claim>" + form + r")" + CLOSING_ZH + "?"), done) for form, done in (
    (r"我" + _JUST + r"吃(完|過|好|掉)?了?(這次的|我的)?" + _PILLS + r"了?", re.compile(r"已經|剛|完|過|好|掉|了")),
    (_JUST + r"吃(完|過|好|掉)?了?(這次的|我的)?" + _PILLS + r"了?", re.compile(r"已經|剛|完|過|好|掉")),
    (r"(我把?)?" + _PILLS + r"我?" + _SINCE + r"吃(了|過了|好了|掉了)|我把" + _PILLS + _SINCE + r"吃完了", None)))
# "I took my medicine", "I've taken my pills", "I have had my meds", "I just took my medication", "I already took my
# pills" (spaced), with one closing word at most
CLAIM_EN = re.compile(r"i( have|'ve)?( already| just)? (took|taken|had|finished taking) (my|the) "
                      r"(medicine|medicines|medication|medications|pills?|meds|tablets?)( already| now| just now)?"
                      r"( (bye|goodbye|thanks|thank you))?")
_CLAUSE_END = re.compile(r"([，,。.!！?？；;：:、…~～\n]+)")
_ASKED = re.compile(r"[?？]")


def _normal(text: str) -> str:
    """The words in Traditional Chinese, lower case. Curly apostrophes are made straight first: the script
    conversion turns ’ into 』, so "I’ve taken my pills" was never read."""
    return _traditional(text.replace("’", "'").replace("‘", "'")).lower()


def _prepare(text: str) -> tuple[str, str]:
    """(compact, spaced): the words in Traditional Chinese, lower case; compact without spaces or punctuation for
    the Chinese patterns, spaced (punctuation as spaces) for the English ones."""
    converted = _normal(text)
    compact = re.sub(r"\s+", "", _PUNCTUATION.sub("", converted.replace("'", "")))
    spaced = re.sub(r"\s+", " ", re.sub(r"[^\w\s']+", " ", converted)).strip()
    return compact, spaced


def _claim_words(text: str) -> tuple[str, str]:
    """(compact, spaced): the turn as the claim reader sees it (_normal), each clause without the call name and the
    fillers around it (CLAIM_LEAD, CLAIM_TRAIL; a clause of nothing else is dropped), the punctuation dropped."""
    clauses = []
    for clause in _CLAUSE_END.split(_normal(text))[::2]:
        words = re.sub(r"\s+", " ", re.sub(r"[^\w\s']+|_", " ", clause)).strip()
        words = CLAIM_TRAIL.sub("", CLAIM_LEAD.sub("", words)).strip()
        if words:
            clauses.append(words)
    spaced = " ".join(clauses)
    return re.sub(r"[\s']+", "", spaced), spaced


def claims_intake(text: str) -> bool:
    """Whether the whole turn is the patient's word that they took their medicine (took_medicine): with the call
    name and the fillers dropped, nothing but one plain first-person finished intake that names the medicine
    (CLAIM_ZH, CLAIM_EN), and at most one closing word after it. Never with a question mark."""
    if not text or not text.strip() or _ASKED.search(text):
        return False
    compact, spaced = _claim_words(text)
    if CLAIM_EN.fullmatch(spaced):
        return True
    for form, done in CLAIM_ZH:
        match = form.fullmatch(compact)
        if match and (done is None or done.search(match.group("claim"))):
            return True
    return False


def closes(text: str) -> bool:
    """Whether the patient's words after a claim leave it standing: one closing word (CLOSING_ZH, CLOSING_EN: 再見,
    拜拜, 謝謝, 囉, 啦, bye, goodbye, thanks, thank you), with at most the call name around it. Never words without a
    closing word, and no filler is dropped here: 對, 好, 嗯, "yes" and "ok" can answer GPT-Live's question
    (「我吃過藥了」, 「是昨天晚上吃的嗎？」, 「對」 or 「對，謝謝」), and the claim would then file today's dose on last
    night's pill (round 3 review of 17 Oct 2026)."""
    if not text or _ASKED.search(text):
        return False
    compact, spaced = (_CALL_NAME.sub(" ", words) for words in _prepare(text))
    return bool(re.fullmatch(CLOSING_ZH, compact.replace(" ", ""))
                or re.fullmatch(CLOSING_EN, re.sub(r"\s+", " ", spaced).strip()))


@functools.lru_cache(maxsize=64)
def _forms(text: str) -> tuple[tuple[str, str, int], ...]:
    """Each clause of the words (between two punctuation marks, _CLAUSE_END) as (compact, spaced, offset): its two
    forms (_prepare) and where its compact form starts in the turn's (the clauses' compact forms joined)."""
    forms, offset = [], 0
    for clause in _CLAUSE_END.split(text)[::2]:
        compact, spaced = _prepare(clause)
        forms.append((compact, spaced, offset))
        offset += len(compact)
    return tuple(forms)


def _compact_at(spaced: str, index: int) -> int:
    """The position in the compact form that a position in the spaced form falls on (the compact form is the spaced
    one without its spaces, apostrophes and underscores)."""
    return len(re.sub(r"[\s'_]", "", spaced[:index]))


def _dance_end(text: str, danced: bool = False) -> int:
    """Where the last dance request of the words ends, as a position in the turn's compact form (the clauses'
    compact forms joined), or -1 without one: the dance PATTERNS, a whole clause that asks (DANCE_CLAUSE), and after a
    dance in this chat (`danced`) 「再跳一次」 or the dance asked to go on (AGAIN)."""
    forms = _forms(text)
    compact = "".join(clause for clause, _, _ in forms)
    spaced = " ".join(words for _, words, _ in forms if words)
    again = AGAIN if danced else ()
    ends = [match.end() for pattern in PATTERNS["dance"] + again[:1] for match in pattern.finditer(compact)]
    ends += [_compact_at(spaced, match.end()) for pattern in PATTERNS["dance"] + again[1:]
             for match in pattern.finditer(spaced)]
    ends += [offset + len(clause) for clause, _, offset in forms if DANCE_CLAUSE.fullmatch(clause)]
    return max(ends, default=-1)


def _blank(pattern: re.Pattern, words: str) -> str:
    """The words with what `pattern` matches made spaces of the same length, so positions stay where they were."""
    return pattern.sub(lambda match: " " * len(match.group()), words)


def stops(text: str, dancing: bool = False, waiting: bool = False, danced: bool = False) -> bool:
    """Whether the patient asks the dance to stop: 「不要跳舞了」 or "stop dancing" at any time (PATTERNS), and while a
    dance actually runs (`dancing`: ToolRegistry.dancing) or a dance request waits for the backstop (`waiting`) a
    stop phrase anywhere in a clause (STOP_WORDS; 「好了」 and 「好啦」 as a whole clause only while one runs,
    OKAY_WORDS), never "don't stop" or another kind of stop (NOT_STOP). Each clause (between two punctuation marks,
    _CLAUSE_END) is read by itself, so a "don't" never reaches across a comma: 「不要，停下來」 is a stop, 「不要停，繼續
    跳」 is not. A stop phrase counts only after the last dance request of the words (_dance_end; `danced`: this chat
    had a dance, so 「再跳一次」 and 「繼續跳」 are requests), so 「停一下，跳舞給我看」 and 「雨停了，跳個舞吧」 ask for a
    dance (round 3 of 17 Oct 2026).
    The relay reads the words at every transcript fragment and stops at the first one that holds a stop, so a
    fragment that ends right after 停 (「停」 then 「車場在哪裡」, 「停」 then 「電了」) stops the dance: that is the
    cost of stopping at once, accepted (round 3); the hold then ends when the patient asks to dance again."""
    if not text or not text.strip():
        return False
    after = _dance_end(text, danced)
    near = dancing or waiting
    chinese = PATTERNS["stop_dance"] + ((STOP_WORDS[0],) if near else ()) + ((OKAY_WORDS,) if dancing else ())
    english = PATTERNS["stop_dance"] + ((STOP_WORDS[1],) if near else ())
    for compact, spaced, offset in _forms(text):
        if offset + len(compact) <= after:
            continue   # the whole clause comes before the last dance request
        unblanked = spaced
        for pattern in NOT_STOP:
            compact, spaced = _blank(pattern, compact), _blank(pattern, spaced)
        if any(offset + match.start() >= after for pattern in chinese for match in pattern.finditer(compact)):
            return True
        if any(offset + _compact_at(unblanked, match.start()) >= after
               for pattern in english for match in pattern.finditer(spaced)):
            return True
    return False


def detect(text: str, dancing: bool = False, danced: bool = False, claims: bool = True, waiting: bool = False,
           stop: bool = True) -> str | None:
    """The tool request in the patient's words: "stop_dance", "took_medicine", "dance", "look", "time", "search", or
    None. `dancing`: a dance actually runs, and `waiting`: a dance request waits for the backstop, so a stop phrase
    anywhere in the words (停, 不要再跳了, stop) is a stop too (stops); `danced`: this chat had a dance, so 「再跳一次」
    and 「繼續跳」 are another; `claims` False leaves the patient's word that the medicine is taken to the caller (the
    relay reads it with claims_intake, beside any other request of the same words), and `stop` False leaves a stop to
    the caller (the relay runs it at once) and reads the words for the other requests (round 3: 「停，你看得到我嗎」
    looks too). A dance asked for before a stop in the same words is no request."""
    if not text or not text.strip():
        return None
    compact, spaced = _prepare(text)
    stopped = stops(text, dancing, waiting, danced)
    for intent in INTENTS:
        if intent == "stop_dance":
            if stop and stopped:
                return intent
            continue
        if intent == "took_medicine":
            if claims and claims_intake(text):
                return intent
            continue
        if intent == "dance":
            if not stopped and _dance_end(text) >= 0:
                return intent
            continue
        if intent == "search" and (NOT_SEARCH.search(compact) or NOT_SEARCH.search(spaced)):
            continue   # 「幫我查一下我今天要吃什麼藥」 is the medicine tool's, 「查一下天氣」 the weather's
        if any(pattern.search(compact) or pattern.search(spaced) for pattern in PATTERNS[intent]):
            return intent
    if danced and not stopped and _dance_end(text, danced) >= 0:
        return "dance"   # 「再跳一次」, 「繼續跳」 (AGAIN)
    return None
