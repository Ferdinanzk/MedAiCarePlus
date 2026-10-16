"""Requests whose tool must run whatever GPT-Live decides, for the live relay's backstop (live_session.py).

GPT-Live alone decides whether to hand a request to the agent's tools, and on 14 Oct 2026 it did not always:
「你可以看到我嗎」 got 「我現在看不到你」 (0 of 3 probe sessions called the camera with the old instructions), and
「跳舞給我看」 got 「好，我來跳」 with no dance (1 of 3 with the new ones). A clear request to dance, to stop dancing or
to look must not depend on that, so the relay reads the patient's words itself, as it does for risk and body
language, and runs the tool when GPT-Live has not asked for it in time.

- `detect(text, dancing)` -> "dance", "stop_dance", "look", "time", "search", "took_medicine" or None: plain
  request forms only, in Traditional Chinese (the transcript may come in either script) or English. Talking about
  dancing or about something seen (「我年輕的時候很喜歡跳舞」, 「我昨天看到一隻狗」), a question about someone else
  (「他會跳舞嗎」) and a compliment (「你跳舞跳得很好」) are no request. "Stop" alone counts only while a dance runs, and
  「再跳一次」 only after a dance.
- "took_medicine" is the patient's own word that the medicine is taken (「我吃完藥了」, "I took my medicine"): GPT-Live
  praised it itself and it often never reached record_medication_taken (agent tool fixes, Task 4, Oct 2026). It is
  read clause by clause and must start and end its clause, never for another person, another time, a plan, a
  condition, a belief or a doubt, someone else's words, a negation, a question, food, running out or an extra dose
  (took_medicine below).
- `TOOLS` names the delegated calls that satisfy an intent (GPT-Live asked for one of them itself).
"""

import re

from app.services.conversation import _traditional

INTENTS = ("stop_dance", "took_medicine", "dance", "look", "time", "search")
TOOLS = {"dance": ("dance",), "stop_dance": ("stop_dance",),
         "look": ("describe_view", "camera", "identify_medicine", "verify_medication"),
         "time": ("get_current_time",), "search": ("search_web",),
         # GPT-Live's backend took the claim: its completion flow or the camera workflow's steps
         "took_medicine": ("check_medication_due", "get_medication_schedule", "get_pill_intake_result",
                           "record_medication_taken")}
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
# The patient's own intake, done (took_medicine), as one whole clause (compact, Traditional): 我 with at most an
# "already" word before 吃 (我吃完藥了, 我已經吃藥了, 我吃了藥); no subject, with 完/過/好/掉 or 已經/剛 (吃完藥了,
# 已經吃藥了; never 「吃藥了」, "medicine time!" said to someone); or the medicine first (藥吃了, 我把藥吃完了; never
# 「把藥吃了」, an order, and 吃完了 only after 我把: 「藥吃完了」 and 「我的藥吃完了」 say it ran out). Each form with what
# makes it done (on the claim's own words, `claim`: 「我也吃藥」 is a habit and 「我有吃藥」 can be "I'm on medication";
# None: the form says it). The claim starts its clause, after at most a call name or a filler (_LEAD: 「日希我吃完藥了」),
# so a condition, a plan, a belief, a doubt or someone else's words in front never fit (如果我吃完藥了, 等我吃完藥了,
# 我以為我吃完藥了, 我不確定我吃過藥了, 他說我吃完藥了), nor does another person (他, 我媽媽). It ends its clause, a
# sentence particle at most: 「我吃完藥了再跟你說」 and 「我吃完藥就去睡覺」 are plans, 「我吃藥了嗎」 a question.
_LEAD = r"^(日希|瑞希|瑞奇|reachy|richy|rishi|嗯|對|好|喔|哦|欸|我跟你說|跟你說|你知道)*"
_ALREADY = r"(已經|剛剛|剛才|剛|有|都|也)*"
_PILLS = r"(藥|藥物|藥丸|藥片)"
_MEDICINE = r"(今天的|這次的|我的)?" + _PILLS
_SINCE = r"(已經|剛剛|剛才|剛|有)?"
_ENDS = r"了?(啦|喔|哦|囉|呀|啊|耶)?$"
TOOK = ((re.compile(_LEAD + r"(?P<claim>我" + _ALREADY + r"吃(完|過|好|掉)?了?" + _MEDICINE + _ENDS + ")"),
         re.compile(r"已經|剛|完|過|好|掉|了")),
        (re.compile(_LEAD + r"(?P<claim>" + _ALREADY + r"吃(完|過|好|掉)?了?" + _MEDICINE + _ENDS + ")"),
         re.compile(r"已經|剛|完|過|好|掉")),
        (re.compile(_LEAD + r"(?P<claim>((我把?)?" + _PILLS + r"我?" + _SINCE + r"吃(了|過了|好了|掉了)|我把" + _PILLS + _SINCE
                    + r"吃完了)(啦|喔|哦|囉|呀|啊|耶)?$)"),
         None))
TOOK_EN = re.compile(r"^((yes|yeah|ok|okay|oh|so|well|and|reachy|richy|rishi) )*i( have|'ve)?( already| just)? "
                     r"(took|taken|had|finished taking)( all)? (my|the) "
                     r"(medicine|medicines|medication|medications|pills?|meds|tablets?)( already| now| just now)?$")
# A claim that names another time is left to GPT-Live (the due check would read today's current dose), and so is one
# its clause asks about
NOT_TOOK = re.compile(r"昨|前天|明天|早上|上午|中午|下午|晚上|凌晨|睡前|點|等一下|待會|是不是|有沒有|是否|yesterday|tomorrow"
                      r"|last night|this morning|this afternoon|this evening|tonight|o'?clock")
