"""Reachy check-in conversations: replies from an LLM through OpenRouter, safety screening, summaries.

Speech is turned into text on the robot; only text reaches this module (robot notice §2-3). Self-harm and overdose
are looked for in three layers, because each one alone has missed real risk:

1. screen(), a keyword list, runs on every patient turn *before* any LLM call. A matched turn never reaches any
   model, and a flagged conversation gets no post-chat model call.
2. classify_risk() asks the model about every turn the list did not flag (the "safety screening" of notice §3),
   while the reply is being written. It catches what the list misses: speech-to-text writes sound-alike characters
   and either script. When it fails or runs out of time, the device API asks again in the background with longer.
3. The end-of-chat summary has a RISK line: the backstop for a conversation neither layer flagged. It is written by
   after_chat: SUMMARY_PROMPT, or AFTER_CHAT_PROMPT (memory.py) when memory is on; missed ones are retried by the
   after-chat sweep.

Layers 2 and 3 depend on OpenRouter, so during an outage the keyword list is the only check that runs; it has to
catch explicit statements on its own. A risk from layer 1 or 2 gets a fixed help-line reply and ends the
conversation. Every layer alerts all verified family contacts through alert_family(), at most once per
conversation (notice §6).

If OpenRouter is unconfigured, rate-limited or returns nothing, the robot still answers with a fixed line, so a
demo never stalls. A reply tries config.LLM_MODEL, then config.LLM_FALLBACK_MODEL, inside one deadline
(config.LLM_DEADLINE_SECONDS), and reports how long each attempt took so slow turns can be traced.

The reply prompt also carries a few lines about the day (date, time, weather, holidays; context_info), built from
data already in the process, so Reachy can chat about it. The risk check and the summary never get them.

With memory consent, the memory block (memory.render_block, facts only) goes to the reply alone, as a second system
message: never to the risk check or the summary.
"""

import asyncio
import logging
import re
import threading
import time

import requests

from app import config
from app.services import context_info, outbox

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_HTTP_LOCAL = threading.local()
HTTP_IDLE_SECONDS = 60.0   # a worker's keep-alive session idle longer than this is replaced (see _http_session)
MAX_PATIENT_TURNS = 6
HISTORY_TURNS = 12
MAX_TEXT = 500
CALL_TIMEOUT = 6           # one model call; the robot is waiting in silence and a fixed line beats a long pause
MIN_CALL_SECONDS = 0.5     # less than this left before the deadline: not worth starting another call
REPLY_MAX_TOKENS = 200
SUMMARY_MAX_TOKENS = 400
SUMMARY_DEADLINE_SECONDS = 25
SUMMARY_CALL_TIMEOUT = 12  # summaries are written in the background; nobody waits, so each model gets longer
# The risk check runs alongside the reply, so a turn waits for the slower of the two: keep it short.
RISK_MAX_TOKENS = 32       # a one-word label, with room for a model that adds a few words after it
RISK_HISTORY_TURNS = 6
RISK_DEADLINE_SECONDS = 4
RISK_CALL_TIMEOUT = 2.5    # leaves the fallback model about 1.5 s when the primary hangs
LATE_RISK_DEADLINE_SECONDS = 30   # the background re-check after a failed one: nobody waits
LATE_RISK_CALL_TIMEOUT = 12
LATE_RISK_RATE_LIMIT_WAIT = 20    # after a 429 the re-check waits first: asking again at once meets the same limit
MOODS = ("happy", "calm", "sad", "worried", "angry", "unknown")
CHECKIN_SCOPES = ("robot_microphone", "cloud_voice", "conversation_analysis", "safety_alerts")

OPENING = {"zh-TW": "今天感覺怎麼樣？想跟我聊聊嗎？",
           "en": "How are you feeling today? Would you like to chat?"}
CLOSING = {"zh-TW": "謝謝您陪我聊天，祝您有美好的一天！",
           "en": "Thank you for chatting with me. Have a lovely day!"}
FALLBACK = {"zh-TW": "我在這裡陪著您。您可以再多說一點嗎？",
            "en": "I'm here with you. Could you tell me a little more?"}
HELPLINE = {"zh-TW": "聽起來您現在很不好受。如果有立即的危險，請馬上打 119。"
                     "也可以打 1925 安心專線，24 小時免費。我會請家人來關心您。",
            "en": "It sounds like you're going through something hard. If you are in danger, please call 119 now. "
                  "You can also call 1925, free, 24 hours. I'm letting your family know so they can check on you."}

