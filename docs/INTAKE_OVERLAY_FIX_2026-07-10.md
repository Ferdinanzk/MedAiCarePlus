# Intake Overlay Clipping Fix — 2026-07-10

## Issue

On shorter intake camera layouts, the helper text near the manual-confirmation action was partially clipped. The confidence card used a fixed `bottom-24` position while the manual/cancel button group independently grew upward from `bottom-4`. A third helper label was anchored at the bottom of the framing guide. These absolute layers could overlap inside the camera container's required `overflow-hidden` boundary.

The always-expanded developer panel also used a higher stacking level than the uncertain-intake confirmation overlay, allowing diagnostics to obscure safety-critical instructions.

## Fix

`frontend_source/src/pages/Intake.tsx` now:

- keeps the existing wide, landscape, `object-contain` camera preview;
- places confidence, manual confirmation, its helper copy, and cancel action in one responsive bottom dock;
- gives that dock a bounded height and internal scrolling on short screens, keeping all important text inside the visible panel;
- increases helper text size, line height, contrast, and allows wrapping;
- makes developer diagnostics collapsed by default, bounded, and scrollable;
- places diagnostics behind the confirmation overlay;
- makes the uncertain confirmation overlay scroll-safe with compact responsive spacing and non-overlapping buttons.

## Verification

- `npm.cmd run build --prefix frontend_source`: passed (1,790 modules transformed).
- Relevant temporal/backend tests were run with the final branch preparation and recorded in the handoff.
- No detection thresholds, event state transitions, scoring, occlusion handling, replay behavior, or camera inference coordinates were changed for this UI fix.
