# Liveness model: MiniFASNetV2 (Silent-Face-Anti-Spoofing), ONNX

Used by `app/services/liveness_service.py` to score the main face of each face-login frame: the probability that it
is a real face, not a photo shown on a phone or on paper.

## The file

| | |
|---|---|
| File | `MiniFASNetV2.onnx` (1 743 581 bytes) |
| SHA-256 | `b32929adc2d9c34b9486f8c4c7bc97c1b69bc0ea9befefc380e4faae4e463907` |
| Downloaded from | https://github.com/yakhyo/face-anti-spoofing/releases/download/weights/MiniFASNetV2.onnx |
| Release | `weights` of yakhyo/face-anti-spoofing, asset uploaded 2025-12-20; GitHub lists the asset's digest as the SHA-256 above. Repository commit when downloaded: `01a38903f4f2680bb59d32a994a6c7b5ae56f49b` |
| Exported by | `onnx_export.py` of that repository (`torch.onnx.export`, opset 17, PyTorch 2.8.0); input `input` float32 `[batch, 3, 80, 80]`, output `output` `[batch, 3]` (logits) |
| Original weights | minivision-ai/Silent-Face-Anti-Spoofing, `resources/anti_spoof_models/2.7_80x80_MiniFASNetV2.pth`, commit `b6d5f04ad78778917853b25c778acef6d5626d15` (2020-08-05); SHA-256 of that file `a5eb02e1843f19b5386b953cc4c9f011c3f985d0ee2bb9819eea9a142099bec0` |
| Downloaded | 2026-11-08, on this laptop |

## Licence

Apache License 2.0. `LICENSE` here is the copy from minivision-ai/Silent-Face-Anti-Spoofing ("Copyright 2020
Minivision"); yakhyo/face-anti-spoofing carries the same Apache-2.0 text. Apache-2.0 allows use and redistribution in
any form, modified or not (section 4), with a copy of the licence and a notice of changes. The change: yakhyo
converted the PyTorch weights to ONNX; nothing was retrained. Neither repository has a NOTICE file.

## Checked against the original, not taken on trust

On 2026-11-08, in a scratch environment (torch 2.15.0 CPU, onnxruntime 1.19.2, opencv 4.11.0), with the original
repository's own code (`src/model_lib/MiniFASNet.py`, `src/generate_patches.py`) and its original `.pth`:

- 200 random 80x80 BGR inputs: the softmax of this ONNX file and of the original PyTorch model differ by at most
  1.3e-7. The file holds the original weights.
- `crop_box` in `liveness_service.py` gives the same rectangle as the original `CropImage._get_new_box` for 5000
  random boxes in a 640x480 frame, including boxes past the frame's edge: 0 differences. On the repository's three
  sample images, the service's input tensor is identical to the original crop's.
- The repository's sample images, the original detector's box, this model alone: `image_T1.jpg` (real) P(real)
  0.990; `image_F1.jpg` and `image_F2.jpg` (spoofs) P(real) 0.0013 and 0.0006. The same crops fed as RGB gave the two
  spoofs 0.93 and 0.99, and divided by 255 gave every face about 0.005, so the colour order and the scaling below
  matter. (The sample images stayed in the scratch folder; no face image is in this repository.)

## Preprocessing (from the source code)

From `src/generate_patches.py`, `src/anti_spoof_predict.py`, `src/data_io/functional.py` and `test.py` at commit
`b6d5f04`:

- **Box:** `[x, y, w, h]` in pixels, top-left corner (the source's detector gives `[left, top, right-left+1,
  bottom-top+1]`). `FaceBox` is the same.
- **Crop scale 2.7:** `scale = min((H-1)/h, (W-1)/w, 2.7)`; the box is scaled by it around its centre; a side that
  falls outside the frame is shifted back inside, keeping the crop's size; the crop is
  `frame[y1:y2+1, x1:x2+1]`, corners inclusive (`_get_new_box`, `crop`).
- **Input size:** 80x80 (the file name's `80x80`), `cv2.resize` with its default (bilinear).
- **Colour order:** BGR, as `cv2.imread` gives it; the source never converts to RGB.
- **Normalisation:** none. The source's `to_tensor` returns the 0-255 values as float (`img.float()`; its
  `.div(255)` is commented out, "modify by zkx"). No mean, no standard deviation. HWC to CHW, batch of 1.
- **Output:** 3 logits; softmax. **Index 1 is "real"** (`test.py`: `if label == 1: ... Real Face`); 0 and 2 are the
  two spoof classes. The service returns the softmax value at index 1.

## Threshold

The source publishes no probability threshold: `test.py` calls a face real when index 1 is the largest (argmax) of
the summed predictions. With one model, P(real) >= 0.5 always means index 1 is the largest, and 0.5 is the lowest
threshold for which that holds, so `FACE_LIVENESS_THRESHOLD` defaults to 0.5 (overridable in `.env`). Below 0.5 a
face can still be argmax-real (for example 0.45 / 0.30 / 0.25); the log-mode measurements decide whether to change
it.

## One model, not two

The source's `test.py` sums two models: this one (MiniFASNetV2, crop scale 2.7) and MiniFASNetV1SE (crop scale 4.0).
Face login uses MiniFASNetV2 alone, because:

- At login the face is close: the main face must cover at least 5% of the frame (`FACE_MIN_AREA_RATIO`), about
  124 px tall or more in 640x480. A 4.0 crop then needs about 496 px or more, more than the frame's 480, so the
  source's own formula lowers the scale and the V1SE crop loses the context it was trained with. The 2.7 crop fits
  for faces up to 177 px tall.
- One 80x80 model per frame keeps the cost low (5 frames per login) and gives one probability, not an average of two
  differently cropped ones.

A face taller than 177 px also gets a scale below 2.7, as in the source. The source's `test.py` also refuses images
whose width:height is not 3:4, because (its comment says) its Android app's video stream is 3:4. The crop does not
depend on the frame's shape; how the model does on this laptop's 4:3 landscape webcam frames is what log mode
measures.
