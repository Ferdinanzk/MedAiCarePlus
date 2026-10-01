"""Download the three MediaPipe models the browser intake page and the robot bridge use.

They are not in git (binary files). Run once after cloning:  python scripts/fetch_mediapipe_models.py
Files come from Google's official storage and are checked against the pinned SHA-256 the app was tested with.
"""

import hashlib
import urllib.request
from pathlib import Path

BASE = "https://storage.googleapis.com/mediapipe-models"
MODELS = {
    "face_landmarker.task": (f"{BASE}/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
                             "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"),
    "hand_landmarker.task": (f"{BASE}/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task",
                             "fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1"),
    "pose_landmarker_lite.task": (f"{BASE}/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task",
                                  "59929e1d1ee95287735ddd833b19cf4ac46d29bc7afddbbf6753c459690d574a"),
}
TARGET = Path(__file__).resolve().parent.parent / "frontend_source" / "public" / "models"

TARGET.mkdir(parents=True, exist_ok=True)
for name, (url, expected) in MODELS.items():
    path = TARGET / name
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == expected:
        print(f"ok        {name}")
        continue
    data = urllib.request.urlopen(url, timeout=60).read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise SystemExit(f"{name}: checksum mismatch ({actual[:12]}…); not saved")
    path.write_bytes(data)
    print(f"fetched   {name} ({len(data) // 1024} KB)")
