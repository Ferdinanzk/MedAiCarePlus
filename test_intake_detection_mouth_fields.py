import unittest

from app.services.intake_detection import HandTrackState, PillIngestionDetector


WIDTH = 1000
HEIGHT = 1000


def norm_point(x, y):
    return {"x": x / WIDTH, "y": y / HEIGHT, "z": 0.0}


def make_hand_landmarks_near_mouth():
    landmarks = [norm_point(500, 600) for _ in range(21)]
    points = {
        0: (500, 620),
        4: (490, 505),
        5: (470, 560),
        6: (480, 530),
        8: (495, 505),
        9: (500, 555),
        10: (500, 530),
        12: (505, 505),
        14: (520, 530),
        16: (520, 505),
        17: (540, 560),
        18: (540, 535),
        20: (540, 510),
    }
    for index, (x, y) in points.items():
        landmarks[index] = norm_point(x, y)
    return landmarks


def make_open_mouth_geometry():
    return {
        "center": (500, 500),
        "upper": (500, 470),
        "lower": (500, 530),
        "left": (450, 500),
        "right": (550, 500),
        "width": 100.0,
        "height": 60.0,
        "rect": (410, 430, 590, 570),
        "lower_rect": (410, 500, 590, 570),
        "mouth_open": True,
        "mouth_open_ratio": 0.42,
        "head_pitch_proxy": -0.12,
        "face_height": 240.0,
        "eye_width": 180.0,
    }


def make_far_closed_mouth_features():
    return {
        "fingertip_to_mouth": 200.0,
        "fingertip_to_mouth_norm": 2.0,
        "mouth_near_distance_px": 60.0,
        "in_mouth_zone": False,
        "mouth_open": False,
        "mouth_open_ratio": 0.0,
        "mouth_occlusion_score": 0.0,
        "occlusion_type": "none",
        "mouth_visible_during_contact": True,
        "flat_palm": False,
        "pinch": False,
        "loose_grip": False,
        "holding_object": False,
        "palm_center_in_mouth_roi": False,
        "palm_center_in_lower_mouth_roi": False,
        "palm_to_mouth_norm": 2.0,
        "palm_to_lower_mouth_norm": 2.0,
    }


def make_seeded_closed_mouth_state():
    state = HandTrackState()
    state.was_near_mouth = True
    state.at_mouth_start_time = 9.5
    state.last_contact_dist = 20.0
    state.last_contact_dist_norm = 0.10
    state.prev_mouth_dist_norm = 2.0
    state.peak_mouth_contact = 1.0
    state.in_mouth_zone_occurred = True
    state.mouth_open_occurred = False
    state.peak_mouth_open_ratio = 0.0
    state.mouth_occlusion_frame_count = 10
    state.mouth_visible_frame_count = 10
    state.min_fingertip_to_mouth_norm = 0.80
    state.min_palm_to_mouth_norm = 0.80
    state.min_palm_to_lower_mouth_norm = 2.0
    state.partial_withdrawal_seen = True
    state.recent_approaches = [0.10, 0.10]
    state.recent_approaches_norm = [0.10, 0.10]
    return state


def close_seeded_event(detector, state):
    detector.hand_states["hand_0"] = state
    return detector.update_hand_state(
        "hand_0",
        make_far_closed_mouth_features(),
        current_time=10.0,
    )


def make_seeded_mouth_cover_open_state():
    """Mimics the True4/False10 'mouth_cover' style: open mouth, flat palm,
    real contact + withdrawal, but only a moderate ~0.45 confidence."""
    state = make_seeded_closed_mouth_state()
    state.at_mouth_start_time = 9.3  # dwell ~0.7s at close (current_time=10.0)
    state.mouth_open_occurred = True
    state.peak_mouth_open_ratio = 0.46
    state.flat_palm_frame_count = 10
    state.flat_palm_overlap_frame_count = 10
    state.pinch_frame_count = 0
    state.loose_grip_frame_count = 0
    state.holding_object_frame_count = 0
    state.palm_lower_mouth_frame_count = 0
    state.flat_palm_lower_mouth_frame_count = 0
    state.min_palm_to_lower_mouth_norm = 2.0
    state.peak_mouth_occlusion_score = 0.70
    state.mouth_visible_frame_count = 0
    state.palm_overlap_frame_count = 0
    state.recent_approaches = [0.0, 0.0]      # no trajectory bonus -> conf ~0.45
    state.recent_approaches_norm = [0.0, 0.0]
    return state


