import math
import time
import hashlib
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Tuple
import asyncio

from app.intake_v1 import config as v1_config
from app.services.intake_detection_style import (
    EventStyleAggregate,
    EventStyleClassification,
    classify_event_style,
)
from app.services.intake_temporal import Observation, TemporalIntakePipeline

# =========================================================
# Configuration
# =========================================================
MOUTH_ZONE_SCALE_X = 1.8
MOUTH_ZONE_SCALE_Y = 2.0

MOUTH_NEAR_DISTANCE_PX = 60
MOUTH_NEAR_DISTANCE_NORM = 0.75
APPROACH_SPEED_THRESHOLD = 4.0
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

# Decision band thresholds. Patient-safety design: auto-log ("confirmed") a
# delivery, route ambiguous events to "uncertain" so the patient confirms with
# one tap, and ignore everything else ("none").
# CONFIRM_THRESHOLD leans POSITIVE: genuine intakes (which scored as low as ~0.45
# on the dev benchmark, and lower still under live camera lag) should auto-confirm
# rather than getting stuck below the bar. Lowered 0.45 -> 0.40 so real intakes
# confirm more easily under live-camera conditions; UNCERTAIN_FLOOR lowered
# 0.38 -> 0.33 so more borderline events surface as one-tap prompts. False
# positives are still held back NOT by this threshold but by the semantic
# contradiction flags (`unknown_open_mouth_no_delivery`, wide-yawn mouth-cover)
# and the base gates (peak_mouth_contact >= 0.5 + mouth-open/recovery), which are
# unchanged.
CONFIRM_THRESHOLD = 0.40
UNCERTAIN_FLOOR = 0.33
WIDE_OPEN_MOUTH_COVER_RATIO = 0.75

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
        self.peak_event_confidence = 0.0
        self.highest_5s_confidence = 0.0

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
        raw_mouth_height = euclidean(upper, lower)
        mouth_height = max(8, raw_mouth_height)

        mouth_open_ratio = raw_mouth_height / mouth_width
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
            "raw_height": raw_mouth_height,
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

        # Resolution-adaptive mouth-near threshold
        # eye_width (distance between landmarks 33 and 263) scales with resolution
        face_width_px = max(1.0, mouth_geom.get("eye_width", 0.0))
        reference_face_width = 200.0
        mouth_near_distance_px = MOUTH_NEAR_DISTANCE_PX * (face_width_px / reference_face_width)
        mouth_near_distance_px = max(30.0, min(120.0, mouth_near_distance_px))

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
            "mouth_open": mouth_geom.get("mouth_open", False),
            "mouth_open_ratio": mouth_geom.get("mouth_open_ratio", 0.0),
            "head_pitch_proxy": mouth_geom.get("head_pitch_proxy"),
            "head_face_height": mouth_geom.get("face_height"),
            "head_eye_width": mouth_geom.get("eye_width"),
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
            "mouth_near_distance_px": mouth_near_distance_px,
        }

    def update_hand_state(self, hand_id: str, features: dict, current_time: float) -> dict:
        state = self.hand_states.setdefault(hand_id, HandTrackState())
        state.last_seen_time = current_time

        curr_dist = features["fingertip_to_mouth"]
        mouth_near_distance_px = features.get("mouth_near_distance_px", MOUTH_NEAR_DISTANCE_PX)
        curr_dist_norm = float(features.get("fingertip_to_mouth_norm", curr_dist / max(1.0, mouth_near_distance_px)))
        in_mouth_zone_now = features.get("in_mouth_zone", False)
        approach_speed_px_based = moving_toward(state.prev_mouth_dist, curr_dist)
        approach_speed_norm = moving_toward(state.prev_mouth_dist_norm, curr_dist_norm)

        self._buffer_push(state.recent_distances, curr_dist_norm)
        self._buffer_push(state.recent_approaches, approach_speed_norm)
        self._buffer_push(state.recent_approaches_norm, approach_speed_norm)

        avg_approach = sum(state.recent_approaches) / len(state.recent_approaches) if state.recent_approaches else 0.0
        approach_std = self._std(state.recent_approaches) if len(state.recent_approaches) >= 2 else 0.0
        avg_approach_norm = sum(state.recent_approaches_norm) / len(state.recent_approaches_norm) if state.recent_approaches_norm else 0.0
        approach_std_norm = self._std(state.recent_approaches_norm) if len(state.recent_approaches_norm) >= 2 else 0.0

        is_approaching = avg_approach > APPROACH_SPEED_THRESHOLD_NORM
        near_mouth_px_based = curr_dist < mouth_near_distance_px or in_mouth_zone_now
        near_mouth_norm_based = curr_dist_norm < MOUTH_NEAR_DISTANCE_NORM or in_mouth_zone_now
        near_mouth = near_mouth_norm_based

        mouth_contact_px_based = min(
            1.0,
            max(
                1.0 - curr_dist / max(1.0, mouth_near_distance_px * 1.2),
                1.0 if in_mouth_zone_now else 0.0,
            ),
        )
        mouth_contact_norm_based = min(
            1.0,
            max(
                1.0 - curr_dist_norm / max(0.01, MOUTH_NEAR_DISTANCE_NORM * 1.2),
                1.0 if in_mouth_zone_now else 0.0,
            ),
        )
        mouth_contact = mouth_contact_norm_based
        mouth_contact_delta_norm_minus_px = mouth_contact_norm_based - mouth_contact_px_based

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

        # Defaults for frames that do not close an at-mouth event. The debug dict
        # below is built on every frame, so these must exist even when the hand
        # has not just left the mouth area.
        peak_mouth_contact = state.peak_mouth_contact
        in_mouth_zone_occurred = state.in_mouth_zone_occurred
        mouth_open_occurred = state.mouth_open_occurred
        mouth_open_allowed = False
        peak_mouth_open_ratio = state.peak_mouth_open_ratio
        dwell = 0.0
        withdrew_enough = False
        withdrew_enough_px_based = False
        mouth_contact_contribution = 0.0
        mouth_activity_contribution = 0.0
        dwell_contribution = 0.0
        trajectory_contribution = 0.0
        withdrawal_contribution = 0.0
        fingertip_delivery_contribution = 0.0
        mouth_occlusion_penalty = 0.0
        missing_mouth_open_soft_penalty = 0.0
        no_mouth_open_palm_dump_contradiction = False
        closed_mouth_strong_palm_dump_recovery = False
        closed_mouth_supported_pinch_recovery = False
        strong_palm_dump_geometry = False
        weak_palm_dump_no_lower_mouth_geometry = False
        weak_palm_dump_cap_exception_applied = False

        unknown_open_mouth_no_delivery_geometry = False
        peak_mouth_occlusion_score = state.peak_mouth_occlusion_score

        palm_overlap_ratio_of_event = 0.0
        mouth_visible_frame_ratio = 0.0
        flat_palm_frame_ratio = 0.0
        pinch_frame_ratio = 0.0
        loose_grip_frame_ratio = 0.0
        holding_object_frame_ratio = 0.0
        possible_palm_dump_delivery = False
        likely_mouth_cover = False
        event_style = "none"
        min_fingertip_to_mouth_norm = state.min_fingertip_to_mouth_norm
        min_palm_to_mouth_norm = state.min_palm_to_mouth_norm
        head_pitch_at_event_start = state.head_pitch_at_event_start
        peak_head_pitch_delta = state.peak_head_pitch_delta
        min_head_pitch_delta = state.min_head_pitch_delta
        head_tilt_back_detected = state.head_tilt_back_detected
        head_tilt_back_frame_count = state.head_tilt_back_frame_count
        min_palm_to_lower_mouth_norm = state.min_palm_to_lower_mouth_norm
        palm_lower_mouth_ratio = 0.0
        flat_palm_lower_mouth_ratio = 0.0
        partial_withdrawal_seen = state.partial_withdrawal_seen
        confidence = state.event_confidence
        decision = "none"
        decision_reason = ""
        safety_contradiction = False
        event_window_closed = bool(state.was_near_mouth and not near_mouth)

        # Evaluate only when hand leaves mouth area
        if event_window_closed:

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

            withdrew_enough_px_based = False
            if state.last_contact_dist is not None:
                withdrew_enough_px_based = (curr_dist - state.last_contact_dist) > 25

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

            # Mouth opening is positive evidence only. A weak/closed mouth should
            # not subtract points by itself; non-intake mouth-cover/talking cases
            # must be blocked by explicit cover/unknown-delivery contradictions.
            no_mouth_open_palm_dump_contradiction = False

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

            closed_mouth_strong_palm_dump_recovery = bool(
                not mouth_open_occurred
                and event_style == "palm_dump_delivery"
                and confidence >= 0.80
                and strong_palm_dump_geometry
                and withdrew_enough
                and partial_withdrawal_seen
                and head_tilt_back_detected
                and not likely_mouth_cover
                and not unknown_open_mouth_no_delivery_geometry
            )
            closed_mouth_supported_pinch_recovery = bool(
                not mouth_open_occurred
                and event_style == "pinch_delivery"
                and confidence >= 0.65
                and mouth_open_allowed
                and withdrew_enough
                and partial_withdrawal_seen
                and not likely_mouth_cover
                and not unknown_open_mouth_no_delivery_geometry
            )
            mouth_open_gate_passed = bool(mouth_open_occurred and mouth_open_allowed)
            weak_mouth_recovery_passed = bool(
                closed_mouth_strong_palm_dump_recovery
                or closed_mouth_supported_pinch_recovery
            )

            # Decide the event band. Keep the Step-B mouth-open gate for ordinary
            # events; recover only the two weak-mouth delivery patterns that have
            # explicit geometry/withdrawal support.
            state.event_confidence = confidence
            state.event_confidence_time = current_time

            # Hard gates shared by confirmed and uncertain bands. A real
            # hand-at-mouth contact with either an open mouth or a supported
            # weak-mouth recovery is enough to *consider* the event; whether it
            # auto-logs or merely prompts is decided below.
            base_gates_passed = bool(
                peak_mouth_contact >= 0.5
                and (mouth_open_gate_passed or weak_mouth_recovery_passed)
            )

            # Soft safety contradiction: geometry contradicts a clean delivery, so
            # even a high-confidence event is downgraded to "uncertain" (requires
            # confirmation) rather than auto-logged. Never silently marks a dose.
            safety_contradiction = bool(
                unknown_open_mouth_no_delivery_geometry
                or (likely_mouth_cover and peak_mouth_open_ratio >= WIDE_OPEN_MOUTH_COVER_RATIO)
            )

            # Weak palm-dump geometry (flat palm that never reaches the lower
            # mouth) blocks AUTO-LOGGING only. It still surfaces as "uncertain"
            # so a genuine but ambiguous delivery can be confirmed by the patient
            # instead of being silently dropped.
            weak_palm_dump_blocks_confirm = bool(
                weak_palm_dump_no_lower_mouth_geometry
                and not weak_palm_dump_cap_exception_applied
            )

            confirmed_blockers = bool(safety_contradiction or weak_palm_dump_blocks_confirm)

            would_confirm = bool(
                base_gates_passed
                and confidence >= CONFIRM_THRESHOLD
                and cooldown_ok
                and not confirmed_blockers
            )

            if would_confirm:
                decision = "confirmed"
                decision_reason = "high_confidence_delivery"
                self.last_event_time = current_time
                self.last_status = "LIKELY_INGESTION"
                event_detected = True
            elif base_gates_passed and confidence >= UNCERTAIN_FLOOR:
                decision = "uncertain"
                if unknown_open_mouth_no_delivery_geometry:
                    decision_reason = "unknown_open_mouth_no_delivery"
                elif safety_contradiction:
                    decision_reason = "wide_open_mouth_cover"
                elif weak_palm_dump_blocks_confirm:
                    decision_reason = "weak_palm_dump_needs_confirmation"
                elif confidence < CONFIRM_THRESHOLD:
                    decision_reason = "below_confirm_threshold"
                else:
                    decision_reason = "needs_confirmation"
                self.last_status = "POSSIBLE_INGESTION"
            else:
                decision = "none"
                decision_reason = ""

            state.reset_event_window()

        # Track peak confidence for telemetry / future use, but do NOT use it as a
        # decision signal. The event-detection gate is now contact/score based;
        # mouth opening contributes positive score but is not a hard TRUE gate.
        # This avoids rejecting true delivery when the mouth is weakly open or
        # temporarily occluded, while keeping high peak confidence out of the
        # video-level decision path.

        if state.event_confidence > self.peak_event_confidence:
            self.peak_event_confidence = state.event_confidence

        # Build event debug dictionary for post-hoc analysis
        event_debug = {
            "event_detected": event_detected,
            "event_window_closed": event_window_closed,
            "event_confidence": state.event_confidence,
            "fingertip_to_mouth": curr_dist,
            "mouth_contact": peak_mouth_contact,
            "peak_mouth_contact": peak_mouth_contact,
            "mouth_contact_px_based": mouth_contact_px_based,
            "mouth_contact_norm_based": mouth_contact_norm_based,
            "mouth_contact_delta_norm_minus_px": mouth_contact_delta_norm_minus_px,
            "near_mouth_px_based": near_mouth_px_based,
            "near_mouth_norm_based": near_mouth_norm_based,
            "mouth_open": mouth_open_occurred,
            "mouth_open_allowed": mouth_open_allowed,
            "mouth_open_ratio": peak_mouth_open_ratio,
            "peak_mouth_open_ratio": peak_mouth_open_ratio,
            "in_mouth_zone": in_mouth_zone_occurred,
            "in_mouth_zone_occurred": in_mouth_zone_occurred,
            "dwell": dwell,
            "withdrew_enough": withdrew_enough,
            "withdrew_enough_px_based": withdrew_enough_px_based,
            "avg_approach": avg_approach,
            "approach_std": approach_std,
            "avg_approach_norm": avg_approach_norm,
            "approach_std_norm": approach_std_norm,
            "baseline_contribution": 0.08,
            "mouth_contact_contribution": mouth_contact_contribution,
            "mouth_open_contribution": mouth_activity_contribution,
            "dwell_contribution": dwell_contribution,
            "trajectory_contribution": trajectory_contribution,
            "withdrawal_contribution": withdrawal_contribution,
            "fingertip_delivery_contribution": fingertip_delivery_contribution,
            "mouth_activity_cap_penalty": 0.0,
            "missing_mouth_open_soft_penalty": -missing_mouth_open_soft_penalty,
            "flat_palm_cover_penalty": -0.12 if likely_mouth_cover else 0.0,
            "no_mouth_open_palm_dump_penalty": -0.12 if no_mouth_open_palm_dump_contradiction else 0.0,
            "closed_mouth_strong_palm_dump_recovery": closed_mouth_strong_palm_dump_recovery,
            "closed_mouth_supported_pinch_recovery": closed_mouth_supported_pinch_recovery,
            "strong_palm_dump_geometry": strong_palm_dump_geometry,
            "palm_dump_geometry_reward": PALM_DUMP_GEOMETRY_REWARD if strong_palm_dump_geometry else 0.0,
            "weak_palm_dump_no_lower_mouth_geometry": weak_palm_dump_no_lower_mouth_geometry,
            "weak_palm_dump_cap_exception_applied": weak_palm_dump_cap_exception_applied,
            "mouth_occlusion_penalty": -mouth_occlusion_penalty,
            "unknown_open_mouth_no_delivery_geometry": unknown_open_mouth_no_delivery_geometry,
            "peak_mouth_occlusion_score": peak_mouth_occlusion_score,
            "palm_overlap_ratio_of_event": palm_overlap_ratio_of_event,
            "mouth_visible_frame_ratio": mouth_visible_frame_ratio,
            "flat_palm_frame_ratio": flat_palm_frame_ratio,
            "pinch_frame_ratio": pinch_frame_ratio,
            "loose_grip_frame_ratio": loose_grip_frame_ratio,
            "holding_object_frame_ratio": holding_object_frame_ratio,
            "possible_palm_dump_delivery": possible_palm_dump_delivery,
            "likely_mouth_cover": likely_mouth_cover,
            "event_style": event_style,
            "min_fingertip_to_mouth_norm": min_fingertip_to_mouth_norm,
            "min_palm_to_mouth_norm": min_palm_to_mouth_norm,
            "head_pitch_at_event_start": head_pitch_at_event_start,
            "peak_head_pitch_delta": peak_head_pitch_delta,
            "min_head_pitch_delta": min_head_pitch_delta,
            "head_tilt_back_detected": head_tilt_back_detected,
            "head_tilt_back_frame_count": head_tilt_back_frame_count,
            "palm_center_in_lower_mouth_roi": features.get("palm_center_in_lower_mouth_roi", False),
            "palm_to_lower_mouth_norm": features.get("palm_to_lower_mouth_norm"),
            "min_palm_to_lower_mouth_norm": min_palm_to_lower_mouth_norm,
            "palm_lower_mouth_ratio": palm_lower_mouth_ratio,
            "flat_palm_lower_mouth_ratio": flat_palm_lower_mouth_ratio,
            "partial_withdrawal_seen": partial_withdrawal_seen,
            "frame_confidence": state.frame_confidence,
            "positive_points_total": (
                0.08
                + mouth_contact_contribution
                + mouth_activity_contribution
                + dwell_contribution
                + trajectory_contribution
                + withdrawal_contribution
                + fingertip_delivery_contribution
                + (PALM_DUMP_GEOMETRY_REWARD if strong_palm_dump_geometry else 0.0)
            ),
            "penalty_points_total": (
                (0.08 if dwell < 0.1 and dwell > 0.0 else 0.0)
                + (0.08 if not withdrew_enough else 0.0)
                + (0.10 if dwell > LONG_DWELL_PENALTY_THRESHOLD else 0.0)
                + (0.05 if approach_std > ERRATIC_APPROACH_STD_NORM else 0.0)
                + mouth_occlusion_penalty
                + missing_mouth_open_soft_penalty
                + (0.12 if likely_mouth_cover else 0.0)
                + (0.12 if no_mouth_open_palm_dump_contradiction else 0.0)
            ),
            "raw_event_score": confidence,
            "decision": decision,
            "decision_reason": decision_reason,
            "safety_contradiction": safety_contradiction,
            "status": self.last_status,
        }

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
        event_debug["frame_confidence"] = frame_confidence

        state.prev_mouth_dist = curr_dist
        state.prev_mouth_dist_norm = curr_dist_norm

        return {
            "event_detected": event_detected,
            "ingestion_detected": event_detected,
            "decision": decision,
            "decision_reason": decision_reason,
            "confidence": state.event_confidence,
            "event_confidence": state.event_confidence,
            "peak_confidence": self.peak_event_confidence,
            "frame_confidence": frame_confidence,
            "status": self.last_status,
            "mouth_open": features.get("mouth_open", False),
            "hand_near_mouth": near_mouth,
            "event_debug": event_debug,
        }

    @staticmethod
    def _std(values: List[float]) -> float:
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        variance = sum((x - mean) ** 2 for x in values) / len(values)
        return math.sqrt(variance)


