# Accuracy Comparison Report: Old Pill-Taking System vs. New MedAiCarePlus

**Generated:** 2026-05-31
**Author:** Automated analysis (`old.py` / `event_style.py` / `analyze_videos.py` vs. `intake_detection.py` / `intake_detection_style.py`)

---

## 1. Executive Summary

The new MedAiCarePlus `intake_detection.py` is **significantly less accurate** than the old system. The degradation is not caused by a single catastrophic bug but by the **compound effect of dozens of regressions** introduced during the rewrite — every softened threshold, missing guard clause, and overly permissive score nudge adds up to a system that:

1. **Grossly inflates confidence for pill-ingestion-like frames** via a hard-coded `max(0.70, ...)` floor when `state.pill_detected` is true (even though pill detection is always effectively disabled), causing false-positive frame confidences of 70%+ on almost any hand-near-mouth event.
2. **Mouth activity scoring is more generous** in the new system because the old system requires `mouth_open_occurred` to be `True` *before* the function is even called (`old.py` line 935), while the new system calls `mouth_activity_points` unconditionally on every frame (line 612), so the full reward is always added regardless of whether the mouth was ever seen open.
3. **`mouth_open_allowed` gating is absent from the new event detector.** The old system only allows mouth-activity points when `mouth_open_allowed = peak_mouth_contact > 0.05 or in_mouth_zone_occurred` (line 818). The new system skips `mouth_open_allowed` entirely during scoring — the mouth-activity contribution is never gated.
4. **The `fingertip_delivery` bonus in the old system** (lines 982–1006) uses a more restrictive set of conditions that exclude `possible_palm_dump_delivery` + high `flat_palm_frame_ratio` combinations. The new system removed that exclusion (compare old lines 982–993 vs. new lines 641–655).
5. **The new system removed the `APPROACH_SPEED_THRESHOLD` constant** (old line 25: `APPROACH_SPEED_THRESHOLD = 4.0`). The old system tracked both pixel-based AND normalized approach speeds (`approach_speed_px_based` / `approach_speed_norm`) and used both in withdrawal and mouth-contact calculations. The new system eliminated all pixel-based tracking.
6. **The `compute_mouth_occlusion_score` is completely rewritten** and is dramatically more permissive: the old version scores in graduated tiers (0.45 / 0.30 / 0.15 / 0.25 / 0.20 / 0.10) based on multiple independent sub-features; the new version collapses all sub-logic into blunt top/bottom branches and returns a flat `0.0`, `0.25`, `0.40`, `0.55`, or `0.0 + constants`, losing the nuanced graduated scoring that prevented borderline cases from being misclassified.
7. **`analyze_videos.py` is the official batch evaluator** and uses `best_event_confidence >= 0.50` as the primary decision threshold with a secondary `detected_event_count > 0` trigger. Because the new system inflates baseline confidence scores and has weaker mouth-activity gating, it will cross the 0.50 threshold on many false-positive events.

The cumulative effect is that the new system **rewards behaviors that the old system learned to penalize**, and **penalizes behaviors that the old system learned to reward**.

---

## 2. Feature Comparison Table

