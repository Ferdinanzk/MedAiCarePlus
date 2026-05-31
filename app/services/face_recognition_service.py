import sys
import numpy as np
from app.config import (
    FACE_REC_BASE, FACE_DET_MODEL, FACE_REID_MODEL,
    LANDMARKS_MODEL, FACE_GALLERY_DIR,
    FACE_DET_CONFIDENCE, FACE_MATCH_THRESHOLD, DEVICE
)

# Lazy imports — only resolved when models are available
_models_loaded = False
FaceDetector = FaceIdentifier = LandmarksDetector = FacesDatabase = Core = None


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

            faces_db = FacesDatabase(str(FACE_GALLERY_DIR), self.face_id, self.lm_det)
            self.face_id.set_faces_database(faces_db)
            FaceRecognitionService._available = True
            print(f"[FaceRecognition] Loaded — {len(faces_db)} identity(ies) in gallery.")
        except Exception as exc:
            FaceRecognitionService._available = False
            print(f"[FaceRecognition] Not available: {exc}")

    @classmethod
    def get_instance(cls) -> "FaceRecognitionService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def reload_gallery(self):
        """Reload faces database from disk after new enrollments."""
        if not FaceRecognitionService._available:
            return
        try:
            faces_db = FacesDatabase(str(FACE_GALLERY_DIR), self.face_id, self.lm_det)
            self.face_id.set_faces_database(faces_db)
            print(f"[FaceRecognition] Gallery reloaded — {len(faces_db)} identity(ies).")
        except Exception as exc:
            print(f"[FaceRecognition] Gallery reload failed: {exc}")

    def get_face_direction(self, frame_bgr: np.ndarray) -> dict:
        """
        Detect face and determine head orientation (front/left/right) from landmarks.
        Used by the onboarding face enrollment wizard.
        Returns: {detected: bool, direction: str, yaw: float, stub: bool}
        """
        if not FaceRecognitionService._available:
            # Stub mode — return front so frontend can fall back to manual capture
            return {"detected": False, "direction": "none", "stub": True}
        try:
            # Run face detection
            rois = self.face_det.infer((frame_bgr,))
            if not rois:
                return {"detected": False, "direction": "none", "stub": False}

            roi = rois[0]  # Use first detected face

            # Get 5-point landmarks: [left_eye, right_eye, nose_tip, left_mouth, right_mouth]
            landmarks_list = self.lm_det.infer((frame_bgr, [roi]))
            if not landmarks_list:
                return {"detected": True, "direction": "front", "stub": False}

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
                return {"detected": True, "direction": "front", "stub": False}

            eye_width = abs(right_eye_x - left_eye_x)
            if eye_width < 1e-6:
                return {"detected": True, "direction": "front", "stub": False}

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

            return {"detected": True, "direction": direction, "yaw": round(yaw, 3), "stub": False}

        except Exception as exc:
            print(f"[FaceRecognition] get_face_direction error: {exc}")
            return {"detected": False, "direction": "none", "stub": True}

    def identify_frame(self, frame_bgr: np.ndarray) -> dict:
        """
        Returns:
            {identified, name, distance, face_count, error}
        """
        if not FaceRecognitionService._available:
            return {"identified": False, "name": None, "distance": None,
                    "face_count": 0, "error": "Model not loaded"}
        try:
            face_rois = self.face_det.infer((frame_bgr,))
            if not face_rois:
                return {"identified": False, "name": None, "distance": None,
                        "face_count": 0, "error": None}

            landmarks = self.lm_det.infer((frame_bgr, face_rois))
            self.face_id.clear()
            self.face_id.start_async(frame_bgr, face_rois, landmarks)
            results, _ = self.face_id.postprocess()

            best = min(results, key=lambda r: r.distance, default=None)
            unknown_label = FaceIdentifier.UNKNOWN_ID_LABEL

            if best is None or self.face_id.get_identity_label(best.id) == unknown_label:
                return {"identified": False, "name": None, "distance": None,
                        "face_count": len(face_rois), "error": None}

            name = self.face_id.get_identity_label(best.id)
            return {"identified": True, "name": name, "distance": float(best.distance),
                    "face_count": len(face_rois), "error": None}
        except Exception as exc:
            return {"identified": False, "name": None, "distance": None,
                    "face_count": 0, "error": str(exc)}