def make_seeded_weak_palm_dump_open_state():
    """Mimics True12: an open-mouth palm-dump delivery whose flat palm never
    reaches the lower-mouth ROI (weak_palm_dump_no_lower_mouth_geometry). This
    must NOT auto-confirm, but must surface as 'uncertain' so the patient can
    confirm a genuine intake instead of it being silently dropped."""
    state = make_seeded_closed_mouth_state()
    state.mouth_open_occurred = True
    state.peak_mouth_open_ratio = 0.65
    state.flat_palm_frame_count = 10
    state.pinch_frame_count = 0
    state.loose_grip_frame_count = 0
    state.holding_object_frame_count = 0
    state.palm_lower_mouth_frame_count = 0          # palm never near lower mouth
    state.flat_palm_lower_mouth_frame_count = 0
    state.min_palm_to_lower_mouth_norm = 2.0        # > 0.90 -> weak geometry
    state.mouth_visible_frame_count = 10
    state.mouth_occlusion_frame_count = 10
    state.peak_mouth_occlusion_score = 0.0
    state.palm_overlap_frame_count = 0
    state.head_tilt_back_detected = False           # no cap exception
    return state


class IntakeDetectionMouthFieldTests(unittest.TestCase):
    def test_compute_hand_features_exports_mouth_and_head_context_consumed_by_event_scoring(self):

        detector = PillIngestionDetector()

        features = detector.compute_hand_features(
            make_hand_landmarks_near_mouth(),
            make_open_mouth_geometry(),
            WIDTH,
            HEIGHT,
        )

        self.assertIs(features["mouth_open"], True)
        self.assertEqual(features["mouth_open_ratio"], 0.42)
        self.assertEqual(features["head_pitch_proxy"], -0.12)
        self.assertEqual(features["head_face_height"], 240.0)
        self.assertEqual(features["head_eye_width"], 180.0)

    def test_update_hand_state_records_mouth_open_from_computed_features(self):
        detector = PillIngestionDetector()
        features = detector.compute_hand_features(
            make_hand_landmarks_near_mouth(),
            make_open_mouth_geometry(),
            WIDTH,
            HEIGHT,
        )

        event_debug = detector.update_hand_state("hand_0", features, current_time=1.0)
        state = detector.hand_states["hand_0"]

        self.assertIs(state.mouth_open_occurred, True)
        self.assertEqual(state.peak_mouth_open_ratio, 0.42)
        self.assertEqual(state.head_pitch_at_event_start, -0.12)
        self.assertIs(event_debug["mouth_open"], True)

    def test_update_hand_state_returns_event_debug_payload_for_benchmark_export(self):
        detector = PillIngestionDetector()
        features = detector.compute_hand_features(
            make_hand_landmarks_near_mouth(),
            make_open_mouth_geometry(),
            WIDTH,
            HEIGHT,
        )

        result = detector.update_hand_state("hand_0", features, current_time=1.0)

        event_debug = result["event_debug"]
        self.assertEqual(event_debug["event_confidence"], result["event_confidence"])
        self.assertIs(event_debug["mouth_open"], True)
        self.assertEqual(event_debug["mouth_open_ratio"], 0.42)
        self.assertIn("event_style", event_debug)
        self.assertIn("event_window_closed", event_debug)
        self.assertIn("positive_points_total", event_debug)
        self.assertIn("penalty_points_total", event_debug)
        self.assertIn("raw_event_score", event_debug)

    def test_generic_closed_mouth_contact_does_not_detect_without_weak_mouth_exception(self):
        detector = PillIngestionDetector()
        state = make_seeded_closed_mouth_state()

        result = close_seeded_event(detector, state)
        event_debug = result["event_debug"]

        self.assertIs(result["event_detected"], False)
        self.assertIs(result["ingestion_detected"], False)
        self.assertIs(event_debug["mouth_open"], False)
        self.assertEqual(event_debug["mouth_open_contribution"], 0.0)
        self.assertGreaterEqual(event_debug["event_confidence"], 0.38)
        self.assertFalse(event_debug["closed_mouth_strong_palm_dump_recovery"])
        self.assertFalse(event_debug["closed_mouth_supported_pinch_recovery"])

    def test_closed_mouth_strong_palm_dump_geometry_can_detect(self):
        detector = PillIngestionDetector()
        state = make_seeded_closed_mouth_state()
        state.min_palm_to_lower_mouth_norm = 0.26
        state.mouth_visible_frame_count = 1
        state.flat_palm_frame_count = 10
        state.palm_lower_mouth_frame_count = 6
        state.flat_palm_lower_mouth_frame_count = 6
        state.head_tilt_back_detected = True
        state.head_tilt_back_frame_count = 2

        result = close_seeded_event(detector, state)
        event_debug = result["event_debug"]

        self.assertIs(result["event_detected"], True)
        self.assertIs(result["ingestion_detected"], True)
        self.assertEqual(event_debug["event_style"], "palm_dump_delivery")
        self.assertIs(event_debug["mouth_open"], False)
        self.assertTrue(event_debug["strong_palm_dump_geometry"])
        self.assertTrue(event_debug["closed_mouth_strong_palm_dump_recovery"])
        self.assertFalse(event_debug["closed_mouth_supported_pinch_recovery"])

    def test_closed_mouth_supported_pinch_geometry_can_detect(self):
        detector = PillIngestionDetector()
        state = make_seeded_closed_mouth_state()
        state.pinch_frame_count = 4
        state.holding_object_frame_count = 4

        result = close_seeded_event(detector, state)
        event_debug = result["event_debug"]

        self.assertIs(result["event_detected"], True)
        self.assertIs(result["ingestion_detected"], True)
        self.assertEqual(event_debug["event_style"], "pinch_delivery")
        self.assertIs(event_debug["mouth_open"], False)
        self.assertTrue(event_debug["closed_mouth_supported_pinch_recovery"])
        self.assertFalse(event_debug["closed_mouth_strong_palm_dump_recovery"])

    def test_generic_closed_mouth_contact_yields_no_decision(self):
        detector = PillIngestionDetector()
        state = make_seeded_closed_mouth_state()

        result = close_seeded_event(detector, state)

        self.assertEqual(result["decision"], "none")
        self.assertEqual(result["event_debug"]["decision"], "none")

    def test_strong_palm_dump_recovery_decision_is_confirmed(self):
        detector = PillIngestionDetector()
        state = make_seeded_closed_mouth_state()
        state.min_palm_to_lower_mouth_norm = 0.26
        state.mouth_visible_frame_count = 1
        state.flat_palm_frame_count = 10
        state.palm_lower_mouth_frame_count = 6
        state.flat_palm_lower_mouth_frame_count = 6
        state.head_tilt_back_detected = True
        state.head_tilt_back_frame_count = 2

        result = close_seeded_event(detector, state)

        self.assertIs(result["event_detected"], True)
        self.assertEqual(result["decision"], "confirmed")

    def test_supported_pinch_recovery_decision_is_confirmed(self):
        detector = PillIngestionDetector()
        state = make_seeded_closed_mouth_state()
        state.pinch_frame_count = 4
        state.holding_object_frame_count = 4

        result = close_seeded_event(detector, state)

        self.assertIs(result["event_detected"], True)
        self.assertEqual(result["decision"], "confirmed")

    def test_genuine_mouth_cover_intake_auto_confirms(self):
        detector = PillIngestionDetector()
        state = make_seeded_mouth_cover_open_state()

        result = close_seeded_event(detector, state)
        event_debug = result["event_debug"]

        # ~0.45 confidence, moderate mouth opening (not a wide yawn): a genuine
        # intake like True4. Lean-positive design auto-confirms it (>= 0.45) so
        # real doses get registered without an extra tap. False positives at this
        # score are held out by the contradiction flags, not the threshold.
        self.assertEqual(result["decision"], "confirmed")
        self.assertIs(result["event_detected"], True)
        self.assertTrue(event_debug["likely_mouth_cover"])
        self.assertFalse(event_debug["safety_contradiction"])
        self.assertGreaterEqual(event_debug["event_confidence"], 0.45)

    def test_wide_open_mouth_cover_is_uncertain_via_safety_contradiction(self):
        detector = PillIngestionDetector()
        state = make_seeded_mouth_cover_open_state()
        state.peak_mouth_open_ratio = 0.85  # yawn-like wide open -> contradiction

        result = close_seeded_event(detector, state)
        event_debug = result["event_debug"]

        self.assertIs(result["event_detected"], False)
        self.assertEqual(result["decision"], "uncertain")
        self.assertTrue(event_debug["safety_contradiction"])
        self.assertEqual(event_debug["decision_reason"], "wide_open_mouth_cover")

    def test_weak_palm_dump_is_uncertain_not_dropped(self):
        detector = PillIngestionDetector()
        state = make_seeded_weak_palm_dump_open_state()

        result = close_seeded_event(detector, state)
        event_debug = result["event_debug"]

        # Weak palm-dump geometry blocks auto-logging but must still prompt the
        # patient rather than silently disappearing as 'none'.
        self.assertIs(result["event_detected"], False)
        self.assertEqual(result["decision"], "uncertain")
        self.assertTrue(event_debug["weak_palm_dump_no_lower_mouth_geometry"])
        self.assertEqual(event_debug["decision_reason"], "weak_palm_dump_needs_confirmation")


if __name__ == "__main__":
    unittest.main()
