# SigLIP Emotion Classifier Migration — 2026-07-10

## Change

The active classifier moved from the custom 64×64 PyTorch CNN at `models/emotion/model4.2.2.pth` to the local Hugging Face checkpoint at `models/emotion_hf/Facial-Emotion-Detection-SigLIP2/`.

The checkpoint architecture is `SiglipForImageClassification`, based on `google/siglip2-base-patch16-224`. MediaPipe face detection, padded cropping, and roll alignment remain. The aligned RGB crop is passed to the checkpoint's `AutoImageProcessor`, which resizes to 224×224 and applies SigLIP normalization. Loading uses `local_files_only=True`; the old CNN is not a live fallback.

## Label policy

All APIs, database writes, history exports, UI lists/charts, and alerts use only:

`happy`, `sad`, `angry`, `neutral`, `surprised`, `disgust`

Both `Surprise` and the checkpoint's non-product raw class normalize immediately to `surprised`; their probability mass is combined. The raw label is never returned or stored.

`disgust` remains schema/UI/manual-entry valid and alert-worthy, but this checkpoint has no direct disgust output. The classifier does not fabricate disgust predictions.

Family alerts apply to `sad`, `angry`, and `disgust` at the existing confidence threshold. `surprised` is not negative by default.

## Files changed

- SigLIP inference and centralized canonical-label policy.
- Emotion APIs, histories, database migration, and family alert query.
- Frontend emotion list, charts, history colors, translations, and types.
- Docker/config/dependencies, evaluation command, and safety tests.

## Verification and risks

The full local weights loaded offline and completed a CPU forward pass with `(1, 3, 224, 224)` input and exactly the six canonical public probability keys. Final test/build commands are recorded in the handoff.

CPU inference is slower than the former CNN. Accuracy and subgroup performance still require representative consented validation. Automatic disgust prediction requires a separately validated checkpoint. Deployments must retrieve Git LFS weights or supply `EMOTION_HF_MODEL_PATH`.
