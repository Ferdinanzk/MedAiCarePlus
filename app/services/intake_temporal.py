"""Observation-tolerant control plane for live pill-intake detection.

This module decides *when a physical event exists*.  Semantic evidence decides
whether a completed event belongs in the high or middle evidence band; missing
observations and request timing never count as positive evidence.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class TemporalConfig:
    """One point in a deliberately small, reviewable parameter neighborhood."""

    entry_distance: float = 0.78
    exit_distance: float = 1.12
    approach_velocity: float = -0.16
    withdraw_velocity: float = 0.12
    calibration_seconds: float = 0.35
    occlusion_grace_seconds: float = 0.55
    event_timeout_seconds: float = 4.5
    cooldown_seconds: float = 1.25
    track_grace_seconds: float = 0.65
    track_match_distance: float = 0.35
    reacquire_match_distance: float = 0.75
    lost_event_close_seconds: float = 0.85
    min_withdraw_delta: float = 0.22


@dataclass
class Observation:
    center: tuple[float, float]
    distance: float
    mouth_open: bool
    pinch: bool
    flat_palm: bool
    occlusion: float
    mouth_open_ratio: float = 0.0
    mouth_open_delta: float = 0.0
    mouth_motion_cycles: int = 0
    tongue_score: Optional[float] = None
    tongue_quality: float = 0.0
    tongue_support: bool = False
    delivery_like: Optional[bool] = None
    contradiction: bool = False
    style: str = "unknown"

    def __post_init__(self) -> None:
        if self.delivery_like is None:
            self.delivery_like = self.pinch


@dataclass
class Track:
    track_id: int
    center: tuple[float, float]
    distance: float
    timestamp: float
    missing_since: Optional[float] = None
    velocity: float = 0.0


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
    delivery_seen: bool = False
    contradiction_seen: bool = False
    flat_palm_ratio_sum: float = 0.0
    max_occlusion: float = 0.0
    samples: int = 0
    style_counts: Dict[str, int] = field(default_factory=dict)
    outward_seen: bool = False
    peak_mouth_open_ratio: float = 0.0
    peak_mouth_open_delta: float = 0.0
    mouth_motion_cycles: int = 0
    peak_tongue_score: float = 0.0
    peak_tongue_quality: float = 0.0
    tongue_samples: List[tuple[float, bool]] = field(default_factory=list)
    tongue_support: bool = False
    candidate_id: int = 0
    occluded_since: Optional[float] = None


class TemporalIntakePipeline:
    """Construct exactly one candidate from approach/contact/withdrawal evidence."""

    # Class aliases preserve the pinned implementation's public tuning surface.
    ENTRY_DISTANCE = TemporalConfig.entry_distance
    EXIT_DISTANCE = TemporalConfig.exit_distance
    APPROACH_VELOCITY = TemporalConfig.approach_velocity
    WITHDRAW_VELOCITY = TemporalConfig.withdraw_velocity
    CALIBRATION_SECONDS = TemporalConfig.calibration_seconds
    OCCLUSION_GRACE_SECONDS = TemporalConfig.occlusion_grace_seconds
    EVENT_TIMEOUT_SECONDS = TemporalConfig.event_timeout_seconds
    COOLDOWN_SECONDS = TemporalConfig.cooldown_seconds
    TRACK_GRACE_SECONDS = TemporalConfig.track_grace_seconds
    TRACK_MATCH_DISTANCE = TemporalConfig.track_match_distance
    REACQUIRE_MATCH_DISTANCE = TemporalConfig.reacquire_match_distance
    LOST_EVENT_CLOSE_SECONDS = TemporalConfig.lost_event_close_seconds
    MIN_WITHDRAW_DELTA = TemporalConfig.min_withdraw_delta

    HIGH_EVIDENCE_CONFIDENCE = 0.82
    MIDDLE_EVIDENCE_CONFIDENCE = 0.48

    def __init__(self, config: Optional[TemporalConfig] = None):
        self.config = config or TemporalConfig()
        self.event = TemporalEvent()
        self.tracks: Dict[int, Track] = {}
        self.next_track_id = 1
        self.last_timestamp: Optional[float] = None
        self.last_missing: Optional[str] = "calibration"
        self.transition_reason = "session_started"
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
        """Associate hands by palm position, independent of MediaPipe array order."""
        available = set(self.tracks)
        matched: Dict[int, Observation] = {}
        for obs in sorted(observations, key=lambda item: item.center):
            candidates = [
                (
                    hypot(
                        obs.center[0] - self.tracks[track_id].center[0],
                        obs.center[1] - self.tracks[track_id].center[1],
                    ),
                    track_id,
                )
                for track_id in available
            ]
            distance, track_id = min(candidates, default=(99.0, -1))
            if distance > self.config.track_match_distance:
                track_id = self.next_track_id
                self.next_track_id += 1
                self.tracks[track_id] = Track(
                    track_id, obs.center, obs.distance, now
                )
            else:
                available.remove(track_id)
            matched[track_id] = obs

        for track_id, obs in matched.items():
            track = self.tracks[track_id]
            # Keep history for every visible hand, including while READY or
            # tracking another hand. Otherwise the next approach is averaged
            # over the entire idle period instead of the observation interval.
            track.velocity = (obs.distance - track.distance) / max(now - track.timestamp, 1e-3)
            track.distance = obs.distance
            track.timestamp = now
            track.missing_since = None
            track.center = obs.center

        for track_id in list(self.tracks):
            if track_id not in matched:
                track = self.tracks[track_id]
                track.missing_since = track.missing_since or now
                if (
                    now - track.missing_since > self.config.track_grace_seconds
                    and track_id != self.event.active_track
                ):
                    del self.tracks[track_id]
        return matched

    def process(
        self, now: float, face_reliable: bool, observations: List[Observation]
    ) -> dict:
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
        event = self.event

        if event.stage == Stage.RESET:
            self._stage(Stage.READY if face_reliable else Stage.CALIBRATING, now, "reset_complete")

        if event.stage == Stage.CALIBRATING:
            self.last_missing = None if face_reliable else "face"
            if not face_reliable:
                event.stage_since = now
            elif now - event.stage_since >= self.config.calibration_seconds:
                self._stage(Stage.READY, now, "face_calibrated")

        active_obs = matched.get(event.active_track) if event.active_track else None
        if event.active_track is not None and active_obs is None:
            self.hand_lost = True
            self.last_missing = "face" if not face_reliable else "active_hand"
            if (
                event.stage in (Stage.AT_MOUTH, Stage.WITHDRAWING)
                and event.last_contact_at is not None
                and now - event.last_contact_at <= self.config.occlusion_grace_seconds
            ):
                event.occluded_since = event.occluded_since or now
                self._stage(Stage.OCCLUDED, now, "active_hand_lost_after_contact")
            elif event.stage == Stage.OCCLUDED:
                active_obs = self._try_reacquire(matched, now)
                if active_obs is None and event.occluded_since is not None:
                    if now - event.occluded_since >= self.config.lost_event_close_seconds:
                        return self._complete(
                            now, "hand_lost_after_contact", force_middle=True
                        )
            elif (
                event.started_at is not None
                and now - event.started_at > self.config.event_timeout_seconds
            ):
                self._reset(now, reason="event_timeout_before_contact_completion")
            if active_obs is None:
                return self.result()

        self.last_missing = None if face_reliable else "face"
        if not face_reliable:
            return self.result()

        if event.stage == Stage.READY:
            for track_id, obs in matched.items():
                track = self.tracks[track_id]
                velocity = track.velocity
                if (
                    obs.distance < self.config.exit_distance
                    and velocity <= self.config.approach_velocity
                ):
                    event.active_track = track_id
                    event.started_at = now
                    event.pre_contact_mouth_open = obs.mouth_open
                    self._stage(Stage.APPROACHING, now, "inward_velocity_toward_mouth")
                    break

        active_obs = matched.get(event.active_track) if event.active_track else None
        if active_obs is not None:
            track = self.tracks[event.active_track]
            velocity = track.velocity
            self.approach_velocity = min(velocity, 0.0)
            self.withdrawal_velocity = max(velocity, 0.0)
            self._accumulate(active_obs, now)

            if event.stage == Stage.APPROACHING and active_obs.distance <= self.config.entry_distance:
                event.contact_at = event.last_contact_at = now
                event.contact_mouth_open = active_obs.mouth_open
                self._stage(Stage.AT_MOUTH, now, "entered_contact_zone")
            elif event.stage == Stage.AT_MOUTH:
                if active_obs.distance <= self.config.exit_distance:
                    event.last_contact_at = now
                elif (
                    velocity >= self.config.withdraw_velocity
                    or active_obs.distance - event.min_distance
                    >= self.config.min_withdraw_delta
                ):
                    event.outward_seen = True
                    self._stage(Stage.WITHDRAWING, now, "visible_exit_after_contact")
                    return self._complete(now, "visible_exit_after_contact")
            elif event.stage == Stage.OCCLUDED:
                if active_obs.distance >= self.config.exit_distance:
                    event.outward_seen = True
                    self._stage(Stage.WITHDRAWING, now, "reacquired_outside_exit_zone")
                    return self._complete(now, "reacquired_outside_exit_zone")
                event.last_contact_at = now
                self._stage(Stage.AT_MOUTH, now, "reacquired_inside_contact_zone")
            elif (
                event.stage == Stage.WITHDRAWING
                and active_obs.distance >= self.config.exit_distance
            ):
                return self._complete(now, "visible_exit_after_withdrawal")

        if (
            event.stage in (
                Stage.APPROACHING,
                Stage.AT_MOUTH,
                Stage.OCCLUDED,
                Stage.WITHDRAWING,
            )
            and
            event.started_at is not None
            and now - event.started_at > self.config.event_timeout_seconds
        ):
            self._reset(now, reason="event_timeout")

        # A medication-intake session represents one physical dose attempt.
        # Stay latched after emitting its candidate; the API's explicit session
        # end starts a fresh generation after rejection/undo.  Time-based
        # re-arming allowed one prolonged/repeated gesture to emit duplicates.
        return self.result()

    def _try_reacquire(
        self, matched: Dict[int, Observation], now: float
    ) -> Optional[Observation]:
        event = self.event
        old_track = self.tracks.get(event.active_track) if event.active_track else None
        candidates = []
        if old_track:
            candidates = [
                (
                    hypot(
                        obs.center[0] - old_track.center[0],
                        obs.center[1] - old_track.center[1],
                    ),
                    track_id,
                    obs,
                )
                for track_id, obs in matched.items()
            ]
        distance, track_id, candidate = min(candidates, default=(99.0, -1, None))
        if candidate is None or not (
            distance <= self.config.reacquire_match_distance or len(matched) == 1
        ):
            return None

        event.active_track = track_id
        self.reacquired = True
        self.hand_lost = False
        self.last_missing = None
        if candidate.distance >= self.config.exit_distance:
            event.outward_seen = True
            # The caller will complete through the OCCLUDED branch.
        return candidate

    def _accumulate(self, obs: Observation, now: float) -> None:
        event = self.event
        event.samples += 1
        event.pinch_seen |= obs.pinch
        event.delivery_seen |= bool(obs.delivery_like)
        event.contradiction_seen |= obs.contradiction
        event.flat_palm_ratio_sum += float(obs.flat_palm)
        event.max_occlusion = max(event.max_occlusion, obs.occlusion)
        event.min_distance = min(event.min_distance, obs.distance)
        event.peak_mouth_open_ratio = max(
            event.peak_mouth_open_ratio, obs.mouth_open_ratio
        )
        event.peak_mouth_open_delta = max(
            event.peak_mouth_open_delta, obs.mouth_open_delta
        )
        event.mouth_motion_cycles = max(
            event.mouth_motion_cycles, obs.mouth_motion_cycles
        )
        if obs.tongue_score is not None:
            event.peak_tongue_score = max(event.peak_tongue_score, obs.tongue_score)
        event.peak_tongue_quality = max(
            event.peak_tongue_quality, obs.tongue_quality
        )
        event.tongue_samples.append((now, obs.tongue_support))
        event.style_counts[obs.style] = event.style_counts.get(obs.style, 0) + 1
        if event.contact_at is None:
            event.pre_contact_mouth_open |= obs.mouth_open
        else:
            event.post_contact_mouth_open |= obs.mouth_open

    def _complete(
        self, now: float, reason: str, force_middle: bool = False
    ) -> dict:
        event = self.event
        self._stage(Stage.COMPLETE_CANDIDATE, now, reason)
        self.completion_reason = reason

        mouth_evidence = (
            event.pre_contact_mouth_open
            or event.contact_mouth_open
            or event.post_contact_mouth_open
        )
        if not mouth_evidence:
            result = self.result(
                decision="none",
                confidence=0.0,
                candidate_id=None,
                decision_reason="closed_mouth_sequence_rejected",
            )
            # A closed-mouth gesture is not a dose candidate. Re-arm immediately
            # so it cannot prompt and does not block a later real intake attempt.
            self._reset(now, stage=Stage.READY, reason="closed_mouth_sequence_rejected")
            self.tracks.clear()
            return result

        event.candidate_id += 1
        contact_at = event.contact_at
        last_contact_at = event.last_contact_at or contact_at
        aligned_tongue_flags = []
        if contact_at is not None and last_contact_at is not None:
            aligned_tongue_flags = [
                supported
                for sample_at, supported in event.tongue_samples
                if contact_at - 0.4 <= sample_at <= last_contact_at + 0.6
            ]
        event.tongue_support = any(
            sum(aligned_tongue_flags[index:index + 3]) >= 2
            for index in range(len(aligned_tongue_flags))
        )
        flat_ratio = event.flat_palm_ratio_sum / max(event.samples, 1)
        coherent_delivery = (
            not force_middle
            and mouth_evidence
            and event.delivery_seen
            and event.outward_seen
            and not event.contradiction_seen
            and flat_ratio < 0.75
        )
        decision = "confirmed" if coherent_delivery else "uncertain"
        tongue_supported_ambiguous = bool(
            not coherent_delivery
            and mouth_evidence
            and event.delivery_seen
            and event.outward_seen
            and event.tongue_support
        )
        confidence = self.HIGH_EVIDENCE_CONFIDENCE if coherent_delivery else (
            0.56 if tongue_supported_ambiguous else self.MIDDLE_EVIDENCE_CONFIDENCE
        )
        decision_reason = "coherent_delivery_sequence" if coherent_delivery else (
            "ambiguous_delivery_with_tongue_support"
            if tongue_supported_ambiguous
            else "completed_ambiguous_or_occluded_sequence"
        )
        result = self.result(
            decision=decision,
            confidence=confidence,
            candidate_id=event.candidate_id,
            decision_reason=decision_reason,
        )
        self._stage(Stage.COOLDOWN, now, "candidate_emitted")
        event.active_track = None
        return result

    def _reset(
        self,
        now: float,
        stage: Stage = Stage.RESET,
        reason: str = "reset",
    ) -> None:
        candidate_id = self.event.candidate_id
        self.event = TemporalEvent(
            stage=stage, stage_since=now, candidate_id=candidate_id
        )
        self.reset_reason = reason
        self.transition_reason = reason

    def result(
        self,
        missing: Optional[str] = None,
        accepted: bool = True,
        decision: str = "none",
        confidence: float = 0.0,
        candidate_id: Optional[int] = None,
        decision_reason: str = "",
    ) -> dict:
        event = self.event
        occlusion_duration = (
            max(0.0, (self.last_timestamp or 0.0) - event.occluded_since)
            if event.occluded_since is not None
            else 0.0
        )
        waiting = {
            Stage.CALIBRATING: "waiting_for_stable_face",
            Stage.READY: "waiting_for_approach",
            Stage.APPROACHING: "waiting_for_contact_zone",
            Stage.AT_MOUTH: "waiting_for_exit_zone",
            Stage.OCCLUDED: "waiting_for_hand_reacquisition_or_loss_completion",
            Stage.WITHDRAWING: "waiting_for_exit_zone",
            Stage.COOLDOWN: "cooldown",
            Stage.RESET: "event_reset_ready_to_retry",
        }.get(event.stage)
        event_style = max(event.style_counts, key=event.style_counts.get, default="none")
        flat_ratio = event.flat_palm_ratio_sum / max(event.samples, 1)
        return {
            "stage": event.stage.value,
            "status": event.stage.value,
            "missing_observation": missing or self.last_missing,
            "waiting_reason": waiting,
            "transition_reason": self.transition_reason,
            "accepted": accepted,
            "decision": decision,
            "decision_reason": decision_reason,
            "event_detected": decision == "confirmed",
            "ingestion_detected": decision == "confirmed",
            "confidence": confidence,
            "event_confidence": confidence,
            "frame_confidence": confidence,
            "candidate_id": candidate_id,
            "event_started_at": event.started_at,
            "approach_velocity": round(self.approach_velocity, 4),
            "withdrawal_velocity": round(self.withdrawal_velocity, 4),
            "hand_lost": self.hand_lost,
            "reacquired": self.reacquired,
            "occlusion_duration": round(occlusion_duration, 3),
            "mouth_open_before": event.pre_contact_mouth_open,
            "mouth_open_during": event.contact_mouth_open,
            "mouth_open_after": event.post_contact_mouth_open,
            "peak_mouth_open_ratio": round(event.peak_mouth_open_ratio, 4),
            "peak_mouth_open_delta": round(event.peak_mouth_open_delta, 4),
            "mouth_motion_cycles": event.mouth_motion_cycles,
            "tongue_peak_score": round(event.peak_tongue_score, 4),
            "tongue_peak_quality": round(event.peak_tongue_quality, 4),
            "tongue_support_frames": sum(
                int(supported) for _, supported in event.tongue_samples
            ),
            "tongue_support": event.tongue_support,
            "contact_distance": (
                None if event.min_distance == 99.0 else round(event.min_distance, 3)
            ),
            "entry_distance": self.config.entry_distance,
            "exit_distance": self.config.exit_distance,
            "completion_reason": self.completion_reason,
            "reset_reason": self.reset_reason,
            "safety_contradiction": event.contradiction_seen,
            "delivery_evidence": event.delivery_seen,
            "flat_palm_ratio": round(flat_ratio, 3),
            "peak_mouth_occlusion": round(event.max_occlusion, 3),
            "event_style": event_style,
        }