GOODBYES = ("再見", "拜拜", "掰掰", "不聊了", "不用了", "先這樣", "再见", "不聊", "bye", "goodbye", "that's all")
# Written in Traditional Chinese: screen() converts what it checks to Traditional first, because the robot's
# speech-to-text writes Simplified ("自残") and a list in one script silently misses the other (2 Oct: "我想自残"
# raised no alert). English entries match whole words.
# Every entry must be a clear statement on its own, because a match alerts family and ends the chat. The check-in
# starts right after a dose, so "我把早上的藥全部吃了" or "我一次吃了五顆" (Taiwan's dose packets hold several pills)
# is everyday news; classify_risk judges whatever needs context.
RISK_WORDS = {
    "self_harm": ("想死", "想去死", "想要死", "要去死", "恨不得死", "恨不得去死", "活不下去", "活夠了", "死了算了",
                  "死掉算了", "不如死", "不如去死", "讓我死", "自殺", "輕生", "尋死", "尋短", "自殘", "自傷",
                  "傷害自己", "弄傷自己", "割自己", "割腕", "結束生命", "結束自己", "了結自己", "了結生命", "自我了斷",
                  "跳樓", "跳河", "跳海", "上吊", "吊死", "燒炭", "燒碳", "想消失",
                  "kill myself", "killing myself", "suicide", "suicidal", "end my life", "end it all", "want to die",
                  "wanna die", "hurt myself", "harm myself", "self harm", "cut myself", "don't want to live",
                  "don't want to be alive", "better off dead", "not worth living"),
    "overdose": ("吞了很多", "overdose", "too many pills", "too many sleeping pills", "too much medicine",
                 "whole bottle of pills", "all my sleeping pills", "double dose", "double my dose"),
}
# Speech-to-text writes sound-alikes: "我不想活了" (don't want to live) came out as "我不想火了" / "我不要货了".
# Every character read "huo" counts as 活 in "don't want to ... anymore / go on".
_HUO = "活火货貨或获獲伙夥和"
# What a plain word cannot say: a guard after it, a sound-alike, or the verb it needs (喝農藥, not 噴農藥).
RISK_PATTERNS = {
    "self_harm": (
        r"(不想|不要|不願)再?活(?![動潑絡]|在過去)",               # 不想活, 不想再活下去; not 不想活動
        rf"(不想|不要|不願|不愿)[{_HUO}](了|下去)|[{_HUO}]不下去",   # the sound-alikes above
        r"自[殘慚慘蠶](?!形穢)",                                   # 自殘 heard as 自慚/自慘/自蠶; not 自慚形穢
        r"活著還?(沒有?|有什麼)(意思|意義)",
        r"死[了掉]?還?(比較|最)好",                                 # 死了比較好; not 他死了好幾年
        r"(想|我要|讓我)要?去?安樂死",                             # not 狗要安樂死
        r"喝了?一?瓶?(農藥|巴拉刈|除草劑)|[吃吞]了?老鼠藥",           # not 噴農藥, 怕吃到農藥
        # never waking up again (a passive death wish); plain 早上好睏，不想醒來了 is sleepiness, left to the model
        r"(永遠|最好)(不想|不要|不願|別|不)再?醒來|(不想|不要|不願|別|不)再醒來|(不想|不要|不願|別|不)醒來(最好|比較好)",
    ),
    "overdose": (
        r"[吃吞服]了?太多[顆粒]?的?(安眠)?藥|藥[吃吞]得?太多",          # 吃太多藥, 藥吃太多了
        r"藥物?過量|過量的?(安眠)?藥|(吃|吞|服用?)了?過量",             # not 喝酒過量, 運動過量
        r"(?<!不)多[吃吞]了?(一|兩|二|三|幾|好幾)?[顆粒]?的?(安眠)?藥",  # 多吃了幾顆藥; not 差不多吃了
        r"[吃吞]了?一?整瓶|[吃吞]了?一?整[盒排]的?(安眠)?藥|整[瓶盒排]的?(安眠)?藥(都|全部?)?[吃吞]",  # not 這整瓶藥還沒開
        r"[吃吞]了?一大?把的?(安眠)?藥|[吃吞服]了?[兩雙]倍的?(藥|劑量|藥量)",
        r"[吃吞]了?(好多|好幾|很多|一堆)[顆粒片]?的?安眠藥|把(所有|全部)?的?安眠藥(全部|都|一次|一起)[吃吞]",
        r"把\w{0,6}藥(全部|都)?一次[吃吞](完|光)",                      # 把藥一次吃完; not 早上的藥全部吃了
    ),
}

