# Object detector (optional): what the hand at the mouth is holding

`yolox_s.onnx` (default) and `yolox_tiny.onnx` are **not committed** (`.gitignore` ignores `*.onnx`). Without the
file the server runs as before: `LandmarkService` logs "hand objects off" and no landmark packet carries `objects`.

- Source: Megvii YOLOX, release 0.1.1rc0 (Apache-2.0), COCO-trained:
  https://github.com/Megvii-BaseDetection/YOLOX/releases/tag/0.1.1rc0
- SHA-256, checked at load (`app/services/landmark_service.py`, `OBJECT_MODEL_SHA256`):
  - `yolox_s.onnx` (640 px, 35,858,002 B): `c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063`
  - `yolox_tiny.onnx` (416 px, 20,219,662 B): `427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7`
- Download:

  ```bash
  curl -L -o models/objects/yolox_s.onnx \
    https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx
  sha256sum models/objects/yolox_s.onnx
  ```

- The Dockerfile copies this folder to `/models/objects/`; `docker-compose.yml` sets `OBJECT_MODEL_PATH`.
- How it is used: `app/services/landmarks/objects.py` (detection and which hand holds what),
  `app/services/intake_temporal.py` (`object_in_hand`), `app/intake_v1/policy.py` and `config.py`
  (`OBJECT_VETO_LABELS`, `OBJECT_VETO`: such an event prompts, or is dropped).
