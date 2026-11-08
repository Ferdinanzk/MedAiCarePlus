import copy
import logging
import sys
import threading
from functools import wraps
import numpy as np
from app.config import (
    FACE_REC_BASE, FACE_DET_MODEL, FACE_REID_MODEL,
    LANDMARKS_MODEL,
    FACE_DET_CONFIDENCE, FACE_MATCH_THRESHOLD, DEVICE,
    FACE_MIN_AREA_RATIO, FACE_SECOND_FACE_RATIO, FACE_LOGIN_MIN_MATCHES,
    FACE_LIVENESS_MODE, FACE_LIVENESS_THRESHOLD,
)
from app.services.face_login import FaceBox, FrameVerdict, LoginDecision, decide, main_face
from app.services.liveness_service import LivenessService

log = logging.getLogger(__name__)

# Lazy imports — only resolved when models are available
_models_loaded = False
FaceDetector = FaceIdentifier = LandmarksDetector = FacesDatabase = Core = None


def synchronized(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return guarded


def _load_imports():
    global FaceDetector, FaceIdentifier, LandmarksDetector, FacesDatabase, Core, _models_loaded
    if _models_loaded:
        return
    sys.path.insert(0, str(FACE_REC_BASE))
    from face_detector import FaceDetector as _FD
    from face_identifier import FaceIdentifier as _FI
    from landmarks_detector import LandmarksDetector as _LD
    from faces_database import FacesDatabase as _DB
    from openvino import Core as _Core
    FaceDetector, FaceIdentifier, LandmarksDetector, FacesDatabase, Core = _FD, _FI, _LD, _DB, _Core
    _models_loaded = True


class FaceRecognitionService:
    """Singleton wrapping the OpenVINO face recognition pipeline."""

    _instance: "FaceRecognitionService | None" = None
    _available: bool = False

    def __init__(self):
        self.lock = threading.RLock()
        try:
            _load_imports()
            core = Core()
            self.face_det = FaceDetector(
                core, str(FACE_DET_MODEL), input_size=(0, 0),
                confidence_threshold=FACE_DET_CONFIDENCE
            )
            self.face_det.deploy(DEVICE)

            self.lm_det = LandmarksDetector(core, str(LANDMARKS_MODEL))
            self.lm_det.deploy(DEVICE, max_requests=10)

            self.face_id = FaceIdentifier(
                core, str(FACE_REID_MODEL),
                match_threshold=FACE_MATCH_THRESHOLD,
                match_algo="MIN_DIST"
            )
            self.face_id.deploy(DEVICE, max_requests=10)

            # The gallery starts empty; the lifespan loads it from the face_vector table (face_vectors.reload).
            from app.services import face_vectors
            faces_db = face_vectors.build_gallery([])
            self.face_id.set_faces_database(faces_db)
            FaceRecognitionService._available = True
            log.info("[FaceRecognition] Loaded: %d identity(ies) in the gallery until the face vectors load",
                     len(faces_db))
        except Exception as exc:
            FaceRecognitionService._available = False
            print(f"[FaceRecognition] Not available: {exc}")

    @classmethod
    def get_instance(cls) -> "FaceRecognitionService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @synchronized
    def load_gallery(self, gallery) -> None:
        """Give face login the gallery built from the face_vector rows (face_vectors.reload)."""
        if not FaceRecognitionService._available:
            return
        self.face_id.set_faces_database(gallery)

    @synchronized
    def get_face_direction(self, frame_bgr: np.ndarray) -> dict:
        """
        Detect face and determine head orientation (front/left/right) from landmarks.
        Used by the onboarding face enrollment wizard.
        Returns: {detected: bool, direction: str, yaw: float, stub: bool}
        """
        if not FaceRecognitionService._available:
            # Stub mode — return front so frontend can fall back to manual capture
            return {"detected": False, "direction": "none", "stub": True, "face_size_ratio": 0.0}
        try:
            # Run face detection
            rois = self.face_det.infer((frame_bgr,))
            if not rois:
                return {"detected": False, "direction": "none", "stub": False, "face_size_ratio": 0.0}

            roi = rois[0]  # Use first detected face

            # Get 5-point landmarks: [left_eye, right_eye, nose_tip, left_mouth, right_mouth]
            landmarks_list = self.lm_det.infer((frame_bgr, [roi]))
            if not landmarks_list:
                return {"detected": True, "direction": "front", "stub": False, "face_size_ratio": 0.0}

            lm = landmarks_list[0]  # shape depends on LandmarksDetector implementation

            # Extract key points (normalized or pixel coords)
            # landmarks-regression-retail-0009 outputs 5 pairs: (x0,y0),(x1,y1),...
            # as a flat array or array of pairs
            if hasattr(lm, '__len__') and len(lm) >= 3:
                if hasattr(lm[0], '__len__'):
                    # Array of (x, y) pairs
                    left_eye_x = float(lm[0][0])
                    right_eye_x = float(lm[1][0])
                    nose_x = float(lm[2][0])
                else:
                    # Flat array: x0, y0, x1, y1, x2, y2...
                    left_eye_x = float(lm[0])
                    right_eye_x = float(lm[2])
                    nose_x = float(lm[4])
            else:
                return {"detected": True, "direction": "front", "stub": False, "face_size_ratio": 0.0}

            eye_width = abs(right_eye_x - left_eye_x)
            if eye_width < 1e-6:
                return {"detected": True, "direction": "front", "stub": False, "face_size_ratio": 0.0}

            eye_center_x = (left_eye_x + right_eye_x) / 2.0
            # Positive yaw = nose to the right of eye center (person turned RIGHT from camera view)
            # Negative yaw = nose to the left of eye center (person turned LEFT from camera view)
            yaw = (nose_x - eye_center_x) / eye_width

            # Thresholds: ±0.12 corresponds to roughly 15-20° head turn
            if yaw < -0.12:
                direction = "left"
            elif yaw > 0.12:
                direction = "right"
            else:
                direction = "front"

            # face_size_ratio: bounding-box width as fraction of cropped frame width
            face_size_ratio = float(roi.size[0]) / float(frame_bgr.shape[1])

            return {
                "detected": True,
                "direction": direction,
                "yaw": round(yaw, 3),
                "face_size_ratio": round(face_size_ratio, 3),
                "stub": False,
            }

        except Exception as exc:
            print(f"[FaceRecognition] get_face_direction error: {exc}")
            return {"detected": False, "direction": "none", "stub": True, "face_size_ratio": 0.0}

    @synchronized
    def detect(self, frame_bgr: np.ndarray) -> list[FaceBox]:
        """Every face the detector finds, as pixel boxes (the detector clips them to the frame)."""
        rois = self.face_det.infer((frame_bgr,))
        return [FaceBox(float(r.position[0]), float(r.position[1]), float(r.size[0]), float(r.size[1]),
                        float(r.confidence)) for r in rois]

    @synchronized
    def match_box(self, frame_bgr: np.ndarray, box: FaceBox) -> tuple[str | None, float | None]:
        """Match one face against the gallery: (label within FACE_MATCH_THRESHOLD or None, distance or None).

        An empty gallery gives (None, None).
        """
        gallery = self.face_id.faces_database
        if not gallery or len(gallery) == 0:
            return None, None
        matched = self._match_identities(frame_bgr, box, gallery)
        return (None, None) if matched is None else matched

    def _match_identities(self, frame_bgr: np.ndarray, box: FaceBox,
                          identities) -> tuple[str | None, float] | None:
        """Landmarks, descriptor and match for one face against identities (a gallery or a view of it).

        Returns (label within FACE_MATCH_THRESHOLD or None, distance), or None when no descriptor was computed.
        """
        descriptor = self._descriptor(frame_bgr, box)
        if descriptor is None:
            return None
        return self._match_descriptor(descriptor, identities)

    def _descriptor(self, frame_bgr: np.ndarray, box: FaceBox) -> np.ndarray | None:
        """The 256-number descriptor of the face in box, computed on the full frame (landmarks, then the
        identifier), or None when none was computed. It does not take the service lock itself; its two callers
        run under it: _match_identities (from match_box, @synchronized, face login) and match_enrollment_face
        (@synchronized, registration)."""
        roi = FaceDetector.Result([0, 0, box.confidence, box.x, box.y, box.w, box.h])
        landmarks = self.lm_det.infer((frame_bgr, [roi]))
        self.face_id.clear()
        self.face_id.start_async(frame_bgr, [roi], landmarks)
        descriptors = self.face_id.get_descriptors()
        return descriptors[0] if descriptors else None

    def _match_descriptor(self, descriptor: np.ndarray, identities) -> tuple[str | None, float]:
        """Login's rule: each label's closest descriptor, then the closest label (MIN_DIST); a distance above
        FACE_MATCH_THRESHOLD is no label."""
        index, distance = identities.match_faces([descriptor], self.face_id.match_algo)[0]
        distance = float(distance)
        if self.face_id.match_threshold < distance:
            return None, distance
        return identities[index].label, distance

    def judge_frame(self, frame_bgr: np.ndarray | None) -> FrameVerdict:
        """One verdict for one frame: the biggest face is scored for liveness and matched; no other face is.
        None (an upload that was not an image) is a frame without a face."""
        if frame_bgr is None or not FaceRecognitionService._available:
            return FrameVerdict("no_face")
        try:
            boxes = self.detect(frame_bgr)
            frame_h, frame_w = frame_bgr.shape[:2]
            code, main = main_face(boxes, frame_w, frame_h, min_area_ratio=FACE_MIN_AREA_RATIO,
                                   second_ratio=FACE_SECOND_FACE_RATIO)
            if main is None:
                return FrameVerdict(code, face_count=len(boxes))
            liveness = LivenessService.get_instance().score(frame_bgr, main)
            if code != "ok":
                return FrameVerdict(code, liveness=liveness, face_count=len(boxes))
            label, distance = self.match_box(frame_bgr, main)
            return FrameVerdict("match" if label is not None else "unknown", label, distance,
                                liveness, len(boxes))
        except Exception as exc:
            log.error("face login frame failed: %s", type(exc).__name__)
            return FrameVerdict("no_face")

    def login(self, frames: list[np.ndarray | None]) -> LoginDecision:
        """Judge each frame, then decide the login from all of them. Logs one line (no names)."""
        verdicts = [self.judge_frame(f) for f in frames]
        decision = decide(verdicts, min_matches=FACE_LOGIN_MIN_MATCHES, liveness_mode=FACE_LIVENESS_MODE,
                          liveness_threshold=FACE_LIVENESS_THRESHOLD)
        scores = [None if v.liveness is None else round(v.liveness, 2) for v in verdicts]
        log.info("face login: reason=%s frames=%d matches=%d liveness=%s mode=%s", decision.reason,
                 len(verdicts), sum(v.code == "match" for v in verdicts), scores, FACE_LIVENESS_MODE)
        return decision

    @synchronized
    def match_enrollment_face(self, frame_bgr: np.ndarray, own_label: str | None = None) -> dict:
        """Find the face an enrollment photo will store, its descriptor, and which other gallery label login
        would take it for.

        It runs on the full photo, as face login (detect, match_box) does: the same detector, landmarks and
        descriptor, and login's own choice of face (main_face: the biggest face, refused when a second one is
        nearly as big or the biggest is too small). The descriptor is computed for that face's box on the full
        photo (_descriptor, login's path), always, also when the gallery has no other label: it is what
        registration stores (a face_vector row). The match is login's rule: FacesDatabase.match_faces (each
        label's closest vector, then the closest label, MIN_DIST) and FaceIdentifier.postprocess's threshold
        (a distance above FACE_MATCH_THRESHOLD is Unknown).

        own_label's vectors are left out: the enrollment replaces them. A face within the threshold of
        another label is confused with it by login whichever of the two is closer on a given frame
        (3 Oct 2026: one face in the gallery as both "a" and "ab" was matched to either), so being closer
        to your own old vectors must not let it through.

        Returns {"box": [x, y, w, h] | None, "label": str | None, "distance": float | None,
        "error": str | None, "reason": "ok" | "no_face" | "two_faces" | "come_closer",
        "descriptor": np.ndarray | None}.
        box is set only when reason is "ok". label is the closest other label within the threshold, else
        None; distance is to the closest other label (None when the gallery has no other label). descriptor is
        set only for a photo that may be stored (reason "ok", no other label, no error); it is never logged
        and never sent in an HTTP answer.
        """
        refused = {"box": None, "label": None, "distance": None, "descriptor": None}
        if not self._available:
            return {**refused, "reason": "no_face", "error": "Face recognition model unavailable"}
        try:
            boxes = self.detect(frame_bgr)
            frame_h, frame_w = frame_bgr.shape[:2]
            reason, main = main_face(boxes, frame_w, frame_h, min_area_ratio=FACE_MIN_AREA_RATIO,
                                     second_ratio=FACE_SECOND_FACE_RATIO)
            if reason != "ok" or main is None:
                return {**refused, "error": None, "reason": reason}
            box = [int(main.x), int(main.y), int(main.w), int(main.h)]
            descriptor = self._descriptor(frame_bgr, main)
            if descriptor is None:
                # Fail closed: a face that could not be checked must not be enrolled.
                return {**refused, "reason": "ok", "error": "No face descriptor computed"}
            gallery = self.face_id.faces_database
            own = (own_label or "").lower()
            others = [gallery[i] for i in range(len(gallery))] if gallery else []
            others = [identity for identity in others if identity.label.lower() != own]
            # No other label: nobody to confuse it with (and match_faces cannot take an argmin over nothing).
            if not others:
                return {"box": box, "label": None, "distance": None, "error": None, "reason": "ok",
                        "descriptor": descriptor}
            view = copy.copy(gallery)
            view.database = others
            label, distance = self._match_descriptor(descriptor, view)
            return {"box": box, "label": label, "distance": distance, "error": None, "reason": "ok",
                    "descriptor": None if label else descriptor}
        except Exception as exc:
            return {**refused, "error": str(exc), "reason": "no_face"}

    @synchronized
    def identify_faces(self, frame_bgr: np.ndarray) -> dict:
        """Return one label and ROI for each face; never choose a global best match."""
        if not self._available:
            return {"faces": [], "error": "Face recognition model unavailable", "saturated": False}
        try:
            rois = self.face_det.infer((frame_bgr,))
            if len(rois) >= 4:
                return {"faces": [], "error": None, "saturated": True}
            if not rois:
                return {"faces": [], "error": None, "saturated": False}
            landmarks = self.lm_det.infer((frame_bgr, rois))
            self.face_id.clear()
            self.face_id.start_async(frame_bgr, rois, landmarks)
            results, _ = self.face_id.postprocess()
            faces = []
            for roi, result in zip(rois, results):
                faces.append({"box": [int(roi.position[0]), int(roi.position[1]),
                                      int(roi.size[0]), int(roi.size[1])],
                              "label": self.face_id.get_identity_label(result.id),
                              "distance": float(result.distance)})
            return {"faces": faces, "error": None, "saturated": False}
        except Exception as exc:
            return {"faces": [], "error": str(exc), "saturated": False}