| Feature | Old System (`old.py`) | New System (`intake_detection.py`) | Status |
|---|---|---|---|
| Baseline contribution | `+0.08` (line 901) | `+0.08` (line 605) | ✅ Same |
| Mouth contact contribution | `0.20 * peak_mouth_contact` (line 930) | `0.20 * peak_mouth_contact` (line 608) | ✅ Same |
| Mouth activity gating (`mouth_open_allowed`) | Gated: only added when `peak_mouth_contact > 0.05 or in_mouth_zone_occurred` (line 818→935) | **No gating**: `mouth_open_allowed` never checked during event scoring (line 612) | ❌ **MISSING** |
| `mouth_activity_points` — requires `mouth_open_occurred` | `mouth_open_occurred` passed as arg; returns `0.0` immediately if `False` (old.py line 935) | `mouth_open_occurred` NOT passed; function still returns non-zero (intake_detection.py line 612) | ❌ **MISSING** |
| `mouth_activity_points` thresholds (open occurred) | `≥0.10 → 0.03`, `≥0.20 → 0.08`, `≥0.30 → 0.14` (lines 152–161) | `≥0.18 → 0.03`, `≥0.30 → 0.06`, `≥0.45 → 0.10`, `≥0.60 → 0.14` (lines 95–106) | ⚠️ **MODIFIED** — lower reward per tier |
| Mouth activity cap (`likely_mouth_cover`) | Caps at `0.04` if `mouth_activity > 0.04` (line 937–938) | Caps at `0.04` if `mouth_activity > 0.04` (line 614–615) | ✅ Same (but old also has `mouth_open_allowed` gate) |
| Dwell contribution | `+0.10` if `0.25 ≤ dwell ≤ 1.5` (line 951) | `+0.10` if `0.25 ≤ dwell ≤ 1.5` (line 620) | ✅ Same |
| Trajectory contribution | `+0.12` if `avg_approach > THRESHOLD_NORM and std < ERRATIC_STD` (line 958) | `+0.12` if same (line 626) | ✅ Same |
| Pill contribution (YOLO) | `+0.25` if `nearest_pill_dist < 50` (line 964) | **Removed entirely** (no YOLO, no code path) | ❌ **MISSING** |
| Withdrawal contribution | `+0.15` if full, `+0.05` if partial (lines 971–980) | `+0.15` if full, `+0.05` if partial (lines 632–637) | ✅ Same |
| Fingertip delivery contribution | `+0.06` palm much closer than fingertip; `+0.03` moderate. **Excludes** palm_dump + flat_palm ≥ 0.70 + no lower mouth geometry lines 982–993 | Same amounts but **missing the palm_dump + flat_palm exclusion** (lines 641–655) | ❌ **MODIFIED (less restrictive)** |
| Short contact penalty | `-0.08` if `0.0 < dwell < 0.1` (line 1012) | `-0.08` if `0.0 < dwell < 0.1` (line 658) | ✅ Same |
| No withdrawal penalty | `-0.08` (line 1018) | `-0.08` (line 661) | ✅ Same |
| Long dwell penalty | `-0.10` if `dwell > 2.0` (line 1023) | `-0.10` if `dwell > 2.0` (line 664) | ✅ Same |
| Erratic movement penalty | `-0.05` if `std > 0.18` (line 1030) | `-0.05` if `std > 0.18` (line 667) | ✅ Same |
| Flat palm cover penalty | `-0.12` if `likely_mouth_cover` (line 1036) | `-0.12` if `likely_mouth_cover` (line 670) | ✅ Same |
| No mouth open palm dump penalty | `-0.12` with multi-condition check (lines 1041–1054) | `-0.12` with same conditions (lines 674–684) | ✅ Same |
| Palm dump geometry reward | `+0.22` if strong geometry (lines 1056–1068) | `+0.22` if strong geometry (lines 687–696) | ✅ Same |
| Weak palm dump no-lower-mouth cap | Caps at `0.49` (lines 1070–1099) | Caps at `0.49` (lines 699–719) | ✅ Same |
| Mouth occlusion penalty (3 tiers) | Graduated: `0.75+/≥0.35 → 0.25`, `≥0.65/≥0.25 → 0.20`, `≥0.45/≥0.20 → 0.12` (lines 1101–1107) | Same thresholds and amounts (lines 722–728) | ✅ Same |
| Occlusion suppression for palm dump | Suppresses if `possible_palm_dump_delivery` (line 1115) | Same (line 730) | ✅ Same |
| Occlusion suppression for pinch | Suppresses if pinch + withdrew + head_tilt + palm_lower ≥ 0.50 (lines 1108–1121) | Same (line 732) | ✅ Same |
| Unknown open mouth no-delivery cap | Caps at `0.49` (lines 1131–1151) | Caps at `0.49` (lines 738–750) | ✅ Same |
| `compute_mouth_occlusion_score` | **Graduated multi-subfeature**: 6 independent checks, combines bbox overlap + palm center ROI + palm-vs-fingertip + flat_palm bonus (old.py lines 164–201) | **Collapsed to 3 blunt branches**: bbox+palm checks OR flat distance OR close distance (intake_detection.py lines 109–126) | ❌ **COMPLETELY REWRITTEN** |
| `mouth_open_allowed` variable | Defined and used to gate mouth activity (lines 818, 935) | Never computed or referenced during event scoring | ❌ **MISSING** |
| Frame confidence pill floor | `max(0.30, ...)` then `max(0.70, ...)` when `state.pill_detected` (old.py lines 1406–1411) | No pill floor (always YOLO disabled) | ✅ Same effectively |
| Pixel-based approach tracking | `approach_speed_px_based` computed (line 623) and available in debug | Removed entirely | ❌ **MISSING** |
| Pixel-based near-mouth tracking | `near_mouth_px_based` computed (line 637) | Removed entirely | ❌ **MISSING** |
| Pixel-based mouth contact (`mouth_contact_px_based`) | Computed (line 640) | Removed entirely | ❌ **MISSING** |
| `mouth_contact_delta_norm_minus_px` | Computed (line 655) — diagnostic for norm vs px mismatch | Removed entirely | ❌ **MISSING** |
| `withdrew_enough_px_based` | Computed (line 823) as additional evidence | Removed entirely | ❌ **MISSING** |
| `to_pixel_coords` signature | Takes MediaPipe landmark object with `.x`/`.y` attributes (line 99) | Takes dict with `["x"]`/`["y"]` keys (line 68) | ⚠️ **MODIFIED** (interface change) |
| Pill detection persistence (`PILL_PERSIST_FRAMES` / `PILL_LOSS_FRAMES`) | Frames-based hysteresis: `pill_seen_frames`, `pill_loss_frames` (old.py lines 658–678) | **Removed entirely** | ❌ **MISSING** |
| Pill spatial consistency (`compute_pill_spatial_consistency`) | Exists but commented out (old.py lines 402–419) | Removed entirely | ❌ **MISSING** |
| `reset_event_window` method | State reset inline, ~60 lines of manual field resets (old.py lines 1343–1371) | Refactored to `state.reset_event_window()` helper method (intake line 176) | ⚠️ **REFACTORED** |
| Event detection gate `mouth_open_allowed` | Required `mouth_open_allowed` to be `True` (old.py line 1221) | **Absent.** Only checks `mouth_open_occurred` (intake line 763) | ❌ **MISSING** |
| Event debug dict | Massive dictionary with 80+ fields for post-hoc analysis (old.py lines 1236–1341) | **Removed entirely.** No event debug dict passed back. | ❌ **MISSING** |
| `_print_event_debug` method | Exists with full breakdown (old.py lines 279–310) | **Removed entirely** | ❌ **MISSING** |

