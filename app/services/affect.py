"""What the patient's words carry emotionally, for Reachy's body language in a live chat (live_session.py).

On 14 Oct 2026 at 00:00 a patient said 「日希我今天很難過」, 「我孩子都不跟我說話」 and 「日希，你為什麼不愛我」; GPT-Live
answered with warmth and the robot did not move: its expression depended on the delegated backend choosing to call a
gesture tool, and it never did. Expression must not depend on a model's whim, so the relay reads the patient's words
itself, as it screens them for risk (conversation.screen), and tells the robot what to show:

- `detect(text)` -> "sad", "worried", "happy" or None: plain phrases, in Traditional Chinese (the transcript may come
  in either script) or English, with the everyday negations removed first (不難過, 沒什麼好怕的, 不開心 is sadness).
  Nothing subtle: an unclear sentence gets no expression, and the reply's own words are a second chance.
- `reply_tone(text)` -> "sad" or None: GPT-Live's words that show it heard sorrow (辛苦, 受傷, 心疼...), for a sad
  sentence the lexicon missed ("my children never call").
- `POSTURE` maps an affect to the robot's sustained posture (bridge/gestures.py: "concern" or "joy"), and `MOVES` to
  the recorded emotion moves the robot may play once as it answers (the SDK's pollen-robotics/reachy-mini-emotions-
  library; a different one each time, so a long sad chat does not repeat itself).
"""

import re

from app.services.conversation import _traditional

AFFECTS = ("sad", "worried", "happy")
POSTURE = {"sad": "concern", "worried": "concern", "happy": "joy"}
# Recorded emotion moves by affect, in the order they are offered (bridge/reachy_hardware.py plays them without their
# sounds; GPT-Live's voice is the sound). Lengths from the library: downcast1 5.9 s, yes_sad1 5.1 s, understanding1
# 3.9 s, calming1 6.1 s, attentive1 4.3 s, cheerful1 2.8 s, enthusiastic2 3.4 s, success2 2.4 s.
MOVES = {"sad": ("downcast1", "yes_sad1", "understanding1"),
         "worried": ("calming1", "attentive1"),
         "happy": ("cheerful1", "enthusiastic2", "success2")}
# What people say about it, as clear statements (Traditional; English whole words). A match sets the robot's posture
# for the turn, nothing more, so the list may be generous where the words are unmistakable.
WORDS = {
    "sad": ("難過", "傷心", "難受", "心痛", "心碎", "心酸", "想哭", "哭了", "一直哭", "好哭", "孤單", "寂寞", "好孤獨",
            "沒人理", "沒有人理", "不理我", "不跟我說話", "不跟我講話", "不愛我", "不要我", "不想要我", "不喜歡我",
            "拋棄", "被拋棄", "丟下我", "離開我", "過世", "走了以後", "去世", "不在了", "好想他", "好想她", "很想念",
            "委屈", "失望", "沮喪", "憂鬱", "鬱悶", "好累", "累了", "沒意思", "無聊透", "好煩", "心情不好",
            "心情很差", "心情很不好", "不開心", "不快樂", "不高興", "不太開心", "難過死了", "唉",
            "sad", "so sad", "unhappy", "lonely", "alone", "miss him", "miss her", "miss them", "cry", "crying",
            "nobody", "no one talks", "heartbroken", "hurts", "depressed", "down today", "feel down",
            "doesn't love me", "don't love me", "left me", "passed away", "miserable", "upset",
            "don't talk to me", "doesn't talk to me", "won't talk to me", "never call", "never calls",
            "never visit", "never visits", "ignores me", "ignore me", "forgot me", "forgotten me"),
    "worried": ("害怕", "好怕", "很怕", "擔心", "好擔心", "很擔心", "緊張", "好緊張", "不安", "恐怖", "焦慮", "慌",
                "睡不著", "嚇到", "嚇死", "怕死了",
                "scared", "afraid", "worried", "nervous", "anxious", "anxiety", "frightened", "terrified",
                "can't sleep", "panic"),
    "happy": ("開心", "很開心", "好開心", "高興", "快樂", "太好了", "太棒了", "好棒", "好讚", "第一名", "得獎", "得了獎",
              "中獎", "恭喜", "成功了", "考上", "錄取", "通過了", "哈哈", "笑死", "興奮", "幸福", "好幸福", "感謝",
              "謝謝你", "好喜歡", "愛你", "好期待", "太開心", "好高興", "真開心", "好玩",
              "happy", "so happy", "great news", "good news", "won", "first place", "excited", "wonderful",
              "so glad", "love it", "love you", "thank you so much", "amazing", "fantastic", "proud", "haha",
              "lol", "delighted"),
}
# Everyday phrases that are not the feeling: removed before matching, each inside one clause.
NOT_AFFECT = re.compile(
    r"不(用|必|要|會|太|那麼|再|怎麼|是|算|怎麼會)?(難過|傷心|擔心|害怕|怕|緊張|孤單|寂寞|委屈|失望|哭|想哭)"
    r"|(沒|沒有|別|不用|不要|不必|不會|不再)(那麼|這麼|什麼好|什麼)?(難過|傷心|擔心|害怕|怕|緊張|哭|想哭|孤單|寂寞|失望|委屈)"
    r"|沒什麼好(怕|擔心)的|不開心的事|開不開心|難不難過|擔不擔心|怕不怕|快不快樂|高不高興"
    r"|happy to help|not sad|not worried|not scared|don't worry|no worries|nothing to worry|not lonely|not alone")
