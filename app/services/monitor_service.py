"""Bind one live intake detector to the authenticated person's face and hands."""

import asyncio
import datetime
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

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

# The detector's thresholds are per-frame deltas calibrated at ~15 fps
# (CLAUDE.md), so a session only auto-commits while landmark capture keeps up.
FPS_WINDOW_SECONDS = 3.0
FPS_MIN_SPAN_SECONDS = 1.0
FPS_MIN = 12.0
FPS_MAX_GAP_SECONDS = 0.25

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


def select_owned_observations(faces: list, poses: list, hands: list, target_box: list | None):
    """Associate the enrolled face to exactly one pose, then its visible wrists to hands."""
    if target_box is None or len(faces) >= 4 or len(poses) >= 4 or len(hands) >= 8:
        return None
    face_matches = sorted(((overlap(face["box"], target_box), i) for i, face in enumerate(faces)), reverse=True)
    if not face_matches or face_matches[0][0] < .20 or (len(face_matches) > 1 and face_matches[0][0] - face_matches[1][0] < .12):
        return None
    target_index = face_matches[0][1]
    face = faces[target_index]
    if len(face.get("points", [])) != len(FACE_INDICES):
        return None
    x, y, width, height = face["box"]
    center = (x + width / 2, y + height / 2)
    pose_matches = []
    for i, pose in enumerate(poses):
        nose = pose.get("nose")
        if nose and x - .04 <= nose[0] <= x + width + .04 and y - .04 <= nose[1] <= y + height + .04:
            pose_matches.append((point_distance(nose, center), i))
    pose_matches.sort()
    if not pose_matches or (len(pose_matches) > 1 and pose_matches[1][0] - pose_matches[0][0] < .03):
        return None
    pose_index = pose_matches[0][1]
    pose = poses[pose_index]
    shoulders = pose.get("shoulders", [])
    if len(shoulders) != 2:
        return None
    shoulder_width = point_distance(shoulders[0], shoulders[1])
    if shoulder_width < .04:
        return None
    owned = []
    for hand in hands:
        if len(hand) != len(HAND_INDICES):
            continue
        root = hand[0]
        distances = []
        for other_index, other_pose in enumerate(poses):
            for wrist in other_pose.get("wrists", []):
                if wrist and len(wrist) >= 3 and wrist[2] >= .5:
                    distances.append((point_distance(root, wrist), other_index))
        distances.sort()
        if not distances:
            continue
        nearest, owner = distances[0]
        margin = max(.02, shoulder_width * .15)
        if nearest > max(.06, shoulder_width * .8):
            continue
        if len(distances) > 1 and distances[1][1] != owner and distances[1][0] - nearest < margin:
            return None
        if owner == pose_index:
            owned.append(hand)
    return {"face": face["points"], "hands": owned[:2], "box": face["box"], "face_index": target_index}


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
    identity_hits: int = 0
    identity_frame_seq: int = 0
    target_box: list | None = None
    last_identity_box: list | None = None
    identity_distance: float | None = None
    identity_status: str = "searching"
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
    candidate: dict | None = None
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


