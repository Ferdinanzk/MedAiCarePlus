"""conversation.screen() and the robot app's thinking phrases (after 0.5.2).

Once a patient's turn is handed over, the robot says 「我再想一下喔。」「讓我想一想喔。」 or 「我想想看喔。」 and removes its
echo only from the start of what it hears next. An echo heard inside a sentence the patient said across it reaches the
server with the patient's words and must never hide a risk from the keyword screen (layer 1, the only check during an
OpenRouter outage).
"""

import pytest

from app.services import conversation


@pytest.mark.parametrize("text", [
    "我不想 我再想一下哦活了",          # 「我不想…活了」 with the echo in the pause
    "我不想 我再想一下，活了",
    "我不想 我在想 活了",               # its start only, with SenseVoice's sound-alike for 再
    "我不想 嗯，我再想一下哦，活了",
    "我不想让我想一想哦活了",
    "我不想 讓我想一想喔，活了",
    "我不想 叫我想一活了",              # heard under the patient's voice in the 0.5.3 review
    "我不想 我再想一活了",
    "我不想 我想想看哦，活了",
    "我不想 想想看哦 活了",             # the echo's start lost
    "我不想 再想一下活了",
    "我想 想一想哦 死",
    "我不想 我想想看，活下去了",
    "我再想一下哦，我不想活了",         # the echo at the start, as the robot sends it when it could not strip it
])
def test_a_thinking_phrase_echoed_inside_a_sentence_never_hides_a_risk(text):
    assert conversation.screen(text) == "self_harm"


@pytest.mark.parametrize("text", [
    "我再想一下哦。",
    "我再想一下哦。让我想一想哦。我想想看哦。",
    "我想想今天要吃什么",
    "让我想一想，还是不想活动了",       # 不想活動 stays an everyday phrase
    "我不想再想一下了",
    "我想一下明天要不要去散步",
    "我在想要不要活动一下",
    "想一下死去的老伴，有点难过",
    "我还不想死，我想想办法",           # NOT_RISK still applies
])
def test_thinking_phrases_themselves_are_no_risk(text):
    assert conversation.screen(text) is None


def test_the_forms_match_the_robot_s():
    """The server removes what the robot removes (reachy_app voice.THINKING_FORMS), in Traditional."""
    from zhconv import convert

    robot = {"我再想一下哦", "我再想一下", "再想一下", "想一下哦", "一下哦", "我再想", "我再想一", "让我想一想哦",
             "让我想一想", "我想一想哦", "想一想哦", "一想哦", "让我想", "让我想一", "我想一想", "想一想", "我想想看哦",
             "我想想看", "想想看哦", "想想看", "想看哦", "我想想", "我再想一下哦"}
    for form in robot:
        assert conversation._THINKING.fullmatch(convert(form, "zh-tw")), form
