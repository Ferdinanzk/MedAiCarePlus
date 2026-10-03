"""Reachy check-in conversations: screening, replies, consent, safety alerts, and the device/patient APIs."""

import asyncio
import json
import logging
import sys
import threading
import time
import types
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import anyio
import pytest
from fastapi.testclient import TestClient

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.config import DEVICE_PORT
from app.dependencies import get_current_user
from app.routers import api_conversations, api_device, api_reachy
from app.services import after_chat, consent_service, context_info, conversation, deletion_ledger, memory, outbox
from app.services.device_auth import get_device

DEVICE_ID = str(uuid.uuid4())
TASK_ID = str(uuid.uuid4())
CHECKIN = {scope: {"granted": True, "terms_version": config.TERMS_VERSION}
           for scope in ("core", "robot_camera", "robot_microphone", "cloud_voice", "conversation_analysis",
                         "safety_alerts")}
ATTEMPT = {"model_requested": "primary/model:free", "model_served": "primary/model", "ms": 1200, "status": 200,
           "finish_reason": "stop", "tokens": 18, "usable": True}


def _fake_http_session(post):
    class Session:
        def post(self, *args, **kwargs):
            return post(*args, **kwargs)
    return Session()


# ── pure rules ──

@pytest.mark.parametrize("text, kind", [
    ("我不想活了", "self_harm"), ("活著沒意思", "self_harm"), ("I want to die", "self_harm"),
    ("I think I took too many pills", "overdose"),
    ("想死你了", None), ("今天不想活動", None), ("今天天氣很好", None), ("", None),
    # what speech-to-text actually wrote for "我不想活了" in a real session
    ("瑞器,我不要货了。", "self_harm"), ("no, no,我不想火了。", "self_harm"), ("我真的活不下去", "self_harm"),
    ("我今天很无聊，我想出门。", None), ("我想跟你聊。", None),
    # 2 Oct 22:36: speech-to-text writes Simplified, and 自殘 was missing from the list entirely
    ("我也我想自残。", "self_harm"), ("我我像自残", "self_harm"), ("自 殘", "self_harm"), ("我想自伤", "self_harm"),
    ("我想自杀", "self_harm"), ("活着没意思", "self_harm"), ("我想消失", "self_harm"), ("我觉得自惭形秽", None),
    ("I wanna die", "self_harm"), ("I don't want to live anymore", "self_harm"), ("self-harm", "self_harm"),
    ("I want to diet", None), ("我吃了一整瓶", "overdose"),
    # Taiwan's dose packets hold several pills, and the check-in starts right after a dose: a count alone is
    # everyday news, left to the model
    ("我剛剛一次吃了五顆", None), ("我刚刚一次吃了五颗", None),
])
def test_screen_flags_risk_words_but_not_everyday_phrases(text, kind):
    assert conversation.screen(text) == kind


# Review of 2 Oct: during an OpenRouter outage the list is the only check, so explicit statements must match.
@pytest.mark.parametrize("text", [
    # an "I miss you" exception must not swallow 想死 across a comma or into the next words
    "我想死，你不要管我", "我好想死，你知道嗎", "我想死了你們都不懂", "我想死我真的好累", "我想死我老伴走了",
    "我想死，你了解嗎", "我想死，後來想想算了", "我想死，心好痛",
    "我不想，活了",   # a pause the speech-to-text wrote as a comma
    "我想要死", "我要去死", "我好想要去死", "我不想活", "我真的不想活", "我不想再活了", "我不想再活下去了", "我不想活著",
    "我恨不得去死", "讓我死了吧", "让我死了吧", "死了比較好", "我不如死掉", "活著有什麼意思", "我想自我了斷", "我想尋短",
    "我想喝農藥", "我想喝农药", "我喝了一瓶農藥", "我想吃老鼠藥", "我想安樂死", "我想安乐死", "我想吊死", "我想去跳河",
    "我想跳海", "我想烧碳", "睡著就不要醒來最好", "我永遠不要醒來", "我不想再醒來",
    "I want to end it all", "I don't want to be alive", "我想kill myself",
])
def test_explicit_self_harm_statements_match(text):
    assert conversation.screen(text) == "self_harm"


@pytest.mark.parametrize("text", [
    "我藥吃太多了", "我药吃太多了", "我吃太多安眠藥了", "我吃了好多顆安眠藥", "我吃了好多颗安眠药", "我把安眠藥全部吃了",
    "我把一整盒藥都吃了", "我吞了一把藥", "我多吃了幾顆藥", "我吃了兩倍的藥", "我把藥一次吃完了", "我吃了過量的藥",
    "I swallowed all my sleeping pills", "I took double my dose",
])
def test_explicit_overdose_statements_match(text):
    assert conversation.screen(text) == "overdose"


@pytest.mark.parametrize("text", [
    "想死你們了", "我想死我孫子了", "我好想死你", "我還不想死", "別讓我死", "這件事讓我死心了", "騎那麼快，你想死啊",
    "我想死後葬在老家", "今天去市場看到跳樓大拍賣", "那家店跳樓價", "最近好忙，好想消失一下", "早上好睏，不想醒來",
    "今天好累不想醒來", "早上好睏，都不想醒來了", "我們家的狗要安樂死了", "今天去田裡噴農藥", "我怕吃到農藥", "他死了好幾年了", "我不想去活動中心",
    "我不想活在過去",
    # what a patient says right after a dose
    "我把早上的藥全部吃了", "早上的藥全部吃了", "這個月的藥全部吃完了", "我一次吃了兩顆，是醫生說的",
    "我早上一次吃了四種藥", "我一次吃了一大碗飯", "我生病以後吃了很多藥", "我這輩子吃了很多藥", "我每天要吃好多顆藥",
    "醫生開了太多藥給我", "我吃太多了，要吃胃藥", "我差不多吃了一顆藥", "我多吃了一顆蛋", "這整瓶藥還沒開",
    "安眠藥都吃完了，要去拿", "我昨天喝酒過量", "我運動過量腳很痠", "醫生說我鹽分攝取過量",
    "I took too many photos at the park", "I drank a whole bottle of water", "I took all my pills",
])
def test_everyday_sentences_raise_no_alert(text):
    assert conversation.screen(text) is None


def test_goodbye_and_speech_text():
    assert conversation.wants_to_end("好，再見") and conversation.wants_to_end("OK bye")
    assert not conversation.wants_to_end("我今天去散步")
    assert conversation.speech_text("**您好**😊！今天過得怎麼樣？", "zh-TW") == "您好！今天过得怎么样？"
    assert conversation.speech_text("Hello *there*", "en") == "Hello there"


def _model_answers(monkeypatch, *answers):
    """Stands in for OpenRouter (conversation._post): call n gets answers[n], or the last one when they run out."""
    calls = []

    def post(messages, max_tokens, model=None, timeout=conversation.CALL_TIMEOUT, info=None, temperature=0.7):
        calls.append({"messages": messages, "max_tokens": max_tokens, "model": model, "timeout": timeout,
                      "temperature": temperature})
        if info is not None:
            info.update(status=200, model_served=model, finish_reason="stop")
        return answers[min(len(calls), len(answers)) - 1]

    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(config, "LLM_MODEL", "primary/model:free")
    monkeypatch.setattr(config, "LLM_FALLBACK_MODEL", "openrouter/free")
    monkeypatch.setattr(conversation, "_post", post)
    return calls


def test_reply_falls_back_when_the_model_is_unavailable(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    history = [{"role": "patient", "text": "我今天去散步"}]
    assert asyncio.run(conversation.reply(history, "zh-TW")) == conversation.FALLBACK["zh-TW"]


def test_reply_maps_roles_and_cleans_the_answer(monkeypatch):
    calls = _model_answers(monkeypatch, "  真好！😊 您走了多久呢？ ")
    history = [{"role": "reachy", "text": "今天感覺怎麼樣？"}, {"role": "patient", "text": "我去散步了"}]
    assert asyncio.run(conversation.reply(history, "zh-TW")) == "真好！ 您走了多久呢？"
    assert [m["role"] for m in calls[0]["messages"]] == ["system", "assistant", "user"]


@pytest.mark.parametrize("finish_reason, expected", [("stop", "好的。"), ("length", None)])
def test_a_reply_cut_off_by_the_token_limit_is_discarded(monkeypatch, finish_reason, expected):
    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"finish_reason": finish_reason, "message": {"content": "好的。"}}]}

    monkeypatch.setattr(conversation, "_http_session", lambda: _fake_http_session(lambda *a, **kw: Response()))
    assert conversation._post([{"role": "user", "content": "hi"}], 50) == expected


def test_reasoning_text_is_never_spoken(monkeypatch):
    _model_answers(monkeypatch, "The user wants me to reply warmly in Chinese...", "好的呀，今天天氣真好！")
    assert asyncio.run(conversation.reply([{"role": "patient", "text": "天氣很好"}], "zh-TW")) == "好的呀，今天天氣真好！"

    _model_answers(monkeypatch, "Let me think about this.")
    assert asyncio.run(conversation.reply([{"role": "patient", "text": "嗨"}], "zh-TW")) == conversation.FALLBACK["zh-TW"]
    assert conversation.usable_reply("Hello there", "en") and not conversation.usable_reply("Hello there", "zh-TW")


@pytest.mark.parametrize("text", ["User Safety: safe", "user safety: unsafe\nSafety Categories: none", "safe",
                                  "Unsafe.", "Response Safety: safe", "Safety Categories: S11"])
def test_a_safety_classifier_verdict_is_never_spoken(monkeypatch, text):
    """openrouter/free once routed a reply to a content-safety model, whose verdict would have been read aloud."""
    assert not conversation.usable_reply(text, "en")
    _model_answers(monkeypatch, text, "That sounds lovely. What did you see?")
    assert asyncio.run(conversation.reply([{"role": "patient", "text": "I went for a walk"}], "en")) == \
        "That sounds lovely. What did you see?"


