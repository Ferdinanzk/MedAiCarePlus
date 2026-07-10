"""Evaluate the local live SigLIP2 classifier using canonical app labels only."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

import torch
from PIL import Image
from transformers import AutoImageProcessor, AutoModelForImageClassification

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services.emotion_labels import normalize_emotion_label
from app.services.emotion_service import normalize_prediction_logits


def iter_images(root: Path):
    for label_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        true_label = normalize_emotion_label(label_dir.name)
        if true_label is None:
            continue
        for path in sorted(label_dir.rglob("*")):
            if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}:
                yield true_label, path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    processor = AutoImageProcessor.from_pretrained(args.model_dir, local_files_only=True, use_fast=False)
    model = AutoModelForImageClassification.from_pretrained(args.model_dir, local_files_only=True).to(device).eval()
    id2label = {int(key): value for key, value in model.config.id2label.items()}
    rows = []
    with torch.no_grad():
        for truth, path in iter_images(args.data_root):
            with Image.open(path) as image:
                inputs = processor(images=image.convert("RGB"), return_tensors="pt")
            logits = model(**{key: value.to(device) for key, value in inputs.items()}).logits
            prediction, confidence, probabilities = normalize_prediction_logits(logits, id2label)
            rows.append({"file": str(path), "true": truth, "predicted": prediction,
                         "confidence": round(confidence, 6), "correct": truth == prediction,
                         **{f"prob_{label}": round(value, 6) for label, value in probabilities.items()}})
    if not rows:
        raise SystemExit("No supported labeled images found")
    with (args.out_dir / "predictions.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0]); writer.writeheader(); writer.writerows(rows)
    summary = {"model": str(args.model_dir), "samples": len(rows),
               "correct": sum(row["correct"] for row in rows),
               "predictions": dict(Counter(row["predicted"] for row in rows)),
               "labels": sorted({row["predicted"] for row in rows})}
    (args.out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
