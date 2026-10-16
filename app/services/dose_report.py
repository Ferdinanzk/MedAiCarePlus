"""How the AI judged a dose, in words for family on LINE (the "dose taken" message and the confirmation request).

The detector's confidence comes in fixed bands (0.82 confirmed; 0.56 or 0.48 uncertain), not a calibrated
probability, so the percentage always carries a word saying how far to trust it. HIGH matches the auto-record
threshold (AUTO_CONFIRM_MIN); anything between UNCERTAIN and HIGH is what the detector itself calls uncertain.
"""

HIGH, UNCERTAIN = 0.75, 0.45
NEEDED_FPS = 12   # the server trusts a detection only at this camera rate or above (monitor_service.FPS_MIN)


def number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def ai_estimate(score: float | None, evidence: dict | None, *, camera_used: bool = True) -> dict:
    """{"zh", "en"}: the AI's estimate that the dose was taken; {"notes_zh", "notes_en"}: what qualifies it;
    "scored": whether a percentage was given (then the footnote about not seeing the pill applies)."""
    evidence = evidence if isinstance(evidence, dict) else {}
    notes_zh, notes_en = [], []
    if score is not None:
        percent = round(max(0.0, min(1.0, score)) * 100)
        level_zh, level_en = (("高", "high") if score >= HIGH else ("不確定", "uncertain") if score >= UNCERTAIN
                              else ("低", "low"))
        zh, en = f"{percent}%（{level_zh}）", f"{percent}% ({level_en})"
    elif evidence.get("camera") == "no_event":
        zh, en = "無法判斷（鏡頭沒有看到服藥動作）", "not determined (the camera did not see the dose being taken)"
    elif not camera_used:
        zh, en = "無（沒有使用鏡頭）", "none (no camera was used)"
    else:
        zh, en = "無法判斷", "not determined"
    if evidence.get("degraded"):
        fps = number(evidence.get("landmark_fps"))
        if fps is not None:
            notes_zh.append(f"當時鏡頭畫面每秒只有 {fps:g} 張（需要 {NEEDED_FPS} 張），AI 判斷僅供參考")
            notes_en.append(f"the camera ran at only {fps:g} frames per second (it needs {NEEDED_FPS}), "
                            f"so treat the AI estimate with caution")
        else:
            notes_zh.append("當時鏡頭畫面不夠順暢，AI 判斷僅供參考")
            notes_en.append("the camera stream was not smooth enough, so treat the AI estimate with caution")
    if evidence.get("said_done"):
        notes_zh.append("本人有說「吃完了」")
        notes_en.append("the patient said they had finished")
    return {"zh": zh, "en": en, "notes_zh": notes_zh, "notes_en": notes_en, "scored": score is not None}


FOOTNOTE_ZH = "（AI 只看得到手把東西送到嘴邊的動作，看不到藥丸本身。）"
FOOTNOTE_EN = "(The AI sees hand-to-mouth movement only; it cannot see the pill itself.)"

# The GPT-Live agent's verify_medication result (tool_registry.py): what the label text read says.
VERIFICATION_WORDS = {
    "match": ("標籤符合預期藥物", "the label matches the expected medicine"),
    "no_match": ("⚠️ 標籤與預期藥物不符，可能拿錯藥", "⚠️ the label does not match the expected medicine: possibly the "
                                                 "wrong medicine"),
    "uncertain": ("無法辨識（沒有讀到標籤文字）", "could not be verified (no label text was read)"),
}
# The robot's or the server's facial-expression reading during the event: only a clear negative one is worth a
# line to family; everyday expressions never change what is sent.
NEGATIVE_EMOTIONS = {"sad": ("難過", "sad"), "angry": ("生氣", "angry"), "fear": ("害怕", "afraid"),
                     "disgust": ("不舒服", "uncomfortable")}
EMOTION_MIN = 0.5


def verification_words(verification: dict | None) -> tuple[str, str] | None:
    """(zh, en) for a verify_medication result dict ({result, expected_medication, names_read}), or None."""
    if not isinstance(verification, dict):
        return None
    words = VERIFICATION_WORDS.get(verification.get("result"))
    if words is None:
        return None
    zh, en = words
    names = [str(n) for n in (verification.get("names_read") or []) if n][:3]
    if names and verification.get("result") == "no_match":
        zh += f"（讀到：{'、'.join(names)}）"
        en += f" (read: {', '.join(names)})"
    return zh, en


def emotion_note(emotion: dict | None) -> tuple[str, str] | None:
    """(zh, en) when the emotion reading is a clear negative one ({top, confidence}), else None."""
    if not isinstance(emotion, dict):
        return None
    top, confidence = emotion.get("top"), number(emotion.get("confidence"))
    words = NEGATIVE_EMOTIONS.get(str(top or "").lower())
    if words is None or confidence is None or confidence < EMOTION_MIN:
        return None
    percent = round(confidence * 100)
    return (f"😟 情緒：鏡頭判斷當時看起來{words[0]}（可能性 {percent}%），請多關心",
            f"😟 Mood: the camera read the patient as {words[1]} at the time ({percent}%); a kind word may help")