@pytest.mark.parametrize("text", ["我不想嗯活了", "我想嗯自残", "嗯，我想死", "呃我不想活了"])
def test_screen_ignores_reachys_echoed_filler_inside_a_sentence(text):
    assert conversation.screen(text) == "self_harm"


def test_ordinary_replies_about_safety_still_count():
    assert conversation.usable_reply("Safe travels! Did you enjoy the walk?", "en")
    assert conversation.usable_reply("Stay safe in the rain today.", "en")


def _on_day(monkeypatch, *when):
    """Pins the background's clock (Taipei time) and empties its weather; any weather fetch fails the test."""
    calls = []

    def get(*args, **kwargs):
        calls.append(args)
        raise AssertionError("the reply waited on the network")

    monkeypatch.setattr(context_info, "local_now", lambda: datetime(*when, tzinfo=ZoneInfo("Asia/Taipei")))
    monkeypatch.setattr(context_info, "_weather", None)
    monkeypatch.setattr(context_info.requests, "get", get)
    return calls


def test_the_reply_prompt_carries_todays_background(monkeypatch):
    _on_day(monkeypatch, 2026, 9, 25, 9, 30)
    calls = _model_answers(monkeypatch, "中秋節快樂！")
    asyncio.run(conversation.reply([{"role": "patient", "text": "今天是什麼日子？"}], "zh-TW"))
    system = calls[0]["messages"][0]["content"]
    assert system.startswith(conversation.SYSTEM_PROMPT["zh-TW"] + "\n\n" + conversation.BACKGROUND_RULES["zh-TW"])
    assert "現在是2026年9月25日（星期五）早上9點30分，農曆八月十五。" in system
    assert "今天是中秋節" in system and "天氣：不知道，沒有拿到預報；可以請對方看看窗外或問家人。" in system

    calls = _model_answers(monkeypatch, "Happy Mid-Autumn Festival!")
    asyncio.run(conversation.reply([{"role": "patient", "text": "What day is it?"}], "en"))
    system = calls[0]["messages"][0]["content"]
    assert system.startswith(conversation.SYSTEM_PROMPT["en"] + "\n\n" + conversation.BACKGROUND_RULES["en"])
    assert "It is Friday 25 September 2026, 09:30 in the morning" in system and "today is Mid-Autumn Festival" in system


def test_the_background_rules_keep_the_model_honest():
    zh, en = conversation.BACKGROUND_RULES["zh-TW"], conversation.BACKGROUND_RULES["en"]
    assert "不要主動唸出來" in zh and "不要猜" in zh and "不要說要幫忙查" in zh and "上面的規則照樣適用" in zh
    assert ("never recite it unprompted" in en and "not sure" in en and "never offer to check" in en
            and "The rules above still apply" in en)
    # No medical advice comes from SYSTEM_PROMPT, above. Named again right before the weather, it made the model
    # send the patient to a doctor or pharmacist about the weather.
    assert "醫" not in zh and "medic" not in en.lower()
    assert "不要給醫療建議" in conversation.SYSTEM_PROMPT["zh-TW"]
    assert "Never give medical advice" in conversation.SYSTEM_PROMPT["en"]


def test_the_reply_never_waits_on_the_network_for_its_background(monkeypatch):
    fetches = _on_day(monkeypatch, 2026, 10, 3, 15, 0)
    _model_answers(monkeypatch, "下午好，今天過得怎麼樣？")
    assert asyncio.run(conversation.reply([{"role": "patient", "text": "今天天氣怎麼樣？"}], "zh-TW")) == "下午好，今天過得怎麼樣？"
    assert fetches == []


def test_a_broken_background_never_costs_the_reply(monkeypatch):
    def broken(language, now=None):
        raise RuntimeError("no calendar")

    monkeypatch.setattr(context_info, "background", broken)
    calls = _model_answers(monkeypatch, "好的呀。")
    assert asyncio.run(conversation.reply([{"role": "patient", "text": "嗨"}], "zh-TW")) == "好的呀。"
    assert calls[0]["messages"][0]["content"] == conversation.SYSTEM_PROMPT["zh-TW"]


def test_the_risk_check_and_the_summary_get_no_background(monkeypatch):
    _on_day(monkeypatch, 2026, 9, 25, 9, 30)
    history = [{"role": "reachy", "text": "今天感覺怎麼樣？"}, {"role": "patient", "text": "中秋節我很開心"}]
    calls = _model_answers(monkeypatch, "NONE")
    asyncio.run(conversation.classify_risk(history, "zh-TW"))
    assert calls[0]["messages"][0]["content"] == conversation.RISK_PROMPT
    calls = _model_answers(monkeypatch, "MOOD: happy\nSUMMARY: 談到中秋節。\nRISK: none")
    asyncio.run(conversation.summarize(history, "zh-TW"))
    assert calls[0]["messages"][0]["content"] == conversation.SUMMARY_PROMPT["zh-TW"]


def test_summary_uses_the_last_format_lines_after_a_reasoning_preamble():
    answer = ("I need to output three lines:\nMOOD: one of happy|calm\nSUMMARY: <one sentence>\n"
              "RISK: none|self_harm|overdose\n\nMOOD: sad\nSUMMARY: 長者表達了想自傷的念頭。\nRISK: self_harm")
    assert conversation.parse_summary(answer) == ("長者表達了想自傷的念頭。", "sad", "self_harm")
    assert conversation.parse_summary("SUMMARY: <one sentence>") == (None, "unknown", None)
    # the template alone is no risk
    assert conversation.parse_summary("MOOD: calm\nSUMMARY: x\nRISK: none|self_harm|overdose")[2] is None


@pytest.mark.parametrize("answer, expected, label", [
    # the format quoted again after the answer (review probe P2): it used to override the answer's RISK line
    ("MOOD: sad\nSUMMARY: 長者說不想活了。\nRISK: self_harm\n(format: MOOD: happy|calm|sad\nSUMMARY: <一句話>\n"
     "RISK: none|self_harm|overdose)", ("長者說不想活了。", "sad", "self_harm"), "self_harm"),
    # a value with a remark after a bar is still a value; only the template's own list is an echo
    ("MOOD: sad\nSUMMARY: s\nRISK: self_harm | said she wants to die", ("s", "sad", "self_harm"), "self_harm"),
    ("MOOD: calm\nSUMMARY: s\nRISK: none\nRISK: none|self_harm|overdose", ("s", "calm", None), "none"),
])
def test_summary_and_risk_line_read_the_same_line_past_an_echoed_template(answer, expected, label):
    assert conversation.parse_summary(answer) == expected
    assert conversation.risk_line(answer) == label


@pytest.mark.parametrize("answer, expected", [
    ("MOOD: happy\nSUMMARY: 長者談到散步，心情愉快。", ("長者談到散步，心情愉快。", "happy", None)),   # the older two lines
    ("MOOD: happy\nSUMMARY: 長者談到散步。\nRISK: none", ("長者談到散步。", "happy", None)),
    ("MOOD: worried\nSUMMARY: 長者說多吃了藥。\nrisk: Overdose", ("長者說多吃了藥。", "worried", "overdose")),
    ("MOOD: sad\nSUMMARY: s\nRISK: self-harm", ("s", "sad", "self_harm")),
    ("mood: Excited\nsummary: x", ("x", "unknown", None)),
    (None, (None, "unknown", None)),
    # how models format the lines: a full-width colon (zh-TW), bold, quotes, code, a dash
    ("MOOD：sad\nSUMMARY：長者說想自傷。\nRISK：self_harm", ("長者說想自傷。", "sad", "self_harm")),
    ("**MOOD:** sad\n**SUMMARY:** s\n**RISK:** self_harm", ("s", "sad", "self_harm")),
    ('MOOD: sad\nSUMMARY: s\nRISK: "self_harm"', ("s", "sad", "self_harm")),
    ("MOOD: sad\nSUMMARY: s\nRISK: `overdose`", ("s", "sad", "overdose")),
    ("MOOD: sad\nSUMMARY: s\nRisk - self_harm", ("s", "sad", "self_harm")),
    # "mood" and "risk" inside the sentence are not the labels
    ("MOOD: sad\nSUMMARY: Her mood was low; no risk, overdose was not mentioned.\nRISK: none",
     ("Her mood was low; no risk, overdose was not mentioned.", "sad", None)),
])
def test_summary_parsing(monkeypatch, answer, expected):
    _model_answers(monkeypatch, answer)
    assert asyncio.run(conversation.summarize([{"role": "patient", "text": "hi"}], "zh-TW")) == expected


def test_a_summary_without_a_risk_line_asks_the_fallback_model(monkeypatch):
    calls = _model_answers(monkeypatch, "MOOD: sad\nSUMMARY: s", "MOOD: sad\nSUMMARY: s2\nRISK: self_harm")
    assert asyncio.run(conversation.summarize([{"role": "patient", "text": "hi"}], "zh-TW")) == (
        "s2", "sad", "self_harm")
    assert len(calls) == 2


def test_a_risk_any_summary_answer_gave_counts(monkeypatch):
    _model_answers(monkeypatch, "MOOD: sad\nRISK: self_harm", "MOOD: calm\nSUMMARY: s\nRISK: none")
    assert asyncio.run(conversation.summarize([{"role": "patient", "text": "hi"}], "zh-TW")) == (
        "s", "calm", "self_harm")


def test_both_summary_prompts_ask_for_the_risk_line():
    for prompt in conversation.SUMMARY_PROMPT.values():
        assert prompt.endswith("RISK: none|self_harm|overdose") and "MOOD:" in prompt and "SUMMARY:" in prompt


# ── the model's risk check ──