# =========================================================
# Temporal observation adapter
# =========================================================
def build_temporal_observations(
    detector: PillIngestionDetector,
    face_landmarks: List[dict],
    hand_landmarks: List[List[dict]],
    width: int,
    height: int,
    *,
    mouth_behavior: Optional["MouthBehaviorState"] = None,
    timestamp: float = 0.0,
    temporal_stage: str = "CALIBRATING",
    mouth_cues: Optional[dict] = None,
) -> Tuple[dict, List[Observation]]:
    """Convert raw MediaPipe landmarks into the live temporal contract."""
    mouth_geom = detector.compute_mouth_geometry(face_landmarks, width, height)
    hand_features = []
    for hand_lm in hand_landmarks:
        if len(hand_lm) < 21:
            continue
        features = detector.compute_hand_features(
            hand_lm, mouth_geom, width, height
        )
        hand_features.append(features)

    cues = mouth_cues if isinstance(mouth_cues, dict) else {}
    raw_tongue_score = cues.get("tongue_score")
    tongue_score = (
        float(raw_tongue_score)
        if isinstance(raw_tongue_score, (int, float)) and math.isfinite(raw_tongue_score)
        else None
    )
    raw_quality = cues.get("quality", 0.0)
    tongue_quality = (
        float(raw_quality)
        if isinstance(raw_quality, (int, float)) and math.isfinite(raw_quality)
        else 0.0
    )
    behavior = mouth_behavior or MouthBehaviorState()
    hand_near = any(
        feature["fingertip_to_mouth_norm"] <= TemporalIntakePipeline.EXIT_DISTANCE
        or feature.get("in_mouth_zone", False)
        for feature in hand_features
    )
    behavior_result = behavior.update(
        ratio=float(mouth_geom["mouth_open_ratio"]),
        timestamp=timestamp,
        reliable=float(mouth_geom["width"]) >= 24.0,
        baseline_allowed=(
            temporal_stage in {"CALIBRATING", "READY", "RESET"} and not hand_near
        ),
        tongue_score=tongue_score,
        tongue_quality=max(0.0, min(tongue_quality, 1.0)),
    )
    mouth_geom.update(behavior_result)

    observations: List[Observation] = []
    for features in hand_features:
        palm = features["palm_center"]
        flat_cover = bool(
            features["flat_palm"]
            and features["mouth_occlusion_score"] >= OCCLUSION_MODERATE_SCORE
        )
        palm_touch = bool(
            features["palm_center_in_mouth_roi"]
            and not features["holding_object"]
        )
        style = (
            "palm_cover"
            if flat_cover
            else "pinch_delivery"
            if features["holding_object"]
            else "unknown"
        )
        observations.append(Observation(
            center=(palm[0] / width, palm[1] / height),
            distance=float(features["fingertip_to_mouth_norm"]),
            mouth_open=bool(behavior_result["mouth_open"]),
            mouth_open_ratio=float(mouth_geom["mouth_open_ratio"]),
            mouth_open_delta=float(behavior_result["mouth_open_delta"]),
            mouth_motion_cycles=int(behavior_result["mouth_motion_cycles"]),
            tongue_score=tongue_score,
            tongue_quality=float(behavior_result["tongue_quality"]),
            tongue_support=bool(behavior_result["tongue_frame_support"]),
            pinch=bool(features["pinch"]),
            flat_palm=bool(features["flat_palm"]),
            occlusion=float(features["mouth_occlusion_score"]),
            delivery_like=bool(features["holding_object"]),
            contradiction=flat_cover or palm_touch,
            style=style,
        ))
    return mouth_geom, observations