SYSTEM_PROMPT = {
    "zh-TW": (
        "你是 Reachy，一個陪伴長者的小機器人。剛才長者吃完藥，你正在和他聊天關心他。"
        "請一律用繁體中文回答，每次只說一到兩個短句，總共不超過 30 個字，溫暖、口語化，適合大聲唸出來。"
        "不要使用表情符號、列表或括號。可以問一個簡單的問題讓對方多說一點。"
        "不要給醫療建議，不要評論藥物或劑量；如果對方問醫療問題，請建議他問醫師或藥師。"
    ),
    "en": (
        "You are Reachy, a small companion robot for an older adult who has just taken their medicine. "
        "Reply in English in one or two short, warm, spoken sentences (under 25 words), suitable for reading aloud. "
        "No emoji, lists or brackets. You may ask one simple question. "
        "Never give medical advice or comment on medicines or doses; suggest asking a doctor or pharmacist."
    ),
}
# Examples for conversational tone. Weather details in the example are illustrative: the live answer must follow
# context_info's current forecast above and must never reuse the example's drizzle when today's data differs.
CONVERSATION_EXAMPLES = {
    "zh-TW": (
        "天氣打亂對方期待的計畫時，先接住失望，再溫和陪伴或提供可選的替代方案，不要保證雨會停。\n"
        "示例（只有今日背景資料確實顯示毛毛雨時，才照此描述天氣）：\n"
        "Reachy：今天感覺怎麼樣？想跟我聊聊嗎？\n"
        "長者：今天天氣怎麼樣？\n"
        "Reachy：外面有毛毛雨，溫度剛好。你出門會帶傘嗎？\n"
        "長者：為什麼今天要下雨？我想出去玩。\n"
        "Reachy：本來想出去玩，碰上下雨真的有點掃興。我可以陪你想想，有沒有能避雨又好玩的地方。"
    ),
    "en": (
        "When the weather disrupts a plan the patient was looking forward to, acknowledge the disappointment, then "
        "offer gentle company or an optional alternative. Do not promise the rain will stop.\n"
        "Patient: Why does it have to rain today? I wanted to go out and have fun.\n"
        "Reachy: It is disappointing when rain gets in the way of something you were looking forward to. "
        "Would you like me to help think of something fun you could do indoors?"
    ),
}
# Introduces the background lines (context_info.background) after SYSTEM_PROMPT, in the reply prompt only.
# The model can't look anything up: without that line, with no weather given it offered to check. "The rules above"
# (SYSTEM_PROMPT's no medical advice) without naming medicine again: repeated right before the data, it sent the
# patient to a doctor or pharmacist about the weather (7 of 18 English replies, 3 Oct 2026). context_info.NO_WEATHER
# gives it somewhere else to point.
BACKGROUND_RULES = {
    "zh-TW": ("以下是今天的背景資料。只在對方問到或話題自然相關時才用，不要主動唸出來。"
              "資料裡沒有的天氣不要猜，就說你不確定。你不能上網或查資料，不要說要幫忙查。上面的規則照樣適用。"),
    "en": ("Background for today follows. Use it only when the patient asks or it fits the conversation; never "
           "recite it unprompted. Never guess weather that is not given below: say you are not sure. You cannot "
           "look anything up, so never offer to check. The rules above still apply."),
}
SUMMARY_PROMPT = {
    "zh-TW": ("以下是陪伴機器人和長者的對話。請用一句繁體中文總結長者談到的主題和心情（不要引用原話），"
              "再判斷整體心情，並判斷安全風險：長者若表達任何想傷害自己、自殺或不想活的念頭，RISK 填 self_harm；"
              "若說吃了過量的藥，填 overdose；否則填 none。長者的話來自語音辨識，可能有同音錯字或簡體字。"
              "若長者因下雨打亂出遊計畫而明確失望或難過，整體心情可判為 sad；不要只因為談到下雨就判 sad。"
              "只輸出三行：\nMOOD: happy|calm|sad|worried|angry|unknown\nSUMMARY: <一句話>\n"
              "RISK: none|self_harm|overdose"),
    "en": ("Below is a conversation between a companion robot and an older adult. Summarise the topics and mood in "
           "one sentence (no direct quotes), then classify the overall mood and the safety risk: self_harm if the "
           "older adult expressed any wish, thought or plan to hurt or kill themselves, or not wanting to live; "
           "overdose if they said they took too much medicine; otherwise none. Their words come from "
           "speech-to-text and may contain wrong sound-alike words. Output exactly three lines:\n"
           "MOOD: happy|calm|sad|worried|angry|unknown\nSUMMARY: <one sentence>\nRISK: none|self_harm|overdose"),
}
# One prompt for both languages: the answer is a label, and the patient's words may come in either script anyway.
RISK_PROMPT = (
    "You check a conversation between Reachy, a companion robot, and an older adult in Taiwan for safety risks. "
    "The patient's words come from speech-to-text: they may contain wrong sound-alike characters (自殘 may come "
    "out as 自慘 or 自蠶, 不想活了 as 不想火了) and may be written in Simplified or Traditional Chinese, or in "
    "English. Judge the latest patient turn, using the recent conversation as context. "
    "Answer with exactly one label and nothing else:\n"
    "SELF_HARM: any wish, thought or plan to hurt or kill oneself, or not wanting to live\n"
    "OVERDOSE: took too much medicine, on purpose or by accident\n"
    "NONE: neither. Everyday sayings such as 想死你了 (I miss you so much) or 累死了 (exhausted) are NONE."
)


def language_of(value: str | None) -> str:
    return value if value in SYSTEM_PROMPT else "zh-TW"


# Everyday phrases that contain a risk word but mean something else, removed before the words are matched. Each
# must fit between two punctuation marks (screen()), so "我想死，你不要管我" keeps its 想死.
NOT_RISK = re.compile(
    # 想死你了 / 想死我孫子了: "I miss you so much", only when the sentence ends there
    r"想死了?(我的?)?([你妳您他她我]|孫子|孫女|兒子|女兒|老伴|家人)們?(?=[了啦囉喔哦啊呀]|$)"
    r"|[你妳您]們?是?想死[啊嗎喔哦呀呢]"                 # 騎那麼快，你想死啊: a scolding
    r"|(不想|不要|不願|不會|別)要?(讓我)?去?死"           # 我還不想死: the opposite
    r"|想要?死後|死心"                                   # 想死後葬在老家; 死心 (giving up hope)
    r"|想消失一下|跳樓大?(拍賣|甩賣|價|清倉)|吊死鬼")
_PUNCTUATION = re.compile(r"(?:[^\w\s]|_)+")
_LATIN = re.compile(r"[a-z]")


def _traditional(text: str) -> str:
    from zhconv import convert

    return convert(text or "", "zh-tw").lower()


def _english_pattern(word: str) -> re.Pattern:
    # Not \b: Chinese characters count as word characters, and "我想kill myself" must match.
    words = [re.escape(part) for part in re.split(r"[\s\-]+", word.replace("'", ""))]
    return re.compile(r"(?<![a-z])" + r"[\s\-]*".join(words) + r"(?![a-z])")


_ENGLISH = {kind: [_english_pattern(word) for word in words if _LATIN.search(word)]
            for kind, words in RISK_WORDS.items()}
