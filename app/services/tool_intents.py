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
- `stops(text, dancing)`: 「不要跳舞了」 or "stop dancing" at any time, and while a dance may still run a stop phrase
  anywhere in a clause of the words (「不要再跳了」, 「日希，停下來」, 「停，不要跳了」, "Reachy, stop"; agent tool fixes,
  Task 5), never "don't stop" or another kind of stop (「不要停」, 「不停」, 「停車」, 「停電」). Each clause is read by
  itself, so a "don't" never reaches across a comma (「不要，停下來」 is a stop).
- "took_medicine" is the patient's own word that the medicine is taken (「我吃完藥了」, "I took my medicine"): GPT-Live
  praised it itself and it often never reached record_medication_taken (agent tool fixes, Task 4, Oct 2026). It is
  read clause by clause and must start and end its clause, never for another person, another time, a plan, a
  condition, a belief or a doubt, someone else's words, a negation, a question, food, running out, an extra dose or
  words that take it back, whichever clause of the turn holds them (took_medicine below).
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
# While a dance may still run (the relay asks the tool registry and its own waiting request), a stop phrase anywhere in
# the patient's words is a stop (agent tool fixes, Task 5: 「不要再跳了」, 「日希，停下來」 and "Reachy, stop" were missed):
# 停 in any form (「停」, 「停下來」, 「停止」, 「好了，停」, 「你可以停下來嗎」, 「暫停」), 「不要再跳了」, 「別跳了」, 「不跳了」,
# "stop" ("please stop", "Reachy, stop"); 「好了」, 「夠了」 and "enough" only as a whole clause (「日希，夠了」; 「我吃好了」
# is no stop). Each clause is read by itself (stops), so 「停，不要跳了」 and 「不要，停下來」 are stops
STOP_WORDS = (re.compile(r"停|(不要|別|不用|不)再?跳了|^((好了|夠了)(吧|啦)?)+$"),
              re.compile(r"(?<![a-z])stop(?![a-z])|^(ok |okay )?(enough|that's enough)( now| please)?$"))
# ... but never "don't stop" or another kind of stop, removed from the clause before any stop pattern reads it (so
# 「不要停止跳舞」 and "don't stop dancing" are no stop either): 「不要停」, 「別停」, 「不停」, 「跳個不停」, 「停不下來」,
# 「停不了」, 「停車」, 「停電」, 「停藥」, "non-stop", "can't stop laughing", "bus stop" (「停不要跳了」 is a stop: only
# 「停不下」, 「停不了」, 「停不住」 and 「停不得」 say "can't stop")
NOT_STOP = (re.compile(r"(不要|不用|別|沒有?|不)停|停(不[下了住得]|車|電|水|留|課|班|工|業|靠|經|藥|用|頓)"),
            re.compile(r"(?<![a-z])(don't|dont|do not|never|not|can't|cannot|couldn't|won't|wouldn't|didn't|non) stop"
                       r"(?![a-z])|(?<![a-z])stop (by|over)(?![a-z])|(?<![a-z])bus stop(?![a-z])"))
_PUNCTUATION = re.compile(r"(?:[^\w\s]|_)+")
# The patient's own intake, done (took_medicine), as one whole clause (compact, Traditional): 我 with at most an
# "already" word before 吃 (我吃完藥了, 我已經吃藥了, 我吃了藥); no subject, with 完/過/好/掉 or 已經/剛 (吃完藥了,
# 已經吃藥了; never 「吃藥了」, "medicine time!" said to someone); or the medicine first (藥吃了, 我把藥吃完了; never
# 「把藥吃了」, an order, and 吃完了 only after 我把: 「藥吃完了」 and 「我的藥吃完了」 say it ran out). Each form with what
# makes it done (on the claim's own words, `claim`: 「我也吃藥」 is a habit and 「我有吃藥」 can be "I'm on medication";
# None: the form says it). The claim starts its clause, after at most a call name or a filler (_LEAD: 「日希我吃完藥了」),
# so a condition, a plan, a belief, a doubt or someone else's words in front never fit (如果我吃完藥了, 等我吃完藥了,
# 我以為我吃完藥了, 我不確定我吃過藥了, 他說我吃完藥了), nor does another person (他, 我媽媽). It ends its clause, a
# sentence particle at most: 「我吃完藥了再跟你說」 and 「我吃完藥就去睡覺」 are plans, 「我吃藥了嗎」 a question. The same words
# split by the transcript's commas are read with the clauses around the claim (claims_intake).
_LEAD =r"^(日希|瑞希|瑞奇|reachy|richy|rishi|嗯|對|好|喔|哦|欸|我跟你說|跟你說|你知道)*"
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
                     r"(took|taken|had|finished taking) (my|the) "
                     r"(medicine|medicines|medication|medications|pills?|meds|tablets?)( already| now| just now)?$")
