"""Check-in conversations in the slot state machine: after the last dose, or as a conversation-only task."""

from medcare_reachy.bridge.app_client import NotAuthorised
from medcare_reachy.bridge.session import CHECKIN_GREETING, CHECKIN_SILENCE
from medcare_reachy.bridge.tests.fakes import Harness, dose, make_task

RECORDED = {"event_id": "ev1", "status": "taken"}


def after_one_dose(checkin=True):
    h = Harness(make_task([dose(1)], microphone=True, checkin=checkin), voice=True, speaker=True)
    h.find_patient()
    h.verify_and_prompt()
    h.see(identity_status="verified", recorded=RECORDED)
    h.tick()
    return h


def test_after_the_last_dose_reachy_chats_then_observes():
    h = after_one_dose()
    assert h.slot.state == "CHECKIN"
    h.tick()   # opens the conversation and speaks the opening line
    assert ("conversation_start", "task-1", "zh-TW") in h.app.calls
    assert h.speaker.said == ["今天感觉怎么样？"]
    assert h.voice.active and h.voice.mode == "chat"
    assert ("monitor_end", "s2") in h.app.calls          # the camera session ends while chatting

    h.voice.speak("我早上去散步了")
    h.tick(1)
    assert ("conversation_turn", "c1", "我早上去散步了") in h.app.calls and h.speaker.said[-1] == "真好"

    h.app.replies = [{"reply": "再見", "speech_text": "再见", "end": True}]
    h.voice.speak("好，拜拜")
    h.tick(1)
    assert ("conversation_end", "c1", "goodbye") in h.app.calls
    assert h.slot.state == "POST_SLOT_OBSERVE" and h.slot.outcomes["checkin"] == "goodbye"
    assert h.voice.active is False


def test_without_checkin_consent_there_is_no_conversation():
    h = after_one_dose(checkin=False)
    assert h.slot.state == "POST_SLOT_OBSERVE"
    assert not h.app.named("conversation_start")


def test_reachy_is_muted_while_it_speaks():
    h = after_one_dose()
    h.tick()
    assert h.voice.max_holds >= 1 and h.voice.holds == 0


def test_silence_ends_the_conversation():
    h = after_one_dose()
    h.tick()
    h.tick(CHECKIN_SILENCE - 1)
    assert h.slot.state == "CHECKIN"
    h.tick(2)
    assert ("conversation_end", "c1", "silence") in h.app.calls and h.slot.state == "POST_SLOT_OBSERVE"


def test_a_risk_answer_ends_with_the_risk_reason():
    h = after_one_dose()
    h.tick()
    h.app.replies = [{"reply": "請打 1925", "speech_text": "请打 1925", "end": True, "risk": True}]
    h.voice.speak("我不想活了")
    h.tick(1)
    assert ("conversation_end", "c1", "risk") in h.app.calls and h.speaker.said[-1] == "请打 1925"


def test_consent_refused_by_the_server_skips_the_conversation():
    h = after_one_dose()
    h.app.fail["conversation_start"] = [NotAuthorised(status=403, detail="checkin_consent_required")]
    h.tick()
    assert h.slot.state == "POST_SLOT_OBSERVE" and h.slot.outcomes["checkin"] == "unavailable"
    assert h.slot.done is False


def test_conversation_only_task_greets_finds_the_patient_and_chats():
    h = Harness(make_task([], reason="checkin", checkin=True), voice=True, speaker=True)
    h.find_patient()
    assert h.speaker.said[0] == CHECKIN_GREETING["zh-TW"]
    assert "wake_greeting" not in h.clips.played
    assert h.slot.state == "CHECKIN" and "in_progress" in h.statuses()
    h.tick()
    h.tick(CHECKIN_SILENCE + 1)
    assert h.slot.state == "WIND_DOWN"
    h.run_until(lambda: h.slot.done)
    assert h.slot.result == "completed"
    assert h.app.named("task_status")[-1][2] == {"outcomes": {"checkin": "silence"}}


def test_stopping_mid_conversation_closes_it():
    h = after_one_dose()
    h.tick()
    h.slot.stop("stop_all")
    h.tick()
    assert ("conversation_end", "c1", "stopped") in h.app.calls and h.slot.done
