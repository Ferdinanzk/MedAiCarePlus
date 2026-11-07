"""Face login decision rules: which face is the main one in a frame, and whether several frames log someone in.

Pure functions. The settings (thresholds, mode) are passed in by the caller; nothing here reads config.
Reason codes: match, unknown, no_face, come_closer, two_faces, not_live.
"""
from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class FaceBox:
    x: float
    y: float
    w: float
    h: float
    confidence: float

    @property
    def area(self) -> float:
        return max(self.w, 0) * max(self.h, 0)


@dataclass(frozen=True)
class FrameVerdict:
    code: str
    label: str | None = None
    distance: float | None = None
    liveness: float | None = None
    face_count: int = 0


@dataclass(frozen=True)
class LoginDecision:
    identified: bool
    label: str | None
    distance: float | None
    reason: str
    frames: int
    face_count: int = 0   # the most faces seen in any one frame


# Order used to break ties between non-match codes (first wins).
_TIE_ORDER = ("come_closer", "no_face", "unknown")


def main_face(boxes: list[FaceBox], frame_w: int, frame_h: int, *, min_area_ratio: float,
              second_ratio: float) -> tuple[str, FaceBox | None]:
    if not boxes:
        return "no_face", None
    ranked = sorted(boxes, key=lambda b: b.area, reverse=True)
    main = ranked[0]
    if len(ranked) > 1 and ranked[1].area >= second_ratio * main.area:
        return "two_faces", main
    if main.area < min_area_ratio * frame_w * frame_h:
        return "come_closer", main
    return "ok", main


def decide(verdicts: list[FrameVerdict], *, min_matches: int, liveness_mode: str,
           liveness_threshold: float) -> LoginDecision:
    frames = len(verdicts)
    face_count = max((v.face_count for v in verdicts), default=0)

    def fail(reason: str) -> LoginDecision:
        return LoginDecision(False, None, None, reason, frames, face_count)

    if any(v.code == "two_faces" for v in verdicts):
        return fail("two_faces")

    if liveness_mode == "block":
        # Only frames that judged a face (match / unknown) carry a liveness score.
        judged = [v for v in verdicts if v.code in ("match", "unknown")]
        if any(v.liveness is None or v.liveness < liveness_threshold for v in judged):
            return fail("not_live")

    matches = [v for v in verdicts if v.code == "match"]
    if len({v.label for v in matches}) > 1:
        return fail("unknown")

    if len(matches) >= min_matches:
        return LoginDecision(True, matches[0].label,
                             min(v.distance for v in matches if v.distance is not None),
                             "match", frames, face_count)

    counts = Counter(v.code for v in verdicts if v.code != "match")
    if not counts:
        return fail("unknown")
    best = max(counts.values())
    tied = [c for c, n in counts.items() if n == best]
    tied.sort(key=lambda c: _TIE_ORDER.index(c) if c in _TIE_ORDER else len(_TIE_ORDER))
    return fail(tied[0])