# A claim that names another time is left to GPT-Live (the due check would read today's current dose), and so is one
# its clause asks about. Read on the claim's clause and on every clause before it: 「早上，我吃過藥了」,
# "Yesterday, I took my medicine", 「是不是，我吃完藥了」
NOT_TOOK = re.compile(r"昨|前天|明天|早上|上午|中午|下午|晚上|凌晨|睡前|點|等一下|待會|是不是|有沒有|是否|yesterday|tomorrow"
                      r"|last night|this morning|this afternoon|this evening|tonight|o'?clock")
# A clause after it that puts the intake at another day or part of the day: 「我吃過藥了，早上吃的」, "I took my medicine,
# last night" (not 有點 or 等一下: 「我吃完藥了，就是有點累」 stays a claim)
EARLIER = re.compile(r"昨|前天|早上|上午|中午|下午|晚上|凌晨"
                     r"|(?<![a-z])(yesterday|last night|this morning|this afternoon|this evening)(?![a-z])")
# A clause that only asks the one before it back: 「我吃完藥了，對不對？」
TAG = re.compile(r"^(對不對|對吧|對嗎|是不是|是吧|是嗎|沒錯吧|好嗎|right|correct|didn't i|haven't i|isn't it)$")
# A clause after it that makes it a time or a condition, as without the transcript's comma: 「我吃完藥了，再跟你說」,
# 「我吃完藥了，就去睡覺」 (never 再見, 就是, 就這樣)
THEN = re.compile(r"^(再|就|才)(?!見|會|是|這樣)")
# A word anywhere in a clause before it that makes it a condition, a belief, a doubt or someone else's words:
# 「如果，我吃完藥了」, 「如果明天，我吃完藥了」, 「我以為昨天，我吃完藥了」, 「他說，我吃完藥了」 (never 我跟你說, a filler)
HEDGE = re.compile(r"如果|要是|假如|萬一|等|以為|不記得|不確定|不知道|忘記|忘了|覺得|好像|可能|應該|也許|大概"
                   r"|(?<!跟你)(?<!告訴你)說|(?<![a-z])(if|when|thought|think|guess|maybe|not sure|don't know"
                   r"|don't remember|forgot|said|says)(?![a-z])")
# A clause before it that names someone other than the patient: 「我先生啊，已經吃過藥了」, 「她呢，吃完藥了」
OTHER = re.compile(r"他|她|媽|爸|先生|老公|老婆|太太|丈夫|妻子|兒子|女兒|孫|爺爺|奶奶|阿公|阿嬤|外公|外婆|哥哥|弟弟|姊姊|姐姐"
                   r"|妹妹|叔叔|伯伯|阿伯|姑姑|阿姨|舅舅|朋友|鄰居|同學|小孩|孩子|家人|別人|大家"
                   r"|(?<![a-z])(he|she|they|his|her|their|wife|husband|mom|mum|mother|dad|father|son|daughter|friend"
                   r"|sister|brother|grandma|grandpa)(?![a-z])")
# A claim without a subject of its own (「已經吃過藥了」, 「藥吃了」) follows only a call name or a filler: after a name
# or anything else, the words may be about someone else (「小明，吃完藥了」)
FILLER = re.compile(_LEAD + r"(啊|呀|啦|喔|哦|嘛|耶|欸|呢)*$")
# A clause after it that takes it back: 「我吃完藥了，不對，我還沒吃」, 「…，騙你的」, "…, no wait, I didn't" (not food:
# 「我吃完藥了，還沒吃早餐」 stays a claim)
TAKEN_BACK = re.compile(r"不對|不是|沒有?吃(?!飯|東西|早餐|午餐|晚餐|點心)|騙你|開玩笑|說錯|記錯|搞錯"
                        r"|(?<![a-z])(no wait|wait no|didn't|did not|haven't|have not|actually no|not yet|just kidding"
                        r"|i'm kidding|i lied)(?![a-z])")
# Words anywhere in the turn that make it no plain claim: an extra dose (「我吃完藥了，好像吃了兩次」: never praised,
# conversation.LIVE_CLAIM_PRAISE, and left to GPT-Live and the safety checks; extra_dose) and running out
# (「我把藥吃完了，要再去拿」). An extra dose is also a whole box or bottle (整盒, not 一整天), everything (全部都吃了,
# "all my pills", read as no claim on purpose), a count above one (吃了五顆) and a later dose taken too (連明天的也吃了,
# 晚上的也一起吃了)
EXTRA_DOSE = re.compile(r"多吃|又吃|吃多了|吃太多|過量|重複吃|[兩二三四幾]次|[兩雙]倍"
                        r"|整(盒|瓶|包|排|罐|袋|條)|全部都?(吃|吞)|通通都?吃"
                        r"|([兩二三四五六七八九十幾][一二三四五六七八九十]*|[2-9]|[1-9]\d+)(顆|粒|包|片|錠)"
                        r"|(盒|瓶|包|排|顆|粒|片)(的藥)?都吃|連.{0,8}?(也|都)(一起)?吃(了|掉|下去)"
                        r"|(明天|晚上|下午|中午|早上|下一次|下次|下一餐)的.{0,6}?(也|一起|一併)都?吃"
                        r"|(?<![a-z])(twice|two times|extra|too much|too many|double|overdose"
                        r"|whole (bottle|box|pack|packet|strip)|all of them"
                        r"|all (of )?(my|the) (pills?|tablets?|capsules?|meds|medicines?|medications?)"
                        r"|(two|three|four|five|six|seven|eight|nine|ten|[2-9]|[1-9]\d+) (pills|tablets|capsules)"
                        r"|(tomorrow|tonight)'s)(?![a-z])")
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