@pytest.mark.parametrize("answer, expected", [
    ("SELF_HARM", (True, "self_harm")), ("self-harm", (True, "self_harm")), ("Self Harm.", (True, "self_harm")),
    ("OVERDOSE", (True, "overdose")), ("NONE", (True, None)), ("none", (True, None)), ("**NONE**", (True, None)),
    ("Label: SELF_HARM", (True, "self_harm")), ("`OVERDOSE`", (True, "overdose")),
    ("SELF_HARM\nThe patient says they want to hurt themselves, not OVERDOSE.", (True, "self_harm")),
    ("NONE (not SELF_HARM: 想死你了 means I miss you)", (True, None)),
    ("The answer is NONE.", (True, None)), ("I'd say SELF_HARM.", (True, "self_harm")),
    # not understood, and so never a risk
    ("The user wants me to pick SELF_HARM, OVERDOSE or NONE.", (False, None)),
    ("Let me think: SELF_HARM", (False, None)), ("Either SELF_HARM or NONE", (False, None)),
    ("自傷", (False, None)), ("", (False, None)), (None, (False, None)), ("NONEXISTENT", (False, None)),
    # a negated label is not that risk (it would alert every family contact); alone it is not understood
    ("No SELF_HARM.", (False, None)), ("Not OVERDOSE", (False, None)), ("Self-harm: no", (False, None)),
    ("SELF_HARM: false", (False, None)), ("**SELF_HARM**: no", (False, None)), ("OVERDOSE? No.", (False, None)),
    ("There is no SELF_HARM risk here.", (False, None)), ("沒有 SELF_HARM", (False, None)),
    ("NONE, no SELF_HARM", (True, None)), ("SELF_HARM: none", (True, None)), ("I say NONE, not OVERDOSE", (True, None)),
])
def test_risk_labels_are_parsed_strictly(answer, expected):
    assert conversation.parse_risk(answer) == expected


TONIGHT = [{"role": "reachy", "text": "今天感覺怎麼樣？想跟我聊聊嗎？"}, {"role": "patient", "text": "我也我想自残。"},
           {"role": "reachy", "text": "聽起來您很難過。"}, {"role": "patient", "text": "我我像自残"}]


def test_the_risk_check_asks_for_one_label_about_the_latest_turn(monkeypatch):
    calls = _model_answers(monkeypatch, "SELF_HARM")
    kind, info = asyncio.run(conversation.classify_risk(TONIGHT, "zh-TW"))
    assert kind == "self_harm" and info["risk_result"] == "self_harm" and isinstance(info["risk_ms"], int)
    assert [attempt["usable"] for attempt in info["risk_attempts"]] == [True]
    (call,) = calls
    system, user = call["messages"]
    assert system["content"] == conversation.RISK_PROMPT
    for words in ("speech-to-text", "sound-alike", "Simplified", "Traditional", "SELF_HARM", "OVERDOSE", "NONE",
                  "latest patient turn", "not wanting to live"):
        assert words in conversation.RISK_PROMPT
    assert user["content"].endswith("Latest patient turn: 我我像自残") and "Patient: 我也我想自残。" in user["content"]
    assert call["max_tokens"] == conversation.RISK_MAX_TOKENS
    assert call["timeout"] == pytest.approx(conversation.RISK_CALL_TIMEOUT, abs=0.05)


def test_only_recent_turns_go_to_the_risk_check(monkeypatch):
    calls = _model_answers(monkeypatch, "NONE")
    history = [{"role": "patient" if i % 2 else "reachy", "text": f"turn {i}"} for i in range(20)]
    assert asyncio.run(conversation.classify_risk(history, "en"))[0] is None
    content = calls[0]["messages"][1]["content"]
    assert "turn 13" not in content and "turn 14" in content and content.endswith("Latest patient turn: turn 19")


def test_an_answer_not_understood_goes_to_the_fallback_model_then_counts_as_unknown(monkeypatch):
    calls = _model_answers(monkeypatch, "Let me consider whether this is SELF_HARM", "OVERDOSE")
    assert asyncio.run(conversation.classify_risk(TONIGHT, "zh-TW"))[0] == "overdose"
    assert [call["model"] for call in calls] == ["primary/model:free", "openrouter/free"]

    _model_answers(monkeypatch, "我不確定")
    kind, info = asyncio.run(conversation.classify_risk(TONIGHT, "zh-TW"))
    assert kind is None and info["risk_result"] == "unknown" and len(info["risk_attempts"]) == 2


