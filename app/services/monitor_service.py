"""Bind one live intake detector to the authenticated person's face and hands."""

import asyncio
import datetime
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from functools import partial

import cv2
import numpy as np

from app.intake_v1.policy import apply_policy
from app.services import dose_emotion, schedule
from app.services.emotion_service import EmotionService, LABELS, crop_face
from app.services.face_recognition_service import FaceRecognitionService
from app.services.intake_detection import IntakeDetectionService

FACE_INDICES = (1, 10, 13, 14, 33, 61, 152, 263, 291)
HAND_INDICES = (0, 4, 5, 6, 8, 9, 10, 12, 14, 16, 17, 18, 20)
PACKET_HISTORY = 48
CANDIDATE_TIMEOUT_SECONDS = 5.0
# A robot session's ready candidate is kept this long from its creation (quick fix 7): the robot reads it only in
# WATCHING, after its ~10-11 s medicine prompt, and a patient who takes the pill during the prompt makes one 1-2 s
# into the session. Unready candidates, and every browser candidate, keep CANDIDATE_TIMEOUT_SECONDS. It equals
# dose_video.EVENT_MAX_SECONDS: a longer window needs a longer event clip, or a late read's clip misses the approach.
READY_CANDIDATE_SECONDS = 15.0

# A session only auto-commits while landmark capture keeps up: fewer frames give the detector fewer samples per
# gesture (its speeds are per second, intake_temporal.py; CLAUDE.md corrected this in quick fix 7).
FPS_WINDOW_SECONDS = 3.0
FPS_MIN_SPAN_SECONDS = 1.0
FPS_MIN = 12.0
FPS_MAX_GAP_SECONDS = 0.25

# A gap in the patient's hand ownership during a hand-to-mouth event (identity not fresh, or the hand and body can't
# be tied to the face: typically the frames where the hand crosses the face) no longer resets the detector at once.
# The event survives a gap shorter than this (on the packets' capture clock); the detector's own grace windows
# (intake_temporal.py: occlusion_grace_seconds 0.55, track_grace_seconds 0.65) decide what to make of the missing
# frames, which it never sees. An event that crossed any gap can prompt but never auto-log (intake_v1.policy's
# ownership_gap). Measured on 33 labelled clips (C:\pilldetector, 3 Nov 2026): the immediate reset threw away 9 of
# 20 real intakes after the pill had reached the mouth; with 0.55 s of grace and this cap, 11 of 20 raise a prompt
# or a record and no "not taken" clip is auto-logged.
OWNERSHIP_GRACE_SECONDS = 0.5

MODES = ("dose", "observe")
CLIENT_TYPES = ("browser", "reachy")
LIVE_SESSION_SECONDS = 5.0
EXTRA_EVENT_CAP = 20
# A session with no landmark packet for this long is dead (a closed tab that never sent /end): sweep_idle ends it.
IDLE_SESSION_SECONDS = 600.0
# The detector stages of one hand-to-mouth event. The event's emotion window starts at the first of the active
# ones reported: the detector can go READY -> APPROACHING -> AT_MOUTH within one frame (a hand first seen near the
# face, or a fast approach at 10 fps), and then never reports APPROACHING.
EVENT_STAGES = ("APPROACHING", "AT_MOUTH", "OCCLUDED", "WITHDRAWING", "COMPLETE_CANDIDATE")
ACTIVE_EVENT_STAGES = ("APPROACHING", "AT_MOUTH", "OCCLUDED", "WITHDRAWING")
# Identity + emotion on a robot's streamed frames (api_device.monitor_frame): every VISION_INTERVAL, and 4 times a
# second while a verified patient's dose session has no emotion result yet (dose_emotion; measured ~4.5 ms per
# emotion call on this laptop). Only once verified: vision() runs identity on every call while the patient is not
# verified, and afterwards at most every 0.5 s (its verified_at gate), so identity never exceeds 2 Hz.
VISION_INTERVAL = 0.5
DOSE_VISION_INTERVAL = 0.25
# A robot session's identity log (_log_identity): a line on every change of identity_status, otherwise at most one
# per IDENTITY_LOG_SECONDS (identity runs up to 2 Hz).
IDENTITY_LOG_SECONDS = 5.0
# The OpenVINO-only rule (openvino_only) counts a hit only for a patient face at least this share of the frame's
# width wide: about 32 px of the robot's 480x360 frame. The MediaPipe gate set a floor of about 50 px by itself;
# without one, faces of about 24 px matched at distances of 0.20-0.27, close to the 0.3 threshold (review of 11 Oct).
OPENVINO_ONLY_MIN_FACE_FRACTION = 0.065

log = logging.getLogger(__name__)


class BusyOtherClient(Exception):
    """A live session for this user belongs to the other client type (first holder wins)."""