# What Reachy says when it heard sorrow (GPT-Live's reply): the posture follows even when the patient's own
# sentence had no listed word.
REPLY_SAD = ("辛苦", "受傷", "心疼", "難過", "傷心", "委屈", "很不容易", "抱抱", "陪著您", "陪著你", "陪您", "陪你",
             "孤單", "寂寞", "失落", "沉重",
             "i'm sorry", "so sorry", "that hurts", "must be hard", "that's hard", "painful", "lonely", "heavy",
             "here for you", "here with you")
_PUNCTUATION = re.compile(r"(?:[^\w\s]|_)+")
_LATIN = re.compile(r"[a-z]")


def _english(word: str) -> re.Pattern:
    words = [re.escape(part) for part in re.split(r"[\s\-]+", word.replace("'", ""))]
    return re.compile(r"(?<![a-z])" + r"[\s\-]*".join(words) + r"(?![a-z])")


_CHINESE = {kind: [w for w in words if not _LATIN.search(w)] for kind, words in WORDS.items()}
_ENGLISH = {kind: [_english(w) for w in words if _LATIN.search(w)] for kind, words in WORDS.items()}
_REPLY_CHINESE = [w for w in REPLY_SAD if not _LATIN.search(w)]
_REPLY_ENGLISH = [_english(w) for w in REPLY_SAD if _LATIN.search(w)]


def _prepare(text: str) -> tuple[str, str]:
    """(compact, spaced): the words in Traditional Chinese, lower case; compact without spaces, punctuation or the
    phrases that are not the feeling, for the Chinese lists; spaced for the English ones."""
    converted = _traditional(text)
    compact = "".join(NOT_AFFECT.sub("", re.sub(r"\s+", "", part)) for part in _PUNCTUATION.split(converted))
    spaced = NOT_AFFECT.sub("", re.sub(r"\s+", " ", converted.replace("'", "").replace("’", "")))
    return compact, spaced


def detect(text: str) -> str | None:
    """The affect the patient's words show: "sad", "worried", "happy", or None when nothing is clear. Sadness and
    worry come before happiness when a sentence has both (「我很開心但也很擔心」 is worry to answer to)."""
    if not text or not text.strip():
        return None
    compact, spaced = _prepare(text)
    for kind in AFFECTS:
        if any(word in compact for word in _CHINESE[kind]) or any(p.search(spaced) for p in _ENGLISH[kind]):
            return kind
    return None


def reply_tone(text: str) -> str | None:
    """"sad" when Reachy's reply shows it heard sorrow, else None."""
    if not text or not text.strip():
        return None
    compact, spaced = _prepare(text)
    if any(word in compact for word in _REPLY_CHINESE) or any(p.search(spaced) for p in _REPLY_ENGLISH):
        return "sad"
    return None


def next_move(affect: str, played: list[str]) -> str | None:
    """The recorded move to play for `affect` now: the first of its list not played lately (`played`: the moves
    this chat played, oldest first), else the one played longest ago."""
    options = MOVES.get(affect, ())
    if not options:
        return None
    for move in options:
        if move not in played:
            return move
    return min(options, key=lambda move: max(i for i, name in enumerate(played) if name == move))
