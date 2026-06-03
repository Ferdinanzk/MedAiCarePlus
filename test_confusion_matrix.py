import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision import transforms
from PIL import Image
import numpy as np
import os
import sys
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import json
import cv2

# ─── CNN Architecture (copied from Facial_Emotion_Detector_Final.py) ───
class CNN(nn.Module):
    def __init__(self, num_classes=4):
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(3, 16, kernel_size=3, padding=1),
            nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(16, 32, kernel_size=3, padding=1),
            nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(64, 128, kernel_size=3, padding=1),
            nn.ReLU(), nn.MaxPool2d(2)
        )
        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.Linear(2048, 512),
            nn.ReLU(),
            nn.Dropout(0.4),
            nn.Linear(512, num_classes)
        )

    def forward(self, x):
        x = self.features(x)
        x = self.classifier(x)
        return x


# ─── Config ───
val_db = "/app/valdb"
model_path = "/models/emotion/model4.2.2.pth"
IMG_SIZE = 64
default_class_names = ["Angry", "Happy", "Neutral", "Sad"]
label_dirs = ["angry", "happy", "neutral", "sad"]

# ─── Load Model ───
print(f"Loading model from: {model_path}")
if not os.path.exists(model_path):
    print(f"❌ Model not found at {model_path}")
    sys.exit(1)

ckpt = torch.load(model_path, map_location="cpu")
state = ckpt.get("model_state", ckpt)
loaded_class_names = ckpt.get("class_names", default_class_names)
img_size = ckpt.get("img_size", IMG_SIZE)
num_classes = len(loaded_class_names)

print(f"Classes: {loaded_class_names}, img_size: {img_size}, num_classes: {num_classes}")

model = CNN(num_classes=num_classes)
model.load_state_dict(state, strict=True)
model.eval()
print("✅ Model loaded")

# ─── Preprocess ───
preprocess = transforms.Compose([
    transforms.Resize((img_size, img_size)),
    transforms.ToTensor(),  # simple [0, 1] scaling; no ImageNet normalization
])
clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))


def apply_clahe_rgb(img: Image.Image) -> Image.Image:
    """Match MedAiCarePlus runtime CLAHE: LAB L-channel normalization before resize."""
    rgb = np.array(img.convert("RGB"))
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
    l_channel, a_channel, b_channel = cv2.split(lab)
    l_channel = clahe.apply(l_channel)
    rgb_norm = cv2.cvtColor(cv2.merge((l_channel, a_channel, b_channel)), cv2.COLOR_LAB2RGB)
    return Image.fromarray(rgb_norm)

# ─── Run Inference ───
y_true = []
y_pred = []
y_conf = []
per_image_results = []

for dir_name, label in zip(label_dirs, loaded_class_names):
    dir_path = os.path.join(val_db, dir_name)
    if not os.path.isdir(dir_path):
        print(f"⚠️ Missing folder: {dir_path}")
        continue

    images = sorted([f for f in os.listdir(dir_path) if f.lower().endswith(('.jpg', '.png', '.jpeg'))])
    print(f"\n=== {label.upper()} ({len(images)} images) ===")

    for img_name in images:
        img_path = os.path.join(dir_path, img_name)
        try:
            img = apply_clahe_rgb(Image.open(img_path))
            inp = preprocess(img).unsqueeze(0)

            with torch.no_grad():
                logits = model(inp)
                probs = F.softmax(logits, dim=1).squeeze().cpu().numpy()

            pred_idx = int(np.argmax(probs))
            pred_label = loaded_class_names[pred_idx]
            confidence = float(probs[pred_idx])

            y_true.append(label)
            y_pred.append(pred_label)
            y_conf.append(confidence)

            all_probs_str = ", ".join([f"{loaded_class_names[i]}:{probs[i]*100:.1f}%" for i in range(num_classes)])
            marker = "✅" if pred_label == label else "❌"
            print(f"  {marker} {img_name} → {pred_label} ({confidence*100:.1f}%) | {all_probs_str}")

            per_image_results.append({
                "file": img_name,
                "true": label,
                "pred": pred_label,
                "confidence": round(confidence * 100, 1),
                "correct": pred_label == label,
                "all_probs": {loaded_class_names[i]: round(float(probs[i]) * 100, 1) for i in range(num_classes)}
            })

        except Exception as e:
            print(f"  ⚠️ Error on {img_name}: {e}")

