"""Objects in the patient's hand at the mouth: a COCO detector (YOLOX, Apache-2.0) on ONNX Runtime.

The landmark models see hands and faces, nothing else. A phone held to the cheek, a cup or a toothbrush at the
mouth makes the same hand-to-mouth geometry as a pill, so the intake detector prompts for it. A COCO detector
cannot see a pill (no such class, and a pill is a few pixels), but it knows the things that are not a pill:
`detect` returns the WATCHED objects in a frame, and `held_objects` says which of them each hand holds, by
overlap with the hand's box. The temporal pipeline counts those readings over the contact zone (`object_in_hand`)
and the policy decides what a veto object means (intake_v1/config.py OBJECT_VETO_LABELS, OBJECT_VETO).

Model: YOLOX from Megvii's release 0.1.1rc0 (Apache-2.0). Fixed input [1, 3, S, S] in BGR 0-255, letterboxed
with 114 (`preprocess`, the repo's preproc), output [1, N, 85] = (cx, cy, w, h, objectness, 80 class scores) per
grid cell of the strides 8, 16 and 32 (`decode`, the repo's demo_postprocess), then class-agnostic NMS at 0.45.
In the app container on this laptop (4 threads, 3 Nov 2026): yolox_s (640) about 170 ms, yolox_tiny (416) about
50 ms, yolox_nano (416) about 20 ms. On the C:\\pilldetector clips only yolox_s saw the phone reliably (0.7-0.8;
tiny 0.46 in one frame of two, nano 0.3 and a false phone on a pill hand), so the engine runs it like the depth
model: only while a hand is near a face, every OBJECT_EVERY frames.
"""

import numpy as np

COCO_CLASSES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
    "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow", "elephant",
    "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
    "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange", "broccoli",
    "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant", "bed", "dining table", "toilet",
    "tv", "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush",
)
# The objects worth reporting near a mouth: things that are not a pill. The policy's veto set is a subset.
WATCHED = frozenset({"cell phone", "remote", "toothbrush", "cup", "bottle", "wine glass", "bowl", "fork", "knife",
                     "spoon", "scissors", "hair drier", "book"})
STRIDES = (8, 16, 32)
PAD = 114
NMS_IOU = 0.45
HAND_MARGIN = 0.25        # the hand's box, grown by this fraction of its size (at least MIN_MARGIN of the frame),
MIN_OVERLAP = 0.3         # must overlap the object's box by this fraction of the smaller of the two boxes
MIN_MARGIN = 0.02


class ObjectDetector:
    def __init__(self, path, threads: int = 4, score: float = 0.5):
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        model_input = self.session.get_inputs()[0]
        self.input_name = model_input.name
        self.size = int(model_input.shape[2])          # [1, 3, S, S]
        self.score = float(score)
        self._grid, self._stride = grid(self.size)

    def detect(self, image) -> list:
        """Pillow RGB image -> WATCHED objects at or above `score`: [{"label", "score", "box": [x, y, w, h]}]
        with the box normalised to the frame."""
        bgr = np.asarray(image)[:, :, ::-1]
        tensor, ratio = preprocess(bgr, self.size)
        raw = self.session.run(None, {self.input_name: tensor})[0]
        predictions = decode(raw[0], self._grid, self._stride)
        return detections(predictions, ratio, image.width, image.height, self.score)


def grid(size: int) -> tuple[np.ndarray, np.ndarray]:
    """Cell centres (N, 2) and strides (N, 1) of every prediction, in the model's output order."""
    grids, strides = [], []
    for stride in STRIDES:
        cells = size // stride
        xv, yv = np.meshgrid(np.arange(cells), np.arange(cells))
        cell = np.stack((xv, yv), 2).reshape(-1, 2)
        grids.append(cell)
        strides.append(np.full((len(cell), 1), stride))
    return np.concatenate(grids).astype(np.float32), np.concatenate(strides).astype(np.float32)


