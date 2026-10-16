"""What the patient's words carry (services/affect.py): the lexicon behind Reachy's body language, on the chat of
14 Oct 2026 00:00 (sad, every turn; nothing moved then), the weather chat just before it (nothing to show), the
negations, English, the reply's tone and the choice of recorded moves."""

import pytest

from app.services import affect

SAD_CHAT = ["日希我今天很難過", "我孩子都不跟我說話", "日希,你為什麼不愛我", "今天我不能有傷心的表情"]
WEATHER_CHAT = ["今天天氣什麼樣", "你要記得我是在花蓮", "所以花蓮的天氣什麼樣", "你可以跟我講高雄的天氣嗎", "你可以幫我查新聞嗎",
                "你可以跳舞嗎", "我今天情緒什麼樣", "喔,一般,我今- 我現在的衣服是什麼顏色", "我今天有安排吃藥嗎", "好喔,謝謝Reachy。拜拜",
                "OK,日希,掰掰"]


@pytest.mark.parametrize("words", SAD_CHAT)
def test_the_sad_chat_of_14_october_reads_as_sad_on_every_turn(words):
    assert affect.detect(words) == "sad"


@pytest.mark.parametrize("words", WEATHER_CHAT)
def test_everyday_talk_shows_nothing(words):
    assert affect.detect(words) is None


def test_simplified_english_and_worry_and_joy():
    assert affect.detect("日希我今天很难过") == "sad"                        # the transcript may be Simplified
    assert affect.detect("I feel really sad today.") == "sad"
    assert affect.detect("My children don't talk to me anymore.") == "sad"
    assert affect.detect("I won first place!") == "happy"
    assert affect.detect("我孫子考上大學了，太好了") == "happy"
    assert affect.detect("我很擔心明天的檢查") == "worried"
    assert affect.detect("I'm so worried about tomorrow") == "worried"
    assert affect.detect("我很開心但也很擔心") == "worried"                  # worry before joy
    assert affect.detect("") is None and affect.detect("   ") is None


def test_negations_and_questions_about_feelings_show_nothing():
    assert affect.detect("我不難過") is None
    assert affect.detect("沒什麼好擔心的") is None
    assert affect.detect("你擔不擔心") is None
    assert affect.detect("I'm not sad, don't worry") is None
    assert affect.detect("我今天不開心") == "sad"                             # 不開心 is sadness, not a negation
    assert affect.detect("我不會難過，我很開心") == "happy"


def test_the_replys_tone_catches_sorrow_the_patients_words_did_not_name():
    assert affect.reply_tone("被這樣對待真的很受傷。您現在心裡最難受的是哪一點?") == "sad"
    assert affect.reply_tone("I'm so sorry, that must be hard.") == "sad"
    assert affect.reply_tone("今天台北晴天二十一度,最高到三十一度") is None
    assert affect.reply_tone("") is None


def test_each_affect_has_a_posture_and_its_moves_come_in_turn_without_repeating_lately():
    for kind in affect.AFFECTS:
        assert affect.POSTURE[kind] in ("concern", "joy") and affect.MOVES[kind]
    assert affect.next_move("sad", []) == "downcast1"
    assert affect.next_move("sad", ["downcast1"]) == "yes_sad1"
    assert affect.next_move("sad", ["downcast1", "yes_sad1", "understanding1"]) == "downcast1"   # the oldest again
    assert affect.next_move("sad", ["yes_sad1", "downcast1", "understanding1"]) == "yes_sad1"
    assert affect.next_move("happy", ["cheerful1"]) == "enthusiastic2"
    assert affect.next_move("calm", []) is None