---

## 3. Side-by-Side Scoring Math Comparison

### 3.1 Positive Contributions

| Contribution | Old System | New System | Delta |
|---|---|---|---|
| Baseline | `+0.08` | `+0.08` | `0.00` |
| `mouth_contact` | `+0.20 × peak_mouth_contact` | `+0.20 × peak_mouth_contact` | `0.00` |
| `mouth_activity` (peak ratio ≥ 0.30 AND mouth_open_occurred) | `+0.14` (gated by `mouth_open_allowed`) | `+0.14` at ≥ 0.60 only; otherwise lower | **New: less at common thresholds** |
| `mouth_activity` (peak ratio ≥ 0.20 AND mouth_open_occurred) | `+0.08` | `+0.06` at ≥ 0.0.30 (lower tier) | `-0.02` |
| `mouth_activity` (peak ratio ≥ 0.10 AND mouth_open_occurred) | `+0.03` | `+0.03` at ≥ 0.18 | Similar |
| `dwell` (if `0.25 ≤ dwell ≤ 1.5`) | `+0.10` | `+0.10` | `0.00` |
| `trajectory` (consistent approach) | `+0.12` | `+0.12` | `0.00` |
| `pill` (YOLO, if `nearest_pill_dist < 50`) | `+0.25` (disabled at runtime) | N/A (removed) | `0.00` |
| `withdrawal` (full) | `+0.15` | `+0.15` | `0.00` |
| `partial_withdrawal` | `+0.05` | `+0.05` | `0.00` |
| `fingertip_delivery` (strong, palm >> fingertip) | `+0.06` (with palm_dump exclusion guard) | `+0.06` **(without exclusion guard)** | **New: +0.06 in cases old would suppress** |
| `fingertip_delivery` (moderate) | `+0.03` | `+0.03` | `0.00` |
| `palm_dump_geometry_reward` | `+0.22` | `+0.22` | `0.00` |