# =========================================================
# Service Singleton
# =========================================================
SESSION_FRAME_LIMIT = 600
SESSION_ACTIVE_TTL_SECONDS = 5 * 60
SESSION_COMPLETED_TTL_SECONDS = 15 * 60
SESSION_CAPACITY = 32


class SessionCapacityReached(RuntimeError):
    """Raised when every telemetry slot belongs to a recently active session."""


class SessionEnded(RuntimeError):
    """Raised when a frame arrives after the session completion barrier."""


@dataclass
class MouthBehaviorState:
    """Per-session adaptive mouth and tongue baselines with hysteresis."""

    ratios: Deque[float] = field(default_factory=lambda: deque(maxlen=30))
    tongue_scores: Deque[float] = field(default_factory=lambda: deque(maxlen=30))
    open_votes: Deque[bool] = field(default_factory=lambda: deque(maxlen=3))
    close_votes: Deque[bool] = field(default_factory=lambda: deque(maxlen=3))
    transition_times: Deque[float] = field(default_factory=deque)
    baseline: float = 0.10
    tongue_baseline: float = 0.0
    is_open: bool = False
    opened_at: Optional[float] = None

    @staticmethod
    def _lower_percentile(values: Deque[float]) -> float:
        ordered = sorted(values)
        index = int((len(ordered) - 1) * 0.20)
        return ordered[index]

    @staticmethod
    def _median(values: Deque[float]) -> float:
        ordered = sorted(values)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2.0

    def update(
        self,
        *,
        ratio: float,
        timestamp: float,
        reliable: bool,
        baseline_allowed: bool,
        tongue_score: Optional[float],
        tongue_quality: float,
    ) -> dict:
        if reliable and baseline_allowed and not self.is_open:
            self.ratios.append(ratio)
            if len(self.ratios) >= 5:
                self.baseline = self._lower_percentile(self.ratios)
            if tongue_score is not None and tongue_quality >= 0.60:
                self.tongue_scores.append(tongue_score)
                if len(self.tongue_scores) >= 5:
                    self.tongue_baseline = self._median(self.tongue_scores)

        delta = ratio - self.baseline
        if v1_config.ADAPTIVE_MOUTH:
            open_condition = ratio >= max(0.16, self.baseline + 0.045)
            self.open_votes.append(open_condition)
            close_condition = ratio <= max(0.12, self.baseline + 0.025)
            self.close_votes.append(close_condition)
            should_open = ratio >= 0.24 or sum(self.open_votes) >= 2
            should_close = len(self.close_votes) == 3 and sum(self.close_votes) >= 2
        else:
            should_open = ratio > 0.35
            should_close = not should_open

        previous = self.is_open
        if not self.is_open and reliable and should_open:
            self.is_open = True
            self.opened_at = timestamp
            self.close_votes.clear()
        elif self.is_open and should_close:
            self.is_open = False
            self.opened_at = None
            self.open_votes.clear()

        if self.is_open != previous:
            self.transition_times.append(timestamp)
        while self.transition_times and self.transition_times[0] < timestamp - 1.5:
            self.transition_times.popleft()

        tongue_delta = (
            None if tongue_score is None else tongue_score - self.tongue_baseline
        )
        tongue_frame_support = bool(
            v1_config.TONGUE_SUPPORT
            and self.is_open
            and tongue_score is not None
            and tongue_quality >= 0.60
            and tongue_score >= 0.65
            and tongue_delta is not None
            and tongue_delta >= 0.20
        )
        return {
            "mouth_open": self.is_open,
            "mouth_open_baseline": self.baseline,
            "mouth_open_delta": delta,
            "mouth_opened_at": self.opened_at,
            "mouth_motion_cycles": len(self.transition_times),
            "tongue_score": tongue_score,
            "tongue_quality": tongue_quality,
            "tongue_delta": tongue_delta,
            "tongue_frame_support": tongue_frame_support,
        }