_CHINESE = {kind: [word for word in words if not _LATIN.search(word)] for kind, words in RISK_WORDS.items()}
_PATTERNS = {kind: re.compile("|".join(patterns)) for kind, patterns in RISK_PATTERNS.items()}
_FILLERS = re.compile("[嗯呃]")
# After 0.5.2 the robot app follows its 「嗯」 with a short thinking phrase once the patient's turn is handed over
# (clips/manifest.json variants "thinking": 「我再想一下喔。」「讓我想一想喔。」「我想想看喔。」). The robot removes its echo
# only from the start of what it hears next; an echo inside a sentence the patient said across it ("我不想 我在想 活了")
# would split 不想活. The same forms the robot removes (reachy_app voice.THINKING_FORMS: each phrase, and what is left
# of it with its start or end lost, at least 3 characters, with or without 喔), in Traditional, with the sound-alikes
# SenseVoice writes (在 for 再, 哦/噢 for 喔, 叫 for 讓, 享/響 for 想).
_THINKING_PHRASES = ("我再想一下喔", "讓我想一想喔", "我想想看喔")
_THINKING_SOUND_ALIKES = {"再": "[再在]", "喔": "[喔哦噢]", "讓": "[讓让叫]", "想": "[想享響响]"}


def _thinking_forms(phrase: str) -> set[str]:
    forms = ({phrase[:end] for end in range(3, len(phrase) + 1)}
             | {phrase[start:] for start in range(len(phrase) - 2)})
    return {form for form in forms | {form.rstrip("喔") for form in forms} if len(form) >= 3}


_THINKING = re.compile("|".join(
    "".join(_THINKING_SOUND_ALIKES.get(ch, re.escape(ch)) for ch in form)
    for form in sorted(set().union(*map(_thinking_forms, _THINKING_PHRASES)), key=len, reverse=True)))


def screen(text: str) -> str | None:
    """'self_harm' / 'overdose' when the words suggest serious risk, else None.

    Checked in Traditional Chinese with spaces and punctuation removed ("自 残。" -> "自殘"), so either script, the
    robot's spacing and a pause written as a comma ("我不想，活了") still match. NOT_RISK phrases are removed first,
    each within the words between two punctuation marks, so they never swallow a risk word across a comma.
    English words are matched whole, in lower case, without apostrophes. Fillers (嗯, 呃) are removed too: robot app
    0.5.2 says 「嗯」 when the patient pauses, and its echo can land inside a sentence ("我不想嗯活了"). Later versions
    follow it with a thinking phrase, so the words are checked a second time without those phrases (_THINKING);
    either check finding a risk counts.
    """
    converted = _FILLERS.sub("", _traditional(text))
    return _screen(converted) or _screen(_THINKING.sub("", converted))


def _screen(converted: str) -> str | None:
    compact = "".join(NOT_RISK.sub("", re.sub(r"\s+", "", part)) for part in _PUNCTUATION.split(converted))
    spaced = re.sub(r"\s+", " ", converted.replace("'", "").replace("’", ""))
    for kind in RISK_WORDS:
        if (any(word in compact for word in _CHINESE[kind]) or _PATTERNS[kind].search(compact)
                or any(p.search(spaced) for p in _ENGLISH[kind])):
            return kind
    return None


def wants_to_end(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in GOODBYES)


_MARKUP = re.compile(r"[*_#`>\[\]()（）【】]|[\U0001F000-\U0001FAFF☀-➿️]")


def clean_reply(text: str) -> str:
    text = _MARKUP.sub("", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:240]


def speech_text(text: str, language: str) -> str:
    """What the robot's voice reads: the Matcha zh-baker voice knows Simplified characters only."""
    text = clean_reply(text)
    if language == "zh-TW":
        from zhconv import convert

        text = convert(text, "zh-cn")
    return text


def _provider() -> dict | None:
    """OpenRouter provider routing from OPENROUTER_PROVIDER_ONLY / OPENROUTER_DATA_COLLECTION, or None when unset.
    It applies to every call (reply, risk check, summary, memory) and to both models."""
    only = [slug.strip() for slug in config.OPENROUTER_PROVIDER_ONLY.split(",") if slug.strip()]
    if not only and not config.OPENROUTER_DATA_COLLECTION:
        return None
    routing: dict = {"allow_fallbacks": not only}
    if only:
        routing["only"] = only
    if config.OPENROUTER_DATA_COLLECTION:
        routing["data_collection"] = config.OPENROUTER_DATA_COLLECTION
    return routing


def _http_session(clock=time.monotonic) -> requests.Session:
    """A pooled HTTP session for this worker thread.

    The synchronous requests.post convenience function creates and closes a Session for every call. The reply and
    safety calls run in the executor, so a thread-local Session lets each worker reuse its OpenRouter keep-alive
    connection (saving the ~0.1 s connection setup) without sharing mutable Session state between concurrent
    workers. Reuse happens only when the same executor thread takes the next call, so the gain is small.

    A session idle for more than HTTP_IDLE_SECONDS is replaced: turns of one conversation are seconds apart, but
    check-ins are hours apart, and a connection dropped silently in between (laptop sleep, NAT) would fail the
    first call after it.
    """
    now = clock()
    session = getattr(_HTTP_LOCAL, "session", None)
    if session is not None and now - getattr(_HTTP_LOCAL, "used", now) > HTTP_IDLE_SECONDS:
        session.close()
        session = None
    if session is None:
        session = requests.Session()
        _HTTP_LOCAL.session = session
    _HTTP_LOCAL.used = now
    return session


