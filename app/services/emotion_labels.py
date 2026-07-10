"""Canonical emotion vocabulary shared by inference, APIs, storage and alerts."""

APP_EMOTION_LABELS = frozenset({
    "happy", "sad", "angry", "neutral", "surprised", "disgust",
})
ALERT_WORTHY_EMOTIONS = frozenset({"sad", "angry", "disgust"})

_ALIASES = {
    "happy": "happy",
    "sad": "sad",
    "angry": "angry",
    "neutral": "neutral",
    "surprise": "surprised",
    "surprised": "surprised",
    # The checkpoint's unsafe/non-product class is treated as surprise.
    "ahegao": "surprised",
    "disgust": "disgust",
}


def normalize_emotion_label(label: str | None, *, default: str | None = None) -> str | None:
    """Return a canonical lowercase app label, never a raw model label."""
    normalized = _ALIASES.get(str(label or "").strip().lower())
    if normalized is not None:
        return normalized
    if default is not None and default not in APP_EMOTION_LABELS:
        raise ValueError(f"Invalid default emotion label: {default}")
    return default


def is_alert_worthy_emotion(label: str | None) -> bool:
    return normalize_emotion_label(label) in ALERT_WORTHY_EMOTIONS