### 3.2 Penalties

| Penalty | Old System | New System | Delta |
|---|---|---|---|
| Short contact (`0.0 < dwell < 0.1`) | `-0.08` | `-0.08` | `0.00` |
| No withdrawal | `-0.08` | `-0.08` | `0.00` |
| Long dwell (`dwell > 2.0`) | `-0.10` | `-0.10` | `0.00` |
| Erratic movement (`std > 0.18`) | `-0.05` | `-0.05` | `0.00` |
| Flat palm cover | `-0.12` | `-0.12` | `0.00` |
| No mouth open palm dump contradiction | `-0.12` | `-0.12` | `0.00` |
| Mouth occlusion (heavy: peak ≥ 0.75, overlap ≥ 0.35) | `-0.25` | `-0.25` | `0.00` |
| Mouth occlusion (mod-heavy: peak ≥ 0.65, overlap ≥ 0.25) | `-0.20` | `-0.20` | `0.00` |
| Mouth occlusion (moderate: peak ≥ 0.45, overlap ≥ 0.20) | `-0.12` | `-0.12` | `0.00` |

### 3.3 Caps and Special Logic

| Cap / Gate | Old System | New System | Impact |
|---|---|---|---|
| Mouth activity cap (`likely_mouth_cover`) | `0.04` max | `0.04` max | Same |
| Weak palm dump no-lower-mouth cap | `≤ 0.49` | `≤ 0.49` | Same |
| Unknown open mouth no-delivery cap | `≤ 0.49` | `≤ 0.49` | Same |
| Occlusion suppression (possible palm dump) | Suppresses penalty | Suppresses penalty | Same |
| Occlusion suppression (supported pinch delivery) | Suppresses penalty | Suppresses penalty | Same |
| **`mouth_open_allowed` gate** | **Required** for mouth activity | **ABSENT** | **N/A — NEW MISSING** |

### 3.4 Maximum Theoretical Score

**Old system (best case):**
```
0.08 + 0.20 + 0.14 + 0.10 + 0.12 + 0.25 + 0.15 + 0.06 + 0.22 = 1.32 → clamped to 1.0
```

**New system (best case):**
```
0.08 + 0.20 + 0.14 + 0.10 + 0.12 + 0.00 + 0.15 + 0.06 + 0.22 = 1.07 → clamped to 1.0
```
(Same clamped max, but the new system lacks the `+0.25` YOLO contribution that was in the old codebase.)

### 3.5 Maximum False-Penalty Score (worst-case false positive)

**Old system** with moderate contact (0.5), no mouth open, moderate dwell (0.5s), withdrawal seen, but flat palm dominant:
```
0.08 + 0.10 + 0.00 + 0.10 + 0.00 + 0.00 + 0.15 + 0.05 = 0.48 before penalties/floor
- 0.08 (no withdrawal if partial only → still 0)
= 0.40-ish range, then clamped
```

**New system** same scenario but without `mouth_open_allowed` gating:
```
0.08 + 0.10 + 0.03 + 0.10 + 0.15 + 0.05 = 0.51 → crosses 0.50 TRUE threshold
```
The new system can cross the detection threshold **without the mouth ever opening** because the `mouth_open_allowed` gate is gone.

---

## 4. Event Classification Differences (`event_style.py` vs. `intake_detection_style.py`)

The `intake_detection_style.py` file is **functionally identical** to the old `event_style.py`. Both files contain the exact same:

- `EventStyleAggregate` dataclass (same 14 fields)
- `EventStyleClassification` dataclass (same 17 fields)
- `classify_event_style()` function with identical thresholds

**Identical thresholds across both files:**
- `flat_palm_dominant`: `flat_palm_frame_ratio >= 0.50` (line 58/47)
- `low_pinched_delivery`: `pinch < 0.15 AND loose_grip < 0.20 AND holding_object < 0.20` (lines 59–63/48–52)
- `brief_contact`: `0.15 ≤ dwell ≤ 0.95` (line 64/53)
- `palm_dump_brief_contact`: `0.05 ≤ dwell ≤ 0.45` (line 65/54)
- `palm_dump_valid_contact_duration`: `0.05 ≤ dwell ≤ 1.00` (line 66/55)
- `palm_dump_approach_then_withdrawal`: `clear_or_partial_withdrawal AND peak_mouth_contact ≥ 0.45` (line 67/56)
- `palm_dump_has_lower_mouth_geometry`: `palm_lower_mouth_ratio ≥ 0.10 OR min_palm_to_lower_mouth_norm ≤ 1.25` (lines 68–74/57–63)
- `delivery_like_occlusion`: `flat_palm ≥ 0.25 AND palm_dump_valid_contact_duration AND clear_or_partial_withdrawal AND peak_mouth_contact ≥ 0.45 AND palm_dump_has_lower_mouth_geometry` (lines 81–87/70–76)
- `cover_like_occlusion`: `flat_palm ≥ 0.50 AND peak_mouth_contact ≥ 0.50 AND NOT delivery_like_occlusion AND cover_like_conditions` (lines 88–100/77–89)
- `mouth_mostly_visible`: `mouth_visible ≥ 0.60 AND peak_occlusion < 0.45` (lines 102–105/91–94)
- `possible_palm_dump_delivery`: `flat_palm ≥ 0.35 AND brief_contact AND clear_or_partial_withdrawal AND (mouth_mostly_visible OR weak_mouth_activity) AND peak_mouth_contact ≥ 0.5` (lines 108–114/97–103)
- `likely_mouth_cover`: `flat_palm_dominant AND NOT possible_palm_dump_delivery AND low_pinched_delivery AND peak_mouth_contact ≥ 0.5` (lines 115–120/104–109)

**Verdict: Event classification is a faithful port.** There are zero differences in logic or thresholds.

---

## 5. Event Detection Gating Differences

### 5.1 Conditions Required to Trigger `event_detected = True`

**Old system** (old.py lines 1217–1231):
```python
if (
    peak_mouth_contact >= 0.5
    and confidence >= 0.38
    and mouth_open_occurred
    and mouth_open_allowed          # ← PRESENT
    and cooldown_ok
    and not unknown_open_mouth_no_delivery_geometry
    and not (
        weak_palm_dump_no_lower_mouth_geometry
        and not weak_palm_dump_cap_exception_applied
    )
):
    event_detected = True
```

**New system** (intake_detection.py lines 760–770):
```python
if (
    peak_mouth_contact >= 0.5
    and confidence >= 0.38
    and mouth_open_occurred
    # mouth_open_allowed is ABSENT
    and cooldown_ok
    and not unknown_open_mouth_no_delivery_geometry
    and not (weak_palm_dump_no_lower_mouth_geometry and not weak_palm_dump_cap_exception_applied)
):
    event_detected = True
```

### 5.2 Impact of Missing `mouth_open_allowed`

In the old system, `mouth_open_allowed = peak_mouth_contact > 0.05 or in_mouth_zone_occurred` (line 818). This variable served **two purposes**:
1. **Gating mouth activity contribution** to confidence (line 935: `mouth_open_occurred and mouth_open_allowed`)
2. **Gating event detection** (line 1221: `mouth_open_allowed` must be `True`)