@dataclass
class IntakeSessionState:
    user_id: int
    session_id: str
    created_at: float
    last_active: float
    last_accessed: float
    detector: PillIngestionDetector = field(default_factory=PillIngestionDetector)
    temporal: TemporalIntakePipeline = field(default_factory=TemporalIntakePipeline)
    mouth_behavior: MouthBehaviorState = field(default_factory=MouthBehaviorState)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_frame_seq: int = -1
    ended: bool = False
    completed_at: Optional[float] = None
    borrowers: int = 0
    total_frames: int = 0
    accepted_frames: int = 0
    stale_frames: int = 0
    records: Deque[dict] = field(
        default_factory=lambda: deque(maxlen=SESSION_FRAME_LIMIT)
    )
    evicted_through_record_id: int = 0
    decision_counts: Dict[str, int] = field(
        default_factory=lambda: {"none": 0, "uncertain": 0, "confirmed": 0}
    )
    candidate_ids: set[int] = field(default_factory=set)
    last_stage: Optional[str] = None
    latest_result: Optional[dict] = None
    inference_mode: Optional[str] = None


class IntakeDetectionService:
    _instance: Optional["IntakeDetectionService"] = None
    _available: bool = False

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._sessions: Dict[str, IntakeSessionState] = {}
        self._registry_lock = asyncio.Lock()
        self._next_record_id = 1
        IntakeDetectionService._available = True

    @classmethod
    def get_instance(cls) -> "IntakeDetectionService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _key(self, u_id: int, session_id: str) -> str:
        return f"{u_id}:{session_id}"

    def _cleanup_expired_locked(self, current_time: float) -> None:
        expired = []
        for key, state in self._sessions.items():
            if state.borrowers:
                continue
            ttl = (
                SESSION_COMPLETED_TTL_SECONDS
                if state.ended
                else SESSION_ACTIVE_TTL_SECONDS
            )
            anchor = state.completed_at if state.ended else state.last_active
            if anchor is not None and current_time - anchor >= ttl:
                expired.append(key)
        for key in expired:
            self._sessions.pop(key, None)

    def _make_room_locked(self) -> None:
        if len(self._sessions) < SESSION_CAPACITY:
            return
        completed = [
            (state.last_accessed, key)
            for key, state in self._sessions.items()
            if state.ended and not state.borrowers
        ]
        if not completed:
            raise SessionCapacityReached("session_capacity_reached")
        _, key = min(completed)
        self._sessions.pop(key, None)

    async def _borrow_session(
        self,
        u_id: int,
        session_id: str,
        *,
        create: bool,
    ) -> Optional[IntakeSessionState]:
        now = self._clock()
        key = self._key(u_id, session_id)
        async with self._registry_lock:
            self._cleanup_expired_locked(now)
            state = self._sessions.get(key)
            if state is None and create:
                self._make_room_locked()
                state = IntakeSessionState(
                    user_id=u_id,
                    session_id=session_id,
                    created_at=now,
                    last_active=now,
                    last_accessed=now,
                )
                self._sessions[key] = state
            if state is not None:
                state.borrowers += 1
        if state is not None:
            try:
                await state.lock.acquire()
            except BaseException:
                async with self._registry_lock:
                    state.borrowers -= 1
                raise
        return state

    async def _release_session(self, state: IntakeSessionState) -> None:
        async with self._registry_lock:
            state.borrowers -= 1
        state.lock.release()

    @staticmethod
    def _compact_result(result: dict) -> dict:
        fields = (
            "accepted", "frame_seq", "last_accepted_frame_seq", "stage",
            "status", "missing_observation", "decision", "decision_reason",
            "candidate_id", "confidence", "frame_confidence",
            "event_confidence", "completion_reason", "event_detected",
            "ingestion_detected", "mouth_open", "hand_near_mouth",
            "mouth_open_ratio", "mouth_open_baseline", "mouth_open_delta",
            "mouth_motion_cycles", "tongue_score", "tongue_quality",
            "tongue_peak_score", "tongue_support",
        )
        compact = {name: deepcopy(result.get(name)) for name in fields if name in result}
        policy = result.get("policy")
        if isinstance(policy, dict):
            compact["policy"] = {
                name: deepcopy(policy.get(name))
                for name in ("event_id", "detector_band", "policy_band")
                if name in policy
            }
        return compact

    def _record_response(
        self,
        state: IntakeSessionState,
        result: dict,
        payload: dict,
        telemetry_payload: Optional[dict],
        now: float,
    ) -> None:
        record_id = self._next_record_id
        self._next_record_id += 1
        if len(state.records) == SESSION_FRAME_LIMIT:
            state.evicted_through_record_id = state.records[0]["record_id"]
        record = {
            "record_id": record_id,
            "recorded_at": now,
            "frame_seq": result.get("frame_seq"),
            "accepted": result.get("accepted", True),
            "inference": {
                "mode": payload.get("inference_mode"),
                "milliseconds": payload.get("inference_ms"),
                "generation": payload.get("generation"),
            },
            "result": deepcopy(result),
        }
        if telemetry_payload is not None:
            record["payload"] = deepcopy(telemetry_payload)
        state.records.append(record)
        state.total_frames += 1
        accepted = result.get("accepted", True) is not False
        if accepted:
            state.accepted_frames += 1
        else:
            state.stale_frames += 1
        decision = str(result.get("decision") or "none")
        state.decision_counts[decision] = state.decision_counts.get(decision, 0) + 1
        candidate_id = result.get("candidate_id")
        if isinstance(candidate_id, int):
            state.candidate_ids.add(candidate_id)
        state.last_stage = result.get("stage") or result.get("status")
        state.latest_result = self._compact_result(result)
        state.inference_mode = payload.get("inference_mode") or state.inference_mode

    async def process_frame(
        self,
        u_id: int,
        session_id: str,
        payload: dict,
        result_transform: Optional[Callable[[dict], dict]] = None,
        telemetry_payload: Optional[dict] = None,
    ) -> dict:
        """Serialize temporal mutation only with requests for this session."""
        state = await self._borrow_session(u_id, session_id, create=True)
        assert state is not None
        try:
            if state.ended:
                raise SessionEnded("session_ended")
            current_time = self._clock()
            state.last_active = current_time
            state.last_accessed = current_time
            raw_frame_seq = payload.get("frame_seq")
            frame_seq = (
                state.last_frame_seq + 1
                if raw_frame_seq is None
                else int(raw_frame_seq)
            )
            if frame_seq <= state.last_frame_seq:
                result = state.temporal.result("stale_frame", accepted=False)
                result["frame_seq"] = frame_seq
                result["last_accepted_frame_seq"] = state.last_frame_seq
                result.setdefault("peak_confidence", 0.0)
                result.setdefault("mouth_open", False)
                result.setdefault("hand_near_mouth", False)
            else:
                state.last_frame_seq = frame_seq
                result = self._process_ordered_frame(
                    state, frame_seq, payload, current_time
                )
            if result_transform is not None:
                result = result_transform(result)
            self._record_response(
                state, result, payload, telemetry_payload, current_time
            )
            return result
        finally:
            await self._release_session(state)

    def _process_ordered_frame(
        self,
        state: IntakeSessionState,
        frame_seq: int,
        payload: dict,
        current_time: float,
    ) -> dict:
        detector = state.detector
        pipeline = state.temporal
        width = int(payload.get("width", 640))
        height = int(payload.get("height", 480))
        face_landmarks = payload.get("face_landmarks", [])
        hand_landmarks = payload.get("hand_landmarks", [])
        raw_timestamp = payload.get("timestamp")
        timestamp = current_time if raw_timestamp is None else float(raw_timestamp)

        if not face_landmarks:
            result = pipeline.process(timestamp, False, [])
            result.update({
                "frame_seq": frame_seq,
                "last_accepted_frame_seq": frame_seq,
                "mouth_open": False,
                "hand_near_mouth": False,
                "peak_confidence": 0.0,
            })
            result["debug"] = self._temporal_debug(
                result, frame_seq, width, height, False, len(hand_landmarks)
            )
            return result

        mouth_geom, observations = build_temporal_observations(
            detector,
            face_landmarks,
            hand_landmarks,
            width,
            height,
            mouth_behavior=state.mouth_behavior,
            timestamp=timestamp,
            temporal_stage=pipeline.event.stage.value,
            mouth_cues=payload.get("mouth_cues"),
        )

        result = pipeline.process(timestamp, True, observations)
        if result.get("candidate_id") is not None:
            key = self._key(state.user_id, state.session_id)
            token_source = f"{key}:{result['candidate_id']}".encode("utf-8")
            result["candidate_token"] = hashlib.sha256(token_source).hexdigest()
        result.update({
            "frame_seq": frame_seq,
            "last_accepted_frame_seq": frame_seq,
            "mouth_open": mouth_geom["mouth_open"],
            "mouth_open_ratio": round(float(mouth_geom["mouth_open_ratio"]), 4),
            "mouth_open_baseline": round(float(mouth_geom["mouth_open_baseline"]), 4),
            "mouth_open_delta": round(float(mouth_geom["mouth_open_delta"]), 4),
            "mouth_opened_at": mouth_geom["mouth_opened_at"],
            "mouth_motion_cycles": mouth_geom["mouth_motion_cycles"],
            "tongue_score": mouth_geom["tongue_score"],
            "tongue_quality": round(float(mouth_geom["tongue_quality"]), 4),
            "tongue_frame_support": mouth_geom["tongue_frame_support"],
            "hand_near_mouth": any(
                obs.distance <= pipeline.config.exit_distance for obs in observations
            ),
            "peak_confidence": result.get("event_confidence", 0.0),
        })
        result["debug"] = self._temporal_debug(
            result, frame_seq, width, height, True, len(observations)
        )
        return result

    @staticmethod
    def _temporal_debug(
        result: dict,
        frame_seq: int,
        width: int,
        height: int,
        face_visible: bool,
        hand_count: int,
    ) -> dict:
        keys = (
            "stage", "waiting_reason", "transition_reason",
            "missing_observation", "contact_distance", "entry_distance",
            "exit_distance", "approach_velocity", "withdrawal_velocity",
            "occlusion_duration", "hand_lost", "reacquired",
            "completion_reason", "reset_reason", "candidate_id",
            "safety_contradiction", "delivery_evidence", "flat_palm_ratio",
            "peak_mouth_occlusion", "event_style", "frame_confidence",
            "event_confidence", "hand_near_mouth", "mouth_open",
            "mouth_open_ratio", "mouth_open_baseline", "mouth_open_delta",
            "mouth_opened_at", "mouth_motion_cycles", "tongue_score",
            "tongue_quality", "tongue_frame_support", "tongue_peak_score",
            "tongue_support", "tongue_support_frames",
        )
        debug = {name: result.get(name) for name in keys}
        debug.update({
            "face_visible": face_visible,
            "hand_visible": hand_count > 0,
            "hands": hand_count,
            "frame_seq": frame_seq,
            "video_width": width,
            "video_height": height,
            "status": result.get("stage"),
            "mouth_open": result.get("mouth_open", False),
        })
        return debug

    def _summary(self, state: IntakeSessionState) -> dict:
        first_record = state.records[0]["record_id"] if state.records else None
        last_record = state.records[-1]["record_id"] if state.records else None
        return {
            "session_id": state.session_id,
            "lifecycle": "completed" if state.ended else "active",
            "created_at": state.created_at,
            "last_activity_at": state.last_active,
            "completed_at": state.completed_at,
            "frame_counts": {
                "total": state.total_frames,
                "accepted": state.accepted_frames,
                "stale": state.stale_frames,
            },
            "retained_frames": {
                "count": len(state.records),
                "first_record_id": first_record,
                "last_record_id": last_record,
            },
            "decision_counts": deepcopy(state.decision_counts),
            "candidate_count": len(state.candidate_ids),
            "last_stage": state.last_stage,
            "inference_mode": state.inference_mode,
            "latest_result": deepcopy(state.latest_result),
        }

    def _recent_summary(self, state: IntakeSessionState) -> dict:
        """Return the small selector representation used by the recent-session list."""
        return {
            "session_id": state.session_id,
            "lifecycle": "completed" if state.ended else "active",
            "last_activity_at": state.last_active,
            "completed_at": state.completed_at,
            "frame_counts": {
                "total": state.total_frames,
                "accepted": state.accepted_frames,
                "stale": state.stale_frames,
            },
            "last_stage": state.last_stage,
        }

    async def list_sessions(self, u_id: int, limit: int = 20) -> list[dict]:
        now = self._clock()
        async with self._registry_lock:
            self._cleanup_expired_locked(now)
            states = [state for state in self._sessions.values() if state.user_id == u_id]
            states.sort(
                key=lambda state: (state.completed_at or state.last_active, state.created_at),
                reverse=True,
            )
            selected = states[:limit]
            for state in selected:
                state.borrowers += 1
        summaries = []
        for state in selected:
            await state.lock.acquire()
            try:
                state.last_accessed = now
                summaries.append(self._recent_summary(state))
            finally:
                await self._release_session(state)
        return summaries

    async def get_session(self, u_id: int, session_id: str) -> Optional[dict]:
        state = await self._borrow_session(u_id, session_id, create=False)
        if state is None:
            return None
        try:
            state.last_accessed = self._clock()
            return self._summary(state)
        finally:
            await self._release_session(state)

    async def get_debug_frames(
        self,
        u_id: int,
        session_id: str,
        *,
        after: int,
        limit: int,
    ) -> Optional[dict]:
        state = await self._borrow_session(u_id, session_id, create=False)
        if state is None:
            return None
        try:
            state.last_accessed = self._clock()
            records = [
                deepcopy(record)
                for record in state.records
                if record["record_id"] > after
            ][:limit]
            return {
                "session_id": session_id,
                "after": after,
                "limit": limit,
                "truncated_before_cursor": (
                    state.evicted_through_record_id > 0
                    and after < state.evicted_through_record_id
                ),
                "records": records,
                "next_after": records[-1]["record_id"] if records else after,
                "has_more": bool(
                    records
                    and any(
                        item["record_id"] > records[-1]["record_id"]
                        for item in state.records
                    )
                ),
            }
        finally:
            await self._release_session(state)

    async def get_debug_payloads(
        self,
        u_id: int,
        session_id: str,
        *,
        after: int,
        limit: int,
    ) -> Optional[dict]:
        state = await self._borrow_session(u_id, session_id, create=False)
        if state is None:
            return None
        try:
            state.last_accessed = self._clock()
            retained = [
                record
                for record in state.records
                if "payload" in record and record["record_id"] > after
            ]
            selected = retained[:limit]
            records = [
                {
                    "record_id": record["record_id"],
                    "recorded_at": record["recorded_at"],
                    "frame_seq": record["frame_seq"],
                    "payload": deepcopy(record["payload"]),
                }
                for record in selected
            ]
            return {
                "session_id": session_id,
                "after": after,
                "limit": limit,
                "truncated_before_cursor": (
                    state.evicted_through_record_id > 0
                    and after < state.evicted_through_record_id
                ),
                "records": records,
                "next_after": records[-1]["record_id"] if records else after,
                "has_more": len(retained) > len(selected),
            }
        finally:
            await self._release_session(state)

    async def end_session(self, u_id: int, session_id: str) -> dict:
        state = await self._borrow_session(u_id, session_id, create=True)
        assert state is not None
        try:
            if not state.ended:
                now = self._clock()
                state.ended = True
                state.completed_at = now
                state.last_accessed = now
            return self._summary(state)
        finally:
            await self._release_session(state)