# ─── Confusion Matrix (manual, no sklearn needed) ───
print("\n\n" + "="*60)
print("CONFUSION MATRIX")
print("="*60)

# Build matrix
cm = np.zeros((num_classes, num_classes), dtype=int)
label_to_idx = {name: i for i, name in enumerate(loaded_class_names)}

for t, p in zip(y_true, y_pred):
    cm[label_to_idx[t]][label_to_idx[p]] += 1

print("\nRaw counts:")
header = "           " + "  ".join(f"{n:>8}" for n in loaded_class_names)
label_header = "True\\Pred"
print(f"{label_header:>10}" + "".join(f"{n:>10}" for n in loaded_class_names))
for i, cls in enumerate(loaded_class_names):
    row = "  ".join(f"{cm[i][j]:>6}" for j in range(num_classes))
    print(f"{cls:>10}  {row}")

# Per-class metrics
print("\nPer-class metrics:")
for i, cls in enumerate(loaded_class_names):
    tp = cm[i, i]
    total = cm[i, :].sum()
    fp = cm[:, i].sum() - tp
    fn = total - tp
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / total if total > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
    acc = tp / total if total > 0 else 0
    print(f"  {cls:>8}: {tp}/{total} correct = {acc*100:5.1f}%  P={precision:.2f} R={recall:.2f} F1={f1:.2f}")

# Overall
correct = int(np.trace(cm))
total = int(cm.sum())
print(f"\nOverall accuracy: {correct}/{total} = {correct/total*100:.1f}%")

# ─── Plot ───
fig, axes = plt.subplots(1, 2, figsize=(16, 6))

# Counts heatmap
ax = axes[0]
im = ax.imshow(cm, interpolation='nearest', cmap='Blues')
ax.figure.colorbar(im, ax=ax)
ax.set_xticks(range(num_classes))
ax.set_yticks(range(num_classes))
ax.set_xticklabels(loaded_class_names)
ax.set_yticklabels(loaded_class_names)
ax.set_xlabel("Predicted")
ax.set_ylabel("True")
ax.set_title(f"Confusion Matrix (n={total})\nOverall: {correct/total*100:.1f}%")

# Annotate cells
for i in range(num_classes):
    for j in range(num_classes):
        ax.text(j, i, str(cm[i, j]), ha="center", va="center",
                color="white" if cm[i, j] > cm.max() / 2 else "black", fontsize=14)

# Normalized
cm_norm = cm.astype('float') / cm.sum(axis=1, keepdims=True)
cm_norm = np.nan_to_num(cm_norm)

ax2 = axes[1]
im2 = ax2.imshow(cm_norm, interpolation='nearest', cmap='Blues', vmin=0, vmax=1)
ax2.figure.colorbar(im2, ax=ax2)
ax2.set_xticks(range(num_classes))
ax2.set_yticks(range(num_classes))
ax2.set_xticklabels(loaded_class_names)
ax2.set_yticklabels(loaded_class_names)
ax2.set_xlabel("Predicted")
ax2.set_ylabel("True")
ax2.set_title("Normalized")

for i in range(num_classes):
    for j in range(num_classes):
        ax2.text(j, i, f"{cm_norm[i, j]*100:.1f}%", ha="center", va="center",
                 color="white" if cm_norm[i, j] > 0.5 else "black", fontsize=14)

plt.tight_layout()
out_path = "/app/confusion_matrix.png"
plt.savefig(out_path, dpi=150)
print(f"\n📊 Plot saved to: {out_path}")

# ─── Save detailed results ───
results = {
    "summary": {
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total * 100, 2),
        "classes": loaded_class_names,
        "confusion_matrix": cm.tolist(),
        "per_class": {}
    },
    "per_image": per_image_results
}

for i, cls in enumerate(loaded_class_names):
    tp = int(cm[i, i])
    total_cls = int(cm[i, :].sum())
    results["summary"]["per_class"][cls] = {
        "correct": tp,
        "total": total_cls,
        "accuracy": round(tp / total_cls * 100, 2) if total_cls > 0 else 0
    }

json_path = "/app/confusion_results.json"
with open(json_path, 'w') as f:
    json.dump(results, f, indent=2)
print(f"📄 Detailed results saved to: {json_path}")
