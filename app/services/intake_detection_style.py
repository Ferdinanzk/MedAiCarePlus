from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class EventStyleAggregate:
    dwell: float
    withdrew_enough: bool
    partial_withdrawal_seen: bool
    peak_mouth_contact: float
    palm_lower_mouth_ratio: float
    min_palm_to_lower_mouth_norm: Optional[float]
    flat_palm_frame_ratio: float
    pinch_frame_ratio: float
    loose_grip_frame_ratio: float
    holding_object_frame_ratio: float
    mouth_visible_frame_ratio: float
    peak_mouth_occlusion_score: float
    palm_overlap_ratio_of_event: float
    peak_mouth_open_ratio: float
    occlusion_moderate_score: float


@dataclass(frozen=True)
class EventStyleClassification:
    flat_palm_dominant: bool
    low_pinched_delivery: bool
    brief_contact: bool
    clear_or_partial_withdrawal: bool
    palm_dump_brief_contact: bool
    palm_dump_valid_contact_duration: bool
    palm_dump_approach_then_withdrawal: bool
    palm_dump_has_lower_mouth_geometry: bool
    palm_dump_temporal_order_valid: bool
    delivery_like_occlusion: bool
    cover_like_occlusion: bool
    cover_like_reason: str
    mouth_mostly_visible: bool
    weak_or_moderate_mouth_activity: bool
    possible_palm_dump_delivery: bool
    likely_mouth_cover: bool
    event_style: str


def classify_event_style(aggregate: EventStyleAggregate) -> EventStyleClassification:
    clear_or_partial_withdrawal = aggregate.withdrew_enough or aggregate.partial_withdrawal_seen
    flat_palm_dominant = aggregate.flat_palm_frame_ratio >= 0.50
    low_pinched_delivery = (
        aggregate.pinch_frame_ratio < 0.15
        and aggregate.loose_grip_frame_ratio < 0.20
        and aggregate.holding_object_frame_ratio < 0.20
    )
    brief_contact = 0.15 <= aggregate.dwell <= 0.95
    palm_dump_brief_contact = 0.05 <= aggregate.dwell <= 0.45
    palm_dump_valid_contact_duration = 0.05 <= aggregate.dwell <= 1.00
    palm_dump_approach_then_withdrawal = clear_or_partial_withdrawal and aggregate.peak_mouth_contact >= 0.45
    palm_dump_has_lower_mouth_geometry = bool(
        aggregate.palm_lower_mouth_ratio >= 0.10
        or (
            aggregate.min_palm_to_lower_mouth_norm is not None
            and aggregate.min_palm_to_lower_mouth_norm <= 1.25
        )
    )
    palm_dump_temporal_order_valid = bool(
        aggregate.flat_palm_frame_ratio >= 0.25
        and palm_dump_valid_contact_duration
        and palm_dump_approach_then_withdrawal
        and palm_dump_has_lower_mouth_geometry
    )
    delivery_like_occlusion = bool(
        aggregate.flat_palm_frame_ratio >= 0.25
        and palm_dump_valid_contact_duration
        and clear_or_partial_withdrawal
        and aggregate.peak_mouth_contact >= 0.45
        and palm_dump_has_lower_mouth_geometry
    )
    cover_like_conditions = []
    if aggregate.dwell > 1.00:
        cover_like_conditions.append("long_dwell")
    if not clear_or_partial_withdrawal:
        cover_like_conditions.append("no_withdrawal")
    if aggregate.palm_overlap_ratio_of_event >= 0.35:
        cover_like_conditions.append("high_palm_overlap")
    cover_like_occlusion = bool(
        aggregate.flat_palm_frame_ratio >= 0.50
        and aggregate.peak_mouth_contact >= 0.50
        and not delivery_like_occlusion
        and cover_like_conditions
    )
    cover_like_reason = ", ".join(cover_like_conditions) if cover_like_occlusion else ""
    mouth_mostly_visible = (
        aggregate.mouth_visible_frame_ratio >= 0.60
        and aggregate.peak_mouth_occlusion_score < aggregate.occlusion_moderate_score
    )

    weak_or_moderate_mouth_activity = aggregate.peak_mouth_open_ratio <= 0.30
    possible_palm_dump_delivery = bool(
        aggregate.flat_palm_frame_ratio >= 0.35
        and brief_contact
        and clear_or_partial_withdrawal
        and (mouth_mostly_visible or weak_or_moderate_mouth_activity)
        and aggregate.peak_mouth_contact >= 0.5
    )
    likely_mouth_cover = bool(
        flat_palm_dominant
        and not possible_palm_dump_delivery
        and low_pinched_delivery
        and aggregate.peak_mouth_contact >= 0.5
    )
    if possible_palm_dump_delivery:
        event_style = "palm_dump_delivery"
    elif likely_mouth_cover:
        event_style = "mouth_cover"
    elif (
        aggregate.pinch_frame_ratio >= 0.15
        or aggregate.loose_grip_frame_ratio >= 0.20
        or aggregate.holding_object_frame_ratio >= 0.20
    ):
        event_style = "pinch_delivery"
    else:
        event_style = "unknown"

    return EventStyleClassification(
        flat_palm_dominant=flat_palm_dominant,
        low_pinched_delivery=low_pinched_delivery,
        brief_contact=brief_contact,
        clear_or_partial_withdrawal=clear_or_partial_withdrawal,
        palm_dump_brief_contact=palm_dump_brief_contact,
        palm_dump_valid_contact_duration=palm_dump_valid_contact_duration,
        palm_dump_approach_then_withdrawal=palm_dump_approach_then_withdrawal,
        palm_dump_has_lower_mouth_geometry=palm_dump_has_lower_mouth_geometry,
        palm_dump_temporal_order_valid=palm_dump_temporal_order_valid,
        delivery_like_occlusion=delivery_like_occlusion,
        cover_like_occlusion=cover_like_occlusion,
        cover_like_reason=cover_like_reason,
        mouth_mostly_visible=mouth_mostly_visible,
        weak_or_moderate_mouth_activity=weak_or_moderate_mouth_activity,
        possible_palm_dump_delivery=possible_palm_dump_delivery,
        likely_mouth_cover=likely_mouth_cover,
        event_style=event_style,
    )