def _post(messages: list[dict], max_tokens: int, model: str | None = None, timeout: float = CALL_TIMEOUT,
          info: dict | None = None, temperature: float = 0.7) -> str | None:
    """One OpenRouter call. `info`, when given, receives status, model_served, finish_reason and tokens.
    temperature: 0 for extraction (memory facts), 0.7 for everything else."""
    info = {} if info is None else info
    # Reasoning off: free reasoning models otherwise spend every token thinking and send back nothing.
    payload = {"model": model or config.LLM_FALLBACK_MODEL, "messages": messages, "max_tokens": max_tokens,
               "temperature": temperature, "reasoning": {"enabled": False}}
    provider = _provider()
    if provider:
        payload["provider"] = provider
    response = _http_session().post(
        OPENROUTER_URL, timeout=timeout,
        headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}", "Content-Type": "application/json",
                 "X-Title": "MedAiCarePlus Reachy check-in"},
        json=payload)
    info["status"] = response.status_code
    if response.status_code in (429, 500, 502, 503):
        raise RuntimeError(f"HTTP {response.status_code}")
    response.raise_for_status()
    body = response.json()
    choices = body.get("choices") or []
    info.update(model_served=body.get("model"), tokens=(body.get("usage") or {}).get("completion_tokens"),
                finish_reason=choices[0].get("finish_reason") if choices else None)
    if not choices or choices[0].get("finish_reason") == "length":
        return None   # cut off mid-sentence (often a reasoning model that ran out of tokens): never speak half a reply
    content = (choices[0].get("message") or {}).get("content")
    return content.strip() if isinstance(content, str) and content.strip() else None


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


async def _complete(messages: list[dict], max_tokens: int, deadline_seconds: float, call_timeout: float,
                    accept, temperature: float = 0.7) -> tuple[object | None, dict]:
    """The primary model, then the fallback model, never past `deadline_seconds` in total.

    accept(answer) turns an answer into the result, or returns None when it is unusable (the next model is tried).
    Returns (result or None, {"llm_ms", "attempts"}). Each attempt records model_requested, model_served, ms,
    status (the HTTP status, or "timeout" / "error" when none came back), finish_reason, tokens and usable.
    With LLM_MODEL unset both calls go to the fallback model, i.e. it is retried once.
    """
    started = time.monotonic()
    deadline = started + deadline_seconds
    attempts: list[dict] = []
    result = None
    if config.OPENROUTER_API_KEY:
        loop = asyncio.get_running_loop()
        for model in (config.LLM_MODEL or config.LLM_FALLBACK_MODEL, config.LLM_FALLBACK_MODEL):
            left = deadline - time.monotonic()
            if left < MIN_CALL_SECONDS:
                break
            timeout = min(call_timeout, left)
            info: dict = {}
            problem = None
            call_started = time.monotonic()
            try:
                # requests' timeout bounds each socket wait, not the whole call; wait_for holds the deadline.
                answer = await asyncio.wait_for(
                    loop.run_in_executor(None, _post, messages, max_tokens, model, timeout, info, temperature),
                    timeout)
                result = accept(answer)
                status = info.get("status")
            except (asyncio.TimeoutError, requests.Timeout):
                status = "timeout"
            except Exception as exc:
                status, problem = info.get("status") or (429 if "429" in str(exc) else "error"), exc
            if result is None:
                log.warning("OpenRouter %s gave no usable answer (status %s, finish %s): %s", model, status,
                            info.get("finish_reason"), problem or "unusable text")
            attempts.append({"model_requested": model, "model_served": info.get("model_served"),
                             "ms": _ms_since(call_started), "status": status,
                             "finish_reason": info.get("finish_reason"), "tokens": info.get("tokens"),
                             "usable": result is not None})
            if result is not None:
                break
    return result, {"llm_ms": _ms_since(started), "attempts": attempts}


def _failed_call(status) -> bool:
    return status in ("timeout", "error") or (isinstance(status, int) and status >= 400)


def _reason(result, metrics: dict) -> str:
    """Why a model chain (_complete) gave no result: ok (it did), no_key, empty (a model answered, but nothing
    usable), rate_limited (no answer, and a 429 among the failures) or unavailable."""
    if result is not None:
        return "ok"
    if not config.OPENROUTER_API_KEY:
        return "no_key"
    statuses = [attempt["status"] for attempt in metrics["attempts"]]
    if any(not _failed_call(status) for status in statuses):
        return "empty"
    return "rate_limited" if 429 in statuses else "unavailable"


async def complete_with_reason(messages: list[dict], max_tokens: int = 200, temperature: float = 0.7, *,
                               accept=None, deadline: float = SUMMARY_DEADLINE_SECONDS,
                               call_timeout: float = SUMMARY_CALL_TIMEOUT) -> tuple[object | None, str]:
    """A completion through the model chain (primary, then fallback, within `deadline`), and why it gave nothing:
    ok, no_key, rate_limited, unavailable or empty (_reason). accept as in _complete; by default any non-empty
    text. For background work (the defaults are the summary's): nobody waits, so each model gets longer."""
    result, metrics = await _complete(messages, max_tokens, deadline, call_timeout,
                                      accept or (lambda answer: answer or None), temperature)
    return result, _reason(result, metrics)