def takes_back(text: str) -> bool:
    """Whether the patient's words take a claim back (TAKEN_BACK: 「不對，我還沒吃」, 「騙你的」, "no wait, I didn't")."""
    return any(TAKEN_BACK.search(words) for words in _prepare(text))


def _found(patterns: tuple, clauses: list) -> bool:
    """Whether one of `patterns` is in one of `clauses` ((compact, spaced) pairs)."""
    return any(pattern.search(words) for clause in clauses for words in clause for pattern in patterns)


def claims_intake(text: str) -> bool:
    """Whether the patient says they took their medicine (took_medicine): a clause that fits a TOOK form or TOOK_EN
    and is not asked (a question mark), read with every other clause of the turn, since the transcript's commas
    come and go. No clause before it names another time or asks (NOT_TOOK), makes it a condition, a belief, a doubt or
    someone else's words (HEDGE) or names another person (OTHER), and one without a subject of its own follows only
    a call name or a filler (FILLER). No clause after it puts it at another time (EARLIER) or takes it back
    (TAKEN_BACK), and the next one neither asks it back nor makes it a plan (TAG, THEN). Never in a turn that speaks
    of an extra dose or of running out (EXTRA_DOSE, RAN_OUT)."""
    if extra_dose(text) or any(RAN_OUT.search(words) for words in _prepare(text)):
        return False
    parts = _CLAUSE_END.split(text)
    clauses = [(_prepare(parts[i]), bool(_ASKED.search("".join(parts[i + 1:i + 2])))) for i in range(0, len(parts), 2)]
    for index, ((compact, spaced), asked) in enumerate(clauses):
        if asked or not compact or NOT_TOOK.search(compact) or NOT_TOOK.search(spaced):
            continue
        before = [words for words, _ in clauses[:index]]
        later = [words for words, _ in clauses[index + 1:]]
        after = later[0] if later else ("", "")
        if (_found((NOT_TOOK, HEDGE, OTHER), before) or _found((EARLIER, TAKEN_BACK), later)
                or any(TAG.search(words) for words in after) or THEN.search(after[0])):
            continue
        if TOOK_EN.search(spaced):
            return True
        plain = all(FILLER.search(words[0]) for words in before)
        for form, done in TOOK:
            match = form.search(compact)
            if (match and (done is None or done.search(match.group("claim")))
                    and ("我" in match.group("claim") or plain)):
                return True
    return False


def stops(text: str, dancing: bool = False) -> bool:
    """Whether the patient asks the dance to stop: 「不要跳舞了」 or "stop dancing" at any time (PATTERNS), and while a
    dance may still run (`dancing`) a stop phrase anywhere in a clause (STOP_WORDS), never "don't stop" or another
    kind of stop (NOT_STOP). Each clause (between two punctuation marks, _CLAUSE_END) is read by itself, so a
    "don't" never reaches across a comma: 「不要，停下來」 is a stop, 「不要停，繼續跳」 is not."""
    if not text or not text.strip():
        return False
    for clause in _CLAUSE_END.split(text)[::2]:
        compact, spaced = _prepare(clause)
        for pattern in NOT_STOP:
            compact, spaced = pattern.sub(" ", compact), pattern.sub(" ", spaced)
        if any(pattern.search(compact) or pattern.search(spaced) for pattern in PATTERNS["stop_dance"]):
            return True
        if dancing and (STOP_WORDS[0].search(compact) or STOP_WORDS[1].search(spaced)):
            return True
    return False


def detect(text: str, dancing: bool = False, danced: bool = False, claims: bool = True) -> str | None:
    """The tool request in the patient's words: "stop_dance", "took_medicine", "dance", "look", "time", "search", or
    None. `dancing`: a dance may still run, so a stop phrase anywhere in the words (停, 不要再跳了, stop) is a stop
    too (stops); `danced`: this chat had a dance, so 「再跳一次」 is another; `claims` False leaves the patient's word
    that the medicine is taken to the caller (the relay reads it with claims_intake, beside any other request of the
    same words)."""
    if not text or not text.strip():
        return None
    compact, spaced = _prepare(text)
    for intent in INTENTS:
        if intent == "stop_dance":
            if stops(text, dancing):
                return intent
            continue
        if intent == "took_medicine":
            if claims and claims_intake(text):
                return intent
            continue
        if intent == "search" and (NOT_SEARCH.search(compact) or NOT_SEARCH.search(spaced)):
            continue   # 「幫我查一下我今天要吃什麼藥」 is the medicine tool's, 「查一下天氣」 the weather's
        if any(pattern.search(compact) or pattern.search(spaced) for pattern in PATTERNS[intent]):
            return intent
    if danced and AGAIN.search(compact):
        return "dance"
    return None
