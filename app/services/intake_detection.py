import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import asyncio

from app.services.intake_detection_style import (
    EventStyleAggregate,
    EventStyleClassification,
    classify_event_style,
)

# =========================================================
# Configuration
# =========================================================
MOUTH_ZONE_SCALE_X = 1.8
MOUTH_ZONE_SCALE_Y = 2.0

MOUTH_NEAR_DISTANCE_PX = 60
MOUTH_NEAR_DISTANCE_NORM = 0.75
APPROACH_SPEED_THRESHOLD_NORM = 0.05
AT_MOUTH_MIN_DURATION = 0.25
AT_MOUTH_MAX_DURATION = 1.5
LONG_DWELL_PENALTY_THRESHOLD = 2.0
WITHDRAW_DISTANCE_DELTA_NORM = 0.30
ERRATIC_APPROACH_STD_NORM = 0.18
EVENT_COOLDOWN = 1.5

PINCH_RATIO_THRESH = 0.38
LOOSE_GRIP_RATIO_THRESH = 0.75
FLAT_PALM_RATIO_THRESH = 0.95
FINGER_EXTENDED_THRESH = 1.12

FEATURE_BUFFER = 8
OCCLUSION_HEAVY_SCORE = 0.65
OCCLUSION_MODERATE_SCORE = 0.45
OCCLUSION_HEAVY_EVENT_RATIO = 0.30
OCCLUSION_MODERATE_EVENT_RATIO = 0.20
OCCLUSION_HEAVY_PENALTY = 0.20
OCCLUSION_MODERATE_PENALTY = 0.12
OCCLUSION_FLAT_PALM_PENALTY = 0.15

HEAD_TILT_BACK_DELTA_THRESHOLD = 0.08
UNKNOWN_OPEN_MOUTH_NO_DELIVERY_CAP = 0.49
PALM_DUMP_GEOMETRY_REWARD = 0.22
WEAK_PALM_DUMP_NO_LOWER_MOUTH_CAP = 0.49

# Face mesh mouth landmarks
UPPER_LIP = 13
LOWER_LIP = 14
LEFT_MOUTH = 61
RIGHT_MOUTH = 291
NOSE_TIP = 1
CHIN = 152
LEFT_EYE_OUTER = 33
RIGHT_EYE_OUTER = 263
FOREHEAD_PROXY = 10


