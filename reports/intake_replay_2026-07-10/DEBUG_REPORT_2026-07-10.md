# MedAiCarePlus Stuck-at-Contact Debug Report — 2026-07-10

## Scope and evidence

This investigation replayed all 30 MP4 files under `C:/Users/user/Desktop/pill-taking-project/EatPill` through MediaPipe Face Landmarker, MediaPipe Hand Landmarker, the production mouth/hand feature extraction, and `TemporalIntakePipeline` at 10 observations per second. `EatTrue` contains 15 expected pill-taking videos and `EatFalse` contains 15 expected negative-action videos.

Detailed artifacts in this folder:

- `frames_baseline.csv` and `frames_fixed.csv`: frame number/time, visibility, input dimensions, mouth geometry, hand/fingertip/palm coordinates, normalized distance, zones, state, transition/waiting reason, velocities, hand loss/reacquisition, occlusion time, mouth context, and decision.
- `events_baseline.json` and `events_fixed.json`: emitted event candidates.
- `videos_baseline.json` and `videos_fixed.json`: machine-readable per-video outcomes.
- `summary_baseline.md` and `summary_fixed.md`: all-video result tables.

Replay command:

```powershell
python scripts/replay_intake_videos.py --input "C:/Users/user/Desktop/pill-taking-project/EatPill" --phase fixed
```

The first invocation downloads the same official float16 MediaPipe task models used by the browser into `.cache/mediapipe`.

## Root cause

The main failure was temporal closure, not event scoring:

1. `AT_MOUTH` refreshed `last_contact_at` for every sample inside the exit boundary.
2. A sample outside the exit boundary also had to exceed a withdrawal-velocity threshold.
3. That first valid exit only changed the state to `WITHDRAWING`; a second outside sample was required to emit the candidate.
4. Face overlap often caused the hand to disappear or return under a new spatial track. The active track remained missing and was reset after a 550 ms grace period.
5. Thus a real intake could visibly exit once, disappear, or reacquire outside, yet remain at contact until reset/timeout.

The replay confirms the observation problem: positive-video hand visibility ranges from 8.5% to 53.8%, even though face visibility is generally 95–100%. Contact is therefore exactly where tolerant loss/reacquisition handling is needed.

The camera preview amplified the issue. The detection modal used a narrow `max-w-lg`, full-height portrait container and `object-cover`, cropping a landscape source. MediaPipe still processed the uncropped intrinsic video frame, so backend coordinates were not mathematically scaled to the CSS crop, but the user could not see the same withdrawal area the detector saw. This was a UX/control mismatch rather than a backend coordinate transform bug.

## Behavior changes

- A first visible crossing of the hysteresis exit boundary after established contact now completes the event; it no longer requires a redundant second outside frame.
- A newly assigned hand track can reassociate with the active event after occlusion. Reacquisition outside the exit zone completes with reason `reacquired_outside_exit_zone`.
- If the hand remains lost after established contact for 850 ms, the event closes as `uncertain` with reason `hand_lost_after_contact`. Loss alone never auto-confirms a dose and can no longer remain stuck forever.
- Actual exit/withdrawal evidence can still auto-confirm only when mouth, pill-like grip, outward movement, and non-cover geometry agree.
- API diagnostics now expose waiting/transition reasons, entry/exit distances, approach/withdrawal velocities, face/hand visibility, occlusion duration, loss/reacquisition, sequence number, and intrinsic video dimensions.
- The browser requests 1280×720 (minimum 640×480, ideal 16:9), displays a wide `max-w-6xl` aspect-video preview with `object-contain`, and overlays a framing/withdrawal guide.
- Canvas inference continues to use `video.videoWidth`/`video.videoHeight` with no CSS or mirror transform. The preview crop cannot change detector coordinates.

## Replay results

| Phase | Confirmed | Uncertain | Rejected | Timeout | Stuck |
|---|---:|---:|---:|---:|---:|
| Baseline | 0 | 8 | 4 | 16 | 2 |
| Fixed | 1 | 25 | 4 | 0 | 0 |

Positive set after the fix: 14/15 produced a candidate and none remained stuck or timed out. `True8.mp4` auto-confirmed. The other 13 completed positive sequences are conservative manual-confirmation candidates because the video landmarks did not provide sufficient grip/mouth evidence for safe automatic logging.

`True12.mp4` remained rejected: hand detection was only 11.5%, and its minimum observed hand-to-mouth distance was 1.248, outside both the 0.78 contact-entry and 1.12 exit boundaries. The debug output therefore explains it as no observed contact, not stuck contact.

Negative set after the fix: 11/15 formed ambiguous candidates and four were rejected. Crucially, **0/15 negative videos auto-confirmed**. The ambiguous negative candidates remain behind the existing manual confirmation fallback. This change closes events safely; it does not make contact/loss itself positive evidence.

## Tests and build

Executed on 2026-07-10:

- `python -m unittest test_intake_temporal_pipeline.py test_intake_detection_mouth_fields.py -v`: 26/26 passed.
- Regression verifies the first visible exit completes without a second frame.
- Permanent post-contact loss closes once as uncertain rather than sticking.
- 200–500 ms occlusion and outside-zone reacquisition complete once.
- Repeated gestures, hand order swaps, two hands, stale frames, scratch/chin/talking/waving, covering, and drinking safeguards remain covered.
- Landscape dimensions, uncropped preview, and absence of a mirrored canvas transform are tested.
- `npm.cmd run build --prefix frontend_source`: passed; 1,790 modules transformed.
- Full real-video fixed replay: 30/30 processed, zero timeout/stuck outcomes, zero negative auto-confirms.

## Files changed

- `app/services/intake_temporal.py`
- `app/services/intake_detection.py`
- `frontend_source/src/lib/ai-api.ts`
- `frontend_source/src/pages/Intake.tsx`
- `scripts/replay_intake_videos.py`
- `test_intake_temporal_pipeline.py`
- replay artifacts under `reports/intake_replay_2026-07-10/`

## Remaining risks

- These videos differ from the user's exact live camera/device. A live retest is still required to confirm browser camera constraints and MediaPipe behavior on that hardware.
- Hand-loss completion is intentionally uncertain, so it solves the frozen UI without silently logging a potentially false dose.
- Eleven negative videos reach the geometric contact sequence and prompt manual confirmation. Future semantic classification should reduce prompt frequency using labeled action/style evidence, not by weakening safety gates.
- Nearest spatial reassociation can be ambiguous when two hands cross close to the face. Reassociated outside-zone completion retains the original event evidence, and ambiguous evidence does not auto-confirm.
- Camera constraint ideals are browser requests, not guarantees. The debug panel reports the actual intrinsic dimensions so unsupported devices are visible immediately.
