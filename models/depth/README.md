# Depth model (optional): hand depth against the face on the robot's frames

`depth_anything_v2_small.onnx` is **not committed** (99,060,839 B, above GitHub's 50 MB warning). Without it the
server runs as before: `LandmarkService` logs "hand depth off" and no landmark packet carries `hand_depth`.

- Source: Hugging Face `onnx-community/depth-anything-v2-small`, file `onnx/model.onnx` (fp32). Licence
  Apache-2.0 (the Small variant only; Base/Large/Giant are CC-BY-NC-4.0).
- SHA-256 `afb6a5c28f3b6bf1618c6e43f02073ef9dfdc70e937502d51603e57b0a1df10c`, checked at load
  (`app/services/landmark_service.py`, `DEPTH_MODEL_SHA256`).
- Download:

  ```bash
  curl -L -o models/depth/depth_anything_v2_small.onnx \
    https://huggingface.co/onnx-community/depth-anything-v2-small/resolve/main/onnx/model.onnx
  sha256sum models/depth/depth_anything_v2_small.onnx
  ```

- The Dockerfile copies this folder to `/models/depth/`; `docker-compose.yml` sets `DEPTH_MODEL_PATH`.
- How it is used: `app/services/landmarks/depth.py` (the reading), `app/services/intake_temporal.py`
  (`depth_no_contact`), `app/intake_v1/policy.py` (such an event may prompt, never auto-log).
