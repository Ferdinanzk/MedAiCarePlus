"""Observation-tolerant control plane for live pill-intake detection.

This module deliberately decides *when an event exists*.  The legacy scorer may
still describe event style, but missing observations and request timing are
handled here rather than being mistaken for negative evidence.
"""
from dataclasses import dataclass, field
from enum import Enum
from math import hypot
from typing import Dict, List, Optional


class Stage(str, Enum):
    CALIBRATING = "CALIBRATING"
    READY = "READY"
    APPROACHING = "APPROACHING"
    AT_MOUTH = "AT_MOUTH"
    OCCLUDED = "OCCLUDED"
    WITHDRAWING = "WITHDRAWING"
    COMPLETE_CANDIDATE = "COMPLETE_CANDIDATE"
    COOLDOWN = "COOLDOWN"
    RESET = "RESET"


@dataclass
class Observation:
    center: tuple[float, float]
    distance: float
    mouth_open: bool
    pinch: bool
    flat_palm: bool
    occlusion: float


@dataclass
class Track:
    track_id: int
    center: tuple[float, float]
    distance: float
    timestamp: float
    missing_since: Optional[float] = None


@dataclass
class TemporalEvent:
    stage: Stage = Stage.CALIBRATING
    stage_since: float = 0.0
    active_track: Optional[int] = None
    started_at: Optional[float] = None
    contact_at: Optional[float] = None
    last_contact_at: Optional[float] = None
    min_distance: float = 99.0
    pre_contact_mouth_open: bool = False
    contact_mouth_open: bool = False
    post_contact_mouth_open: bool = False
    pinch_seen: bool = False
    flat_palm_ratio_sum: float = 0.0
    samples: int = 0
    outward_seen: bool = False
    candidate_id: int = 0
    occluded_since: Optional[float] = None