# =========================================================
# Helper functions
# =========================================================
def euclidean(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    return float(math.hypot(p1[0] - p2[0], p1[1] - p2[1]))


def to_pixel_coords(norm_landmark: dict, width: int, height: int) -> Tuple[int, int]:
    return int(norm_landmark["x"] * width), int(norm_landmark["y"] * height)


def moving_toward(prev_dist: Optional[float], curr_dist: float) -> float:
    if prev_dist is None:
        return 0.0
    return prev_dist - curr_dist


def is_extended(tip: Tuple[int, int], pip: Tuple[int, int], palm_center: Tuple[int, int]) -> bool:
    tip_to_palm = euclidean(tip, palm_center)
    pip_to_palm = euclidean(pip, palm_center)
    return tip_to_palm > pip_to_palm * FINGER_EXTENDED_THRESH


def rect_overlap_ratio(hand_bbox, mouth_rect) -> float:
    hx1, hy1, hx2, hy2 = hand_bbox
    mx1, my1, mx2, my2 = mouth_rect
    ix1, iy1 = max(hx1, mx1), max(hy1, my1)
    ix2, iy2 = min(hx2, mx2), min(hy2, my2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter_area = (ix2 - ix1) * (iy2 - iy1)
    mouth_area = max(1.0, (mx2 - mx1) * (my2 - my1))
    return inter_area / mouth_area


def mouth_activity_points(peak_mouth_open_ratio: float, mouth_open_occurred: bool) -> float:
    if mouth_open_occurred:
        return 0.14
    if peak_mouth_open_ratio >= 0.30:
        return 0.14
    if peak_mouth_open_ratio >= 0.20:
        return 0.08
    if peak_mouth_open_ratio >= 0.10:
        return 0.03
    return 0.0


def compute_mouth_occlusion_score(
    hand_bbox_mouth_overlap_ratio: float,
    palm_center_in_mouth_roi: bool,
    palm_to_mouth_norm: float,
    fingertip_to_mouth_norm: float,
    flat_palm: bool,
) -> Tuple[float, str]:
    score = 0.0
    if hand_bbox_mouth_overlap_ratio >= 0.60:
        score += 0.45
    elif hand_bbox_mouth_overlap_ratio >= 0.30:
        score += 0.30
    elif hand_bbox_mouth_overlap_ratio >= 0.10:
        score += 0.15

    if palm_center_in_mouth_roi:
        score += 0.25

    palm_close = palm_to_mouth_norm <= 1.25
    palm_as_close_as_fingertip = palm_to_mouth_norm <= fingertip_to_mouth_norm + 0.25
    if palm_close and palm_as_close_as_fingertip:
        score += 0.20

    if flat_palm and score >= 0.35:
        score += 0.10

    score = min(score, 1.0)
    if score >= OCCLUSION_HEAVY_SCORE:
        occlusion_type = "heavy_occlusion"
    elif score < OCCLUSION_MODERATE_SCORE and fingertip_to_mouth_norm <= 1.0:
        occlusion_type = "fingertip_contact"
    elif palm_center_in_mouth_roi or hand_bbox_mouth_overlap_ratio >= 0.30:
        occlusion_type = "palm_overlap"
    elif fingertip_to_mouth_norm <= 1.0:
        occlusion_type = "fingertip_contact"
    else:
        occlusion_type = "none"
    return score, occlusion_type


# =========================================================
# State
# =========================================================
@dataclass
class HandTrackState:
    prev_index_tip: Optional[Tuple[int, int]] = None
    prev_mouth_dist: Optional[float] = None
    prev_mouth_dist_norm: Optional[float] = None
    at_mouth_start_time: Optional[float] = None
    was_near_mouth: bool = False
    last_contact_dist: Optional[float] = None
    last_contact_dist_norm: Optional[float] = None
    peak_mouth_contact: float = 0.0
    in_mouth_zone_occurred: bool = False
    mouth_open_occurred: bool = False
    peak_mouth_open_ratio: float = 0.0
    peak_mouth_occlusion_score: float = 0.0
    mouth_occlusion_score_sum: float = 0.0
    mouth_occlusion_frame_count: int = 0
    palm_overlap_frame_count: int = 0
    mouth_visible_frame_count: int = 0
    flat_palm_overlap_frame_count: int = 0
    flat_palm_frame_count: int = 0
    pinch_frame_count: int = 0
    loose_grip_frame_count: int = 0
    holding_object_frame_count: int = 0
    min_fingertip_to_mouth_norm: Optional[float] = None
    min_palm_to_mouth_norm: Optional[float] = None
    min_palm_to_lower_mouth_norm: Optional[float] = None
    partial_withdrawal_seen: bool = False
    head_pitch_at_event_start: Optional[float] = None
    peak_head_pitch_delta: float = 0.0
    min_head_pitch_delta: float = 0.0
    head_tilt_back_detected: bool = False
    head_tilt_back_frame_count: int = 0
    palm_lower_mouth_frame_count: int = 0
    flat_palm_lower_mouth_frame_count: int = 0
    last_seen_time: float = field(default_factory=time.time)

    recent_distances: List[float] = field(default_factory=list)
    recent_approaches: List[float] = field(default_factory=list)
    recent_approaches_norm: List[float] = field(default_factory=list)

    event_confidence: float = 0.0
    event_confidence_time: float = 0.0
    frame_confidence: float = 0.0

    def reset_event_window(self):
        self.at_mouth_start_time = None
        self.was_near_mouth = False
        self.last_contact_dist = None
        self.last_contact_dist_norm = None
        self.peak_mouth_contact = 0.0
        self.in_mouth_zone_occurred = False
        self.mouth_open_occurred = False
        self.peak_mouth_open_ratio = 0.0
        self.peak_mouth_occlusion_score = 0.0
        self.mouth_occlusion_score_sum = 0.0
        self.mouth_occlusion_frame_count = 0
        self.palm_overlap_frame_count = 0
        self.mouth_visible_frame_count = 0
        self.flat_palm_overlap_frame_count = 0
        self.flat_palm_frame_count = 0
        self.pinch_frame_count = 0
        self.loose_grip_frame_count = 0
        self.holding_object_frame_count = 0
        self.min_fingertip_to_mouth_norm = None
        self.min_palm_to_mouth_norm = None
        self.min_palm_to_lower_mouth_norm = None
        self.partial_withdrawal_seen = False
        self.head_pitch_at_event_start = None
        self.peak_head_pitch_delta = 0.0
        self.min_head_pitch_delta = 0.0
        self.head_tilt_back_detected = False
        self.head_tilt_back_frame_count = 0
        self.palm_lower_mouth_frame_count = 0
        self.flat_palm_lower_mouth_frame_count = 0


# =========================================================
# Detector
# =========================================================
class PillIngestionDetector:
    def __init__(self):
        self.hand_states: Dict[str, HandTrackState] = {}
        self.last_event_time = 0.0
        self.last_status = "IDLE"

    def compute_head_pose_proxy(self, face_landmarks: List[dict], width: int, height: int) -> dict:
        nose = to_pixel_coords(face_landmarks[NOSE_TIP], width, height)
        chin = to_pixel_coords(face_landmarks[CHIN], width, height)
        forehead = to_pixel_coords(face_landmarks[FOREHEAD_PROXY], width, height)
        left_eye = to_pixel_coords(face_landmarks[LEFT_EYE_OUTER], width, height)
        right_eye = to_pixel_coords(face_landmarks[RIGHT_EYE_OUTER], width, height)

        face_height = max(1.0, euclidean(forehead, chin))
        eye_width = max(1.0, euclidean(left_eye, right_eye))
        nose_to_chin_y = (chin[1] - nose[1]) / face_height
        forehead_to_nose_y = (nose[1] - forehead[1]) / face_height
        pitch_proxy = nose_to_chin_y - forehead_to_nose_y

        return {
            "head_pitch_proxy": pitch_proxy,
            "face_height": face_height,
            "eye_width": eye_width,
        }

    def compute_mouth_geometry(self, face_landmarks: List[dict], width: int, height: int) -> dict:
        upper = to_pixel_coords(face_landmarks[UPPER_LIP], width, height)
        lower = to_pixel_coords(face_landmarks[LOWER_LIP], width, height)
        left = to_pixel_coords(face_landmarks[LEFT_MOUTH], width, height)
        right = to_pixel_coords(face_landmarks[RIGHT_MOUTH], width, height)

        mouth_center = (
            int((left[0] + right[0]) / 2),
            int((upper[1] + lower[1]) / 2),
        )

        mouth_width = max(10, euclidean(left, right))
        mouth_height = max(8, euclidean(upper, lower))

        mouth_open_ratio = mouth_height / mouth_width
        mouth_open = mouth_open_ratio > 0.35

        zone_w = int(mouth_width * MOUTH_ZONE_SCALE_X)
        zone_h = int(max(mouth_height * 2.2, mouth_width * 0.6) * MOUTH_ZONE_SCALE_Y)

        x1 = mouth_center[0] - zone_w // 2
        y1 = mouth_center[1] - zone_h // 2
        x2 = mouth_center[0] + zone_w // 2
        y2 = mouth_center[1] + zone_h // 2

        lower_y1 = int(mouth_center[1])
        lower_y2 = y2

        lower_rect = (x1, lower_y1, x2, lower_y2)

        return {
            "center": mouth_center,
            "upper": upper,
            "lower": lower,
            "left": left,
            "right": right,
            "width": mouth_width,
            "height": mouth_height,
            "rect": (x1, y1, x2, y2),
            "lower_rect": lower_rect,
            "mouth_open_ratio": mouth_open_ratio,
            "mouth_open": mouth_open,
            **self.compute_head_pose_proxy(face_landmarks, width, height),
        }

    def point_in_rect(self, pt: Tuple[int, int], rect: Tuple[int, int, int, int]) -> bool:
        x1, y1, x2, y2 = rect
        return x1 <= pt[0] <= x2 and y1 <= pt[1] <= y2

    def _buffer_push(self, arr: List[float], value: float, max_len: int = FEATURE_BUFFER):
        arr.append(value)
        if len(arr) > max_len:
            arr.pop(0)

    def compute_hand_features(self, hand_landmarks: List[dict], mouth_geom: dict, width: int, height: int) -> dict:
        wrist = to_pixel_coords(hand_landmarks[0], width, height)
        thumb_tip = to_pixel_coords(hand_landmarks[4], width, height)
        index_mcp = to_pixel_coords(hand_landmarks[5], width, height)
        index_pip = to_pixel_coords(hand_landmarks[6], width, height)
        index_tip = to_pixel_coords(hand_landmarks[8], width, height)
        middle_mcp = to_pixel_coords(hand_landmarks[9], width, height)
        middle_pip = to_pixel_coords(hand_landmarks[10], width, height)
        middle_tip = to_pixel_coords(hand_landmarks[12], width, height)
        ring_pip = to_pixel_coords(hand_landmarks[14], width, height)
        ring_tip = to_pixel_coords(hand_landmarks[16], width, height)
        pinky_mcp = to_pixel_coords(hand_landmarks[17], width, height)
        pinky_pip = to_pixel_coords(hand_landmarks[18], width, height)
        pinky_tip = to_pixel_coords(hand_landmarks[20], width, height)

        palm_center = middle_mcp
        palm_width = euclidean(index_mcp, pinky_mcp)
        palm_length = euclidean(wrist, palm_center)
        palm_size = max(25.0, (palm_width + palm_length) / 2.0)

        index_ext = is_extended(index_tip, index_pip, palm_center)
        middle_ext = is_extended(middle_tip, middle_pip, palm_center)
        ring_ext = is_extended(ring_tip, ring_pip, palm_center)
        pinky_ext = is_extended(pinky_tip, pinky_pip, palm_center)
        extended_count = sum([index_ext, middle_ext, ring_ext, pinky_ext])
        fist = extended_count <= 1

        thumb_index_dist = euclidean(thumb_tip, index_tip)
        pinch_ratio = thumb_index_dist / palm_size
        avg_tip_dist = sum([
            euclidean(index_tip, palm_center),
            euclidean(middle_tip, palm_center),
            euclidean(ring_tip, palm_center),
            euclidean(pinky_tip, palm_center),
        ]) / 4.0
        flat_palm_ratio = avg_tip_dist / palm_size

        flat_palm = not fist and extended_count >= 3 and flat_palm_ratio > FLAT_PALM_RATIO_THRESH
        pinch = not fist and thumb_index_dist < palm_size * PINCH_RATIO_THRESH and index_ext and not flat_palm
        loose_grip = not fist and not pinch and not flat_palm and thumb_index_dist < palm_size * LOOSE_GRIP_RATIO_THRESH and 1 <= extended_count <= 3
        holding_object = pinch or loose_grip

        mouth_center = mouth_geom["center"]
        mouth_rect = mouth_geom["rect"]
        mouth_lower_rect = mouth_geom.get("lower_rect", mouth_rect)

        index_to_mouth = euclidean(index_tip, mouth_center)
        thumb_to_mouth = euclidean(thumb_tip, mouth_center)
        middle_to_mouth = euclidean(middle_tip, mouth_center)
        fingertip_to_mouth = min(index_to_mouth, thumb_to_mouth, middle_to_mouth)

        mouth_width = max(1.0, float(mouth_geom.get("width") or (mouth_rect[2] - mouth_rect[0])))
        hand_points = [wrist, thumb_tip, index_mcp, index_pip, index_tip, middle_mcp, middle_pip, middle_tip, ring_pip, ring_tip, pinky_mcp, pinky_pip, pinky_tip]
        xs = [pt[0] for pt in hand_points]
        ys = [pt[1] for pt in hand_points]
        hand_bbox = (min(xs), min(ys), max(xs), max(ys))
        hand_bbox_mouth_overlap_ratio = rect_overlap_ratio(hand_bbox, mouth_rect)
        palm_center_in_mouth_roi = self.point_in_rect(palm_center, mouth_rect)
        palm_center_in_lower_mouth_roi = self.point_in_rect(palm_center, mouth_lower_rect)
        lower_mouth_center = (
            int((mouth_lower_rect[0] + mouth_lower_rect[2]) / 2),
            int((mouth_lower_rect[1] + mouth_lower_rect[3]) / 2),
        )
        palm_to_mouth_dist = euclidean(palm_center, mouth_center)
        palm_to_mouth_norm = palm_to_mouth_dist / mouth_width
        palm_to_lower_mouth_dist = euclidean(palm_center, lower_mouth_center)
        palm_to_lower_mouth_norm = palm_to_lower_mouth_dist / mouth_width
        fingertip_to_mouth_norm = fingertip_to_mouth / mouth_width
        palm_vs_fingertip_mouth_delta = palm_to_mouth_norm - fingertip_to_mouth_norm
        hand_center_x_offset_from_mouth_norm = abs(palm_center[0] - mouth_center[0]) / mouth_width

        mouth_occlusion_score, occlusion_type = compute_mouth_occlusion_score(
            hand_bbox_mouth_overlap_ratio=hand_bbox_mouth_overlap_ratio,
            palm_center_in_mouth_roi=palm_center_in_mouth_roi,
            palm_to_mouth_norm=palm_to_mouth_norm,
            fingertip_to_mouth_norm=fingertip_to_mouth_norm,
            flat_palm=flat_palm,
        )
        mouth_visible_during_contact = mouth_occlusion_score < OCCLUSION_MODERATE_SCORE and not palm_center_in_mouth_roi

        in_mouth_zone = (
            self.point_in_rect(index_tip, mouth_lower_rect)
            or self.point_in_rect(thumb_tip, mouth_lower_rect)
            or self.point_in_rect(middle_tip, mouth_lower_rect)
        )

        return {
            "wrist": wrist,
            "thumb_tip": thumb_tip,
            "index_tip": index_tip,
            "middle_tip": middle_tip,
            "ring_tip": ring_tip,
            "pinky_tip": pinky_tip,
            "palm_center": palm_center,
            "palm_size": palm_size,
            "index_ext": index_ext,
            "middle_ext": middle_ext,
            "ring_ext": ring_ext,
            "pinky_ext": pinky_ext,
            "extended_count": extended_count,
            "fist": fist,
            "pinch": pinch,
            "loose_grip": loose_grip,
            "flat_palm": flat_palm,
            "holding_object": holding_object,
            "fingertip_to_mouth": fingertip_to_mouth,
            "fingertip_to_mouth_norm": fingertip_to_mouth_norm,
            "palm_to_mouth_norm": palm_to_mouth_norm,
            "palm_to_lower_mouth_norm": palm_to_lower_mouth_norm,
            "palm_vs_fingertip_mouth_delta": palm_vs_fingertip_mouth_delta,
            "hand_center_x_offset_from_mouth_norm": hand_center_x_offset_from_mouth_norm,
            "mouth_occlusion_score": mouth_occlusion_score,
            "occlusion_type": occlusion_type,
            "mouth_visible_during_contact": mouth_visible_during_contact,
            "in_mouth_zone": in_mouth_zone,
            "palm_center_in_mouth_roi": palm_center_in_mouth_roi,
            "palm_center_in_lower_mouth_roi": palm_center_in_lower_mouth_roi,
            "hand_bbox_mouth_overlap_ratio": hand_bbox_mouth_overlap_ratio,
        }

    def update_hand_state(self, hand_id: str, features: dict, current_time: float) -> dict:
        state = self.hand_states.setdefault(hand_id, HandTrackState())
        state.last_seen_time = current_time

        curr_dist = features["fingertip_to_mouth"]
        curr_dist_norm = float(features.get("fingertip_to_mouth_norm", curr_dist / max(1.0, MOUTH_NEAR_DISTANCE_PX)))
        in_mouth_zone_now = features.get("in_mouth_zone", False)
        approach_speed_norm = moving_toward(state.prev_mouth_dist_norm, curr_dist_norm)

        self._buffer_push(state.recent_distances, curr_dist_norm)
        self._buffer_push(state.recent_approaches, approach_speed_norm)
        self._buffer_push(state.recent_approaches_norm, approach_speed_norm)

        avg_approach = sum(state.recent_approaches) / len(state.recent_approaches) if state.recent_approaches else 0.0
        approach_std = self._std(state.recent_approaches) if len(state.recent_approaches) >= 2 else 0.0
        avg_approach_norm = sum(state.recent_approaches_norm) / len(state.recent_approaches_norm) if state.recent_approaches_norm else 0.0
        approach_std_norm = self._std(state.recent_approaches_norm) if len(state.recent_approaches_norm) >= 2 else 0.0

        is_approaching = avg_approach > APPROACH_SPEED_THRESHOLD_NORM
        near_mouth_norm_based = curr_dist_norm < MOUTH_NEAR_DISTANCE_NORM or in_mouth_zone_now
        near_mouth = near_mouth_norm_based

        mouth_contact_norm_based = min(
            1.0,
            max(
                1.0 - curr_dist_norm / max(0.01, MOUTH_NEAR_DISTANCE_NORM * 1.2),
                1.0 if in_mouth_zone_now else 0.0,
            ),
        )
        mouth_contact = mouth_contact_norm_based

        # Track mouth contact window
        if near_mouth:
            if not state.was_near_mouth:
                state.at_mouth_start_time = current_time
                state.last_contact_dist = curr_dist
                state.last_contact_dist_norm = curr_dist_norm
                state.peak_mouth_contact = 0.0
                state.in_mouth_zone_occurred = False
                state.mouth_open_occurred = False
                state.peak_mouth_open_ratio = 0.0
                state.peak_mouth_occlusion_score = 0.0
                state.mouth_occlusion_score_sum = 0.0
                state.mouth_occlusion_frame_count = 0
                state.palm_overlap_frame_count = 0
                state.mouth_visible_frame_count = 0
                state.flat_palm_overlap_frame_count = 0
                state.flat_palm_frame_count = 0
                state.pinch_frame_count = 0
                state.loose_grip_frame_count = 0
                state.holding_object_frame_count = 0
                state.min_fingertip_to_mouth_norm = None
                state.min_palm_to_mouth_norm = None
                state.min_palm_to_lower_mouth_norm = None
                state.partial_withdrawal_seen = False
                state.head_pitch_at_event_start = features.get("head_pitch_proxy")
                state.peak_head_pitch_delta = 0.0
                state.min_head_pitch_delta = 0.0
                state.head_tilt_back_detected = False
                state.head_tilt_back_frame_count = 0
                state.palm_lower_mouth_frame_count = 0
                state.flat_palm_lower_mouth_frame_count = 0
            state.peak_mouth_contact = max(state.peak_mouth_contact, mouth_contact)
            state.in_mouth_zone_occurred = state.in_mouth_zone_occurred or features.get("in_mouth_zone", False)
            state.mouth_open_occurred = state.mouth_open_occurred or features.get("mouth_open", False)
            state.peak_mouth_open_ratio = max(state.peak_mouth_open_ratio, features.get("mouth_open_ratio", 0.0))

            mouth_occlusion_score = float(features.get("mouth_occlusion_score", 0.0) or 0.0)
            state.peak_mouth_occlusion_score = max(state.peak_mouth_occlusion_score, mouth_occlusion_score)
            state.mouth_occlusion_score_sum += mouth_occlusion_score
            state.mouth_occlusion_frame_count += 1
            if features.get("occlusion_type") in {"palm_overlap", "heavy_occlusion"} or features.get("palm_center_in_mouth_roi", False):
                state.palm_overlap_frame_count += 1
            if features.get("mouth_visible_during_contact", True):
                state.mouth_visible_frame_count += 1
            if features.get("flat_palm", False) and mouth_occlusion_score >= OCCLUSION_MODERATE_SCORE:
                state.flat_palm_overlap_frame_count += 1
            if features.get("flat_palm", False):
                state.flat_palm_frame_count += 1
            if features.get("pinch", False):
                state.pinch_frame_count += 1
            if features.get("loose_grip", False):
                state.loose_grip_frame_count += 1
            if features.get("holding_object", False):
                state.holding_object_frame_count += 1
            if features.get("palm_center_in_lower_mouth_roi", False):
                state.palm_lower_mouth_frame_count += 1
            if features.get("flat_palm", False) and features.get("palm_center_in_lower_mouth_roi", False):
                state.flat_palm_lower_mouth_frame_count += 1

            current_pitch = features.get("head_pitch_proxy")
            if current_pitch is not None and state.head_pitch_at_event_start is not None:
                head_pitch_delta = float(current_pitch) - float(state.head_pitch_at_event_start)
                state.peak_head_pitch_delta = max(state.peak_head_pitch_delta, head_pitch_delta)
                state.min_head_pitch_delta = min(state.min_head_pitch_delta, head_pitch_delta)
                if abs(head_pitch_delta) >= HEAD_TILT_BACK_DELTA_THRESHOLD:
                    state.head_tilt_back_detected = True
                    state.head_tilt_back_frame_count += 1

            fingertip_norm = features.get("fingertip_to_mouth_norm")
            if fingertip_norm is not None:
                state.min_fingertip_to_mouth_norm = (
                    float(fingertip_norm)
                    if state.min_fingertip_to_mouth_norm is None
                    else min(state.min_fingertip_to_mouth_norm, float(fingertip_norm))
                )
            palm_norm = features.get("palm_to_mouth_norm")
            if palm_norm is not None:
                state.min_palm_to_mouth_norm = (
                    float(palm_norm)
                    if state.min_palm_to_mouth_norm is None
                    else min(state.min_palm_to_mouth_norm, float(palm_norm))
                )
            palm_lower_norm = features.get("palm_to_lower_mouth_norm")
            if palm_lower_norm is not None:
                state.min_palm_to_lower_mouth_norm = (
                    float(palm_lower_norm)
                    if state.min_palm_to_lower_mouth_norm is None
                    else min(state.min_palm_to_lower_mouth_norm, float(palm_lower_norm))
                )
            withdrawal_delta_norm = None
            if state.last_contact_dist_norm is not None:
                withdrawal_delta_norm = curr_dist_norm - state.last_contact_dist_norm
            if withdrawal_delta_norm is not None and withdrawal_delta_norm > (WITHDRAW_DISTANCE_DELTA_NORM * 0.5):
                state.partial_withdrawal_seen = True

            state.was_near_mouth = True
            self.last_status = "HAND_AT_MOUTH"
        else:
            self.last_status = "IDLE"

        event_detected = False

        # Evaluate only when hand leaves mouth area
        if state.was_near_mouth and not near_mouth:
            peak_mouth_contact = state.peak_mouth_contact
            in_mouth_zone_occurred = state.in_mouth_zone_occurred
            mouth_open_occurred = state.mouth_open_occurred
            peak_mouth_open_ratio = state.peak_mouth_open_ratio
            occlusion_frame_count = max(1, state.mouth_occlusion_frame_count)
            peak_mouth_occlusion_score = state.peak_mouth_occlusion_score
            palm_overlap_frame_count = state.palm_overlap_frame_count
            palm_overlap_ratio_of_event = palm_overlap_frame_count / occlusion_frame_count
            mouth_visible_frame_ratio = state.mouth_visible_frame_count / occlusion_frame_count
            flat_palm_frame_ratio = state.flat_palm_frame_count / occlusion_frame_count
            pinch_frame_ratio = state.pinch_frame_count / occlusion_frame_count
            loose_grip_frame_ratio = state.loose_grip_frame_count / occlusion_frame_count
            holding_object_frame_ratio = state.holding_object_frame_count / occlusion_frame_count
            min_fingertip_to_mouth_norm = state.min_fingertip_to_mouth_norm
            min_palm_to_mouth_norm = state.min_palm_to_mouth_norm
            min_palm_to_lower_mouth_norm = state.min_palm_to_lower_mouth_norm
            partial_withdrawal_seen = state.partial_withdrawal_seen
            head_pitch_at_event_start = state.head_pitch_at_event_start
            peak_head_pitch_delta = state.peak_head_pitch_delta
            min_head_pitch_delta = state.min_head_pitch_delta
            head_tilt_back_detected = state.head_tilt_back_detected
            head_tilt_back_frame_count = state.head_tilt_back_frame_count
            palm_lower_mouth_frame_count = state.palm_lower_mouth_frame_count
            palm_lower_mouth_ratio = palm_lower_mouth_frame_count / occlusion_frame_count
            flat_palm_lower_mouth_ratio = state.flat_palm_lower_mouth_frame_count / occlusion_frame_count

            mouth_open_allowed = peak_mouth_contact > 0.05 or in_mouth_zone_occurred

            dwell = 0.0
            if state.at_mouth_start_time is not None:
                dwell = current_time - state.at_mouth_start_time

            withdrew_enough = False
            if state.last_contact_dist_norm is not None:
                withdrawal_delta_norm = curr_dist_norm - state.last_contact_dist_norm
                withdrew_enough = withdrawal_delta_norm > WITHDRAW_DISTANCE_DELTA_NORM

            cooldown_ok = (current_time - self.last_event_time) > EVENT_COOLDOWN

            event_style_aggregate = EventStyleAggregate(
                dwell=dwell,
                withdrew_enough=withdrew_enough,
                partial_withdrawal_seen=partial_withdrawal_seen,
                peak_mouth_contact=peak_mouth_contact,
                palm_lower_mouth_ratio=palm_lower_mouth_ratio,
                min_palm_to_lower_mouth_norm=min_palm_to_lower_mouth_norm,
                flat_palm_frame_ratio=flat_palm_frame_ratio,
                pinch_frame_ratio=pinch_frame_ratio,
                loose_grip_frame_ratio=loose_grip_frame_ratio,
                holding_object_frame_ratio=holding_object_frame_ratio,
                mouth_visible_frame_ratio=mouth_visible_frame_ratio,
                peak_mouth_occlusion_score=peak_mouth_occlusion_score,
                palm_overlap_ratio_of_event=palm_overlap_ratio_of_event,
                peak_mouth_open_ratio=peak_mouth_open_ratio,
                occlusion_moderate_score=OCCLUSION_MODERATE_SCORE,
            )
            event_style_classification = classify_event_style(event_style_aggregate)
            event_style = event_style_classification.event_style
            likely_mouth_cover = event_style_classification.likely_mouth_cover
            possible_palm_dump_delivery = event_style_classification.possible_palm_dump_delivery
            weak_or_moderate_mouth_activity = event_style_classification.weak_or_moderate_mouth_activity

            confidence = 0.08  # baseline

            # Mouth contact contribution
            mouth_contact_contribution = 0.20 * peak_mouth_contact
            confidence += mouth_contact_contribution

            # Mouth activity (open mouth)
            raw_mouth_activity_contribution = mouth_activity_points(peak_mouth_open_ratio, mouth_open_occurred and mouth_open_allowed)
            mouth_activity_contribution = raw_mouth_activity_contribution
            if likely_mouth_cover and mouth_activity_contribution > 0.04:
                mouth_activity_contribution = 0.04
            confidence += mouth_activity_contribution

            # Dwell contribution
            dwell_contribution = 0.0
            if AT_MOUTH_MIN_DURATION <= dwell <= AT_MOUTH_MAX_DURATION:
                dwell_contribution = 0.10
                confidence += dwell_contribution

            # Trajectory contribution
            trajectory_contribution = 0.0
            if avg_approach > APPROACH_SPEED_THRESHOLD_NORM and approach_std < ERRATIC_APPROACH_STD_NORM:
                trajectory_contribution = 0.12
                confidence += trajectory_contribution

            # Withdrawal contribution
            withdrawal_contribution = 0.0
            if withdrew_enough:
                withdrawal_contribution = 0.15
                confidence += withdrawal_contribution
            elif partial_withdrawal_seen:
                withdrawal_contribution = 0.05
                confidence += withdrawal_contribution

            # Fingertip delivery contribution
            fingertip_delivery_contribution = 0.0
            if (
                peak_mouth_contact >= 0.5
                and peak_mouth_occlusion_score < OCCLUSION_MODERATE_SCORE
                and min_fingertip_to_mouth_norm is not None
                and not likely_mouth_cover
                and not (
                    event_style == "palm_dump_delivery"
                    and flat_palm_frame_ratio >= 0.70
                    and palm_lower_mouth_ratio <= 0.0
                    and min_palm_to_lower_mouth_norm is not None
                    and min_palm_to_lower_mouth_norm > 0.90
                )
            ):
                if (
                    min_palm_to_mouth_norm is not None
                    and min_fingertip_to_mouth_norm <= 1.0
                    and min_palm_to_mouth_norm > min_fingertip_to_mouth_norm + 0.35
                ):
                    fingertip_delivery_contribution = 0.06
                elif min_fingertip_to_mouth_norm <= 1.25:
                    fingertip_delivery_contribution = 0.03
                confidence += fingertip_delivery_contribution

            # Penalties
            if dwell < 0.1 and dwell > 0.0:
                confidence -= 0.08

            if not withdrew_enough:
                confidence -= 0.08

            if dwell > LONG_DWELL_PENALTY_THRESHOLD:
                confidence -= 0.10

            if approach_std > ERRATIC_APPROACH_STD_NORM:
                confidence -= 0.05

            if likely_mouth_cover:
                confidence -= 0.12

            # Palm dump no mouth open penalty
            no_mouth_open_palm_dump_contradiction = bool(
                event_style == "palm_dump_delivery"
                and not mouth_open_occurred
                and peak_mouth_open_ratio <= 0.18
                and not head_tilt_back_detected
                and palm_lower_mouth_ratio <= 0.0
                and min_palm_to_lower_mouth_norm is not None
                and min_palm_to_lower_mouth_norm > 0.80
            )
            if no_mouth_open_palm_dump_contradiction:
                confidence -= 0.12

            # Palm dump geometry reward
            strong_palm_dump_geometry = bool(
                event_style == "palm_dump_delivery"
                and event_style_classification.palm_dump_temporal_order_valid
                and event_style_classification.delivery_like_occlusion
                and palm_lower_mouth_ratio >= 0.25
                and min_palm_to_lower_mouth_norm is not None
                and min_palm_to_lower_mouth_norm <= 0.75
            )
            if strong_palm_dump_geometry:
                confidence += PALM_DUMP_GEOMETRY_REWARD

            # Weak palm dump cap
            weak_palm_dump_no_lower_mouth_geometry = bool(
                event_style == "palm_dump_delivery"
                and flat_palm_frame_ratio >= 0.70
                and palm_lower_mouth_ratio <= 0.0
                and min_palm_to_lower_mouth_norm is not None
                and min_palm_to_lower_mouth_norm > 0.90
                and not strong_palm_dump_geometry
            )
            weak_palm_dump_cap_exception_applied = bool(
                weak_palm_dump_no_lower_mouth_geometry
                and head_tilt_back_detected
                and raw_mouth_activity_contribution >= 0.08
                and withdrew_enough
                and not event_style_classification.delivery_like_occlusion
            )
            if (
                weak_palm_dump_no_lower_mouth_geometry
                and not weak_palm_dump_cap_exception_applied
                and confidence > WEAK_PALM_DUMP_NO_LOWER_MOUTH_CAP
            ):
                confidence = WEAK_PALM_DUMP_NO_LOWER_MOUTH_CAP

            # Mouth occlusion penalty
            mouth_occlusion_penalty = 0.0
            if peak_mouth_occlusion_score >= 0.75 and palm_overlap_ratio_of_event >= 0.35:
                mouth_occlusion_penalty = 0.25
            elif peak_mouth_occlusion_score >= OCCLUSION_HEAVY_SCORE and palm_overlap_ratio_of_event >= 0.25:
                mouth_occlusion_penalty = 0.20
            elif peak_mouth_occlusion_score >= OCCLUSION_MODERATE_SCORE and palm_overlap_ratio_of_event >= 0.20:
                mouth_occlusion_penalty = 0.12

            if mouth_occlusion_penalty > 0.0 and possible_palm_dump_delivery:
                mouth_occlusion_penalty = 0.0
            elif event_style == "pinch_delivery" and withdrew_enough and head_tilt_back_detected and palm_lower_mouth_ratio >= 0.50:
                mouth_occlusion_penalty = 0.0

            if mouth_occlusion_penalty > 0.0:
                confidence -= mouth_occlusion_penalty

            # Unknown open mouth cap
            unknown_open_mouth_no_delivery_geometry = bool(
                event_style == "unknown"
                and mouth_open_occurred
                and peak_mouth_open_ratio >= 0.30
                and not event_style_classification.palm_dump_temporal_order_valid
                and not event_style_classification.delivery_like_occlusion
                and palm_lower_mouth_ratio <= 0.0
                and (min_palm_to_lower_mouth_norm is None or min_palm_to_lower_mouth_norm > 1.25)
                and not head_tilt_back_detected
            )
            if unknown_open_mouth_no_delivery_geometry and confidence > UNKNOWN_OPEN_MOUTH_NO_DELIVERY_CAP:
                confidence = UNKNOWN_OPEN_MOUTH_NO_DELIVERY_CAP

            # Clamp confidence
            confidence = max(0.0, min(confidence, 1.0))

            # Update event confidence
            state.event_confidence = confidence
            state.event_confidence_time = current_time

            # Check if ingestion detected
            if (
                peak_mouth_contact >= 0.5
                and confidence >= 0.38
                and mouth_open_occurred
                and mouth_open_allowed
                and cooldown_ok
                and not unknown_open_mouth_no_delivery_geometry
                and not (weak_palm_dump_no_lower_mouth_geometry and not weak_palm_dump_cap_exception_applied)
            ):
                self.last_event_time = current_time
                self.last_status = "LIKELY_INGESTION"
                event_detected = True

            state.reset_event_window()

        # Frame-level confidence for real-time feedback
        time_since_event = current_time - state.event_confidence_time
        event_decay = max(0.0, state.event_confidence * (1.0 - time_since_event * 0.3))
        frame_confidence = event_decay * 0.8

        if near_mouth:
            frame_confidence += 0.06
        mouth_open_frame_allowed = mouth_contact > 0.05 or features.get("in_mouth_zone", False) or state.in_mouth_zone_occurred
        if features.get("mouth_open", False) and mouth_open_frame_allowed:
            frame_confidence += 0.05
        if mouth_contact >= 0.5 and features.get("mouth_open", False):
            frame_confidence += 0.12

        frame_confidence = max(0.0, min(frame_confidence, 1.0))
        state.frame_confidence = frame_confidence

        state.prev_mouth_dist = curr_dist
        state.prev_mouth_dist_norm = curr_dist_norm

        return {
            "event_detected": event_detected,
            "event_confidence": state.event_confidence,
            "frame_confidence": frame_confidence,
            "status": self.last_status,
            "mouth_open": features.get("mouth_open", False),
            "hand_near_mouth": near_mouth,
        }

    @staticmethod
    def _std(values: List[float]) -> float:
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        variance = sum((x - mean) ** 2 for x in values) / len(values)
        return math.sqrt(variance)


# =========================================================
# Service Singleton
# =========================================================
class IntakeDetectionService:
    _instance: Optional["IntakeDetectionService"] = None
    _available: bool = False

    def __init__(self):
        self._detectors: Dict[str, PillIngestionDetector] = {}
        self._last_active: Dict[str, float] = {}
        self._lock = asyncio.Lock()
        IntakeDetectionService._available = True

    @classmethod
    def get_instance(cls) -> "IntakeDetectionService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _key(self, u_id: int, session_id: str) -> str:
        return f"{u_id}:{session_id}"

    def _cleanup_stale(self, current_time: float):
        stale_threshold = 300  # 5 minutes
        stale_keys = [
            k for k, t in self._last_active.items()
            if current_time - t > stale_threshold
        ]
        for k in stale_keys:
            self._detectors.pop(k, None)
            self._last_active.pop(k, None)

    async def process_frame(self, u_id: int, session_id: str, payload: dict) -> dict:
        key = self._key(u_id, session_id)
        current_time = time.time()

        async with self._lock:
            self._cleanup_stale(current_time)
            self._last_active[key] = current_time

            if key not in self._detectors:
                self._detectors[key] = PillIngestionDetector()

            detector = self._detectors[key]

        width = payload.get("width", 640)
        height = payload.get("height", 480)
        face_landmarks = payload.get("face_landmarks", [])
        hand_landmarks = payload.get("hand_landmarks", [])
        timestamp = payload.get("timestamp", current_time)

        if not face_landmarks:
            return {
                "confidence": 0.0,
                "mouth_open": False,
                "hand_near_mouth": False,
                "status": "NO_FACE",
                "event_detected": False,
            }

        mouth_geom = detector.compute_mouth_geometry(face_landmarks, width, height)

        best_result = {
            "confidence": 0.0,
            "mouth_open": mouth_geom["mouth_open"],
            "hand_near_mouth": False,
            "status": detector.last_status,
            "event_detected": False,
        }

        for i, hand_lm in enumerate(hand_landmarks):
            hand_id = f"hand_{i}"
            features = detector.compute_hand_features(hand_lm, mouth_geom, width, height)
            result = detector.update_hand_state(hand_id, features, timestamp)

            if result["frame_confidence"] > best_result["confidence"]:
                best_result = {
                    "confidence": result["frame_confidence"],
                    "mouth_open": result["mouth_open"],
                    "hand_near_mouth": result["hand_near_mouth"],
                    "status": result["status"],
                    "event_detected": result["event_detected"],
                }

            if result["event_detected"]:
                best_result["event_detected"] = True
                best_result["confidence"] = result["event_confidence"]
                break

        return best_result

    async def end_session(self, u_id: int, session_id: str):
        key = self._key(u_id, session_id)
        async with self._lock:
            self._detectors.pop(key, None)
            self._last_active.pop(key, None)
