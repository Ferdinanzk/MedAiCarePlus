import asyncio
import unittest
from pathlib import Path

from app.services.intake_detection import IntakeDetectionService, euclidean, to_pixel_coords
from app.services.intake_temporal import Observation, Stage, TemporalIntakePipeline


def hand(distance, x=.5, mouth=True, pinch=True, flat=False):
    return Observation((x, .5), distance, mouth, pinch, flat, 0.0)


def ready():
    pipeline = TemporalIntakePipeline()
    pipeline.process(0.0, True, [])
    pipeline.process(.4, True, [hand(1.6)])
    return pipeline


def replay(step=.2, loss=0.0, *, pinch=True, flat=False):
    pipeline, now, results = ready(), .4, []
    for distance in (1.35, 1.0, .7):
        now += step
        results.append(pipeline.process(now, True, [hand(distance, pinch=pinch, flat=flat)]))
    if loss:
        now += loss
        results.append(pipeline.process(now, True, []))
    for distance in (1.25, 1.45):
        now += step
        results.append(pipeline.process(now, True, [hand(distance, pinch=pinch, flat=flat)]))
    return pipeline, results, now


class TemporalReplayTests(unittest.TestCase):
    def test_normal_slow_and_fast_intakes_complete_once(self):
        for step in (.2, .45, .08):
            with self.subTest(step=step):
                _, results, _ = replay(step)
                self.assertEqual(sum(r["decision"] == "confirmed" for r in results), 1)

    def test_200_to_500ms_loss_and_reacquisition_outside(self):
        for loss in (.2, .35, .5):
            with self.subTest(loss=loss):
                _, results, _ = replay(.12, loss)
                self.assertIn("OCCLUDED", [r["stage"] for r in results])
                self.assertEqual(sum(r["decision"] == "confirmed" for r in results), 1)

    def test_permanently_lost_hand_closes_as_manual_fallback_not_stuck(self):
        p = ready()
        p.process(.6, True, [hand(1.0)])
        p.process(.8, True, [hand(.7)])
        p.process(1.0, True, [])
        p.process(1.5, True, [])
        result = p.process(1.9, True, [])
        self.assertEqual(result["stage"], "COMPLETE_CANDIDATE")
        self.assertEqual(result["decision"], "uncertain")
        self.assertEqual(result["completion_reason"], "hand_lost_after_contact")

    def test_first_visible_exit_frame_completes_without_second_outside_frame(self):
        p = ready()
        p.process(.6, True, [hand(1.0)])
        p.process(.8, True, [hand(.7)])
        result = p.process(1.0, True, [hand(1.25)])
        self.assertEqual(result["stage"], "COMPLETE_CANDIDATE")
        self.assertEqual(result["completion_reason"], "visible_exit_after_contact")

    def test_repeated_fast_gestures_do_not_duplicate_or_accumulate(self):
        p, results, now = replay(.08)
        for distance in (1.0, .65, 1.3, 1.5):
            now += .06
            results.append(p.process(now, True, [hand(distance)]))
        self.assertEqual(sum(r["decision"] == "confirmed" for r in results), 1)

    def test_hand_order_changes_and_two_hands(self):
        p = ready()
        frames = [
            [hand(1.0, .3), hand(1.7, .8)],
            [hand(1.7, .8), hand(.7, .3)],
            [hand(1.3, .3), hand(1.7, .8)],
            [hand(1.5, .3), hand(1.7, .8)],
        ]
        results = [p.process(.6+i*.2, True, frame) for i, frame in enumerate(frames)]
        self.assertEqual(sum(r["decision"] == "confirmed" for r in results), 1)

    def test_scratching_chin_talking_waving_do_not_confirm(self):
        for distances in ([1.4, 1.2, 1.4], [1.5, 1.15, 1.3], [1.3, 1.2, 1.3], [1.8, 1.4, 1.8]):
            p = ready()
            results = [p.process(.6+i*.2, True, [hand(d, mouth=False, pinch=False)]) for i, d in enumerate(distances)]
            self.assertFalse(any(r["decision"] == "confirmed" for r in results))

    def test_mouth_cover_and_drinking_require_manual_confirmation(self):
        for flat, pinch in ((True, False), (False, False)):
            _, results, _ = replay(.2, pinch=pinch, flat=flat)
            self.assertFalse(any(r["decision"] == "confirmed" for r in results))
            self.assertTrue(any(r["decision"] == "uncertain" for r in results))

    def test_unreliable_face_advances_time_without_corrupting_event(self):
        p = ready()
        p.process(.6, True, [hand(1.0)])
        p.process(.8, True, [hand(.7)])
        result = p.process(1.0, False, [])
        self.assertEqual(result["stage"], "OCCLUDED")
        self.assertEqual(result["missing_observation"], "face")


class SequencingIntegrationTests(unittest.TestCase):
    def test_delayed_duplicate_and_out_of_order_requests(self):
        async def scenario():
            svc = IntakeDetectionService()
            base = {"width": 640, "height": 480, "face_landmarks": [], "hand_landmarks": [], "timestamp": 1.0}
            first = await svc.process_frame(7, "seq", {**base, "frame_seq": 2})
            duplicate = await svc.process_frame(7, "seq", {**base, "frame_seq": 2})
            old = await svc.process_frame(7, "seq", {**base, "frame_seq": 1})
            self.assertTrue(first["accepted"])
            self.assertFalse(duplicate["accepted"])
            self.assertFalse(old["accepted"])
            self.assertEqual(old["missing_observation"], "stale_frame")
        asyncio.run(scenario())


class CameraCoordinateTests(unittest.TestCase):
    def test_normalized_geometry_is_scale_consistent_in_landscape_frames(self):
        mouth, fingertip = {"x": .5, "y": .4}, {"x": .6, "y": .4}
        ratios = []
        for width, height in ((640, 480), (1280, 720), (1920, 1080)):
            mouth_px, fingertip_px = to_pixel_coords(mouth, width, height), to_pixel_coords(fingertip, width, height)
            ratios.append(euclidean(mouth_px, fingertip_px) / (width * .1))
        self.assertTrue(all(abs(value - 1.0) < .02 for value in ratios))

    def test_preview_mirroring_does_not_change_detector_coordinates(self):
        # CSS preview mirroring must not be applied to the canvas/landmarks.
        source = Path("frontend_source/src/hooks/useIntakeDetection.ts").read_text(encoding="utf-8")
        self.assertIn("ctx.drawImage(video, 0, 0, canvas.width, canvas.height)", source)
        self.assertNotIn("ctx.scale(-1", source)

    def test_intake_camera_requests_landscape_and_avoids_cover_crop(self):
        source = Path("frontend_source/src/pages/Intake.tsx").read_text(encoding="utf-8")
        self.assertIn("aspectRatio: { ideal: 16 / 9 }", source)
        self.assertIn('className="w-full h-full object-contain"', source)

    def test_ended_session_clears_sequence_for_new_generation(self):
        async def scenario():
            svc = IntakeDetectionService()
            base = {"frame_seq": 4, "width": 640, "height": 480, "face_landmarks": [], "hand_landmarks": [], "timestamp": 1.0}
            await svc.process_frame(8, "ended", base)
            await svc.end_session(8, "ended")
            fresh = await svc.process_frame(8, "ended", {**base, "frame_seq": 1, "timestamp": 2.0})
            self.assertTrue(fresh["accepted"])
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