# A clause that only asks the one before it back: 「我吃完藥了，對不對？」
TAG = re.compile(r"^(對不對|對吧|對嗎|是不是|是吧|是嗎|沒錯吧|好嗎|right|correct|didn't i|haven't i|isn't it)$")
# A clause after it that makes it a time or a condition, as without the transcript's comma: 「我吃完藥了，再跟你說」,
# 「我吃完藥了，就去睡覺」 (never 再見, 就是, 就這樣)
THEN = re.compile(r"^(再|就|才)(?!見|會|是|這樣)")
# A clause before it that makes it a condition, a belief, a doubt or someone else's words: 「如果，我吃完藥了」,
# 「我以為，我吃完藥了」, 「他說，我吃完藥了」 (never 我跟你說, a filler)
HEDGE = re.compile(r"(如果|要是|假如|萬一|等|等到|以為|不記得|不確定|不知道|忘記|忘了|覺得|好像|可能|應該|也許|大概"
                   r"|(?<!跟你)(?<!告訴你)說)$|(?<![a-z])(if|when|thought|think|guess|maybe|not sure|don't know"
                   r"|don't remember|forgot|said|says)$")
# Words anywhere in the turn that make it no plain claim: an extra dose (「我吃完藥了，好像吃了兩次」: never praised,
# conversation.LIVE_CLAIM_PRAISE, and left to GPT-Live and the safety checks; extra_dose) and running out
# (「我把藥吃完了，要再去拿」)
EXTRA_DOSE = re.compile(r"多吃|又吃|吃多了|吃太多|過量|重複吃|[兩二三四幾]次|[兩雙]倍"
                        r"|(?<![a-z])(twice|two times|extra|too much|too many|double|overdose)(?![a-z])")
RAN_OUT = re.compile(r"再去?拿|拿藥|領藥|買藥|沒藥了|沒有藥了|藥沒了|吃光|藥用完|(?<![a-z])(ran out|run out|refill)(?![a-z])")
_CLAUSE_END = re.compile(r"([，,。.!！?？；;：:、…~～\n]+)")
_ASKED = re.compile(r"[?？]")


def _prepare(text: str) -> tuple[str, str]:
    """(compact, spaced): the words in Traditional Chinese, lower case; compact without spaces or punctuation for
    the Chinese patterns, spaced (punctuation as spaces) for the English ones."""
    converted = _traditional(text).lower().replace("’", "'")
    compact = re.sub(r"\s+", "", _PUNCTUATION.sub("", converted.replace("'", "")))
    spaced = re.sub(r"\s+", " ", re.sub(r"[^\w\s']+", " ", converted)).strip()
    return compact, spaced


def extra_dose(text: str) -> bool:
    """Whether the patient's words say they took it twice, took extra or took too much (EXTRA_DOSE)."""
    compact, spaced = _prepare(text)
    return bool(EXTRA_DOSE.search(compact) or EXTRA_DOSE.search(spaced))


def claims_intake(text: str) -> bool:
    """Whether the patient says they took their medicine (took_medicine): a clause that fits a TOOK form or TOOK_EN,
    names no other time (NOT_TOOK), is not asked (a question mark), follows no clause that makes it a condition or a
    doubt (HEDGE), and is neither asked back nor made a plan by the next one (TAG, THEN); never in a turn that speaks
    of an extra dose or of running out (EXTRA_DOSE, RAN_OUT)."""
    if extra_dose(text) or any(RAN_OUT.search(words) for words in _prepare(text)):
        return False
    parts = _CLAUSE_END.split(text)
    clauses = [(_prepare(parts[i]), bool(_ASKED.search("".join(parts[i + 1:i + 2])))) for i in range(0, len(parts), 2)]
    for index, ((compact, spaced), asked) in enumerate(clauses):
        if asked or not compact or NOT_TOOK.search(compact) or NOT_TOOK.search(spaced):
            continue
        before = clauses[index - 1][0] if index else ("", "")
        after = clauses[index + 1][0] if index + 1 < len(clauses) else ("", "")
        if (any(HEDGE.search(words) for words in before) or any(TAG.search(words) for words in after)
                or THEN.search(after[0])):
            continue
        if TOOK_EN.search(spaced):
            return True
        for form, done in TOOK:
            match = form.search(compact)
            if match and (done is None or done.search(match.group("claim"))):
                return True
    return False


def detect(text: str, dancing: bool = False, danced: bool = False) -> str | None:
    """The tool request in the patient's words: "stop_dance", "took_medicine", "dance", "look", "time", "search", or
    None. `dancing`: a dance runs now, so a bare "stop" (停, 夠了, stop) is a stop too; `danced`: this chat had a
    dance, so 「再跳一次」 is another."""
    if not text or not text.strip():
        return None
    compact, spaced = _prepare(text)
    if dancing and (STOP_WORDS[0].search(compact) or STOP_WORDS[1].search(spaced)):
        return "stop_dance"
    for intent in INTENTS:
        if intent == "took_medicine":
            if claims_intake(text):
                return intent
            continue
        if intent == "search" and (NOT_SEARCH.search(compact) or NOT_SEARCH.search(spaced)):
            continue   # 「幫我查一下我今天要吃什麼藥」 is the medicine tool's, 「查一下天氣」 the weather's
        if any(pattern.search(compact) or pattern.search(spaced) for pattern in PATTERNS[intent]):
            return intent
    if danced and AGAIN.search(compact):
        return "dance"
    return None