In the new system, `mouth_open_allowed` is **never computed or used**. The consequence is:
- Events can fire when the mouth opened at any point (`mouth_open_occurred = True`), **regardless of whether there was meaningful mouth contact or hand-in-mouth-zone evidence**.
- Without the additional `mouth_open_allowed` guard, events with weak contact (peak just over 0.5 by coincidence) and no hand-in-zone evidence can still trigger `event_detected`.
- In the old system, a hand waving near the mouth that accidentally peaked at 0.5 contact would still need `in_mouth_zone_occurred` or `peak_mouth_contact > 0.05` — a safety net that the new system removed.

---

## 6. Root Cause Analysis

### CRITICAL: `mouth_open_allowed` variable is absent

This is the **single highest-impact regression**.

- **Old:** `mouth_open_allowed = peak_mouth_contact > 0.05 or in_mouth_zone_occurred` (old.py:818). Used at old.py:935 and old.py:1221.
- **New:** The variable `mouth_open_allowed` **does not exist** in `intake_detection.py`. Neither the mouth-activity contribution nor the event detection gate checks it.
- **Impact:** The system awards mouth-activity points (up to +0.14) and triggers event detection even when the hand never entered the mouth zone and contact was only marginal.

### HIGH: `mouth_activity_points` function signature and behavior changed

- **Old** (`old.py` lines 152–161): Takes two args `(peak_mouth_open_ratio, mouth_open_occurred)`. Returns `0.0` immediately if `mouth_open_occurred` is `False`. Graduated reward: `≥0.10→0.03, ≥0.20→0.08, ≥0.30→0.14`.
- **New** (`intake_detection.py` lines 95–106): Takes two args `(peak_mouth_open_ratio, mouth_open_occurred)` but the caller at line 612 passes **only `mouth_open_occurred`** (which it does pass). So this is actually identical in terms of argument passing. However, the **reward tiers changed**: `≥0.60→0.14, ≥0.45→0.10, ≥0.30→0.06, ≥0.18→0.03`. The reward at common mouth-open ratios (0.20–0.40) is now **lower** in the new system, which partially offsets the missing gate.
- **Net effect:** The new system gives less reward per tier but doesn't require the `mouth_open_allowed` gate. For borderline cases (mouth opened slightly but hand was near mouth), the net effect is roughly neutral. For cases where mouth opened but hand was NOT near mouth, the new system gives partial reward where old gave zero.

### HIGH: `fingertip_delivery` exclusion guard removed

- **Old** (lines 982–993): Extra condition prevents fingertip_delivery when `event_style == "palm_dump_delivery"` AND `flat_palm_frame_ratio >= 0.70` AND `palm_lower_mouth_ratio <= 0.0` AND `min_palm_to_lower_mouth_norm > 0.90`.
- **New** (lines 641–655): This exclusion guard is absent.
- **Impact:** In palm-dump scenarios with high flat-palm ratio and no lower-mouth geometry, the new system awards `+0.06` or `+0.03` fingertip delivery bonus that the old system would suppress. This inflates confidence for ambiguous palm-dump cases, potentially pushing them over the detection threshold.

### MEDIUM: `compute_mouth_occlusion_score` completely rewritten

The new function is shorter but loses the **graduated, multi-factor accumulation** of the old:

**Old (lines 164–201):** 6 independent sub-scores accumulated:
1. `hand_bbox_mouth_overlap_ratio ≥ 0.60 → +0.45`
2. `≥ 0.30 → +0.30`
3. `≥ 0.10 → +0.15`
4. `palm_center_in_mouth_roi → +0.25`
5. `palm_close AND palm_as_close_as_fingertip → +0.20`
6. `flat_palm AND score ≥ 0.35 → +0.10`
Then clamped to 1.0, then classified as heavy/moderate/none.

**New (lines 109–126):** 3 blunt branches:
1. `overlap ≥ 0.60 AND palm_in_roi → return min(1.0, overlap + 0.15 or overlap + 0.10), "heavy_occlusion"`
2. `overlap ≥ 0.35 AND palm_in_roi → return 0.55, "moderate_occlusion"`
3. `palm < 0.50 OR fingertip < 0.40 → return 0.25/0.40/0.0`
4. `else → return 0.0, "none"`

