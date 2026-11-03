"""Hand depth against the patient's face from one camera frame: Depth Anything V2 Small on ONNX Runtime.

The landmark models say where a hand is in the image. They can't say whether a hand that covers the mouth in
2D is at the lips or 20 cm in front of them. A monocular depth model predicts, for every pixel, how near it is:
a relative inverse depth ("disparity", larger = nearer) with an unknown scale and offset per frame, so no single
value means anything on its own. `hand_depth` turns a disparity map plus the frame's landmarks into one number
per hand:

    front = (disparity on the finger - disparity of the face) / disparity of the face

The model reads the far background as about 0, so disparity is about proportional to 1 / depth and `front` is
about (face depth - finger depth) / finger depth: 0 at the lips, 0.3 for a hand 20 cm in front of a face 80 cm
away, negative behind the face. The scale cancels out; an offset only shrinks the number (conservative). The
face reference is the median of the face box with every hand's box cut out, so a hand over the mouth never
becomes its own reference, and the finger sample sits a little back from the tip, where the patch is all hand.
The entries also carry the raw readings (`tip`, `ref`, `lip`, `spread`, `far`, `cover`) for offline analysis.

Model: onnx-community/depth-anything-v2-small, onnx/model.onnx (fp32, Apache-2.0; the fp16 file runs twice as
slow on CPU). Dynamic input [1, 3, H, W] with H and W multiples of 14, output [1, H, W]. In the app container on
this laptop: about 170 ms at 266 px and 340 ms at 364 px on 4 threads (3 Nov 2026), so the engine runs it only
while a hand is near a face and at most every DEPTH_EVERY frames.
"""

import math

import numpy as np

MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
PATCH = 14                 # the ViT patch: both input sides are multiples of it
DEFAULT_LONG_SIDE = 266

# Compact packet indices (packets.FACE_INDICES / HAND_INDICES): Face Mesh 13/14 are the lips, 61/291 the mouth
# corners; hand 0 is the wrist, 4/8/12 the thumb, index and middle tips (the tips the detector measures with).
UPPER_LIP, LOWER_LIP, LEFT_MOUTH, RIGHT_MOUTH = 2, 3, 5, 8
WRIST, THUMB_TIP, INDEX_TIP, MIDDLE_TIP = 0, 1, 4, 7
TIPS = (THUMB_TIP, INDEX_TIP, MIDDLE_TIP)
TIP_INSET = 0.15           # sample this far from the tip toward the wrist: on the finger, not on its edge
NEAR_FACE = 0.6            # a hand is near a face within this many box widths/heights of the box
MIN_FACE_PX = 12           # a face box narrower than this on the map gives no reference
MIN_UNCOVERED = 0.25       # of the face box must be free of hands for the face reference
MIN_REFERENCE = 1e-3


class DepthModel:
    def __init__(self, path, long_side: int = DEFAULT_LONG_SIDE, threads: int = 4):
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        self.long_side = int(long_side)

    def input_size(self, width: int, height: int) -> tuple[int, int]:
        """The model input for a frame: the long side scaled to long_side, both sides multiples of PATCH."""
        scale = self.long_side / max(width, height)
        return (max(PATCH, round(width * scale / PATCH) * PATCH), max(PATCH, round(height * scale / PATCH) * PATCH))

    def disparity(self, image) -> np.ndarray:
        """Pillow RGB image -> (rows, cols) float32 relative inverse depth (larger = nearer) at the input size."""
        from PIL import Image

        patch = image.resize(self.input_size(*image.size), Image.Resampling.BICUBIC)
        tensor = ((np.asarray(patch, dtype=np.float32) / 255.0 - MEAN) / STD).transpose(2, 0, 1)[None]
        out = self.session.run(None, {self.input_name: np.ascontiguousarray(tensor)})[0]
        return np.asarray(out, dtype=np.float32).reshape(out.shape[-2], out.shape[-1])


def near_face(faces: list, hands: list) -> bool:
    """Whether a fingertip of any hand is within NEAR_FACE box sizes of any face box (packet coordinates)."""
    for face in faces:
        x, y, width, height = face["box"]
        for hand in hands:
            for tip in TIPS:
                px, py = hand[tip][0], hand[tip][1]
                if (x - NEAR_FACE * width <= px <= x + width * (1 + NEAR_FACE)
                        and y - NEAR_FACE * height <= py <= y + height * (1 + NEAR_FACE)):
                    return True
    return False


