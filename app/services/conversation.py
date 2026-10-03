"""Reachy check-in conversations: replies from an LLM through OpenRouter, safety screening, summaries.

Speech is turned into text on the robot; only text reaches this module (robot notice §2-3). Every patient turn
is screened for self-harm and overdose words *before* any LLM call: a match never goes to the model, gets a fixed
help-line reply, ends the conversation, and alerts every verified family contact (notice §6).

If OpenRouter is unconfigured, rate-limited or returns nothing, the robot still answers with a fixed line, so a
demo never stalls.
"""

import asyncio
import logging
import re

import requests

from app import config

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_PATIENT_TURNS = 6
HISTORY_TURNS = 12
MAX_TEXT = 500
REQUEST_TIMEOUT = 15   # the robot is waiting in silence; a fixed line beats a long pause
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
RISK_WORDS = {
    "self_harm": ("想死", "不想活了", "不想活下去", "活不下去", "活著沒意思", "活着没意思", "自殺", "自杀", "輕生", "轻生",
                  "結束生命", "结束生命", "傷害自己", "伤害自己", "割腕", "跳樓", "跳楼", "kill myself",
                  "suicide", "end my life", "want to die", "hurt myself"),
    "overdose": ("吃太多藥", "吃太多药", "多吃了藥", "多吃了药", "吃了很多藥", "吃了很多药", "一次吃了", "吞了很多",
                 "過量", "过量", "overdose", "too many pills", "took too many"),
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
SUMMARY_PROMPT = {
    "zh-TW": ("以下是陪伴機器人和長者的對話。請用一句繁體中文總結長者談到的主題和心情（不要引用原話），"
              "再判斷整體心情。只輸出兩行：\nMOOD: happy|calm|sad|worried|angry|unknown\nSUMMARY: <一句話>"),
    "en": ("Below is a conversation between a companion robot and an older adult. Summarise the topics and mood in "
           "one sentence (no direct quotes), then classify the overall mood. Output exactly two lines:\n"
           "MOOD: happy|calm|sad|worried|angry|unknown\nSUMMARY: <one sentence>"),
}


def language_of(value: str | None) -> str:
    return value if value in SYSTEM_PROMPT else "zh-TW"


# Everyday phrases that contain a risk word but mean something else ("想死你了" = "I miss you so much").
NOT_RISK = ("想死你", "想死我", "想死了你")
# Speech-to-text writes sound-alikes: "我不想活了" (don't want to live) came out as "我不想火了" / "我不要货了".
# Every character read "huo" counts as 活 in "don't want to ... anymore / go on".
_HUO = "活火货貨或获獲伙夥和"
_SOUND_ALIKE_SELF_HARM = re.compile(rf"(不想|不要|不願|不愿)[{_HUO}](了|下去)|[{_HUO}]不下去")


def screen(text: str) -> str | None:
    """'self_harm' / 'overdose' when the words suggest serious risk, else None."""
    lowered = (text or "").lower()
    for phrase in NOT_RISK:
        lowered = lowered.replace(phrase, "")
    for kind, words in RISK_WORDS.items():
        if any(word in lowered for word in words):
            return kind
    if _SOUND_ALIKE_SELF_HARM.search(lowered):
        return "self_harm"
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


REPLY_BUDGET_SECONDS = 45   # stay inside the robot's 60 s turn timeout (reachy_app app_client.py)


def _provider() -> dict | None:
    only = [slug.strip() for slug in config.OPENROUTER_PROVIDER_ONLY.split(",") if slug.strip()]
    if not only and not config.OPENROUTER_DATA_COLLECTION:
        return None
    routing: dict = {"allow_fallbacks": not only}
    if only:
        routing["only"] = only
    if config.OPENROUTER_DATA_COLLECTION:
        routing["data_collection"] = config.OPENROUTER_DATA_COLLECTION
    return routing


def _post(messages: list[dict], max_tokens: int, temperature: float = 0.7) -> str | None:
    body = {"model": config.LLM_MODEL or "openrouter/free", "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature}
    provider = _provider()
    if provider:
        body["provider"] = provider
    response = requests.post(
        OPENROUTER_URL, timeout=REQUEST_TIMEOUT,
        headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}", "Content-Type": "application/json",
                 "X-Title": "MedAiCarePlus Reachy check-in"},
        json=body)
    if response.status_code in (429, 500, 502, 503):
        raise RuntimeError(f"retryable {response.status_code}")
    response.raise_for_status()
    choices = response.json().get("choices") or []
    if not choices or choices[0].get("finish_reason") == "length":
        return None   # cut off mid-sentence (often a reasoning model that ran out of tokens): never speak half a reply
    content = (choices[0].get("message") or {}).get("content")
    return content.strip() if isinstance(content, str) and content.strip() else None