def preprocess(bgr: np.ndarray, size: int) -> tuple[np.ndarray, float]:
    """YOLOX preproc: letterbox to size x size with 114, BGR 0-255 float32 NCHW; returns the scale ratio."""
    import cv2

    padded = np.full((size, size, 3), PAD, dtype=np.uint8)
    ratio = min(size / bgr.shape[0], size / bgr.shape[1])
    resized = cv2.resize(bgr, (int(bgr.shape[1] * ratio), int(bgr.shape[0] * ratio)), interpolation=cv2.INTER_LINEAR)
    padded[: resized.shape[0], : resized.shape[1]] = resized
    return np.ascontiguousarray(padded.transpose(2, 0, 1), dtype=np.float32)[None], ratio


def decode(raw: np.ndarray, cell: np.ndarray, stride: np.ndarray) -> np.ndarray:
    """YOLOX demo_postprocess: (N, 85) raw -> (N, 85) with boxes as cx, cy, w, h in input pixels."""
    out = np.array(raw, dtype=np.float32)
    out[:, :2] = (out[:, :2] + cell) * stride
    out[:, 2:4] = np.exp(out[:, 2:4]) * stride
    return out


def nms(boxes: np.ndarray, scores: np.ndarray, threshold: float = NMS_IOU) -> list:
    """YOLOX's numpy NMS: indices to keep, highest score first."""
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        index = order[0]
        keep.append(int(index))
        xx1, yy1 = np.maximum(x1[index], x1[order[1:]]), np.maximum(y1[index], y1[order[1:]])
        xx2, yy2 = np.minimum(x2[index], x2[order[1:]]), np.minimum(y2[index], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1 + 1) * np.maximum(0.0, yy2 - yy1 + 1)
        iou = inter / (areas[index] + areas[order[1:]] - inter)
        order = order[1:][iou <= threshold]
    return keep


def detections(predictions: np.ndarray, ratio: float, width: int, height: int, score: float) -> list:
    """Decoded predictions -> WATCHED objects above `score` after class-agnostic NMS, boxes normalised."""
    boxes, class_scores = predictions[:, :4], predictions[:, 4:5] * predictions[:, 5:]
    labels = class_scores.argmax(1)
    confidence = class_scores[np.arange(len(labels)), labels]
    keep = confidence >= score
    if not keep.any():
        return []
    boxes, labels, confidence = boxes[keep], labels[keep], confidence[keep]
    corners = np.stack([boxes[:, 0] - boxes[:, 2] / 2, boxes[:, 1] - boxes[:, 3] / 2,
                        boxes[:, 0] + boxes[:, 2] / 2, boxes[:, 1] + boxes[:, 3] / 2], axis=1) / ratio
    out = []
    for index in nms(corners, confidence):
        label = COCO_CLASSES[labels[index]]
        if label not in WATCHED:
            continue
        x1, y1, x2, y2 = corners[index]
        x1, y1 = max(0.0, float(x1)) / width, max(0.0, float(y1)) / height
        x2, y2 = min(float(width), float(x2)) / width, min(float(height), float(y2)) / height
        if x2 > x1 and y2 > y1:
            out.append({"label": label, "score": round(float(confidence[index]), 3),
                        "box": [round(x1, 4), round(y1, 4), round(x2 - x1, 4), round(y2 - y1, 4)]})
    return out


def _overlap_fraction(a: tuple, b: tuple) -> float:
    """Intersection over the smaller box's area; boxes are (x1, y1, x2, y2)."""
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return width * height / smaller if smaller > 0 else 0.0


def held_objects(objects: list, hands: list) -> list:
    """Per hand, in step with `hands`: the detected object it holds ({"label", "score"}, the largest overlap with
    the hand's grown box), or {"label": None} when the detector ran and found nothing on that hand."""
    out = []
    for hand in hands:
        xs, ys = [point[0] for point in hand], [point[1] for point in hand]
        grow_x = max(MIN_MARGIN, HAND_MARGIN * (max(xs) - min(xs)))
        grow_y = max(MIN_MARGIN, HAND_MARGIN * (max(ys) - min(ys)))
        hand_box = (min(xs) - grow_x, min(ys) - grow_y, max(xs) + grow_x, max(ys) + grow_y)
        best = None
        for item in objects:
            x, y, width, height = item["box"]
            fraction = _overlap_fraction(hand_box, (x, y, x + width, y + height))
            if fraction >= MIN_OVERLAP and (best is None or fraction > best[0]):
                best = (fraction, item)
        out.append({"label": best[1]["label"], "score": best[1]["score"]} if best else {"label": None})
    return out