def overlap(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    union = a[2] * a[3] + b[2] * b[3] - intersection
    return intersection / union if union > 0 else 0.0


def point_distance(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** .5


def full_points(compact, indices):
    points = [{} for _ in range(max(indices) + 1)]
    for index, point in zip(indices, compact):
        points[index] = {"x": float(point[0]), "y": float(point[1])}
    return points


# The hand-to-body rule remembers what it confirmed (OwnershipMemory, one per session, on the packets' capture
# clock). A hand confirmed as the patient's within OWNERSHIP_CARRY_SECONDS stays theirs while it moves on smoothly
# (within OWNERSHIP_CARRY_DISTANCE + OWNERSHIP_CARRY_SPEED x the time since it was last seen) on the frames where
# this frame's own vote fails: the patient's pose lost or its wrists hidden (a hand over the face does both),
# another person's wrist nearly as close, or a neighbour's wrist a little closer. The patient's body likewise: when
# no pose has its nose in the face box, the pose whose shoulders stay within POSE_CARRY_DISTANCE of the last
# matched ones, for POSE_CARRY_SECONDS. A frame that rests on memory is marked `carried`: an event that uses one
# may prompt, never auto-log (monitor_service.landmarks sets event_ownership_gap). Before 3 Nov 2026 the rule had
# no memory, refused any frame with 4 faces, poses or 8 hands, and refused the whole frame when one hand's owner
# was unclear: on the C:\pilldetector clips that cost 4 of the 5 real intakes still missed after the grace.
OWNERSHIP_CARRY_SECONDS = 3.0
OWNERSHIP_CARRY_DISTANCE = 0.06
OWNERSHIP_CARRY_SPEED = 0.4           # frame widths per second
POSE_CARRY_SECONDS = 2.0
POSE_CARRY_DISTANCE = 0.12
SAME_POSE_FACE_WIDTHS = 0.25          # two poses whose noses are this close (in face widths) are one person twice
WEAK_RIVAL_MIN = 0.04                 # a neighbour's wrist nearer than this to a hand's root is that neighbour's hand


@dataclass
class OwnershipMemory:
    """What select_owned_observations remembers between a session's frames (times are the packets' timestamps)."""
    shoulders: tuple | None = None          # the patient's shoulder midpoint when a nose last matched the face
    shoulders_at: float = 0.0
    hands: list = field(default_factory=list)   # [[wrist (x, y), confirmed_at, seen_at]] of the patient's hands

    def clear(self) -> None:
        self.shoulders, self.shoulders_at, self.hands = None, 0.0, []

    def update(self, selected: dict | None, timestamp: float) -> None:
        """Record a frame's selection: the shoulders a nose matched, each owned hand (a confirmed one afresh, a
        carried one at its new place with its old confirmation), and forget hands confirmed too long ago."""
        if selected is not None:
            if selected.get("shoulders") is not None and not selected.get("pose_carried"):
                self.shoulders, self.shoulders_at = selected["shoulders"], timestamp
            for hand, carried, index in zip(selected["hands"], selected["carried_hands"], selected["memory_indices"]):
                wrist = (float(hand[0][0]), float(hand[0][1]))
                if index is not None and index < len(self.hands):
                    entry = self.hands[index]
                    entry[0], entry[2] = wrist, timestamp
                    if not carried:
                        entry[1] = timestamp
                elif not carried:
                    self.hands.append([wrist, timestamp, timestamp])
        self.hands = [entry for entry in self.hands if timestamp - entry[1] <= OWNERSHIP_CARRY_SECONDS][-4:]


def _shoulder_mid(pose: dict):
    shoulders = pose.get("shoulders") or []
    if len(shoulders) != 2:
        return None
    return ((shoulders[0][0] + shoulders[1][0]) / 2, (shoulders[0][1] + shoulders[1][1]) / 2)


def _shoulder_width(pose: dict) -> float:
    shoulders = pose.get("shoulders") or []
    return point_distance(shoulders[0], shoulders[1]) if len(shoulders) == 2 else 0.0


def _distinct_poses(poses: list, face_width: float) -> list:
    """(index, pose) for every person once: a pose whose nose is within SAME_POSE_FACE_WIDTHS of an earlier
    pose's is the same person seen twice, and the one with the wider shoulders is kept."""
    kept = []
    for index, pose in enumerate(poses):
        nose = pose.get("nose")
        if not nose:
            continue
        twin = next((k for k, (_, other) in enumerate(kept)
                     if point_distance(nose, other["nose"]) <= SAME_POSE_FACE_WIDTHS * max(face_width, 1e-6)), None)
        if twin is None:
            kept.append((index, pose))
        elif _shoulder_width(pose) > _shoulder_width(kept[twin][1]):
            kept[twin] = (index, pose)
    return kept


def _continuations(memory, root, now: float) -> list:
    """(distance, index) of every remembered hand this root may continue: confirmed within the carry time, and
    within the continuity distance of where it was last seen."""
    out = []
    for index, (wrist, confirmed_at, seen_at) in enumerate(memory.hands):
        if now - confirmed_at > OWNERSHIP_CARRY_SECONDS:
            continue
        distance = point_distance(root, wrist)
        if distance <= OWNERSHIP_CARRY_DISTANCE + OWNERSHIP_CARRY_SPEED * max(0.0, now - seen_at):
            out.append((distance, index))
    return out


def select_owned_observations(faces: list, poses: list, hands: list, target_box: list | None,
                              memory: OwnershipMemory | None = None, timestamp: float | None = None,
                              explain: bool = False):
    """Associate the enrolled face to one pose, then its visible wrists to hands; with `memory`, carry what an
    earlier frame confirmed (OwnershipMemory, above). Returns the selection (at most 2 owned hands, possibly
    none), or None when the patient's face can't be told from the others. With explain=True, (selection, reason):
    the reason of a None, or why a selection has no hands ('' when it has)."""
    def fail(reason):
        return (None, reason) if explain else None

    if target_box is None:
        return fail("identity")
    face_matches = sorted(((overlap(face["box"], target_box), i) for i, face in enumerate(faces)), reverse=True)
    if not face_matches or face_matches[0][0] < .20 or (len(face_matches) > 1 and face_matches[0][0] - face_matches[1][0] < .12):
        return fail("face_ambiguous")
    target_index = face_matches[0][1]
    face = faces[target_index]
    if len(face.get("points", [])) != len(FACE_INDICES):
        return fail("face_points")
    x, y, width, height = face["box"]
    center = (x + width / 2, y + height / 2)
    now = float(timestamp or 0.0)
    bodies = _distinct_poses(poses, width)
    remembered = (memory.shoulders if memory is not None and memory.shoulders is not None
                  and now - memory.shoulders_at <= POSE_CARRY_SECONDS else None)
    # The patient's body: the pose with its nose in the face box (two of them: the remembered shoulders decide);
    # none: the pose whose shoulders stayed where the last matched ones were.
    note = ""
    pose_index, pose_carried = None, False
    nose_matches = sorted((point_distance(pose["nose"], center), index) for index, pose in bodies
                          if x - .04 <= pose["nose"][0] <= x + width + .04 and y - .04 <= pose["nose"][1] <= y + height + .04)
    if nose_matches and (len(nose_matches) == 1 or nose_matches[1][0] - nose_matches[0][0] >= .03):
        pose_index = nose_matches[0][1]
    elif nose_matches:
        near = [index for _, index in nose_matches[:2] if remembered is not None and _shoulder_mid(poses[index]) is not None
                and point_distance(_shoulder_mid(poses[index]), remembered) <= POSE_CARRY_DISTANCE]
        if len(near) == 1:
            pose_index = near[0]
        else:
            note = "two_bodies"
    else:
        candidates = sorted((point_distance(mid, remembered), index) for index, pose in bodies
                            for mid in [_shoulder_mid(pose)]
                            if remembered is not None and mid is not None and point_distance(mid, remembered) <= POSE_CARRY_DISTANCE)
        if candidates:
            pose_index, pose_carried = candidates[0][1], True
        else:
            note = "no_body"
    shoulders, shoulder_width = None, 0.0
    if pose_index is not None:
        shoulder_width = _shoulder_width(poses[pose_index])
        if shoulder_width < .04:
            pose_index, note = None, "shoulders"
        else:
            shoulders = _shoulder_mid(poses[pose_index])
    # Hands: each to the nearest visible wrist of any body, unless another body's wrist is nearly as close (that
    # hand alone is then undecided). A hand the memory carries stays the patient's when undecided, or when a
    # neighbour's wrist won it by little; a neighbour's wrist right on the root is that neighbour's hand.
    reach = max(.06, shoulder_width * .8) if pose_index is not None else .06
    margin = max(.02, shoulder_width * .15) if pose_index is not None else .02
    votes = []   # (hand_index, hand, confirmed by this frame, may be carried)
    for hand_index, hand in enumerate(hands):
        if len(hand) != len(HAND_INDICES):
            continue
        root = hand[0]
        distances = sorted((point_distance(root, wrist), index) for index, pose in bodies
                           for wrist in pose.get("wrists", []) if wrist and len(wrist) >= 3 and wrist[2] >= .5)
        tie = tie_distance = None
        if distances and distances[0][0] <= reach:
            nearest, owner = distances[0]
            rival = next((d for d, index in distances[1:] if index != owner), None)
            if rival is None or rival - nearest >= margin:
                tie, tie_distance = owner, nearest
        patient_distance = min((d for d, index in distances if index == pose_index), default=None)
        confirmed = tie is not None and tie == pose_index
        weak = tie is None or tie_distance >= max(WEAK_RIVAL_MIN,
                                                   0.5 * patient_distance if patient_distance is not None else 0.0)
        votes.append((hand_index, hand, confirmed, weak))
    # Each remembered hand continues at most one hand of this frame, the nearest; the hands this frame confirmed
    # claim theirs first (so a carried hand never takes a confirmed one's memory).
    claims = {}
    if memory is not None:
        used = set()
        for confirmed_first in (True, False):
            candidates = sorted((distance, hand_index, entry)
                                for hand_index, hand, confirmed, weak in votes
                                if confirmed == confirmed_first and (confirmed or weak)
                                for distance, entry in _continuations(memory, hand[0], now))
            for _, hand_index, entry in candidates:
                if hand_index not in claims and entry not in used:
                    claims[hand_index] = entry
                    used.add(entry)
    owned, owned_indices, carried_flags, memory_indices = [], [], [], []
    for hand_index, hand, confirmed, weak in votes:
        if confirmed:
            carried = False
        elif hand_index in claims:
            carried = True
        else:
            continue
        owned.append(hand)
        owned_indices.append(hand_index)
        carried_flags.append(carried)
        memory_indices.append(claims.get(hand_index))
    selection = {"face": face["points"], "hands": owned[:2], "hand_indices": owned_indices[:2], "box": face["box"],
                 "face_index": target_index, "pose_index": pose_index, "pose_carried": pose_carried,
                 "shoulders": shoulders, "carried_hands": carried_flags[:2], "memory_indices": memory_indices[:2],
                 "carried": pose_carried or any(carried_flags[:2])}
    if explain:
        return selection, (note if not owned else "")
    return selection


@dataclass
class MonitorSession:
    session_id: str
    generation: str
    u_id: int
    intk_id: int | None
    face_label: str
    display_name: str
    # 'observe' sessions carry no dose and have no commit path (see vision()).
    mode: str = "dose"
    # The mode the session started in (set in __post_init__), never changed: `mode` becomes 'observe' after a record,
    # so only this tells the search before talking (a robot session started in 'observe') apart (openvino_only).
    started_mode: str = ""
    # The robot started this session for a check-in task (reachy_task.reason 'checkin', read at
    # /api/device/monitor/start): its search verifies with the OpenVINO match alone (openvino_only).
    checkin_search: bool = False
    client_type: str = "browser"
    auto_commit: bool = True
    detector_session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_frame_seq: int = 0
    last_vision_seq: int = 0
    last_packet: dict | None = None
    # Landmark requests and JPEG requests are independent HTTP streams. Keep
    # enough immutable landmark snapshots to pair a vision result with the
    # frame that produced it while a newer landmark request is arriving.
    packets: dict[int, dict] = field(default_factory=dict)
    verified_at: float = 0.0
    # The last hit that made the status "verified" (identity_hits >= 2; monotonic): the live relay's camera-first
    # check (live_session.patient_seen_recently) counts only a verified sighting, never a first hit still "verifying"
    confirmed_at: float = 0.0
    identity_hits: int = 0
    identity_frame_seq: int = 0
    target_box: list | None = None
    last_identity_box: list | None = None
    identity_distance: float | None = None
    identity_status: str = "searching"
    # The identity log (_log_identity): the status it last logged, and when (monotonic).
    identity_logged_status: str | None = None
    identity_logged_at: float = float("-inf")
    # The live expression shown to the client: only ever an uncovered face.
    emotion: dict | None = None
    # The latest scored face had its mouth covered (a hand at the mouth): scored, kept as an occluded sample.
    emotion_occluded: bool = False
    # (monotonic time of the frame's landmark packet, probabilities, mouth covered, 'server' | 'robot') for the last
    # dose_emotion.KEEP_SECONDS; emotion_totals counts the whole session (services/dose_emotion.py).
    emotion_samples: list = field(default_factory=list)
    emotion_totals: dict = field(default_factory=dose_emotion.new_totals)
    # When each landmark packet arrived (monotonic), so a JPEG scored later is placed at its own frame's moment.
    packet_times: dict[int, float] = field(default_factory=dict)
    # Start (first active detector stage) of the hand-to-mouth event under way, reset when the event is abandoned;
    # and (start, end) of the session's latest candidate event: the window a dose's emotion result is centred on.
    emotion_event_start: float | None = None
    last_event: tuple | None = None
    # The dose's emotion result (dose_emotion): when this session resolved the dose (wall clock), its delayed
    # write, and whether it was written (once per session).
    dose_emotion_resolved_at: datetime.datetime | None = None
    dose_emotion_timer: asyncio.Task | None = None
    dose_emotion_done: bool = False
    # Kept for dose_video: set at the first APPROACHING of the session.
    event_started_at: float | None = None
    # A gap in the patient's hand ownership during an active detector stage: when it began (the packets' capture
    # clock) and the stage then, until OWNERSHIP_GRACE_SECONDS pass (a reset) or an owned frame arrives (the event
    # goes on). event_ownership_gap says the event under way crossed such a gap: the policy caps it at 'uncertain'.
    ownership_lost_at: float | None = None
    ownership_lost_stage: str | None = None
    event_ownership_gap: bool = False
    # What the hand-to-body rule remembers between frames (OwnershipMemory); cleared when identity is lost.
    ownership: OwnershipMemory = field(default_factory=OwnershipMemory)
    candidate: dict | None = None
    # What used the current candidate (mark_candidate_used): 'confirmation' (the robot's request named its event),
    # 'auto_commit', or the browser's 'confirmed_by_user' / 'not_taken'; None while nothing has. A dose candidate
    # that expires unused re-arms the detector (_expire_candidate); a used one keeps its latch.
    candidate_used: str | None = None
    # The event of the last dose candidate that expired unused, until a late request names it (logged once): a robot
    # that read it just before READY_CANDIDATE_SECONDS can file its request just after.
    expired_event_id: str | None = None
    latest_detector: dict | None = None
    recorded: dict | None = None
    ended: bool = False
    # Capture timestamps (seconds) of accepted landmark packets in the last
    # FPS_WINDOW_SECONDS. With no history the session is degraded: fail closed.
    capture_times: deque = field(default_factory=deque)
    degraded: bool = True
    landmark_fps: float = 0.0
    extra_events: list = field(default_factory=list)
    last_activity_at: float = field(default_factory=time.monotonic)
    # Reachy frame stream (api_device.monitor_frame): this session's landmark trackers, the lock that keeps
    # its frames in order, and the background identity/emotion call on a streamed frame.
    vision_engine: object | None = None
    frame_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    vision_task: asyncio.Task | None = None
    last_vision_started: float = float("-inf")
    # The patient switched dose videos on (consent 'dose_video', checked at start): its camera frames are also
    # held in memory for a clip (services/dose_video.py).
    clip_enabled: bool = False
    # When the session began (wall clock, the overdose rules' clock): start had allowed the dose then, so a pill seen
    # later in the session is judged for "due" and "expired" at this moment (dose_safety.evaluate's started_at).
    started_at: datetime.datetime = field(default_factory=schedule.current_time)

    def __post_init__(self) -> None:
        if not self.started_mode:
            self.started_mode = self.mode

    def verified(self, now: float | None = None) -> bool:
        return self.identity_hits >= 2 and (now or time.monotonic()) - self.verified_at <= 1.5

    def live(self, now: float | None = None) -> bool:
        return not self.ended and (now or time.monotonic()) - self.last_activity_at <= LIVE_SESSION_SECONDS

    def public(self) -> dict:
        return {"session_id": self.session_id, "generation": self.generation,
                "intk_id": self.intk_id, "name": self.display_name,
                "frame_seq": self.last_frame_seq,
                "identity_status": "verified" if self.verified() else self.identity_status,
                "identity_distance": self.identity_distance,
                "emotion": self.emotion if self.verified() else None,
                # The face was last scored with its mouth covered (the live emotion then stays empty).
                "emotion_occluded": self.emotion_occluded if self.verified() else False,
                # The verified patient's face box (normalized): a robot scores emotion on this face itself.
                "target_box": self.target_box if self.verified() else None,
                "detector": self.latest_detector,
                "candidate": self.candidate, "recorded": self.recorded,
                "mode": self.mode, "client_type": self.client_type, "auto_commit": self.auto_commit,
                "degraded": self.degraded, "landmark_fps": self.landmark_fps,
                "extra_events": list(self.extra_events)}


EVIDENCE_KEYS = {
    # The detector's per-event evidence (intake_detection.py's result) grouped as the live agent's intake tool
    # reports it: how the hand approached, touched the mouth, stayed and withdrew (tool_registry.get_pill_intake_result)
    "approach": ("avg_approach_norm", "approach_std_norm"),
    "contact": ("peak_mouth_contact", "in_mouth_zone_occurred", "mouth_open_allowed"),
    "dwell": ("dwell",),
    "withdrawal": ("withdrew_enough", "partial_withdrawal_seen"),
    "trajectory": ("trajectory_contribution", "mouth_contact_contribution", "dwell_contribution",
                   "withdrawal_contribution", "fingertip_delivery_contribution"),
    # The depth model's reading of the contact (robot frames; services/landmarks/depth.py, intake_temporal.py)
    "depth": ("depth_front_median", "depth_samples", "depth_no_contact"),
    # What the hand held through the contact (robot frames; services/landmarks/objects.py)
    "objects": ("object_in_hand", "object_samples"),
}


def detector_evidence(result: dict | None) -> dict:
    """The JSON-safe subset of a detector result the live agent's intake tool reports, by EVIDENCE_KEYS group, plus
    the event's style; a key the detector did not set is left out."""
    if not isinstance(result, dict):
        return {}

    def plain(value):
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, (int, float)):
            return round(float(value), 4)
        return str(value)

    evidence = {}
    for group, keys in EVIDENCE_KEYS.items():
        values = {key: plain(result[key]) for key in keys if key in result and result[key] is not None}
        if values:
            evidence[group] = values
    if result.get("event_style") is not None:
        evidence["event_style"] = str(result["event_style"])
    return evidence


def _mouth_hidden(selected: dict, hands: list, box: list) -> bool:
    """True when the owned face's mouth can't be scored: no mouth points, or a hand over it."""
    mouth_points = [selected["face"][index] for index in (5, 8)
                    if len(selected["face"]) > index and len(selected["face"][index]) >= 2]
    if not mouth_points:
        return True
    mouth = (sum(point[0] for point in mouth_points) / len(mouth_points),
             sum(point[1] for point in mouth_points) / len(mouth_points))
    return any(point_distance(hand[0], mouth) < max(.04, box[2] * .6) for hand in hands if hand)


def _accept_robot_emotion(state: "MonitorSession", packet: dict, now: float) -> None:
    """A robot scores emotion on its own camera. Accept it under the same gates as the server's own model:
    identity verified and the scored face is the owned (enrolled) face. A covered mouth is kept as an occluded
    sample for the dose's result (dose_emotion), never shown as the live emotion."""
    report = packet.get("emotion")
    if report is None:
        return
    selected = (select_owned_observations(packet["faces"], packet["poses"], packet["hands"], state.target_box)
                if state.verified(now) and state.target_box is not None else None)
    if selected is None or selected["face_index"] != report["face_index"]:
        state.emotion = None
        return
    probabilities = {name: float(report["probabilities"][name]) for name in LABELS}
    occluded = _mouth_hidden(selected, packet["hands"], state.target_box)
    dose_emotion.add_sample(state, now, probabilities, occluded, "robot")
    state.emotion_occluded = occluded
    if occluded:
        state.emotion = None
        return
    winner = max(LABELS, key=lambda name: probabilities[name])
    state.emotion = {"detected": True, "emotion_type": winner.capitalize(), "emotion_score": probabilities[winner],
                     "probabilities": probabilities, "error": None, "source": "robot"}


def mark_candidate_used(state: MonitorSession, event_id: str, by: str) -> bool:
    """Note that `by` acted on the session's current candidate, the one with event `event_id`: when it goes, the
    detector keeps its latch, so one gesture never gives two events. False when there is no such candidate."""
    candidate = state.candidate
    if not candidate or candidate.get("event_id") != event_id:
        if event_id is not None and event_id == state.expired_event_id:
            state.expired_event_id = None
            log.info("intake candidate %s of session %s: used by %s after it expired", event_id, state.session_id, by)
        return False
    if state.candidate_used is None:
        state.candidate_used = by
        log.info("intake candidate %s of session %s: used by %s", event_id, state.session_id, by)
    return True


async def _new_detector_session(state: MonitorSession) -> None:
    """End the session's detector session and start a fresh one: a new id, the temporal pipeline at its start."""
    await IntakeDetectionService.get_instance().end_session(state.u_id, state.detector_session_id)
    state.detector_session_id = str(uuid.uuid4())
    state.event_started_at = None
    state.ownership_lost_at = state.ownership_lost_stage = None
    state.event_ownership_gap = False


async def _expire_candidate(state: MonitorSession, now: float) -> None:
    """Drop a prompt that was never completed, even when vision is idle: an unready candidate after
    CANDIDATE_TIMEOUT_SECONDS, a robot session's ready one after READY_CANDIDATE_SECONDS, both from its creation.
    A dose candidate nothing used (mark_candidate_used) re-arms the detector at once, so the next real gesture
    makes a new candidate (quick fix 7: before, the session stayed latched and every later gesture was ignored)."""
    candidate = state.candidate
    if not candidate:
        return
    age = now - float(candidate.get("created_at", now))
    ready = bool(candidate.get("ready"))
    if age <= (READY_CANDIDATE_SECONDS if ready and state.client_type == "reachy" else CANDIDATE_TIMEOUT_SECONDS):
        return
    state.candidate = None
    if state.candidate_used is not None or state.mode != "dose" or state.ended:
        return
    log.info("intake candidate %s of session %s: expired unread (ready=%s, age %.1f s)",
             candidate.get("event_id"), state.session_id, ready, age)
    state.expired_event_id = candidate.get("event_id")
    await _new_detector_session(state)
    state.emotion_event_start = None
    log.info("intake detector of session %s: re-armed with a fresh detector session", state.session_id)


def _update_frame_rate(state: MonitorSession, timestamp: float) -> None:
    """Server-side frame-rate gate over the packets' capture timestamps."""
    times = state.capture_times
    if times and timestamp <= times[-1]:
        # A capture clock that repeats or runs backwards can't be trusted: restart the window.
        times.clear()
    times.append(timestamp)
    while times[0] < timestamp - FPS_WINDOW_SECONDS:
        times.popleft()
    span = times[-1] - times[0]
    fps = (len(times) - 1) / span if span > 0 else 0.0
    gap = times[-1] - times[-2] if len(times) > 1 else None
    state.landmark_fps = round(fps, 1)
    state.degraded = (span < FPS_MIN_SPAN_SECONDS or fps < FPS_MIN
                      or gap is None or gap > FPS_MAX_GAP_SECONDS)


def _hold_reason(state: MonitorSession, candidate: dict) -> str | None:
    """Why a confirmed candidate must not auto-commit, or None when it may."""
    if state.mode != "dose" or state.intk_id is None:
        return "observe"
    if state.degraded or candidate.get("degraded"):
        return "degraded"
    if not state.auto_commit:
        return "auto_commit_off"
    return None


def _hold(candidate: dict, reason: str) -> None:
    # Policy may downgrade, never upgrade: a held 'confirmed' becomes 'uncertain'.
    if candidate["decision"] == "confirmed":
        candidate["detector_decision"] = "confirmed"
        candidate["decision"] = "uncertain"
    candidate["hold_reason"] = reason


def vision_interval(state: MonitorSession) -> float:
    """How often a robot's streamed frames get identity + emotion: 4 times a second while the verified patient's
    dose has no emotion result yet (so its before/after windows get enough uncovered samples), otherwise every 0.5 s.
    Never faster while unverified: each call then runs identity, which must stay at most 2 Hz."""
    if state.intk_id is not None and not state.dose_emotion_done and state.verified():
        return DOSE_VISION_INTERVAL
    return VISION_INTERVAL


def openvino_only(state: MonitorSession) -> bool:
    """The identity rule of a check-in's camera: a robot session that started in observe mode for a check-in task
    (the search before the chat), or that /api/device/monitor/start received with companion true (robot app 0.9.0,
    which keeps its camera on during the chat and never records), is marked checkin_search and verifies the patient
    with the OpenVINO match alone. MediaPipe's short-range face detector misses faces under about 50 px wide (beyond
    about 1.35 m from the robot), where dashboard login, which uses OpenVINO alone, still works. Under this rule a hit
    needs a patient face at least OPENVINO_ONLY_MIN_FACE_FRACTION of the frame's width wide. No dose is ever recorded
    from such a session. Every other session also needs a MediaPipe face on the same box: dose sessions (also once a
    record switched them to observe), browser sessions, and the search and the watch of a dose task. Narrowed to
    check-in searches after the final review of 11 Oct: a dose task's search could verify a patient the strict dose
    session after it then found 'ambiguous', and the robot asked that patient for the person whose medicine it was."""
    return state.client_type == "reachy" and state.started_mode == "observe" and state.checkin_search


def _log_identity(state: MonitorSession, rule: str, distance: float | None, box_px: int | None,
                  ov_faces: int, mp_faces: int) -> None:
    """One INFO line for a robot session's identity check: on every change of identity_status, otherwise at most one
    per IDENTITY_LOG_SECONDS. Numbers only: no image, name or face label."""
    if state.client_type != "reachy":
        return
    now = time.monotonic()
    if state.identity_status == state.identity_logged_status and now - state.identity_logged_at < IDENTITY_LOG_SECONDS:
        return
    state.identity_logged_status, state.identity_logged_at = state.identity_status, now
    log.info("identity of session %s: status=%s rule=%s distance=%s box_px=%s ov_faces=%d mp_faces=%d",
             state.session_id, state.identity_status, rule,
             "none" if distance is None else f"{distance:.3f}", "none" if box_px is None else box_px,
             ov_faces, mp_faces)


class MonitorRegistry:
    def __init__(self):
        self.sessions: dict[str, MonitorSession] = {}
        self.by_user: dict[int, str] = {}

    def _check_busy(self, u_id: int, client_type: str) -> None:
        old_id = self.by_user.get(u_id)
        old = self.sessions.get(old_id) if old_id else None
        if old and old.client_type != client_type and old.live():
            raise BusyOtherClient(old.client_type)

    def start(self, u_id: int, intk_id: int | None, face_label: str, display_name: str, *,
              mode: str = "dose", client_type: str = "browser", auto_commit: bool = True,
              checkin_search: bool = False) -> MonitorSession:
        if mode not in MODES:
            raise ValueError("Unknown monitor mode")
        if client_type not in CLIENT_TYPES:
            raise ValueError("Unknown client type")
        if mode == "dose" and intk_id is None:
            raise ValueError("A dose session needs a dose")
        if mode == "observe":
            intk_id, auto_commit = None, False
        # Synchronous re-check: no other start can interleave between it and the registry write.
        self._check_busy(u_id, client_type)
        old_id = self.by_user.get(u_id)
        if old_id in self.sessions:
            old = self.sessions.pop(old_id)
            old.ended = True
            # The replaced session's dose gets its emotion result now (dose left pending, or resolved earlier).
            dose_emotion.finalize_soon(old, "replaced")
        # Sessions ended by the HTTP endpoint should not accumulate forever.
        for session_id, session in tuple(self.sessions.items()):
            if session.ended:
                self.sessions.pop(session_id, None)
                if self.by_user.get(session.u_id) == session_id:
                    self.by_user.pop(session.u_id, None)
        state = MonitorSession(str(uuid.uuid4()), str(uuid.uuid4()), u_id, intk_id, face_label.lower(), display_name,
                               mode=mode, client_type=client_type, auto_commit=bool(auto_commit),
                               checkin_search=bool(checkin_search))
        self.sessions[state.session_id] = state
        self.by_user[u_id] = state.session_id
        return state

    async def replace(self, u_id: int, intk_id: int | None, face_label: str, display_name: str, *,
                      mode: str = "dose", client_type: str = "browser", auto_commit: bool = True,
                      checkin_search: bool = False) -> MonitorSession:
        """Close an existing detector session before replacing its registry entry.

        First holder wins across client types: a live session (activity within
        LIVE_SESSION_SECONDS) of the other client type raises BusyOtherClient.
        """
        if mode not in MODES or client_type not in CLIENT_TYPES or (mode == "dose" and intk_id is None):
            raise ValueError("Invalid monitor session request")
        self._check_busy(u_id, client_type)
        old_id = self.by_user.get(u_id)
        old = self.sessions.get(old_id) if old_id else None
        if old and not old.ended:
            async with old.lock:
                old.ended = True
                await IntakeDetectionService.get_instance().end_session(old.u_id, old.detector_session_id)
        return self.start(u_id, intk_id, face_label, display_name,
                          mode=mode, client_type=client_type, auto_commit=auto_commit, checkin_search=checkin_search)

    async def end(self, state: MonitorSession) -> None:
        async with state.lock:
            if not state.ended:
                state.ended = True
                await IntakeDetectionService.get_instance().end_session(state.u_id, state.detector_session_id)
        self.sessions.pop(state.session_id, None)
        if self.by_user.get(state.u_id) == state.session_id:
            self.by_user.pop(state.u_id, None)
        dose_emotion.finalize_soon(state, "session_end")

    async def sweep_idle(self, max_idle: float = IDLE_SESSION_SECONDS) -> int:
        """End sessions with no landmark packet for max_idle seconds: a closed tab or a robot that went away without
        /end. Their detector state is freed and their dose gets its emotion result. Returns how many were ended."""
        now = time.monotonic()
        stale = [state for state in tuple(self.sessions.values()) if now - state.last_activity_at > max_idle]
        for state in stale:
            try:
                await self.end(state)
            except Exception:
                log.exception("could not end idle monitor session %s", state.session_id)
        return len(stale)

    def get(self, u_id: int, session_id: str, generation: str, client_type: str | None = None) -> MonitorSession:
        state = self.sessions.get(session_id)
        if not state or state.u_id != u_id or state.generation != generation or state.ended or self.by_user.get(u_id) != session_id:
            raise ValueError("Session expired or belongs to another account")
        if client_type is not None and state.client_type != client_type:
            raise ValueError("Session belongs to another client")
        return state

    async def landmarks(self, state: MonitorSession, packet: dict) -> dict:
        async with state.lock:
            frame_seq = int(packet["frame_seq"])
            if frame_seq <= state.last_frame_seq:
                return state.public()
            state.last_frame_seq = frame_seq
            state.last_packet = packet
            now = time.monotonic()
            state.last_activity_at = now
            _update_frame_rate(state, float(packet["timestamp"]))
            state.packets[frame_seq] = packet
            state.packet_times[frame_seq] = now
            cutoff = frame_seq - PACKET_HISTORY
            for old_seq in tuple(state.packets):
                if old_seq < cutoff:
                    del state.packets[old_seq]
            for old_seq in tuple(state.packet_times):
                if old_seq < cutoff:
                    del state.packet_times[old_seq]
            await _expire_candidate(state, now)
            if state.client_type == "reachy":
                _accept_robot_emotion(state, packet, now)
            # A dose session pauses detection while its candidate is resolved.
            # After a record the session is in observe mode and keeps detecting.
            if state.mode == "dose" and (state.recorded or state.candidate):
                return state.public()
            verified = state.verified(now)
            timestamp = float(packet["timestamp"])
            if not verified:
                state.ownership.clear()
            selected = (select_owned_observations(packet["faces"], packet["poses"], packet["hands"], state.target_box,
                                                  memory=state.ownership, timestamp=timestamp) if verified else None)
            if selected is not None:
                state.ownership.update(selected, timestamp)
            if not selected:
                if state.ownership_lost_at is None:
                    stage = (state.latest_detector or {}).get("stage")
                    if stage in ACTIVE_EVENT_STAGES:
                        # The first frame of a gap in an active event: it survives OWNERSHIP_GRACE_SECONDS.
                        state.ownership_lost_at, state.ownership_lost_stage = timestamp, stage
                        state.event_ownership_gap = True
                    else:
                        state.emotion_event_start = None
                if state.ownership_lost_at is not None and timestamp - state.ownership_lost_at >= OWNERSHIP_GRACE_SECONDS:
                    stage = state.ownership_lost_stage
                    await _new_detector_session(state)
                    log.info("intake detector of session %s: reset at stage %s (%s)", state.session_id, stage,
                             "ownership" if verified else "identity")
                    state.emotion_event_start = None
                state.latest_detector = {"stage": "WAITING_FOR_PEARL", "decision": "none"}
                return state.public()
            state.ownership_lost_at = state.ownership_lost_stage = None
            if selected.get("carried"):
                # This frame's ownership rests partly on memory, not on its own vote: an event that uses it may
                # prompt, never auto-log (the ownership-gap cap).
                state.event_ownership_gap = True
            payload = {"frame_seq": frame_seq, "timestamp": float(packet["timestamp"]),
                       "width": packet["width"], "height": packet["height"],
                       "face_landmarks": full_points(selected["face"], FACE_INDICES),
                       "hand_landmarks": [full_points(hand, HAND_INDICES) for hand in selected["hands"]]}
            hand_depth = packet.get("hand_depth")
            if hand_depth is not None:
                # Robot frames with the depth model: the owned hands' depth against the patient's face, in step
                # with hand_landmarks (services/landmarks/depth.py).
                payload["hand_depth"] = [hand_depth[index] if index < len(hand_depth) else None
                                         for index in selected["hand_indices"]]
            hand_objects = packet.get("hand_objects")
            if hand_objects is not None:
                # Robot frames with the object detector: what each owned hand holds (services/landmarks/objects.py).
                payload["hand_objects"] = [hand_objects[index] if index < len(hand_objects) else None
                                           for index in selected["hand_indices"]]
            result = await IntakeDetectionService.get_instance().process_frame(
                state.u_id, state.detector_session_id, payload,
                result_transform=partial(apply_policy, ownership_gap=state.event_ownership_gap))
            if result.get("reset_reason"):
                # The temporal pipeline gave up on an event (a timeout, or a closed-mouth sequence): once per event.
                log.info("intake detector of session %s: reset at stage %s (%s)", state.session_id,
                         (state.latest_detector or {}).get("stage"), result["reset_reason"])
            state.latest_detector = {key: result.get(key) for key in
                                     ("stage", "decision", "event_confidence", "mouth_open", "hand_near_mouth", "frame_seq")}
            state.latest_detector["evidence"] = detector_evidence(result)   # the live agent's intake tool reads it
            stage = result.get("stage")
            if stage == "APPROACHING" and state.event_started_at is None:
                state.event_started_at = now
            if stage in ACTIVE_EVENT_STAGES and state.emotion_event_start is None:
                # The detector set its own event start on this frame, also when it went straight to AT_MOUTH.
                state.emotion_event_start = now
            event_id = (result.get("policy") or {}).get("event_id")
            is_event = result.get("decision") in ("confirmed", "uncertain") and bool(event_id)
            if is_event:
                event_start = state.emotion_event_start if state.emotion_event_start is not None else now
                state.emotion_event_start = None     # the next event starts at its own first active stage
            elif stage not in EVENT_STAGES:
                state.emotion_event_start = None     # an approach that came to nothing
            if stage not in EVENT_STAGES:
                state.event_ownership_gap = False    # the event closed, or the detector gave it up
            if is_event and state.mode != "dose":
                # Observe mode: report the event, never turn it into a candidate.
                if all(event["event_id"] != event_id for event in state.extra_events):
                    state.extra_events.append({"event_id": event_id, "decision": result["decision"],
                                               "confidence": float(result.get("event_confidence") or 0),
                                               "frame_seq": frame_seq, "evidence": detector_evidence(result)})
                    del state.extra_events[:-EXTRA_EVENT_CAP]
            elif is_event:
                # The commit's emotion row (and the alert job's input), as before: the faces since this event's
                # start, now only the uncovered ones (a covered mouth biases the model). The dose's full result,
                # with the before- and after-windows, is dose_emotion's.
                state.last_event = (event_start, now)
                average = dose_emotion.unoccluded_mean(state.emotion_samples, event_start)
                state.candidate = {"event_id": result["policy"]["event_id"],
                                   "decision": result["decision"],
                                   "confidence": float(result.get("event_confidence") or 0),
                                   "created_at": now, "frame_seq": frame_seq,
                                   "emotion_probabilities": average,
                                   "identity_distance": state.identity_distance,
                                   "degraded": state.degraded,
                                   "evidence": detector_evidence(result),
                                   "ready": False}
                state.candidate_used = None
                reason = _hold_reason(state, state.candidate)
                if reason:
                    _hold(state.candidate, reason)
                log.info("intake candidate %s of session %s (detector session %s): created at frame_seq=%d, "
                         "decision=%s, confidence=%.2f, hold=%s", event_id, state.session_id,
                         state.detector_session_id, frame_seq, state.candidate["decision"],
                         state.candidate["confidence"], state.candidate.get("hold_reason") or "none")
            return state.public()

    async def vision(self, state: MonitorSession, frame_seq: int, image_bytes: bytes, commit_callback) -> dict:
        frame = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if frame is None or frame.shape[0] > 1080 or frame.shape[1] > 1920:
            raise ValueError("Invalid camera frame")
        async with state.lock:
            now = time.monotonic()
            await _expire_candidate(state, now)
            # Use the landmark packet for this exact camera frame. Reading
            # last_packet here made normal request skew discard almost every
            # identity result once landmark capture ran faster than JPEG
            # uploads, and could associate a face with another frame.
            packet = state.packets.get(frame_seq)
            if frame_seq <= state.last_vision_seq or packet is None:
                return state.public()
            state.last_vision_seq = frame_seq
            pending_candidate = (state.candidate is not None and not state.candidate.get("ready", False)
                                 and frame_seq >= int(state.candidate.get("frame_seq", frame_seq)))
            # A detector candidate can be emitted while the previous identity
            # proof is still fresh. Require a new face match from this event
            # frame (or a later one) before allowing a commit.
            run_identity = pending_candidate or time.monotonic() - state.verified_at >= .5 or not state.verified()
            faces = packet["faces"]
        loop = asyncio.get_running_loop()
        matches = None
        if run_identity:
            matches = await loop.run_in_executor(None, FaceRecognitionService.get_instance().identify_faces, frame)
        async with state.lock:
            # A newer vision call owns the state now; never let this slower
            # inference overwrite its identity, emotion, or candidate.
            packet = state.packets.get(frame_seq)
            if state.ended or state.last_vision_seq != frame_seq or packet is None:
                return state.public()
            now = time.monotonic()
            if matches is not None:
                rule = "openvino_only" if openvino_only(state) else "with_mediapipe"
                targets = [face for face in matches["faces"] if face["label"].lower() == state.face_label]
                if matches["error"] or matches["saturated"] or len(targets) != 1:
                    state.identity_hits = 0
                    state.target_box = None
                    state.identity_status = "mismatch" if matches["faces"] else "searching"
                else:
                    target = targets[0]
                    h, w = frame.shape[:2]
                    box = [target["box"][0] / w, target["box"][1] / h, target["box"][2] / w, target["box"][3] / h]
                    failed = None   # the status of a check that is not a hit
                    if rule == "with_mediapipe":
                        # The MediaPipe gate: a MediaPipe face on the same box, in a frame of fewer than 4 faces.
                        geometric_matches = sorted(((overlap(face["box"], box), i) for i, face in enumerate(faces)),
                                                   reverse=True)
                        if not geometric_matches or geometric_matches[0][0] < .20 or len(faces) >= 4:
                            failed = "ambiguous"
                    elif target["box"][2] < OPENVINO_ONLY_MIN_FACE_FRACTION * w:
                        failed = "searching"   # too small to trust the OpenVINO match alone
                    if failed:
                        state.identity_hits = 0
                        state.target_box = None
                        state.identity_status = failed
                    else:
                        if state.last_identity_box and overlap(box, state.last_identity_box) < .20:
                            state.identity_hits = 0
                        state.identity_hits += 1
                        state.last_identity_box = box
                        state.target_box = box
                        state.identity_frame_seq = frame_seq
                        state.identity_distance = target["distance"]
                        state.verified_at = now
                        if state.identity_hits >= 2:
                            state.confirmed_at = now
                        state.identity_status = "verified" if state.identity_hits >= 2 else "verifying"
                # The closest patient-labelled face (its OpenVINO box is in pixels of this frame). A frame of 4 or
                # more faces comes back saturated with no faces at all: logged as 4.
                best = min(targets, key=lambda face: face["distance"], default=None)
                _log_identity(state, rule, None if best is None else best["distance"],
                              None if best is None else best["box"][2],
                              4 if matches["saturated"] else len(matches["faces"]), len(faces))
            if not state.verified(now) or state.target_box is None:
                state.emotion, state.emotion_occluded = None, False
                return state.public()
            box = state.target_box
            h, w = frame.shape[:2]
            pixel_box = (int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h))
            selected = select_owned_observations(faces, packet["poses"], packet["hands"], box)
            if selected is None:
                state.emotion, state.emotion_occluded = None, False
                return state.public()
            # A covered mouth (the pill going in) is still scored, as an occluded sample for the dose's result
            # (dose_emotion prefers uncovered faces around the event); the live emotion shows only uncovered faces.
            occluded = _mouth_hidden(selected, packet["hands"], box)
            if occluded:
                state.emotion = None
            sample_time = state.packet_times.get(frame_seq, now)
        # A robot doing its own vision scores emotion itself and sends it with its landmark packets, so its
        # snapshot is identity-only. A robot streaming frames (vision_engine set) relies on the server for both.
        robot_scores_emotion = state.client_type == "reachy" and state.vision_engine is None
        emotion = None if robot_scores_emotion else await loop.run_in_executor(
            None, EmotionService.get_instance().predict_crop, crop_face(frame, pixel_box))
        async with state.lock:
            if not state.verified() or state.ended or state.last_vision_seq != frame_seq:
                return state.public()
            if emotion is not None:
                if emotion.get("detected"):
                    dose_emotion.add_sample(state, sample_time, emotion["probabilities"], occluded, "server")
                    state.emotion_occluded = occluded
                    if not occluded:
                        state.emotion = emotion
                elif not occluded:
                    state.emotion = None
            if occluded:
                # Unchanged recording policy: a candidate becomes ready only on a frame with the mouth uncovered.
                return state.public()
            if state.candidate and not state.candidate["ready"]:
                await _expire_candidate(state, time.monotonic())   # an unready one's 5 s, after the identity call
            if state.candidate and not state.candidate["ready"]:
                if (frame_seq >= state.candidate["frame_seq"]
                        and state.identity_frame_seq >= state.candidate["frame_seq"]
                        and state.verified()):
                    state.candidate["ready"] = True
                    reason = _hold_reason(state, state.candidate)
                    log.info("intake candidate %s of session %s: ready after %.1f s", state.candidate["event_id"],
                             state.session_id, time.monotonic() - state.candidate["created_at"])
                    if reason:
                        _hold(state.candidate, reason)
                    elif state.candidate["decision"] == "confirmed" and state.mode == "dose":
                        # The commit uses the candidate whatever its outcome (a refusal alerted family already).
                        mark_candidate_used(state, state.candidate["event_id"], "auto_commit")
                        state.recorded = await commit_callback(state, state.candidate, "auto")
                        state.mode = "observe"
            return state.public()


registry = MonitorRegistry()