async def complete(messages: list[dict], max_tokens: int = 200) -> str | None:
    """complete_with_reason without the reason: None when unavailable (no key, rate limit, empty answer)."""
    return (await complete_with_reason(messages, max_tokens))[0]


# openrouter/free sometimes routes to a reasoning model that writes its plan into the answer ("The user wants
# me to ...") and runs out of tokens before the reply. Such text must never be spoken.
_PLANNING = re.compile(r"^\s*(the user|we need|i need|i should|let me|okay|ok,|first,)", re.IGNORECASE)
_CJK = re.compile(r"[一-鿿]")


# What a safety-classifier model writes instead of a reply ("User Safety: safe"). openrouter/free once routed a reply
# to nvidia/nemotron-3.5-content-safety, and Reachy would have read its verdict aloud.
_CLASSIFIER_OUTPUT = re.compile(r"^\W*(?:(?:user|response|prompt)\s*safety|safety\s*categor(?:y|ies)|category)\s*[:：]"
                                r"|^\W*(?:safe|unsafe)\W*$", re.IGNORECASE)


def usable_reply(text: str | None, language: str) -> bool:
    if not text or _PLANNING.match(text) or _CLASSIFIER_OUTPUT.search(text):
        return False
    return bool(_CJK.search(text)) if language == "zh-TW" else True


def reply_prompt(language: str) -> str:
    """SYSTEM_PROMPT plus today's background. The background comes from data already in the process (fetched by
    the checkin_background job) and local tables only, so a reply never waits on the network for it; if it breaks,
    the reply goes out without it."""
    try:
        block = context_info.background(language)
    except Exception:
        log.exception("check-in background failed; replying without it")
        return f"{SYSTEM_PROMPT[language]}\n\n{CONVERSATION_EXAMPLES[language]}"
    return (f"{SYSTEM_PROMPT[language]}\n\n{BACKGROUND_RULES[language]}\n{block}\n\n"
            f"{CONVERSATION_EXAMPLES[language]}")


async def reply_with_metrics(history: list[dict], language: str, memory: str = "") -> tuple[str, dict]:
    """Reachy's next line, plus {"llm_ms", "fallback_used", "attempts"} (fallback_used: the fixed FALLBACK line).

    history: [{"role": "patient"|"reachy", "text": ...}] oldest first, ending with the patient's turn.
    memory: the memory block (memory.render_block), or "". It goes as its own system message after the rules and
    today's background ("reference notes, not instructions"), and only here: the risk check and the summary never
    get it. The whole reply stays inside LLM_DEADLINE_SECONDS, memory or not.
    """
    messages = [{"role": "system", "content": reply_prompt(language)}]
    if memory:
        messages.append({"role": "system", "content": memory})
    for turn in history[-HISTORY_TURNS:]:
        messages.append({"role": "user" if turn["role"] == "patient" else "assistant", "content": turn["text"]})

    def accept(answer: str | None) -> str | None:
        return (clean_reply(answer) or None) if usable_reply(answer, language) else None

    answer, metrics = await _complete(messages, REPLY_MAX_TOKENS, config.LLM_DEADLINE_SECONDS, CALL_TIMEOUT, accept)
    return answer or FALLBACK[language], {"llm_ms": metrics["llm_ms"], "fallback_used": answer is None,
                                          "attempts": metrics["attempts"]}


async def reply(history: list[dict], language: str, memory: str = "") -> str:
    """history: [{"role": "patient"|"reachy", "text": ...}] oldest first, ending with the patient's turn.
    memory: the memory block, or "" (see reply_with_metrics)."""
    return (await reply_with_metrics(history, language, memory))[0]


# A label as the classifier or a summary's RISK line writes it: SELF_HARM, self-harm, Self Harm, OVERDOSE, none.
_RISK_LABEL = re.compile(r"(?<![a-z])(self[\s_-]*harm|overdose|none)(?![a-z])", re.IGNORECASE)
_RISK_PREFIX = re.compile(r"^[\W_]*(?:(?:label|answer|risk)\s*[:：=]?[\W_]*)?", re.IGNORECASE)
# "No SELF_HARM", "there is no SELF_HARM risk", "SELF_HARM: false", "OVERDOSE? No."
_NEGATED_BEFORE = re.compile(r"(?:(?<![a-z])(?:no|not|non|without|isn'?t)|無|沒有|不是|非)[\s_-]*$", re.IGNORECASE)
_NEGATED_AFTER = re.compile(r"^[\W_]*?[:：=?？]\W*(?:no|not|false|none)(?![a-z])", re.IGNORECASE)


def _risk_kind(label: str) -> str | None:
    """'self_harm' / 'overdose' for a matched label, None for NONE."""
    label = re.sub(r"[\s_-]+", "", label.lower())
    return {"selfharm": "self_harm", "overdose": "overdose"}.get(label)


def parse_risk(answer: str | None) -> tuple[bool, str | None]:
    """(understood, kind) for the classifier's answer; kind is 'self_harm', 'overdose' or None.

    An answer that starts with a label means that label (a model may explain after it); otherwise exactly one
    label may appear ("The answer is NONE."). A risk label that is negated ("No SELF_HARM", "SELF_HARM: no") is
    not that risk. Planning text, several labels, only negated ones or none at all are not understood, and not
    understood is never a risk: it is asked again later.
    """
    if not answer or _PLANNING.match(answer):
        return False, None
    labels = []
    for match in _RISK_LABEL.finditer(answer):
        kind = _risk_kind(match.group(1))
        negated = kind is not None and bool(_NEGATED_BEFORE.search(answer[:match.start()])
                                            or _NEGATED_AFTER.match(answer[match.end():]))
        labels.append((kind, negated))
    if not labels:
        return False, None
    rest = _RISK_PREFIX.sub("", answer.strip(), count=1)
    first = _RISK_LABEL.match(rest)
    if first and not (_risk_kind(first.group(1)) and _NEGATED_AFTER.match(rest[first.end():])):
        return True, _risk_kind(first.group(1))
    kinds = {kind for kind, negated in labels if not negated}
    return (True, kinds.pop()) if len(kinds) == 1 else (False, None)