def _median_patch(disparity: np.ndarray, x: float, y: float, radius: int):
    rows, cols = disparity.shape
    cx, cy = int(round(x)), int(round(y))
    if not (0 <= cx < cols and 0 <= cy < rows):
        return None
    patch = disparity[max(0, cy - radius):cy + radius + 1, max(0, cx - radius):cx + radius + 1]
    return float(np.median(patch)) if patch.size else None


def _face_reference(disparity: np.ndarray, face: dict, hand_boxes: list):
    rows, cols = disparity.shape
    x, y, width, height = face["box"]
    x0, y0 = max(0, int(math.floor(x * cols))), max(0, int(math.floor(y * rows)))
    x1, y1 = min(cols, int(math.ceil((x + width) * cols))), min(rows, int(math.ceil((y + height) * rows)))
    if x1 - x0 < MIN_FACE_PX or y1 - y0 < MIN_FACE_PX:
        return None
    region = disparity[y0:y1, x0:x1]
    margin = 0.1 * (x1 - x0)
    mask = np.ones(region.shape, dtype=bool)
    for hx0, hy0, hx1, hy1 in hand_boxes:
        a0, b0 = max(0, int(hx0 - margin) - x0), max(0, int(hy0 - margin) - y0)
        a1, b1 = min(x1 - x0, int(math.ceil(hx1 + margin)) - x0), min(y1 - y0, int(math.ceil(hy1 + margin)) - y0)
        if a1 > a0 and b1 > b0:
            mask[b0:b1, a0:a1] = False
    uncovered = region[mask]
    if uncovered.size < MIN_UNCOVERED * region.size:
        return None
    points = face["points"]
    mouth = ((points[LEFT_MOUTH][0] + points[RIGHT_MOUTH][0]) / 2 * cols,
             (points[UPPER_LIP][1] + points[LOWER_LIP][1]) / 2 * rows)
    lip_covered = any(hx0 - margin <= mouth[0] <= hx1 + margin and hy0 - margin <= mouth[1] <= hy1 + margin
                      for hx0, hy0, hx1, hy1 in hand_boxes)
    radius = max(1, round(0.04 * (x1 - x0)))
    return {"mouth": mouth, "width": float(x1 - x0), "radius": radius,
            "ref": float(np.median(uncovered)),
            "spread": float(np.percentile(uncovered, 90) - np.percentile(uncovered, 10)),
            "lip": None if lip_covered else _median_patch(disparity, mouth[0], mouth[1], radius),
            "cover": 1.0 - uncovered.size / region.size}


def hand_depth(disparity: np.ndarray, faces: list, hands: list) -> list:
    """One entry per hand, in step with `hands`: {"face": index, "front": float, ...}, or None when there is no
    usable reference (no face, the face too small on the map, or hands covering most of it)."""
    rows, cols = disparity.shape
    far = float(np.percentile(disparity, 5)) if disparity.size else 0.0
    hand_boxes = []
    for hand in hands:
        xs, ys = [point[0] * cols for point in hand], [point[1] * rows for point in hand]
        hand_boxes.append((min(xs), min(ys), max(xs), max(ys)))
    references = [_face_reference(disparity, face, hand_boxes) for face in faces]
    out = []
    for hand in hands:
        best = None   # the face nearest a fingertip, in face widths, and that tip
        for index, reference in enumerate(references):
            if reference is None:
                continue
            for tip in TIPS:
                distance = math.hypot(hand[tip][0] * cols - reference["mouth"][0],
                                      hand[tip][1] * rows - reference["mouth"][1]) / reference["width"]
                if best is None or distance < best[0]:
                    best = (distance, index, tip)
        if best is None:
            out.append(None)
            continue
        _, index, tip = best
        reference = references[index]
        wrist = hand[WRIST]
        value = _median_patch(disparity,
                              (hand[tip][0] + TIP_INSET * (wrist[0] - hand[tip][0])) * cols,
                              (hand[tip][1] + TIP_INSET * (wrist[1] - hand[tip][1])) * rows, reference["radius"])
        if value is None or reference["ref"] <= MIN_REFERENCE:
            out.append(None)
            continue
        out.append({"face": index, "front": round((value - reference["ref"]) / reference["ref"], 4),
                    "tip": round(value, 4), "ref": round(reference["ref"], 4),
                    "lip": None if reference["lip"] is None else round(reference["lip"], 4),
                    "spread": round(reference["spread"], 4), "far": round(far, 4),
                    "cover": round(reference["cover"], 3)})
    return out