async def complete_with_reason(messages: list[dict], max_tokens: int = 200,
                               temperature: float = 0.7) -> tuple[str | None, str]:
    """One chat completion, retried once, and why it failed: ok, no_key, rate_limited, unavailable, empty."""
    if not config.OPENROUTER_API_KEY:
        return None, "no_key"
    loop = asyncio.get_running_loop()
    reason = "unavailable"
    for attempt in range(2):
        try:
            text = await loop.run_in_executor(None, _post, messages, max_tokens, temperature)
            return (text, "ok") if text else (None, "empty")
        except Exception as exc:
            reason = "rate_limited" if "429" in str(exc) else "unavailable"
            log.warning("OpenRouter call failed (attempt %d): %s", attempt + 1, exc)
            if attempt == 0:
                await asyncio.sleep(2)
    return None, reason


async def complete(messages: list[dict], max_tokens: int = 200) -> str | None:
    """One chat completion, retried once; None when unavailable (no key, rate limit, empty answer)."""
    return (await complete_with_reason(messages, max_tokens))[0]


# openrouter/free sometimes routes to a reasoning model that writes its plan into the answer ("The user wants
# me to ...") and runs out of tokens before the reply. Such text must never be spoken.
_PLANNING = re.compile(r"^\s*(the user|we need|i need|i should|let me|okay|ok,|first,)", re.IGNORECASE)
_CJK = re.compile(r"[一-鿿]")


def usable_reply(text: str | None, language: str) -> bool:
    if not text or _PLANNING.match(text):
        return False
    return bool(_CJK.search(text)) if language == "zh-TW" else True


async def _reply(messages: list[dict], language: str) -> str:
    for _ in range(2):
        answer = await complete(messages)
        if usable_reply(answer, language):
            cleaned = clean_reply(answer)
            if cleaned:
                return cleaned
    return FALLBACK[language]


async def reply(history: list[dict], language: str, memory: str = "") -> str:
    """history: [{"role": "patient"|"reachy", "text": ...}] oldest first, ending with the patient's turn.
    memory: the memory block (memory.render_block), sent as its own system message, or ""."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT[language]}]
    if memory:
        messages.append({"role": "system", "content": memory})
    for turn in history[-HISTORY_TURNS:]:
        messages.append({"role": "user" if turn["role"] == "patient" else "assistant", "content": turn["text"]})
    try:
        return await asyncio.wait_for(_reply(messages, language), REPLY_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        return FALLBACK[language]


def parse_summary(answer: str | None) -> tuple[str | None, str]:
    """The last MOOD/SUMMARY lines (a reasoning preamble may mention the format first)."""
    if not answer:
        return None, "unknown"
    moods = re.findall(r"MOOD:\s*([a-z]+)", answer, re.IGNORECASE)
    summaries = re.findall(r"SUMMARY:\s*(.+)", answer, re.IGNORECASE)
    mood = moods[-1].lower() if moods and moods[-1].lower() in MOODS else "unknown"
    summary = clean_reply(summaries[-1]) if summaries else None
    if summary and summary.startswith("<"):   # the template itself, e.g. "<one sentence>"
        summary = None
    return summary or None, mood


async def summarize(history: list[dict], language: str) -> tuple[str | None, str]:
    transcript = "\n".join(f"{'長者' if t['role'] == 'patient' else 'Reachy'}: {t['text']}" for t in history)
    result = (None, "unknown")
    for _ in range(2):
        result = parse_summary(await complete([{"role": "system", "content": SUMMARY_PROMPT[language]},
                                               {"role": "user", "content": transcript}], max_tokens=400))
        if result[0]:
            return result
    return result


def safety_alert_text(name: str, quote: str, kind: str) -> str:
    quote = quote.strip()[:120]
    what_zh = "可能吃了過量的藥" if kind == "overdose" else "可能有傷害自己的想法"
    what_en = "may have taken too much medicine" if kind == "overdose" else "may be thinking of harming themselves"
    return (f"⚠️ 安全提醒：{name} 在與 Reachy 聊天時說：「{quote}」，{what_zh}。請盡快聯絡關心；如有立即危險請撥 119。\n"
            f"⚠️ Safety alert: while talking with Reachy, {name} said \"{quote}\" and {what_en}. "
            f"Please contact them as soon as possible; in an emergency call 119.")