def _risk_messages(history: list[dict], language: str) -> list[dict]:
    recent = "\n".join(f"{'Patient' if turn['role'] == 'patient' else 'Reachy'}: {turn['text']}"
                       for turn in history[-RISK_HISTORY_TURNS:])
    latest = next((turn["text"] for turn in reversed(history) if turn["role"] == "patient"), "")
    spoken = "Chinese" if language == "zh-TW" else "English"
    return [{"role": "system", "content": RISK_PROMPT},
            {"role": "user", "content": f"The conversation is in {spoken}. Recent conversation:\n{recent}\n\n"
                                        f"Latest patient turn: {latest}"}]


async def classify_risk(history: list[dict], language: str, late: bool = False) -> tuple[str | None, dict]:
    """The model's judgement of the latest patient turn: 'self_harm', 'overdose' or None, plus
    {"risk_ms", "risk_result", "risk_attempts"}.

    risk_result is "self_harm", "overdose", "none", or "unknown" when no model gave a label it understood in time
    (no key, rate limits, a timeout, other text); unknown is never a risk, and the caller re-checks it with
    late=True, which gives each model longer because nobody is waiting. Never raises: a broken check must not
    break the conversation.
    history: [{"role": "patient"|"reachy", "text": ...}] oldest first, ending with the patient's turn.
    """
    deadline, call_timeout = ((LATE_RISK_DEADLINE_SECONDS, LATE_RISK_CALL_TIMEOUT) if late
                              else (RISK_DEADLINE_SECONDS, RISK_CALL_TIMEOUT))

    def accept(answer: str | None) -> str | None:
        understood, kind = parse_risk(answer)
        if answer and not understood:
            log.warning("risk check answer not understood: %r", answer[:20])
        return (kind or "none") if understood else None

    try:
        result, metrics = await _complete(_risk_messages(history, language), RISK_MAX_TOKENS, deadline,
                                          call_timeout, accept)
    except Exception:
        log.exception("risk check failed")
        result, metrics = None, {"llm_ms": 0, "attempts": []}
    kind = result if result in RISK_WORDS else None
    return kind, {"risk_ms": metrics["llm_ms"], "risk_result": result or "unknown",
                  "risk_attempts": metrics["attempts"]}


# Models bold, quote or code-format the labels and values, and under the zh-TW prompt write a full-width colon.
_SUMMARY_MARKUP = re.compile(r"[*`\"“”]")
_SUMMARY_FIELD = r"\s*[:：=\-]\s*"
_RISK_CHOICES = r"self[\s_-]*harm|overdose|none"
_SUMMARY_RISK = re.compile(rf"RISK{_SUMMARY_FIELD}({_RISK_CHOICES})(?![a-z])", re.IGNORECASE)
_SUMMARY_MOOD = re.compile(rf"MOOD{_SUMMARY_FIELD}([a-z]+)", re.IGNORECASE)
_SUMMARY_TEXT = re.compile(rf"SUMMARY{_SUMMARY_FIELD}(.+)", re.IGNORECASE)
# The template echoed back after a value ("RISK: none|self_harm|overdose", "MOOD: happy|calm|..."): no judgement.
_RISK_ECHO = re.compile(rf"[ \t]*\|[ \t]*(?:{_RISK_CHOICES})(?![a-z])", re.IGNORECASE)
_MOOD_ECHO = re.compile(rf"[ \t]*\|[ \t]*(?:{'|'.join(MOODS)})(?![a-z])", re.IGNORECASE)


def _field_values(field: re.Pattern, echo: re.Pattern, text: str) -> list[str]:
    """Every value the answer gives a MOOD or RISK field, leaving out the template echoed back."""
    return [match.group(1) for match in field.finditer(text) if not echo.match(text, match.end())]


def parse_summary(answer: str | None) -> tuple[str | None, str, str | None]:
    """(summary, mood, risk) from the last MOOD/SUMMARY/RISK lines (a reasoning preamble, or a reminder after the
    answer, may quote the format: the template itself is never a value). risk is 'self_harm' / 'overdose', or None
    for none, a missing line or an older two-line answer. "**RISK:** self_harm", "RISK：self_harm" and
    "Risk - `self_harm`" all count; a label needs its colon (or dash), so "risk, overdose" inside the summary
    sentence does not. The RISK line read here is the one risk_line reads."""
    if not answer:
        return None, "unknown", None
    text = _SUMMARY_MARKUP.sub("", answer)
    moods = _field_values(_SUMMARY_MOOD, _MOOD_ECHO, text)
    # "<one sentence>" / "<一句話>" is the template itself
    summaries = [line for line in (clean_reply(value) for value in _SUMMARY_TEXT.findall(text))
                 if line and not line.startswith("<")]
    risks = _field_values(_SUMMARY_RISK, _RISK_ECHO, text)
    mood = moods[-1].lower() if moods and moods[-1].lower() in MOODS else "unknown"
    return (summaries[-1] if summaries else None), mood, _risk_kind(risks[-1]) if risks else None