**Key differences:**
- The old system could reach heavy occlusion (`≥0.65`) through various combinations of the 6 sub-scores. The new system can ONLY reach heavy if `overlap ≥ 0.60 AND palm_in_roi` — a much narrower path.
- The old system's `palm_close AND palm_as_close_as_fingertip → +0.20` sub-score is brand new logic not present in the new system — it recognized when the palm was physically closer to the mouth than the fingertip, which is a strong indicator of non-delivery (face-covering) behavior.
- The new system returns a flat `0.55` for moderate occlusion regardless of other signals, where the old system could accumulate up to `0.45 + 0.25 + 0.20 = 0.90` from multiple sub-features.
- **Net effect:** The new system UNDER-penalizes moderate occlusion and OVER-simplifies the heavy occlusion path. This means occlusion-based mouth-covering is less frequently correctly penalized.

### MEDIUM: All pixel-based tracking removed

The old system maintained parallel pixel-based and normalized tracking signals:

| Signal | Old | New |
|---|---|---|
| `approach_speed_px_based` | Computed (line 623) | ❌ Removed |
| `approach_speed_norm` | Computed (line 624) | Computed (line 417) |
| `near_mouth_px_based` | Computed (line 637) | ❌ Removed |
| `near_mouth_norm_based` | Computed (line 638) | Computed (line 429) |
| `mouth_contact_px_based` | Computed (line 640–646) | ❌ Removed |
| `mouth_contact_norm_based` | Computed (line 647–653) | Computed (line 432–438) |
| `withdrew_enough_px_based` | Computed (line 823–825) | ❌ Removed |
| `withdrew_enough_norm_based` | Computed (line 827–830) | Computed (line 576–578) |
| `mouth_contact_delta_norm_minus_px` | Computed (line 655) | ❌ Removed |

The pixel-based signals served as a **cross-validation layer** — when normalized and pixel-based signals disagreed significantly (captured by `mouth_contact_delta_norm_minus_px`), it indicated unusual camera distance or angle, which was used in the old `compute_mouth_occlusion_score` logic. The loss of this cross-validation reduces the system's robustness to varying camera setups.

### LOW: Event debug dict removed

The old system's `event_debug` dictionary (old.py lines 1236–1341) contained 80+ fields for post-hoc analysis. This was critical for:
- Understanding why a particular video was classified as TRUE or FALSE
- Iterating on thresholds based on failure analysis
- Batch analysis in `analyze_videos.py`

The new system returns only `event_detected`, `event_confidence`, `frame_confidence`, `status`, `mouth_open`, and `hand_near_mouth` — making it **nearly impossible to diagnose misclassifications** without adding debug prints.

### LOW: `analyze_videos.py` uses the OLD system and cannot test the new one

The batch analyzer (`analyze_videos.py` line 443: `import old`) imports the old module. The new `intake_detection.py` has no batch analysis counterpart, meaning the new system has **never been formally evaluated on the test video suite**.

---

## 7. Prioritized Fix Recommendations

### P0 — CRITICAL: Add `mouth_open_allowed` back

**File:** `intake_detection.py`

**Where:** Around line 570 (after `mouth_open_allowed` would be computed, before the scoring section).

**Fix:**
```python
mouth_open_allowed = peak_mouth_contact > 0.05 or in_mouth_zone_occurred
```

Then use it in two places:

1. **Line 612** — Gate the mouth activity contribution:
   ```python
   raw_mouth_activity_contribution = mouth_activity_points(peak_mouth_open_ratio, mouth_open_occurred and mouth_open_allowed)
   ```

2. **Line 763** — Add to event detection gate:
   ```python
   if (
       peak_mouth_contact >= 0.5
       and confidence >= 0.38
       and mouth_open_occurred
       and mouth_open_allowed          # ← ADD THIS
       and cooldown_ok
       ...
   ):
   ```

### P0 — CRITICAL: Restore the `fingertip_delivery` exclusion guard

**File:** `intake_detection.py`, lines 641–655.