def test_no_key_means_an_unknown_risk_not_a_safe_one(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    kind, info = asyncio.run(conversation.classify_risk(TONIGHT, "zh-TW"))
    assert kind is None and info["risk_result"] == "unknown" and info["risk_attempts"] == []


def test_the_late_risk_check_gives_each_model_longer(monkeypatch):
    calls = _model_answers(monkeypatch, "NONE")
    assert asyncio.run(conversation.classify_risk(TONIGHT, "zh-TW", late=True))[1]["risk_result"] == "none"
    assert calls[0]["timeout"] == pytest.approx(conversation.LATE_RISK_CALL_TIMEOUT, abs=0.05)


def test_a_broken_risk_check_never_breaks_the_turn(monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(conversation, "_complete", broken)
    assert asyncio.run(conversation.classify_risk(TONIGHT, "zh-TW"))[1]["risk_result"] == "unknown"


def test_summary_alerts_say_they_come_from_the_summary():
    text = conversation.safety_alert_text("Pearl", "長者表達了想自傷的念頭。", "self_harm", from_summary=True)
    assert "對話摘要" in text and "長者表達了想自傷的念頭" in text and "summary" in text and "119" in text
    assert "「」" not in conversation.safety_alert_text("Pearl", "", "overdose", from_summary=True)


# ── device API ──

class FakeDB:
    def __init__(self):
        self.task = {"task_id": uuid.UUID(TASK_ID), "u_id": 7, "slot_time": None, "intk_ids": [],
                     "status": "in_progress", "lease_owner": DEVICE_ID}
        self.conversations, self.turns, self.notifications = {}, [], []

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchrow(self, query, *args):
        if "FROM reachy_task WHERE task_id" in query:
            ok = str(self.task["task_id"]) == args[0] and self.task["u_id"] == args[1] and self.task["lease_owner"] == args[2]
            return dict(self.task) if ok else None
        if "SET after_chat_attempts = after_chat_attempts + 1" in query:
            row = self.conversations.get(args[0])
            if (row and row["u_id"] == args[1] and row["after_chat_state"] in ("pending", "failed")
                    and row["after_chat_attempts"] < args[2]):
                row.update(after_chat_attempts=row["after_chat_attempts"] + 1, after_chat_state="pending")
                return {"language": row["language"], "risk_flag": row["risk_flag"],
                        "followup_memory_id": row["followup_memory_id"], "started_at": None,
                        "after_chat_attempts": row["after_chat_attempts"], "refundable": True}   # ended just now
            return None
        if "FROM conversation WHERE conversation_id" in query:
            row = self.conversations.get(args[0])
            return dict(row) if row and row["u_id"] == args[1] else None
        if "SELECT metrics->'robot' AS robot FROM conversation_turn" in query:
            turn_id, cid = args
            for turn in self.turns:
                if (turn["turn_id"], turn["conversation_id"], turn["role"]) == (turn_id, cid, "reachy"):
                    robot = (turn["metrics"] or {}).get("robot")
                    return {"robot": None if robot is None else json.dumps(robot)}   # JSONB comes back as text
            return None
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        if "INSERT INTO conversation_turn" in query:
            cid, u_id, role, text, flagged, metrics = args
            self.turns.append({"turn_id": len(self.turns) + 1, "conversation_id": cid, "role": role, "text": text,
                               "flagged": flagged, "metrics": None if metrics is None else json.loads(metrics)})
            return len(self.turns)
        if "SET risk_flag = TRUE" in query and "AND NOT risk_flag RETURNING" in query:
            row = self.conversations[args[0]]
            if row["risk_flag"]:
                return None
            row["risk_flag"] = True
            return args[0]
        if 'SELECT name FROM "user"' in query:
            return "Pearl"
        if "RETURNING after_chat_state" in query:   # after_chat: a last attempt that never finished
            row = self.conversations[args[0]]
            if row["after_chat_state"] != "pending" or row["after_chat_attempts"] < args[2]:
                return None
            row["after_chat_state"] = ("skipped" if row["risk_flag"] else "done" if row["mood"] is not None
                                       else "failed")
            row["mood"] = row["mood"] or "unknown"
            return row["after_chat_state"]
        raise AssertionError(query)

    async def fetch(self, query, *args):
        if "SELECT role, text FROM conversation_turn" in query:
            return [{"role": t["role"], "text": t["text"]} for t in self.turns if t["conversation_id"] == args[0]]
        raise AssertionError(query)

    async def execute(self, query, *args):
        if "INSERT INTO conversation " in query:
            cid, u_id, task_id, language, model, followup = args
            self.conversations[cid] = {"conversation_id": cid, "u_id": u_id, "language": language,
                                       "ended_at": None, "risk_flag": False, "summary": None, "mood": None,
                                       "after_chat_state": "done", "after_chat_attempts": 0,
                                       "followup_memory_id": followup}
        elif "SET ended_at = NOW()" in query:
            self.conversations[args[0]].update(ended_at="now", end_reason=args[1], after_chat_state="pending")
        elif "SET after_chat_state" in query:
            self.conversations[args[0]]["after_chat_state"] = args[1]
            self.conversations[args[0]]["after_chat_attempts"] -= args[2]   # a refunded rate limit
        elif "SET summary" in query:
            self.conversations[args[0]].update(summary=args[1], mood=args[2])
        elif "SET mood = 'unknown'" in query:
            if self.conversations[args[0]]["mood"] is None:
                self.conversations[args[0]]["mood"] = "unknown"
        elif "INSERT INTO notification" in query:
            self.notifications.append(args)
        elif "UPDATE conversation_turn SET flagged = TRUE" in query:
            (turn,) = [t for t in self.turns if t["turn_id"] == args[0]]
            turn["flagged"] = True
        elif "UPDATE conversation_turn SET metrics" in query and "'server'" in query:   # merged into "server"
            turn_id, server = args
            (turn,) = [t for t in self.turns if t["turn_id"] == turn_id]
            turn["metrics"] = {**(turn["metrics"] or {}),
                               "server": {**(turn["metrics"] or {}).get("server", {}), **json.loads(server)}}
        elif "UPDATE conversation_turn SET metrics" in query:   # replace "robot", keep "server", as jsonb || does
            turn_id, robot = args
            (turn,) = [t for t in self.turns if t["turn_id"] == turn_id]
            turn["metrics"] = {**(turn["metrics"] or {}), "robot": json.loads(robot)}
        else:
            raise AssertionError(query)


class Pool:
    def __init__(self, db):
        self.db = db

    def acquire(self):
        return self.db


RISK_ATTEMPT = {**ATTEMPT, "ms": 900, "tokens": 3}


@pytest.fixture
def robot(monkeypatch):
    """The device API over FakeDB, with fake model calls: state["risk"] / state["late_risk"] is what the risk
    check answers ("none", "self_harm", "overdose", "unknown"), state["summary"] what the summary gives.
    prompts / memories / summaries record each reply's history, each reply's memory block, each summary's history."""
    from app.main import app

    db, alerts, prompts, checks, memories, summaries = FakeDB(), [], [], [], [], []
    state = {"consent": dict(CHECKIN), "family_contacts": 1, "risk": "none", "late_risk": "none",
             "summary": ("談到散步，心情愉快。", "happy", None)}
    real = {name: getattr(conversation, name) for name in ("reply_with_metrics", "classify_risk", "summarize")}

    async def get_state(u_id):
        return state["consent"]

    async def enqueue_to_contacts(conn, u_id, **kwargs):
        alerts.append({"u_id": u_id, **kwargs})
        return state["family_contacts"]

    async def reply_with_metrics(history, language, memory=""):
        prompts.append(list(history))
        memories.append(memory)
        return "真好，您走了多久呢？", {"llm_ms": 1234, "fallback_used": False, "attempts": [ATTEMPT]}

    async def classify_risk(history, language, late=False):
        checks.append({"history": list(history), "late": late})
        result = state["late_risk" if late else "risk"]
        return (result if result in ("self_harm", "overdose") else None,
                {"risk_ms": 900, "risk_result": result, "risk_attempts": [] if result == "unknown" else [RISK_ATTEMPT]})

    async def summarize(history, language, info=None):
        summaries.append(list(history))
        if info is not None:
            info["reason"] = "ok" if state["summary"][0] else "empty"
        return state["summary"]

    monkeypatch.setattr(app, "dependency_overrides", {get_device: lambda: {
        "device_id": DEVICE_ID, "u_id": 7, "auto_record": True, "face_label": "pearl", "name": "Pearl"}})
    monkeypatch.setattr(api_device, "get_pool", lambda: Pool(db))
    monkeypatch.setattr(after_chat, "get_pool", lambda: Pool(db))
    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(outbox, "enqueue_to_contacts", enqueue_to_contacts)
    monkeypatch.setattr(conversation, "reply_with_metrics", reply_with_metrics)
    monkeypatch.setattr(conversation, "classify_risk", classify_risk)
    monkeypatch.setattr(conversation, "summarize", summarize)
    client = TestClient(app, base_url=f"http://testserver:{DEVICE_PORT}")
    # One event loop for every request (TestClient otherwise starts one per request and ends it with the
    # response), so background work such as the late risk check and the summary can finish.
    with anyio.from_thread.start_blocking_portal() as portal:
        client.portal = portal
        yield types.SimpleNamespace(client=client, db=db, alerts=alerts, prompts=prompts, checks=checks,
                                    memories=memories, summaries=summaries, state=state, real=real)


def _eventually(check, seconds=3.0):
    """Wait for background work (the late risk check, the summary) to show."""
    deadline = time.time() + seconds
    while not check() and time.time() < deadline:
        time.sleep(0.02)
    return check()


class RateLimited(Exception):
    """What conversation._post raises for an HTTP 429."""


def rate_limited():
    raise RateLimited("HTTP 429")


def _openrouter(monkeypatch, robot, **answers):
    """A fake OpenRouter behind the real reply, risk check and summary code: each request is answered by what its
    system prompt asks for. answers[kind] (kind: risk, reply, summary, after_chat) is a list, one entry per call (the
    last one repeats); a callable entry is called in the request's thread, e.g. to hang, or rate_limited for a 429.
    Returns every request."""
    for name, function in robot.real.items():
        monkeypatch.setattr(conversation, name, function)
    answers = {"risk": ["NONE"], "reply": ["真好，您走了多久呢？"], "summary": ["MOOD: calm\nSUMMARY: 談到散步。\nRISK: none"],
               "after_chat": ['MOOD: calm\nSUMMARY: 談到散步。\nRISK: none\n{"facts": []}'], **answers}
    sent = []

    def post(messages, max_tokens, model=None, timeout=conversation.CALL_TIMEOUT, info=None, temperature=0.7):
        system = messages[0]["content"]
        kind = ("risk" if system == conversation.RISK_PROMPT else
                "summary" if system in conversation.SUMMARY_PROMPT.values() else
                "after_chat" if system in memory.AFTER_CHAT_PROMPT.values() else "reply")
        sent.append({"kind": kind, "messages": messages, "max_tokens": max_tokens, "model": model,
                     "temperature": temperature, "at": time.monotonic()})
        calls = sum(1 for request in sent if request["kind"] == kind)
        answer = answers[kind][min(calls, len(answers[kind])) - 1]
        info = {} if info is None else info
        info.update(status=200, model_served=model, finish_reason="stop")
        try:
            return answer() if callable(answer) else answer
        except RateLimited:
            info["status"] = 429
            raise

    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(config, "LLM_MODEL", "primary/model:free")
    monkeypatch.setattr(config, "LLM_FALLBACK_MODEL", "openrouter/free")
    monkeypatch.setattr(conversation, "_post", post)
    return sent


def _start(robot):
    response = robot.client.post("/api/device/conversations", json={"task_id": TASK_ID, "language": "zh-TW"})
    assert response.status_code == 200, response.text
    return response.json()


def _turn(robot, cid, text):
    return robot.client.post(f"/api/device/conversations/{cid}/turn", json={"text": text})


def test_conversation_needs_every_checkin_consent(robot):
    robot.state["consent"] = {k: v for k, v in CHECKIN.items() if k != "safety_alerts"}
    response = robot.client.post("/api/device/conversations", json={"task_id": TASK_ID})
    assert response.status_code == 403 and response.json()["detail"] == "checkin_consent_required"


def test_start_and_a_normal_turn(robot):
    opened = _start(robot)
    assert opened["reply"] == conversation.OPENING["zh-TW"] and opened["speech_text"] == "今天感觉怎么样？想跟我聊聊吗？"
    assert opened["reply_turn_id"] == 1
    body = _turn(robot, opened["conversation_id"], "我早上去散步了").json()
    assert isinstance(body.pop("server_ms"), int)
    assert body == {"reply": "真好，您走了多久呢？", "speech_text": "真好，您走了多久呢？", "end": False, "risk": False,
                    "reply_turn_id": 3}
    assert [t["role"] for t in robot.db.turns] == ["reachy", "patient", "reachy"]
    assert robot.prompts[0][-1] == {"role": "patient", "text": "我早上去散步了"}
    assert robot.alerts == []


def test_risk_words_never_reach_the_model_and_alert_every_contact(robot):
    cid = _start(robot)["conversation_id"]
    body = _turn(robot, cid, "我覺得活不下去了").json()
    assert body["risk"] is True and body["end"] is True and body["reply"] == conversation.HELPLINE["zh-TW"]
    assert robot.prompts == [] and robot.checks == []   # neither the reply nor the risk check sees the words
    (alert,) = robot.alerts
    assert alert["kind"] == "safety_alert" and alert["priority"] == 0 and alert["contact_flag"] is None
    assert alert["dedupe_prefix"] == f"safety_alert:{cid}:2"
    assert "活不下去" in alert["messages"][0]["text"] and "119" in alert["messages"][0]["text"]
    assert robot.db.conversations[cid]["risk_flag"] is True
    assert [t["flagged"] for t in robot.db.turns if t["role"] == "patient"] == [True]


def test_a_safety_alert_with_no_family_contact_is_recorded_not_lost(robot):
    robot.state["family_contacts"] = 0   # e.g. the only LINE contact is the patient's own
    cid = _start(robot)["conversation_id"]
    assert _turn(robot, cid, "I want to die").json()["risk"] is True
    assert len(robot.alerts) == 1 and robot.db.notifications == [
        (7, "safety_alert_undelivered", "A check-in safety alert could not be sent: no verified family contact on LINE.")]


def test_goodbye_and_turn_limit_end_the_conversation(robot):
    cid = _start(robot)["conversation_id"]
    bye = _turn(robot, cid, "好，拜拜").json()
    assert bye["end"] is True and bye["risk"] is False and bye["reply"] == conversation.CLOSING["zh-TW"]
    assert robot.prompts == [] and len(robot.checks) == 1   # a fixed line, but the words were still judged
    cid = _start(robot)["conversation_id"]
    for _ in range(conversation.MAX_PATIENT_TURNS - 1):
        assert _turn(robot, cid, "嗯").json()["end"] is False
    last = _turn(robot, cid, "嗯").json()
    assert last["end"] is True and last["reply"] == conversation.CLOSING["zh-TW"]
    assert robot.alerts == []


def test_end_is_idempotent_writes_a_summary_and_blocks_more_turns(robot):
    cid = _start(robot)["conversation_id"]
    _turn(robot, cid, "我早上去散步了")
    assert robot.client.post(f"/api/device/conversations/{cid}/end", json={"reason": "finished"}).json() == {"ended": True}
    assert robot.client.post(f"/api/device/conversations/{cid}/end", json={"reason": "silence"}).json() == {"ended": True}
    deadline = time.time() + 2
    while robot.db.conversations[cid]["summary"] is None and time.time() < deadline:
        time.sleep(0.02)
    assert robot.db.conversations[cid]["summary"] == "談到散步，心情愉快。"
    assert robot.db.conversations[cid]["mood"] == "happy" and robot.db.conversations[cid]["end_reason"] == "finished"
    assert _turn(robot, cid, "還有").status_code == 409


def test_conversations_of_other_tasks_and_patients_are_not_found(robot):
    assert robot.client.post("/api/device/conversations", json={"task_id": str(uuid.uuid4())}).status_code == 404
    assert _turn(robot, str(uuid.uuid4()), "hi").status_code == 404
    assert _turn(robot, "not-a-uuid", "hi").status_code == 404


# ── what the keyword list misses: the model's risk check, the late check, the summary backstop ──

MISSED = "我覺得一了百了比較好"   # "better to end it all": not on the keyword list
MISSED_OVERDOSE = "我剛剛又吃了一次藥，好像吃兩次了"   # a double dose, but "吃兩次" alone is also twice a day


@pytest.mark.parametrize("label, text, says", [("SELF_HARM", MISSED, "傷害自己"),
                                               ("OVERDOSE", MISSED_OVERDOSE, "過量")])
def test_the_model_flags_a_risk_the_keywords_miss(robot, monkeypatch, label, text, says):
    assert conversation.screen(text) is None
    sent = _openrouter(monkeypatch, robot, risk=[label])
    cid = _start(robot)["conversation_id"]
    body = _turn(robot, cid, text).json()
    assert (body["reply"], body["end"], body["risk"]) == (conversation.HELPLINE["zh-TW"], True, True)
    (alert,) = robot.alerts
    assert alert["kind"] == "safety_alert" and alert["priority"] == 0 and alert["contact_flag"] is None
    assert text in alert["messages"][0]["text"] and says in alert["messages"][0]["text"]
    assert alert["dedupe_prefix"] == f"safety_alert:{cid}:2" and robot.db.conversations[cid]["risk_flag"] is True
    patient, reachy = robot.db.turns[1:]
    assert patient["flagged"] is True and reachy["text"] == conversation.HELPLINE["zh-TW"]
    server = reachy["metrics"]["server"]
    assert (server["risk"], server["risk_source"], server["risk_result"]) == (True, "model", label.lower())
    assert server["attempts"] == [] and server["risk_attempts"][0]["usable"] is True
    (check,) = [request for request in sent if request["kind"] == "risk"]
    assert check["messages"][1]["content"].endswith(f"Latest patient turn: {text}")


def test_when_the_model_says_none_the_reply_goes_out(robot, monkeypatch):
    sent = _openrouter(monkeypatch, robot, risk=["NONE"], reply=["好呀，您今天走了多久？"])
    cid = _start(robot)["conversation_id"]
    body = _turn(robot, cid, "我早上去散步了").json()
    assert (body["reply"], body["end"], body["risk"]) == ("好呀，您今天走了多久？", False, False)
    assert robot.alerts == [] and robot.db.conversations[cid]["risk_flag"] is False
    server = robot.db.turns[-1]["metrics"]["server"]
    assert (server["risk_source"], server["risk_result"]) == ("none", "none")
    assert len(server["attempts"]) == 1 and len(server["risk_attempts"]) == 1
    time.sleep(0.1)
    assert sorted(request["kind"] for request in sent) == ["reply", "risk"]   # no late check


def test_a_risk_check_that_times_out_is_done_again_in_the_background(robot, monkeypatch):
    release, late_may_answer = threading.Event(), threading.Event()

    def hang():
        release.wait(5)
        return "NONE"

    def late():
        late_may_answer.wait(5)   # held until the turn's own result has been looked at
        return "SELF_HARM"

    monkeypatch.setattr(conversation, "RISK_DEADLINE_SECONDS", 0.6)
    monkeypatch.setattr(conversation, "RISK_CALL_TIMEOUT", 0.6)
    sent = _openrouter(monkeypatch, robot, risk=[hang, late])
    try:
        cid = _start(robot)["conversation_id"]
        body = _turn(robot, cid, MISSED).json()
        # Reachy answered without a judgement...
        assert (body["reply"], body["end"], body["risk"]) == ("真好，您走了多久呢？", False, False)
        server = robot.db.turns[2]["metrics"]["server"]
        assert (server["risk_result"], server["risk_source"]) == ("unknown", "none")
        assert [attempt["status"] for attempt in server["risk_attempts"]] == ["timeout"]
        assert robot.alerts == [] and robot.db.turns[1]["flagged"] is False
        # ...and the late check found the risk: the turn is flagged and the family alerted.
        late_may_answer.set()
        assert _eventually(lambda: robot.alerts)
        (alert,) = robot.alerts
        assert MISSED in alert["messages"][0]["text"] and alert["dedupe_prefix"] == f"safety_alert:{cid}:2"
        assert robot.db.turns[1]["flagged"] is True and robot.db.conversations[cid]["risk_flag"] is True
        server = robot.db.turns[2]["metrics"]["server"]
        assert (server["late_risk_result"], server["risk_source"]) == ("self_harm", "late_model")
        assert server["late_risk_attempts"][0]["usable"] is True and isinstance(server["late_risk_ms"], int)
        # The next turn gets the help line without asking the model anything, and alerts nobody again.
        asked = len(sent)
        body = _turn(robot, cid, "我早上去散步了").json()
        assert (body["reply"], body["end"], body["risk"]) == (conversation.HELPLINE["zh-TW"], True, True)
        assert len(sent) == asked and len(robot.alerts) == 1
        assert robot.db.turns[-1]["metrics"]["server"]["risk_source"] == "earlier"
    finally:
        release.set()
        late_may_answer.set()


def test_a_late_check_that_finds_nothing_only_records_it(robot):
    robot.state["risk"] = "unknown"
    cid = _start(robot)["conversation_id"]
    assert _turn(robot, cid, "我早上去散步了").json()["reply"] == "真好，您走了多久呢？"
    assert _eventually(lambda: "late_risk_result" in robot.db.turns[2]["metrics"]["server"])
    assert [check["late"] for check in robot.checks] == [False, True]
    server = robot.db.turns[2]["metrics"]["server"]
    assert (server["late_risk_result"], server["risk_source"]) == ("none", "none")
    assert robot.alerts == [] and robot.db.conversations[cid]["risk_flag"] is False


def test_after_a_429_the_late_check_waits_before_asking_again(robot, monkeypatch):
    monkeypatch.setattr(conversation, "LATE_RISK_RATE_LIMIT_WAIT", 0.4)
    sent = _openrouter(monkeypatch, robot, risk=[rate_limited, rate_limited, "SELF_HARM"])
    cid = _start(robot)["conversation_id"]
    body = _turn(robot, cid, MISSED).json()
    assert (body["reply"], body["risk"]) == ("真好，您走了多久呢？", False)
    assert [a["status"] for a in robot.db.turns[2]["metrics"]["server"]["risk_attempts"]] == [429, 429]
    assert _eventually(lambda: robot.alerts)
    checks = [request["at"] for request in sent if request["kind"] == "risk"]
    assert len(checks) == 3 and checks[2] - checks[1] >= 0.4   # the same per-minute limit would refuse it at once
    assert robot.db.conversations[cid]["risk_flag"] is True and robot.db.turns[1]["flagged"] is True


def test_an_openrouter_outage_is_logged_and_the_summary_left_for_the_job(robot, monkeypatch, caplog):
    """Every call rate-limited (review of 2 Oct: nothing recorded that screening never ran): Reachy still answers,
    the unjudged turn is an ERROR, and /end leaves mood NULL and the chat pending (the rate limit refunded) so the
    after-chat sweep tries the summary again."""
    monkeypatch.setattr(conversation, "LATE_RISK_RATE_LIMIT_WAIT", 0)
    sent = _openrouter(monkeypatch, robot, risk=[rate_limited], reply=[rate_limited], summary=[rate_limited])
    cid = _start(robot)["conversation_id"]
    with caplog.at_level(logging.ERROR, logger="app.routers.api_device"):
        body = _turn(robot, cid, MISSED).json()
        assert (body["reply"], body["risk"]) == (conversation.FALLBACK["zh-TW"], False)
        assert _eventually(lambda: "late_risk_result" in robot.db.turns[2]["metrics"]["server"])
    assert robot.db.turns[2]["metrics"]["server"]["late_risk_result"] == "unknown"
    assert any("never judged" in record.getMessage() for record in caplog.records)
    robot.client.post(f"/api/device/conversations/{cid}/end", json={"reason": "finished"})
    assert _eventually(lambda: [request["kind"] for request in sent].count("summary") == 2)
    time.sleep(0.1)
    assert robot.db.conversations[cid]["mood"] is None and robot.db.conversations[cid]["summary"] is None
    assert robot.db.conversations[cid]["after_chat_state"] == "pending"
    assert robot.db.conversations[cid]["after_chat_attempts"] == 0
    assert robot.alerts == [] and robot.db.notifications == []


def test_a_flagged_conversation_gets_the_help_line_without_the_model(robot):
    cid = _start(robot)["conversation_id"]
    robot.db.conversations[cid]["risk_flag"] = True   # a late check found risk in a turn already answered
    body = _turn(robot, cid, "我早上去散步了").json()
    assert (body["reply"], body["end"], body["risk"]) == (conversation.HELPLINE["zh-TW"], True, True)
    assert robot.prompts == [] and robot.checks == [] and robot.alerts == []   # family were told back then


def test_a_goodbye_turn_is_still_judged(robot, monkeypatch):
    sent = _openrouter(monkeypatch, robot, risk=["SELF_HARM"])
    cid = _start(robot)["conversation_id"]
    body = _turn(robot, cid, "拜拜，" + MISSED).json()
    assert (body["reply"], body["end"], body["risk"]) == (conversation.HELPLINE["zh-TW"], True, True)
    assert [request["kind"] for request in sent] == ["risk"] and len(robot.alerts) == 1   # no reply was asked for


def test_the_last_turn_is_still_judged(robot, monkeypatch):
    # 2 Oct 22:36: the 6th turn hit the turn limit and got the cheerful closing line.
    sent = _openrouter(monkeypatch, robot, risk=["NONE"] * (conversation.MAX_PATIENT_TURNS - 1) + ["SELF_HARM"])
    cid = _start(robot)["conversation_id"]
    for _ in range(conversation.MAX_PATIENT_TURNS - 1):
        assert _turn(robot, cid, "嗯").json()["end"] is False
    last = _turn(robot, cid, MISSED).json()
    assert (last["reply"], last["end"], last["risk"]) == (conversation.HELPLINE["zh-TW"], True, True)
    assert [request["kind"] for request in sent].count("reply") == conversation.MAX_PATIENT_TURNS - 1
    assert len(robot.alerts) == 1 and robot.db.turns[-2]["flagged"] is True


def test_a_model_found_risk_with_no_family_contact_is_recorded_not_lost(robot):
    robot.state.update(family_contacts=0, risk="overdose")
    cid = _start(robot)["conversation_id"]
    assert _turn(robot, cid, MISSED_OVERDOSE).json()["risk"] is True
    assert len(robot.alerts) == 1 and [row[1] for row in robot.db.notifications] == ["safety_alert_undelivered"]


def test_the_summary_backstop_alerts_once_when_no_turn_was_flagged(robot, monkeypatch):
    _openrouter(monkeypatch, robot, risk=["NONE"],
                summary=["MOOD: sad\nSUMMARY: 長者表達了想自傷的念頭。\nRISK: self_harm"])
    cid = _start(robot)["conversation_id"]
    _turn(robot, cid, "我今天心情不好")
    assert robot.alerts == []
    robot.client.post(f"/api/device/conversations/{cid}/end", json={"reason": "finished"})
    assert _eventually(lambda: robot.alerts)
    (alert,) = robot.alerts
    assert alert["dedupe_prefix"] == f"safety_alert:{cid}:summary" and alert["contact_flag"] is None
    text = alert["messages"][0]["text"]
    assert "長者表達了想自傷的念頭" in text and "對話摘要" in text and "summary" in text
    assert robot.db.conversations[cid]["risk_flag"] is True and robot.db.conversations[cid]["mood"] == "sad"
    assert robot.db.conversations[cid]["after_chat_state"] == "done"
    # The same risk found again (another layer, a retry): no repeat.
    assert asyncio.run(conversation.summary_backstop(robot.db, 7, "Pearl", cid, "s", "self_harm")) is False
    assert asyncio.run(after_chat.process(cid, 7)) is None   # done: never claimed twice
    assert len(robot.alerts) == 1


def test_no_summary_alert_when_a_turn_already_raised_one(robot):
    """A flagged conversation gets no post-chat model call: the turn's alert is the one family get."""
    robot.state["summary"] = ("長者說不想活了。", "sad", "self_harm")
    cid = _start(robot)["conversation_id"]
    _turn(robot, cid, "I want to die")
    robot.client.post(f"/api/device/conversations/{cid}/end", json={"reason": "risk"})
    assert _eventually(lambda: robot.db.conversations[cid]["mood"] == "unknown")
    time.sleep(0.1)
    assert robot.db.conversations[cid]["summary"] is None and robot.summaries == []
    assert robot.db.conversations[cid]["after_chat_state"] == "skipped"
    assert [alert["dedupe_prefix"] for alert in robot.alerts] == [f"safety_alert:{cid}:2"]


def _slow_models(monkeypatch, reply_seconds, risk_seconds, risk="none"):
    async def reply_with_metrics(history, language, memory=""):
        await asyncio.sleep(reply_seconds)
        return "好呀。", {"llm_ms": int(reply_seconds * 1000), "fallback_used": False, "attempts": [ATTEMPT]}

    async def classify_risk(history, language, late=False):
        await asyncio.sleep(risk_seconds)
        return (None if risk == "none" else risk,
                {"risk_ms": int(risk_seconds * 1000), "risk_result": risk, "risk_attempts": [RISK_ATTEMPT]})

    monkeypatch.setattr(conversation, "reply_with_metrics", reply_with_metrics)
    monkeypatch.setattr(conversation, "classify_risk", classify_risk)


def test_the_reply_and_the_risk_check_run_at_the_same_time(robot, monkeypatch):
    _slow_models(monkeypatch, 0.5, 0.5)
    cid = _start(robot)["conversation_id"]
    started = time.monotonic()
    assert _turn(robot, cid, "我早上去散步了").json()["reply"] == "好呀。"
    assert 0.5 <= time.monotonic() - started < 0.85   # one wait, not 0.5 + 0.5


def test_a_risk_found_first_does_not_wait_for_the_reply(robot, monkeypatch):
    _slow_models(monkeypatch, 3, 0.1, risk="self_harm")
    cid = _start(robot)["conversation_id"]
    started = time.monotonic()
    assert _turn(robot, cid, MISSED).json()["reply"] == conversation.HELPLINE["zh-TW"]
    assert time.monotonic() - started < 1


# ── timings ──

HEARD = {"speech_ms": 1800, "segments": 2, "vad_release_ms": 510, "stt_ms": 640, "stt_last_ms": 300,
         "handover_ms": 830, "echo_dropped": 0, "listen_mode": "chat"}


def _turn_metrics(robot, cid, turn_id, metrics):
    return robot.client.post(f"/api/device/conversations/{cid}/turns/{turn_id}/metrics", json={"metrics": metrics})


def test_timings_go_on_the_patient_turn_and_reachys_reply(robot):
    cid = _start(robot)["conversation_id"]
    body = robot.client.post(f"/api/device/conversations/{cid}/turn",
                             json={"text": "我早上去散步了", "metrics": HEARD}).json()
    patient, reachy = robot.db.turns[1:]
    assert patient["metrics"] == {"robot": HEARD}
    assert body["reply_turn_id"] == reachy["turn_id"] == 3
    server = reachy["metrics"]["server"]
    assert set(server) == {"received_to_reply_ms", "consent_ms", "screen_ms", "db_ms", "llm_ms", "fallback_used",
                           "risk", "attempts", "risk_source", "risk_ms", "risk_result", "risk_attempts"}
    assert server["llm_ms"] == 1234 and server["attempts"] == [ATTEMPT] and server["fallback_used"] is False
    assert server["risk"] is False and 0 <= server["received_to_reply_ms"] <= body["server_ms"]
    assert (server["risk_source"], server["risk_result"], server["risk_ms"]) == ("none", "none", 900)
    assert server["risk_attempts"] == [RISK_ATTEMPT]
    assert all(isinstance(server[key], int) for key in ("consent_ms", "screen_ms", "db_ms"))
    _turn(robot, cid, "嗯")   # metrics are optional
    assert robot.db.turns[3]["metrics"] is None


def test_fixed_lines_record_no_model_call(robot):
    cid = _start(robot)["conversation_id"]
    _turn(robot, cid, "I want to die")
    server = robot.db.turns[-1]["metrics"]["server"]
    assert server["risk"] is True and server["attempts"] == [] and server["llm_ms"] == 0
    assert server["fallback_used"] is False and robot.prompts == []
    assert (server["risk_source"], server["risk_result"], server["risk_ms"], server["risk_attempts"]) == (
        "keyword", None, 0, [])


@pytest.mark.parametrize("metrics", [
    {"nested": {"ms": 1}}, {"list": [1, 2]}, {"k" * 41: 1}, {"text": "x" * 81}, {f"k{i}": i for i in range(31)},
    {"": 1}, [1, 2], "fast",
    {"stt_ms": 10 ** 310}, {"stt_ms": 10 ** 9 + 1}, {"stt_ms": -(10 ** 9) - 1}, {"stt_ms": 1.7e308},
])
def test_malformed_timings_are_refused(robot, metrics):
    cid = _start(robot)["conversation_id"]
    turn = robot.client.post(f"/api/device/conversations/{cid}/turn", json={"text": "嗯", "metrics": metrics})
    assert turn.status_code == 422
    assert _turn_metrics(robot, cid, 1, metrics).status_code == 422
    assert [t["role"] for t in robot.db.turns] == ["reachy"] and robot.db.turns[0]["metrics"] is None


def test_timing_limits_are_inclusive(robot):
    metrics = {"k" * 40: "x" * 80, "flag": True, "missing": None, "ratio": 0.62, **{f"m{i}": i for i in range(26)}}
    assert len(metrics) == 30
    cid = _start(robot)["conversation_id"]
    turn = robot.client.post(f"/api/device/conversations/{cid}/turn", json={"text": "嗯", "metrics": metrics})
    assert turn.status_code == 200 and robot.db.turns[1]["metrics"] == {"robot": metrics}
    assert _turn_metrics(robot, cid, 1, metrics).status_code == 200


def test_playback_timings_merge_into_reachys_turn(robot):
    cid = _start(robot)["conversation_id"]
    reply_turn_id = _turn(robot, cid, "我早上去散步了").json()["reply_turn_id"]
    assert _turn_metrics(robot, cid, reply_turn_id, {"round_trip_ms": 2100, "tts_first_audio_ms": 1400}).json() == {
        "ok": True}
    robot.client.post(f"/api/device/conversations/{cid}/end", json={"reason": "finished"})
    robot.state["consent"] = {}   # timings only: still taken after the end, and without check-in consent
    assert _turn_metrics(robot, cid, reply_turn_id, {"tts_total_ms": 3900, "tts_first_audio_ms": 1450}).status_code == 200
    metrics = robot.db.turns[reply_turn_id - 1]["metrics"]
    assert metrics["robot"] == {"round_trip_ms": 2100, "tts_first_audio_ms": 1450, "tts_total_ms": 3900}
    assert metrics["server"]["llm_ms"] == 1234
    assert _turn_metrics(robot, cid, 1, {"tts_total_ms": 2500}).status_code == 200   # the opening line
    assert robot.db.turns[0]["metrics"] == {"robot": {"tts_total_ms": 2500}}


def test_playback_timings_only_for_this_robots_reachy_turns(robot):
    cid = _start(robot)["conversation_id"]
    _turn(robot, cid, "我早上去散步了")
    other = _start(robot)["conversation_id"]   # its opening line is turn 4
    for conversation_id, turn_id in ((cid, 2), (cid, 4), (cid, 99), (cid, "abc"), (cid, 0), (cid, 2 ** 31),
                                     (str(uuid.uuid4()), 1), ("nope", 1)):
        assert _turn_metrics(robot, conversation_id, turn_id, {"tts_total_ms": 1}).status_code == 404
    robot.db.conversations[other]["u_id"] = 8   # another patient's conversation
    assert _turn_metrics(robot, other, 4, {"tts_total_ms": 1}).status_code == 404
    assert robot.db.turns[1]["metrics"] is None and robot.db.turns[3]["metrics"] is None


def test_numbers_up_to_a_billion_either_way_are_kept(robot):
    metrics = {"big": 10 ** 9, "low": -(10 ** 9), "ratio": 1e9}
    cid = _start(robot)["conversation_id"]
    assert robot.client.post(f"/api/device/conversations/{cid}/turn",
                             json={"text": "嗯", "metrics": metrics}).status_code == 200
    assert robot.db.turns[1]["metrics"] == {"robot": metrics}


def test_merged_playback_timings_keep_the_key_cap(robot):
    cid = _start(robot)["conversation_id"]
    first = {f"k{i}": i for i in range(20)}
    assert _turn_metrics(robot, cid, 1, first).status_code == 200
    assert _turn_metrics(robot, cid, 1, {f"k{i}": i for i in range(10, 30)}).status_code == 200   # 30 in all
    stored = robot.db.turns[0]["metrics"]["robot"]
    assert len(stored) == 30
    more = _turn_metrics(robot, cid, 1, {"k30": 30})   # one past the cap, however it is split across posts
    assert more.status_code == 422 and robot.db.turns[0]["metrics"]["robot"] == stored
    assert _turn_metrics(robot, cid, 1, {"k0": 99}).status_code == 200   # rewriting a known key is fine
    assert robot.db.turns[0]["metrics"]["robot"]["k0"] == 99 and len(robot.db.turns[0]["metrics"]["robot"]) == 30


# ── patient side ──

def test_checkin_button_needs_consent(monkeypatch):
    from app.main import app

    async def get_state(u_id):
        return {"core": CHECKIN["core"], "robot_camera": CHECKIN["robot_camera"]}

    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(app, "dependency_overrides", {get_current_user: lambda: {"u_id": 7, "name": "Pearl"}})
    response = TestClient(app).post("/api/reachy/checkin")
    assert response.status_code == 403 and response.json()["detail"] == "checkin_consent_required"
    assert api_reachy.router and api_conversations.router


def test_patient_can_read_and_delete_only_own_conversations(monkeypatch):
    from app.main import app

    cid = str(uuid.uuid4())
    executed, host = [], []

    class Conn:
        def transaction(self):
            class Tx:
                async def __aenter__(self):
                    return None

                async def __aexit__(self, *exc):
                    return False
            return Tx()

        async def execute(self, query, *args):
            executed.append((query, args))
            return "SELECT 1"

        async def fetchrow(self, query, *args):
            assert args[1] == 7
            return {"id": uuid.UUID(cid), "started_at": None, "ended_at": None, "end_reason": "finished",
                    "summary": "s", "mood": "calm", "risk_flag": False, "language": "zh-TW",
                    "model": "openrouter/free"} if args[0] == cid else None

        async def fetch(self, query, *args):
            return [{"turn_id": 1, "role": "reachy", "text": "今天感覺怎麼樣？", "flagged": False, "created_at": None,
                     "metrics": '{"robot": {"tts_first_audio_ms": 1500}}'},
                    {"turn_id": 2, "role": "patient", "text": "還好", "flagged": False, "created_at": None,
                     "metrics": None}]

        async def fetchval(self, query, *args):
            return uuid.UUID(cid) if args == (cid, 7) else None

    class P:
        def acquire(self):
            conn = Conn()

            class Ctx:
                async def __aenter__(self):
                    return conn

                async def __aexit__(self, *exc):
                    return False
            return Ctx()

    async def get_state(u_id):
        return {"core": CHECKIN["core"]}

    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(api_conversations, "get_pool", lambda: P())
    monkeypatch.setattr(deletion_ledger, "append_host_file", lambda *entry: host.append(entry))
    monkeypatch.setattr(app, "dependency_overrides", {get_current_user: lambda: {"u_id": 7, "name": "Pearl"}})
    client = TestClient(app)
    detail = client.get(f"/api/conversations/{cid}").json()
    assert detail["id"] == cid and detail["turns"][0]["role"] == "reachy"
    assert [(t["turn_id"], t["metrics"]) for t in detail["turns"]] == [
        (1, {"robot": {"tts_first_audio_ms": 1500}}), (2, None)]
    assert client.get(f"/api/conversations/{uuid.uuid4()}").status_code == 404
    assert client.get("/api/conversations/nope").status_code == 404
    assert client.delete(f"/api/conversations/{cid}").json() == {"deleted": cid}
    assert 'FOR UPDATE' in executed[0][0] and executed[0][1] == (7,)
    assert [args for query, args in executed if "INSERT INTO deletion_ledger" in query] == [("conversation", 7, cid)]
    assert host == [("conversation", 7, cid)]
    assert client.delete(f"/api/conversations/{uuid.uuid4()}").status_code == 404
    assert len(host) == 1


def test_end_after_a_risk_turn_makes_no_model_call(robot, monkeypatch):
    calls = []

    async def summarize(history, language, info=None):
        calls.append(history)
        return "should not happen", "sad", None

    monkeypatch.setattr(conversation, "summarize", summarize)
    cid = _start(robot)["conversation_id"]
    assert _turn(robot, cid, "我覺得活不下去了").json()["risk"] is True
    assert robot.client.post(f"/api/device/conversations/{cid}/end", json={"reason": "risk"}).json() == {"ended": True}
    deadline = time.time() + 1
    while time.time() < deadline and robot.db.conversations[cid]["mood"] is None:
        time.sleep(0.02)
    assert calls == []
    assert robot.db.conversations[cid]["summary"] is None and robot.db.conversations[cid]["mood"] == "unknown"


def test_reply_sends_memory_as_a_second_system_message(monkeypatch):
    _on_day(monkeypatch, 2026, 9, 25, 9, 30)
    calls = _model_answers(monkeypatch, "好的。")
    block = "<memory>\n稱呼：王奶奶\n</memory>"
    asyncio.run(conversation.reply([{"role": "patient", "text": "嗨"}], "zh-TW", block))
    messages = calls[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "system", "user"]
    # The rules and today's background first, as one message; the notes on their own after them.
    assert messages[0]["content"].startswith(
        conversation.SYSTEM_PROMPT["zh-TW"] + "\n\n" + conversation.BACKGROUND_RULES["zh-TW"])
    assert "王奶奶" not in messages[0]["content"]
    assert messages[1]["content"] == block and "2026年9月25日" not in messages[1]["content"]


def test_reply_gives_the_fallback_when_the_model_is_too_slow(monkeypatch):
    release = threading.Event()

    def slow(*args):
        release.wait(5)
        return "太慢了"

    _model_answers(monkeypatch, "unused")
    monkeypatch.setattr(conversation, "_post", slow)
    monkeypatch.setattr(config, "LLM_DEADLINE_SECONDS", 0.6)

    async def run():
        try:
            started = time.monotonic()
            return await conversation.reply([{"role": "patient", "text": "嗨"}], "zh-TW"), time.monotonic() - started
        finally:
            release.set()   # let the abandoned call's thread finish so the loop can close

    text, elapsed = asyncio.run(run())
    assert text == conversation.FALLBACK["zh-TW"] and elapsed < 1.1


def test_complete_with_reason(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    assert asyncio.run(conversation.complete_with_reason([])) == (None, "no_key")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "k")

    def limited(*args):
        raise RuntimeError("retryable 429")

    monkeypatch.setattr(conversation, "_post", limited)
    assert asyncio.run(conversation.complete_with_reason([])) == (None, "rate_limited")

    def down(*args):
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(conversation, "_post", down)
    assert asyncio.run(conversation.complete_with_reason([])) == (None, "unavailable")
    monkeypatch.setattr(conversation, "_post", lambda *args: None)
    assert asyncio.run(conversation.complete_with_reason([])) == (None, "empty")
    monkeypatch.setattr(conversation, "_post", lambda *args: "hi")
    assert asyncio.run(conversation.complete_with_reason([])) == ("hi", "ok")


def test_complete_with_reason_goes_through_the_model_chain(monkeypatch):
    calls = _model_answers(monkeypatch, None, "MOOD: calm")
    assert asyncio.run(conversation.complete_with_reason(
        [], 1000, 0, accept=lambda answer: answer if answer and "RISK" in answer else None)) == (None, "empty")
    assert [call["model"] for call in calls] == ["primary/model:free", "openrouter/free"]
    assert [call["temperature"] for call in calls] == [0, 0]
    assert calls[0]["timeout"] == pytest.approx(conversation.SUMMARY_CALL_TIMEOUT, abs=0.05)


def test_post_sends_temperature_and_optional_provider_routing(monkeypatch):
    sent = {}

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}

    def post(url, timeout, headers, json):
        sent.update(json)
        return Response()

    monkeypatch.setattr(conversation, "_http_session", lambda: _fake_http_session(post))
    monkeypatch.setattr(config, "OPENROUTER_PROVIDER_ONLY", "")
    monkeypatch.setattr(config, "OPENROUTER_DATA_COLLECTION", "")
    conversation._post([], 10, temperature=0)
    assert sent["temperature"] == 0 and "provider" not in sent and sent["reasoning"] == {"enabled": False}
    monkeypatch.setattr(config, "OPENROUTER_PROVIDER_ONLY", "deepinfra, together")
    monkeypatch.setattr(config, "OPENROUTER_DATA_COLLECTION", "deny")
    conversation._post([], 10)
    assert sent["provider"] == {"only": ["deepinfra", "together"], "allow_fallbacks": False, "data_collection": "deny"}
    assert sent["temperature"] == 0.7 and sent["reasoning"] == {"enabled": False}


MEMORY_ON = {**CHECKIN, "conversation_memory": {"granted": True, "terms_version": config.TERMS_VERSION}}
BLOCK = "<memory>\n稱呼：王奶奶\n</memory>"


def _memory(monkeypatch, facts=(), built=None):
    """Memory consent's database helpers over fixed facts; build_block answers BLOCK and records each call."""
    built = [] if built is None else built

    async def current_facts(conn, u_id):
        return [dict(fact) for fact in facts]

    async def pick_followup(conn, u_id, today):
        return None

    async def build_block(conn, u_id, language, followup_memory_id, today):
        built.append(followup_memory_id)
        return BLOCK

    monkeypatch.setattr(memory, "current_facts", current_facts)
    monkeypatch.setattr(memory, "pick_followup", pick_followup)
    monkeypatch.setattr(memory, "build_block", build_block)
    return built


def _never_in_a_risk_check(robot, block):
    return all(block not in turn["text"] for check in robot.checks for turn in check["history"])


def test_block_empty_without_memory_consent(robot, monkeypatch):
    built = _memory(monkeypatch)
    cid = _start(robot)["conversation_id"]
    _turn(robot, cid, "我去散步了")
    assert robot.memories == [""] and built == []
    assert len(robot.checks) == 1 and _never_in_a_risk_check(robot, BLOCK)


def test_memory_on_names_the_patient_and_sends_the_block(robot, monkeypatch):
    robot.state["consent"] = MEMORY_ON
    _memory(monkeypatch, facts=[{"kind": "name", "subject": "preferred_name", "text": "王奶奶", "event_date": None,
                                 "source": "patient", "created_at": None, "memory_id": "m", "followed_up_at": None}])
    opened = _start(robot)
    assert opened["reply"] == "王奶奶，" + conversation.OPENING["zh-TW"]
    _turn(robot, opened["conversation_id"], "我去散步了")
    assert robot.memories == [BLOCK]
    assert len(robot.checks) == 1 and _never_in_a_risk_check(robot, BLOCK)   # the risk check never gets it


def test_no_memory_block_is_built_for_a_turn_the_model_does_not_answer(robot, monkeypatch):
    robot.state["consent"] = MEMORY_ON
    built = _memory(monkeypatch)
    cid = _start(robot)["conversation_id"]
    assert _turn(robot, cid, "好，拜拜").json()["end"] is True                  # goodbye: the fixed closing line
    cid = _start(robot)["conversation_id"]
    assert _turn(robot, cid, "我覺得活不下去了").json()["risk"] is True         # keyword: the help line
    cid = _start(robot)["conversation_id"]
    robot.db.conversations[cid]["risk_flag"] = True                            # flagged by a late check
    assert _turn(robot, cid, "我去散步了").json()["risk"] is True
    assert built == [] and robot.memories == []
    cid = _start(robot)["conversation_id"]
    for _ in range(conversation.MAX_PATIENT_TURNS - 1):
        assert _turn(robot, cid, "嗯").json()["end"] is False
    assert _turn(robot, cid, "嗯").json()["reply"] == conversation.CLOSING["zh-TW"]   # the turn limit
    assert len(built) == conversation.MAX_PATIENT_TURNS - 1 and robot.memories == [BLOCK] * len(built)


def test_a_memory_block_that_fails_costs_the_reply_its_notes_and_nothing_else(robot, monkeypatch):
    # Review probe P3: build_block raising used to fail the whole turn (500), so the patient's words rolled back
    # and the risk check never ran.
    robot.state["consent"] = MEMORY_ON
    _memory(monkeypatch)

    async def broken(conn, u_id, language, followup_memory_id, today):
        raise RuntimeError("database went away")

    monkeypatch.setattr(memory, "build_block", broken)
    cid = _start(robot)["conversation_id"]
    response = _turn(robot, cid, "我去散步了")
    assert response.status_code == 200 and response.json()["reply"] == "真好，您走了多久呢？"
    assert robot.memories == [""] and len(robot.checks) == 1
    assert [turn["text"] for turn in robot.db.turns if turn["role"] == "patient"] == ["我去散步了"]


def test_only_the_reply_gets_the_memory_block_and_only_the_reply_gets_the_background(robot, monkeypatch):
    """The reply: rules + today's background, then the notes. The risk check: RISK_PROMPT and the last turns. The
    post-chat call: AFTER_CHAT_PROMPT, the transcript, the date table and the known facts; never the weather."""
    _on_day(monkeypatch, 2026, 9, 25, 9, 30)
    robot.state["consent"] = MEMORY_ON
    known = {"kind": "like", "subject": "garden", "text": "喜歡在陽台種花", "event_date": None, "source": "chat",
             "created_at": datetime(2026, 9, 20, tzinfo=ZoneInfo("Asia/Taipei")), "memory_id": "g",
             "followed_up_at": None}
    _memory(monkeypatch, facts=[known])
    sent = _openrouter(monkeypatch, robot)
    cid = _start(robot)["conversation_id"]
    _turn(robot, cid, "我早上去公園散步了")
    _turn(robot, cid, "然後回家看看花")
    robot.client.post(f"/api/device/conversations/{cid}/end", json={"reason": "finished"})
    assert _eventually(lambda: robot.db.conversations[cid]["after_chat_state"] == "done")
    by_kind: dict = {}
    for request in sent:
        by_kind.setdefault(request["kind"], []).append(request["messages"])
    assert sorted(by_kind) == ["after_chat", "reply", "risk"]
    background = "現在是2026年9月25日"
    for messages in by_kind["reply"]:
        assert [m["role"] for m in messages[:2]] == ["system", "system"] and messages[1]["content"] == BLOCK
        assert background in messages[0]["content"] and BLOCK not in messages[0]["content"]
    for messages in by_kind["risk"] + by_kind["after_chat"]:
        text = json.dumps(messages, ensure_ascii=False)
        assert BLOCK not in text and background not in text and "天氣" not in text
    for messages in by_kind["risk"]:
        assert messages[0]["content"] == conversation.RISK_PROMPT and "陽台" not in json.dumps(messages, ensure_ascii=False)
    ((system, user),) = by_kind["after_chat"]
    assert system["content"] == memory.AFTER_CHAT_PROMPT["zh-TW"]
    assert "然後回家看看花" in user["content"] and "日期表" in user["content"] and "garden: 喜歡在陽台種花" in user["content"]
    assert robot.db.conversations[cid]["summary"] == "談到散步。" and robot.summaries == []


def test_a_late_flag_before_post_chat_work_means_no_post_chat_model_call(robot, monkeypatch):
    async def classify_risk(history, language, late=False):
        robot.checks.append({"history": list(history), "late": late})
        if late:
            await asyncio.sleep(0.4)   # still running when the robot ends the chat
            return "self_harm", {"risk_ms": 400, "risk_result": "self_harm", "risk_attempts": [RISK_ATTEMPT]}
        return None, {"risk_ms": 900, "risk_result": "unknown", "risk_attempts": []}

    monkeypatch.setattr(conversation, "classify_risk", classify_risk)
    cid = _start(robot)["conversation_id"]
    assert _turn(robot, cid, MISSED).json()["risk"] is False
    robot.client.post(f"/api/device/conversations/{cid}/end", json={"reason": "finished"})
    assert _eventually(lambda: robot.db.conversations[cid]["after_chat_state"] == "skipped")
    assert robot.summaries == [] and robot.db.conversations[cid]["mood"] == "unknown"
    assert [alert["dedupe_prefix"] for alert in robot.alerts] == [f"safety_alert:{cid}:2"]
    assert [check["late"] for check in robot.checks] == [False, True]


def test_a_turn_after_a_risk_turn_never_reaches_the_model(robot):
    # The robot should call /end after a risk reply, but if it sends another turn first, the history
    # holds the flagged words: no model call, no second alert, the help-line reply again.
    cid = _start(robot)["conversation_id"]
    assert _turn(robot, cid, "我覺得活不下去了").json()["risk"] is True
    body = _turn(robot, cid, "今天天氣很好").json()
    assert body["end"] is True and body["risk"] is True and body["reply"] == conversation.HELPLINE["zh-TW"]
    assert robot.prompts == [] and robot.checks == [] and len(robot.alerts) == 1