def risk_line(answer: str | None) -> str | None:
    """The last RISK line's label: 'none', 'self_harm' or 'overdose'; None when the answer has no RISK line (the
    backstop could not judge). The same line parse_summary reads, so a risk this accepts is the risk that counts."""
    risks = _field_values(_SUMMARY_RISK, _RISK_ECHO, _SUMMARY_MARKUP.sub("", answer or ""))
    return (_risk_kind(risks[-1]) or "none") if risks else None


async def summarize(history: list[dict], language: str,
                    info: dict | None = None) -> tuple[str | None, str, str | None]:
    """(summary, mood, risk); see parse_summary.

    An answer without a RISK line is not enough (the backstop could not judge), so the fallback model is asked
    too. A risk any answer gave counts: the backstop errs towards telling family.
    info, when given, receives {"reason", "attempts"} (reason as complete_with_reason gives it), so the caller
    can tell a rate limit from an empty answer.
    """
    transcript = "\n".join(f"{'長者' if t['role'] == 'patient' else 'Reachy'}: {t['text']}" for t in history)
    parsed: list[tuple[str | None, str, str | None]] = []

    def accept(answer: str | None) -> tuple[str, str, str | None] | None:
        parsed.append(parse_summary(answer))
        if parsed[-1][0] and risk_line(answer) is None:
            log.warning("summary answer has no RISK line")
            return None
        return parsed[-1] if parsed[-1][0] else None

    result, metrics = await _complete([{"role": "system", "content": SUMMARY_PROMPT[language]},
                                       {"role": "user", "content": transcript}],
                                      SUMMARY_MAX_TOKENS, SUMMARY_DEADLINE_SECONDS, SUMMARY_CALL_TIMEOUT, accept)
    if info is not None:
        info.update(reason=_reason(result, metrics), attempts=metrics["attempts"])
    # Otherwise the first answer with a summary, or else the mood the last answer gave.
    summary, mood, risk = (result or next((answer for answer in parsed if answer[0]), None)
                           or (parsed[-1] if parsed else (None, "unknown", None)))
    return summary, mood, risk or next((answer[2] for answer in parsed if answer[2]), None)


def safety_alert_text(name: str, quote: str, kind: str, from_summary: bool = False) -> str:
    """The family's LINE alert. from_summary: the quote is the end-of-chat summary, not the patient's words."""
    quote = quote.strip()[:120]
    what_zh = "可能吃了過量的藥" if kind == "overdose" else "可能有傷害自己的想法"
    what_en = "may have taken too much medicine" if kind == "overdose" else "may be thinking of harming themselves"
    if from_summary:
        said_zh = f"摘要：「{quote}」。" if quote else ""
        said_en = f" The summary says: \"{quote}\"." if quote else ""
        return (f"⚠️ 安全提醒：根據 {name} 與 Reachy 聊天後的對話摘要，{name} {what_zh}。{said_zh}"
                f"請盡快聯絡關心；如有立即危險請撥 119。\n"
                f"⚠️ Safety alert: according to the summary of {name}'s chat with Reachy, {name} {what_en}."
                f"{said_en} Please contact them as soon as possible; in an emergency call 119.")
    return (f"⚠️ 安全提醒：{name} 在與 Reachy 聊天時說：「{quote}」，{what_zh}。請盡快聯絡關心；如有立即危險請撥 119。\n"
            f"⚠️ Safety alert: while talking with Reachy, {name} said \"{quote}\" and {what_en}. "
            f"Please contact them as soon as possible; in an emergency call 119.")


async def alert_family(conn, u_id: int, conversation_id: str, text: str, dedupe: str) -> bool:
    """Set the conversation's risk_flag and alert every verified family contact, whatever their other alert
    settings (robot notice §6). Returns whether this call raised the alert.

    At most once per conversation: the flag is tested and set in one statement, which takes the row lock, so the
    keyword, model, late and summary layers can find the same risk in any order and the family is told once.
    dedupe names the source within the conversation (a turn id, or "summary").
    """
    first = await conn.fetchval(
        "UPDATE conversation SET risk_flag = TRUE WHERE conversation_id = $1::uuid AND NOT risk_flag "
        "RETURNING conversation_id", str(conversation_id))
    if first is None:
        return False
    notified = await outbox.enqueue_to_contacts(
        conn, u_id, kind="safety_alert", priority=0, messages=[{"type": "text", "text": text}],
        dedupe_prefix=f"safety_alert:{conversation_id}:{dedupe}", contact_flag=None)
    if not notified:
        # Nobody to tell (no verified *family* contact; the patient's own LINE is not one): never silent.
        log.warning("safety alert for user %s reached no family contact", u_id)
        await conn.execute(
            "INSERT INTO notification (u_id, category, type, message) VALUES ($1, 'family', $2, $3)",
            u_id, "safety_alert_undelivered",
            "A check-in safety alert could not be sent: no verified family contact on LINE.")
    return True


async def summary_backstop(conn, u_id: int, name: str, conversation_id: str, summary: str | None,
                           risk: str | None) -> bool:
    """Layer 3: a summary whose RISK line no earlier layer acted on alerts family, quoting the summary."""
    if not risk:
        return False
    return await alert_family(conn, u_id, conversation_id,
                              safety_alert_text(name, summary or "", risk, from_summary=True), "summary")
