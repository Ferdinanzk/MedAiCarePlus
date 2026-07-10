import json
import unittest
from pathlib import Path
from unittest.mock import patch

import torch
from PIL import Image
from transformers import AutoImageProcessor

from app.services.emotion_labels import (
    ALERT_WORTHY_EMOTIONS,
    APP_EMOTION_LABELS,
    is_alert_worthy_emotion,
    normalize_emotion_label,
)
from app.services.emotion_service import EmotionService, normalize_prediction_logits

MODEL_DIR = Path("models/emotion_hf/Facial-Emotion-Detection-SigLIP2")


class SiglipEmotionTests(unittest.TestCase):
    def test_raw_label_normalization(self):
        expected = {
            "Happy": "happy", "sad": "sad", "Angry": "angry",
            "Neutral": "neutral", "Surprise": "surprised",
            "Surprised": "surprised", "Ahegao": "surprised", "Disgust": "disgust",
        }
        for raw, normalized in expected.items():
            self.assertEqual(normalize_emotion_label(raw), normalized)
        self.assertIsNone(normalize_emotion_label("unsupported"))

    def test_raw_checkpoint_class_collapses_to_surprised(self):
        id2label = {0: "Ahegao", 1: "Angry", 2: "Happy", 3: "Neutral", 4: "Sad", 5: "Surprise"}
        label, confidence, probabilities = normalize_prediction_logits(
            torch.tensor([[9.0, 0.0, 0.0, 0.0, 0.0, 8.0]]), id2label,
        )
        self.assertEqual(label, "surprised")
        self.assertGreater(confidence, .99)
        self.assertEqual(set(probabilities), APP_EMOTION_LABELS)
        self.assertEqual(probabilities["disgust"], 0.0)
        serialized = json.dumps({"emotion_type": label, "probabilities": probabilities}).lower()
        self.assertNotIn("ahe" + "gao", serialized)

    def test_local_processor_uses_siglip_224_preprocessing(self):
        processor = AutoImageProcessor.from_pretrained(MODEL_DIR, local_files_only=True, use_fast=False)
        pixels = processor(images=Image.new("RGB", (80, 120), "white"), return_tensors="pt")["pixel_values"]
        self.assertEqual(tuple(pixels.shape), (1, 3, 224, 224))
        self.assertAlmostEqual(float(pixels.max()), 1.0, places=4)

    def test_model_assets_and_supported_labels(self):
        config = json.loads((MODEL_DIR / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["architectures"], ["SiglipForImageClassification"])
        normalized = {normalize_emotion_label(value) for value in config["id2label"].values()}
        self.assertEqual(normalized, {"happy", "sad", "angry", "neutral", "surprised"})
        self.assertNotIn("disgust", normalized)
        self.assertTrue((MODEL_DIR / "model.safetensors").is_file())

    def test_model_load_failure_is_safe_and_has_no_cnn_fallback(self):
        previous = EmotionService._available
        try:
            with patch("app.services.emotion_service.AutoImageProcessor.from_pretrained", side_effect=OSError("missing")):
                service = EmotionService(model_path=Path("missing-model"))
            self.assertFalse(EmotionService._available)
            result = service.predict_frame(torch.zeros((64, 64, 3), dtype=torch.uint8).numpy())
            self.assertFalse(result["detected"])
            self.assertIn("SigLIP2", result["error"])
        finally:
            EmotionService._available = previous
        source = Path("app/services/emotion_service.py").read_text(encoding="utf-8")
        self.assertNotIn("model4.2.2", source)
        self.assertNotIn("class _CNN", source)

    def test_alert_policy_does_not_treat_surprised_as_bad(self):
        self.assertEqual(ALERT_WORTHY_EMOTIONS, {"sad", "angry", "disgust"})
        self.assertTrue(all(is_alert_worthy_emotion(label) for label in ALERT_WORTHY_EMOTIONS))
        self.assertFalse(is_alert_worthy_emotion("surprised"))
        self.assertFalse(is_alert_worthy_emotion("happy"))

    def test_all_public_labels_are_canonical(self):
        self.assertEqual(APP_EMOTION_LABELS, {"happy", "sad", "angry", "neutral", "surprised", "disgust"})

    def test_api_and_storage_paths_apply_canonical_normalization(self):
        api_source = Path("app/routers/api_emotion.py").read_text(encoding="utf-8")
        legacy_source = Path("app/routers/emotion.py").read_text(encoding="utf-8")
        self.assertIn("emotion_type = normalize_emotion_label(payload.emotion_type)", api_source)
        self.assertIn("emotion_type = normalize_emotion_label(body.get", legacy_source)
        self.assertNotIn("emotion_type.capitalize", api_source + legacy_source)
        self.assertEqual(normalize_emotion_label("Ahegao"), "surprised")


if __name__ == "__main__":
    unittest.main()