class TemporalIntakePipeline:
    ENTRY_DISTANCE = 0.78
    EXIT_DISTANCE = 1.12
    APPROACH_VELOCITY = -0.16       # face-widths / second
    WITHDRAW_VELOCITY = 0.12
    CALIBRATION_SECONDS = 0.35
    OCCLUSION_GRACE_SECONDS = 0.55
    EVENT_TIMEOUT_SECONDS = 4.5
    COOLDOWN_SECONDS = 1.25
    TRACK_GRACE_SECONDS = 0.65
    TRACK_MATCH_DISTANCE = 0.35
    REACQUIRE_MATCH_DISTANCE = 0.75
    LOST_EVENT_CLOSE_SECONDS = 0.85
    MIN_WITHDRAW_DELTA = 0.22

    def __init__(self):
        self.event = TemporalEvent()
        self.tracks: Dict[int, Track] = {}
        self.next_track_id = 1
        self.last_timestamp: Optional[float] = None
        self.last_missing: Optional[str] = "calibration"
        self.transition_reason = "session_started"
        self.waiting_reason = "calibration"
        self.approach_velocity = 0.0
        self.withdrawal_velocity = 0.0
        self.hand_lost = False
        self.reacquired = False
        self.completion_reason: Optional[str] = None
        self.reset_reason: Optional[str] = None

    def _stage(self, stage: Stage, now: float, reason: str) -> None:
        self.event.stage = stage
        self.event.stage_since = now
        self.transition_reason = reason

    def _match(self, observations: List[Observation], now: float) -> Dict[int, Observation]:
        available = set(self.tracks)
        matched: Dict[int, Observation] = {}
        for obs in sorted(observations, key=lambda o: o.center):
            candidates = [(hypot(obs.center[0]-self.tracks[i].center[0], obs.center[1]-self.tracks[i].center[1]), i) for i in available]
            distance, track_id = min(candidates, default=(99.0, -1))
            if distance > self.TRACK_MATCH_DISTANCE:
                track_id = self.next_track_id
                self.next_track_id += 1
                self.tracks[track_id] = Track(track_id, obs.center, obs.distance, now)
            else:
                available.remove(track_id)
            matched[track_id] = obs
        for track_id, obs in matched.items():
            track = self.tracks[track_id]
            track.missing_since = None
            track.center = obs.center
        for track_id in list(self.tracks):
            if track_id not in matched:
                track = self.tracks[track_id]
                track.missing_since = track.missing_since or now
                if now - track.missing_since > self.TRACK_GRACE_SECONDS and track_id != self.event.active_track:
                    del self.tracks[track_id]
        return matched

    def process(self, now: float, face_reliable: bool, observations: List[Observation]) -> dict:
        self.reacquired = False
        self.hand_lost = False
        self.completion_reason = None
        self.reset_reason = None
        if self.last_timestamp is None:
            self.event.stage_since = now
        if self.last_timestamp is not None and now <= self.last_timestamp:
            return self.result("stale_timestamp", accepted=False)
        self.last_timestamp = now
        matched = self._match(observations, now)
        e = self.event

        if e.stage == Stage.CALIBRATING:
            self.last_missing = None if face_reliable else "face"
            if face_reliable and now - e.stage_since >= self.CALIBRATION_SECONDS:
                self._stage(Stage.READY, now, "face_calibrated")

        active_obs = matched.get(e.active_track) if e.active_track else None
        if e.active_track is not None and active_obs is None:
            self.hand_lost = True
            self.last_missing = "face" if not face_reliable else "active_hand"
            if e.stage in (Stage.AT_MOUTH, Stage.WITHDRAWING) and e.last_contact_at is not None and now - e.last_contact_at <= self.OCCLUSION_GRACE_SECONDS:
                e.occluded_since = e.occluded_since or now
                self._stage(Stage.OCCLUDED, now, "active_hand_lost_after_contact")
            elif e.stage == Stage.OCCLUDED:
                # MediaPipe frequently assigns a new track after face overlap.
                # Reassociate a sole/plausibly-near observation and use a clear
                # outside-zone reacquisition as direct withdrawal evidence.
                old_track = self.tracks.get(e.active_track)
                candidates = []
                if old_track:
                    candidates = [(hypot(o.center[0]-old_track.center[0], o.center[1]-old_track.center[1]), tid, o) for tid, o in matched.items()]
                distance, track_id, candidate = min(candidates, default=(99.0, -1, None))
                if candidate is not None and (distance <= self.REACQUIRE_MATCH_DISTANCE or len(matched) == 1):
                    e.active_track = track_id
                    active_obs = candidate
                    self.reacquired = True
                    self.hand_lost = False
                    self.last_missing = None
                    if candidate.distance >= self.EXIT_DISTANCE:
                        e.outward_seen = True
                        return self._complete(now, "reacquired_outside_exit_zone")
                    self._stage(Stage.AT_MOUTH, now, "active_hand_reacquired_inside_contact_zone")
                elif e.occluded_since is not None and now - e.occluded_since >= self.LOST_EVENT_CLOSE_SECONDS:
                    # Never remain stuck forever. Loss alone is not enough for
                    # auto-confirmation, so this closes as manual fallback.
                    return self._complete(now, "hand_lost_after_contact", force_uncertain=True)
            elif e.started_at is not None and now - e.started_at > self.EVENT_TIMEOUT_SECONDS:
                self._reset(now, reason="event_timeout_before_contact_completion")
            if active_obs is None:
                return self.result()

        self.last_missing = None if face_reliable else "face"
        if not face_reliable:
            return self.result()

        if e.stage == Stage.READY:
            for track_id, obs in matched.items():
                track = self.tracks[track_id]
                dt = max(now - track.timestamp, 1e-3)
                velocity = (obs.distance - track.distance) / dt
                if obs.distance < self.EXIT_DISTANCE and velocity <= self.APPROACH_VELOCITY:
                    e.active_track, e.started_at = track_id, now
                    e.pre_contact_mouth_open = obs.mouth_open
                    self._stage(Stage.APPROACHING, now, "inward_velocity_toward_mouth")
                    break

        active_obs = matched.get(e.active_track) if e.active_track else None
        if active_obs is not None:
            track = self.tracks[e.active_track]
            dt = max(now - track.timestamp, 1e-3)
            velocity = (active_obs.distance - track.distance) / dt
            self.approach_velocity = min(velocity, 0.0)
            self.withdrawal_velocity = max(velocity, 0.0)
            e.samples += 1
            e.pinch_seen |= active_obs.pinch
            e.flat_palm_ratio_sum += float(active_obs.flat_palm)
            e.min_distance = min(e.min_distance, active_obs.distance)
            if e.contact_at is None:
                e.pre_contact_mouth_open |= active_obs.mouth_open
            else:
                e.post_contact_mouth_open |= active_obs.mouth_open
            if e.stage == Stage.APPROACHING and active_obs.distance <= self.ENTRY_DISTANCE:
                e.contact_at = e.last_contact_at = now
                e.contact_mouth_open = active_obs.mouth_open
                self._stage(Stage.AT_MOUTH, now, "entered_contact_zone")
            elif e.stage == Stage.AT_MOUTH:
                if active_obs.distance <= self.EXIT_DISTANCE:
                    e.last_contact_at = now
                    self.waiting_reason = "waiting_for_exit_zone"
                elif velocity >= self.WITHDRAW_VELOCITY or active_obs.distance - e.min_distance >= self.MIN_WITHDRAW_DELTA:
                    e.outward_seen = True
                    self._stage(Stage.WITHDRAWING, now, "visible_exit_after_contact")
                    # Crossing the hysteresis exit boundary after established
                    # contact is already a complete withdrawal observation; a
                    # second outside frame was the live stuck-contact defect.
                    return self._complete(now, "visible_exit_after_contact")
            elif e.stage == Stage.OCCLUDED:
                if active_obs.distance >= self.EXIT_DISTANCE:
                    e.outward_seen = True
                    self._stage(Stage.WITHDRAWING, now, "reacquired_outside_exit_zone")
                    return self._complete(now, "reacquired_outside_exit_zone")
                else:
                    e.last_contact_at = now
                    self._stage(Stage.AT_MOUTH, now, "reacquired_inside_contact_zone")
            elif e.stage == Stage.WITHDRAWING and active_obs.distance >= self.EXIT_DISTANCE:
                return self._complete(now, "visible_exit_after_withdrawal")
            track.distance, track.timestamp = active_obs.distance, now

        if e.started_at is not None and now - e.started_at > self.EVENT_TIMEOUT_SECONDS:
            self._reset(now, reason="event_timeout")
        if e.stage == Stage.COOLDOWN and now - e.stage_since >= self.COOLDOWN_SECONDS and all(o.distance >= self.EXIT_DISTANCE for o in observations):
            self._reset(now, Stage.READY, "cooldown_complete")
        return self.result()

    def _complete(self, now: float, reason: str, force_uncertain: bool = False) -> dict:
        e = self.event
        self._stage(Stage.COMPLETE_CANDIDATE, now, reason)
        self.completion_reason = reason
        e.candidate_id += 1
        mouth_evidence = e.pre_contact_mouth_open or e.contact_mouth_open or e.post_contact_mouth_open
        flat_ratio = e.flat_palm_ratio_sum / max(e.samples, 1)
        confirmed = not force_uncertain and mouth_evidence and e.pinch_seen and e.outward_seen and flat_ratio < 0.75
        decision = "confirmed" if confirmed else "uncertain"
        confidence = 0.82 if confirmed else 0.48
        result = self.result(decision=decision, confidence=confidence, candidate_id=e.candidate_id)
        self._stage(Stage.COOLDOWN, now, "candidate_emitted")
        e.active_track = None
        return result

    def _reset(self, now: float, stage: Stage = Stage.RESET, reason: str = "reset") -> None:
        candidate_id = self.event.candidate_id
        self.event = TemporalEvent(stage=stage, stage_since=now, candidate_id=candidate_id)
        self.reset_reason = reason
        self.transition_reason = reason

    def result(self, missing: Optional[str] = None, accepted: bool = True, decision: str = "none", confidence: float = 0.0, candidate_id: Optional[int] = None) -> dict:
        e = self.event
        occlusion_duration = max(0.0, (self.last_timestamp or 0.0) - e.occluded_since) if e.occluded_since is not None else 0.0
        waiting = {
            Stage.CALIBRATING: "waiting_for_stable_face",
            Stage.READY: "waiting_for_approach",
            Stage.APPROACHING: "waiting_for_contact_zone",
            Stage.AT_MOUTH: "waiting_for_exit_zone",
            Stage.OCCLUDED: "waiting_for_hand_reacquisition_or_loss_completion",
            Stage.WITHDRAWING: "waiting_for_exit_zone",
            Stage.COOLDOWN: "cooldown",
            Stage.RESET: "event_reset_ready_to_retry",
        }.get(e.stage)
        return {"stage": e.stage.value, "status": e.stage.value, "missing_observation": missing or self.last_missing,
                "waiting_reason": waiting, "transition_reason": self.transition_reason, "accepted": accepted,
                "decision": decision, "event_detected": decision == "confirmed", "ingestion_detected": decision == "confirmed",
                "confidence": confidence, "event_confidence": confidence, "candidate_id": candidate_id,
                "approach_velocity": round(self.approach_velocity, 4), "withdrawal_velocity": round(self.withdrawal_velocity, 4),
                "hand_lost": self.hand_lost, "reacquired": self.reacquired, "occlusion_duration": round(occlusion_duration, 3),
                "mouth_open_before": e.pre_contact_mouth_open, "mouth_open_during": e.contact_mouth_open,
                "mouth_open_after": e.post_contact_mouth_open, "contact_distance": None if e.min_distance == 99.0 else round(e.min_distance, 3),
                "entry_distance": self.ENTRY_DISTANCE, "exit_distance": self.EXIT_DISTANCE,
                "completion_reason": self.completion_reason, "reset_reason": self.reset_reason}
