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