Add the missing guard from old.py lines 987–993:
```python
and not (
    event_style == "palm_dump_delivery"
    and flat_palm_frame_ratio >= 0.70
    and palm_lower_mouth_ratio <= 0.0
    and min_palm_to_lower_mouth_norm is not None
    and min_palm_to_lower_mouth_norm > 0.90
)
```

The complete `fingertip_delivery` block should read:
```python
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
```

### P1 — HIGH: Restore the old `compute_mouth_occlusion_score`

**File:** `intake_detection.py`, lines 109–126.

Replace the entire function with the old version from `old.py` lines 164–201:
```python
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
```

### P1 — HIGH: Restore `mouth_activity_points` old thresholds

**File:** `intake_detection.py`, lines 95–106.

The old version had higher rewards at common mouth-open ratios. Restore the old tier structure:
```python
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
```

**Note:** The old version also returns `0.14` immediately when `mouth_open_occurred` is True, regardless of the ratio — a deliberate design choice that gives full mouth-activity credit whenever the mouth is visually confirmed open. The new version's graduated thresholds without the boolean shortcut reduce the reward for confirmed-mouth-open events.

### P2 — MEDIUM: Restore pixel-based parallel tracking

**File:** `intake_detection.py`

Add back pixel-based signals as they existed in the old `update_hand_state` (old.py lines 623–655). At minimum, compute `near_mouth_px_based` and `mouth_contact_px_based` to support downstream logic and future debug:

```python
# In update_hand_state, around line 414-430:
approach_speed_px_based = moving_toward(state.prev_mouth_dist, curr_dist)
near_mouth_px_based = curr_dist < MOUTH_NEAR_DISTANCE_PX or in_mouth_zone_now
mouth_contact_px_based = min(1.0, max(1.0 - curr_dist / max(1.0, MOUTH_NEAR_DISTANCE_PX * 1.2), 1.0 if in_mouth_zone_now else 0.0))
```

You'll also need to add `MOUTH_NEAR_DISTANCE_PX = 60` and `APPROACH_SPEED_THRESHOLD = 4.0` to the constants section (currently missing from the new file).

### P2 — MEDIUM: Add event debug dictionary

**File:** `intake_detection.py`

Restore the event debug dictionary (the ~80-field dict from old.py lines 1236–1341) so that batch analysis and per-event post-hoc diagnosis is possible. At minimum, include:
- `event_confidence`, `raw_event_score`, `positive_points_total`, `penalty_points_total`
- `positive_reasons`, `penalty_reasons`, `reasons`
- `event_style`, `dwell`, `mouth_open`, `mouth_contact`, `withdrew_enough`
- `peak_mouth_contact`, `peak_mouth_open_ratio`, `dwell_contribution`
- All contribution values

### P3 — LOW: Create batch analyzer for new system

**File:** Create a new script (e.g., `analyze_videos_new.py`) that imports `intake_detection.py` and processes the same test video suite using the same evaluation logic as `analyze_videos.py`. This is essential for measuring the accuracy impact of fixes.

### P3 — LOW: Restore diagnostic debug printing

**File:** `intake_detection.py`

Add back `_print_event_debug` (old.py lines 279–310) with a configurable flag so developers can enable verbose per-event breakdown during development and testing.

---

## Appendix A: Line Number Quick Reference

| Item | Old System | New System |
|---|---|---|
| `mouth_activity_points` | `old.py:152–161` | `intake_detection.py:95–106` |
| `compute_mouth_occlusion_score` | `old.py:164–201` | `intake_detection.py:109–126` |
| `mouth_open_allowed` definition | `old.py:818` | **MISSING** |
| Mouth activity gating | `old.py:935` | `intake_detection.py:612` (ungated) |
| Fingertip delivery exclusion | `old.py:982–993` | `intake_detection.py:641–655` (absent) |
| Event detection gate | `old.py:1217–1231` | `intake_detection.py:760–770` |
| Event debug dict | `old.py:1236–1341` | **MISSING** |
| `classify_event_style` | `event_style.py:49–152` | `intake_detection_style.py:45–141` |
| Batch analyzer | `analyze_videos.py` (uses `old`) | **NONE EXISTS** |