def _expire_candidate(state: MonitorSession, now: float) -> None:
    """Drop a prompt that was never completed, even when vision is idle."""
    candidate = state.candidate
    if candidate and now - float(candidate.get("created_at", now)) > CANDIDATE_TIMEOUT_SECONDS:
        state.candidate = None


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
              mode: str = "dose", client_type: str = "browser", auto_commit: bool = True) -> MonitorSession:
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
                               mode=mode, client_type=client_type, auto_commit=bool(auto_commit))
        self.sessions[state.session_id] = state
        self.by_user[u_id] = state.session_id
        return state

    async def replace(self, u_id: int, intk_id: int | None, face_label: str, display_name: str, *,
                      mode: str = "dose", client_type: str = "browser", auto_commit: bool = True) -> MonitorSession:
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
                          mode=mode, client_type=client_type, auto_commit=auto_commit)

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
            _expire_candidate(state, now)
            if state.client_type == "reachy":
                _accept_robot_emotion(state, packet, now)
            # A dose session pauses detection while its candidate is resolved.
            # After a record the session is in observe mode and keeps detecting.
            if state.mode == "dose" and (state.recorded or state.candidate):
                return state.public()
            selected = select_owned_observations(packet["faces"], packet["poses"], packet["hands"], state.target_box) if state.verified(now) else None
            if not selected:
                if state.latest_detector and state.latest_detector.get("stage") in ("APPROACHING", "AT_MOUTH", "OCCLUDED", "WITHDRAWING"):
                    await IntakeDetectionService.get_instance().end_session(state.u_id, state.detector_session_id)
                    state.detector_session_id = str(uuid.uuid4())
                    state.event_started_at = None
                state.emotion_event_start = None
                state.latest_detector = {"stage": "WAITING_FOR_PEARL", "decision": "none"}
                return state.public()
            payload = {"frame_seq": frame_seq, "timestamp": float(packet["timestamp"]),
                       "width": packet["width"], "height": packet["height"],
                       "face_landmarks": full_points(selected["face"], FACE_INDICES),
                       "hand_landmarks": [full_points(hand, HAND_INDICES) for hand in selected["hands"]]}
            result = await IntakeDetectionService.get_instance().process_frame(
                state.u_id, state.detector_session_id, payload, result_transform=apply_policy)
            state.latest_detector = {key: result.get(key) for key in
                                     ("stage", "decision", "event_confidence", "mouth_open", "hand_near_mouth", "frame_seq")}
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
            if is_event and state.mode != "dose":
                # Observe mode: report the event, never turn it into a candidate.
                if all(event["event_id"] != event_id for event in state.extra_events):
                    state.extra_events.append({"event_id": event_id, "decision": result["decision"],
                                               "confidence": float(result.get("event_confidence") or 0),
                                               "frame_seq": frame_seq})
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
                                   "ready": False}
                reason = _hold_reason(state, state.candidate)
                if reason:
                    _hold(state.candidate, reason)
            return state.public()

    async def vision(self, state: MonitorSession, frame_seq: int, image_bytes: bytes, commit_callback) -> dict:
        frame = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if frame is None or frame.shape[0] > 1080 or frame.shape[1] > 1920:
            raise ValueError("Invalid camera frame")
        async with state.lock:
            now = time.monotonic()
            _expire_candidate(state, now)
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
                targets = [face for face in matches["faces"] if face["label"].lower() == state.face_label]
                if matches["error"] or matches["saturated"] or len(targets) != 1:
                    state.identity_hits = 0
                    state.target_box = None
                    state.identity_status = "mismatch" if matches["faces"] else "searching"
                else:
                    target = targets[0]
                    h, w = frame.shape[:2]
                    box = [target["box"][0] / w, target["box"][1] / h, target["box"][2] / w, target["box"][3] / h]
                    geometric_matches = sorted(((overlap(face["box"], box), i) for i, face in enumerate(faces)), reverse=True)
                    if not geometric_matches or geometric_matches[0][0] < .20 or len(faces) >= 4:
                        state.identity_hits = 0
                        state.target_box = None
                        state.identity_status = "ambiguous"
                    else:
                        if state.last_identity_box and overlap(box, state.last_identity_box) < .20:
                            state.identity_hits = 0
                        state.identity_hits += 1
                        state.last_identity_box = box
                        state.target_box = box
                        state.identity_frame_seq = frame_seq
                        state.identity_distance = target["distance"]
                        state.verified_at = now
                        state.identity_status = "verified" if state.identity_hits >= 2 else "verifying"
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
                if time.monotonic() - state.candidate["created_at"] > CANDIDATE_TIMEOUT_SECONDS:
                    state.candidate = None
                elif (frame_seq >= state.candidate["frame_seq"]
                      and state.identity_frame_seq >= state.candidate["frame_seq"]
                      and state.verified()):
                    state.candidate["ready"] = True
                    reason = _hold_reason(state, state.candidate)
                    if reason:
                        _hold(state.candidate, reason)
                    elif state.candidate["decision"] == "confirmed" and state.mode == "dose":
                        state.recorded = await commit_callback(state, state.candidate, "auto")
                        state.mode = "observe"
            return state.public()


registry = MonitorRegistry()
