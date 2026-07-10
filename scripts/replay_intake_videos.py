"""Replay real videos through MediaPipe and the live intake geometry/control plane.

Usage:
  python scripts/replay_intake_videos.py --input C:/.../EatPill
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.request
from collections import Counter
from pathlib import Path

import cv2
import mediapipe as mp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.intake_detection import PillIngestionDetector
from app.services.intake_temporal import Observation, TemporalIntakePipeline

MODEL_URLS = {
    "hand_landmarker.task": "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task",
    "face_landmarker.task": "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task",
}


def model_path(name: str) -> Path:
    target = ROOT / ".cache" / "mediapipe" / name
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        print(f"Downloading {name}...", flush=True)
        urllib.request.urlretrieve(MODEL_URLS[name], target)
    return target


def as_dicts(landmarks):
    return [{"x": p.x, "y": p.y, "z": p.z} for p in landmarks]


def make_landmarkers():
    BaseOptions = mp.tasks.BaseOptions
    vision = mp.tasks.vision
    hands = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(model_path("hand_landmarker.task"))),
        running_mode=vision.RunningMode.VIDEO, num_hands=2,
        min_hand_detection_confidence=.35, min_tracking_confidence=.35,
    ))
    face = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(model_path("face_landmarker.task"))),
        running_mode=vision.RunningMode.VIDEO, num_faces=1,
        min_face_detection_confidence=.35, min_tracking_confidence=.35,
    ))
    return hands, face


def replay(video: Path, hands, face, sample_fps: float):
    cap = cv2.VideoCapture(str(video))
    source_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    stride = max(1, round(source_fps / sample_fps))
    detector, pipeline = PillIngestionDetector(), TemporalIntakePipeline()
    rows, events, last_stage = [], [], None
    source_frame = -1
    while True:
        ok, bgr = cap.read()
        if not ok:
            break
        source_frame += 1
        if source_frame % stride:
            continue
        timestamp = source_frame / source_fps
        timestamp_ms = max(1, round(timestamp * 1000))
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        face_result = face.detect_for_video(image, timestamp_ms)
        hand_result = hands.detect_for_video(image, timestamp_ms)
        face_lm = as_dicts(face_result.face_landmarks[0]) if face_result.face_landmarks else []
        hand_lms = [as_dicts(item) for item in hand_result.hand_landmarks]
        observations, features_list, mouth = [], [], None
        if face_lm:
            mouth = detector.compute_mouth_geometry(face_lm, width, height)
            for landmarks in hand_lms:
                f = detector.compute_hand_features(landmarks, mouth, width, height)
                features_list.append(f)
                palm = f["palm_center"]
                observations.append(Observation(
                    center=(palm[0] / width, palm[1] / height),
                    distance=float(f["fingertip_to_mouth_norm"]),
                    mouth_open=bool(f["mouth_open"]), pinch=bool(f["holding_object"]),
                    flat_palm=bool(f["flat_palm"]), occlusion=float(f["mouth_occlusion_score"]),
                ))
        result = pipeline.process(timestamp, bool(face_lm), observations)
        best = min(features_list, key=lambda f: f["fingertip_to_mouth_norm"], default={})
        transition = last_stage is not None and result["stage"] != last_stage
        row = {
            "folder": video.parent.name, "video": video.name, "frame": source_frame,
            "timestamp": round(timestamp, 3), "frame_width": width, "frame_height": height,
            "face_detected": bool(face_lm), "hand_detected": bool(hand_lms), "hand_count": len(hand_lms),
            "mouth_center_x": (mouth or {}).get("center", (None, None))[0],
            "mouth_center_y": (mouth or {}).get("center", (None, None))[1],
            "mouth_scale": (mouth or {}).get("width"), "mouth_open_ratio": (mouth or {}).get("mouth_open_ratio"),
            "palm_x": best.get("palm_center", (None, None))[0], "palm_y": best.get("palm_center", (None, None))[1],
            "fingertip_x": best.get("index_tip", (None, None))[0], "fingertip_y": best.get("index_tip", (None, None))[1],
            "hand_to_mouth_distance": best.get("fingertip_to_mouth_norm"),
            "contact_zone": bool(best and best.get("fingertip_to_mouth_norm", 99) <= pipeline.ENTRY_DISTANCE),
            "exit_zone": bool(best and best.get("fingertip_to_mouth_norm", 0) >= pipeline.EXIT_DISTANCE),
            "state": result["stage"], "state_transition": transition,
            "state_transition_reason": result.get("transition_reason"),
            "waiting_reason": result.get("waiting_reason") or result.get("missing_observation"),
            "approach_velocity": result.get("approach_velocity"), "withdrawal_velocity": result.get("withdrawal_velocity"),
            "hand_lost": result.get("hand_lost", not bool(hand_lms)), "reacquired": result.get("reacquired", False),
            "occlusion_duration": result.get("occlusion_duration", 0.0),
            "mouth_open_before": result.get("mouth_open_before"), "mouth_open_during": result.get("mouth_open_during"),
            "mouth_open_after": result.get("mouth_open_after"), "confidence": result.get("confidence", 0),
            "decision": result.get("decision", "none"), "candidate_id": result.get("candidate_id"),
            "result_reason": result.get("completion_reason") or result.get("reset_reason"),
        }
        rows.append(row)
        if result.get("decision") != "none":
            events.append({k: row[k] for k in ("folder", "video", "frame", "timestamp", "state", "decision", "confidence", "candidate_id", "result_reason")})
        last_stage = result["stage"]
    cap.release()
    decisions = [e["decision"] for e in events]
    if "confirmed" in decisions:
        final = "confirmed"
    elif "uncertain" in decisions:
        final = "uncertain"
    elif any(r["state"] in ("AT_MOUTH", "OCCLUDED", "WITHDRAWING") for r in rows[-max(1, int(sample_fps)):]):
        final = "stuck_at_contact"
    elif any(r["contact_zone"] for r in rows):
        final = "timeout"
    else:
        final = "rejected"
    stages = Counter(r["state"] for r in rows)
    summary = {"folder": video.parent.name, "video": video.name, "expected": "positive" if video.parent.name.lower().endswith("true") else "negative",
               "result": final, "frames": len(rows), "duration": round(rows[-1]["timestamp"], 2) if rows else 0,
               "dimensions": f"{width}x{height}", "face_rate": round(sum(r["face_detected"] for r in rows)/max(len(rows), 1), 3),
               "hand_rate": round(sum(r["hand_detected"] for r in rows)/max(len(rows), 1), 3),
               "min_distance": round(min((r["hand_to_mouth_distance"] for r in rows if r["hand_to_mouth_distance"] is not None), default=99), 3),
               "stages": dict(stages), "reason": (events[-1].get("result_reason") if events else rows[-1].get("waiting_reason") if rows else "no_frames")}
    return rows, events, summary


def write_report(out: Path, summaries, phase: str):
    counts = Counter(s["result"] for s in summaries)
    lines = [f"# EatPill Replay Debug Report — {phase}", "", f"Videos: {len(summaries)}. Results: {dict(counts)}", "",
             "| Folder | Video | Expected | Result | Face | Hand | Min distance | Dimensions | Reason |", "|---|---|---:|---|---:|---:|---:|---|---|"]
    for s in summaries:
        lines.append(f"| {s['folder']} | {s['video']} | {s['expected']} | {s['result']} | {s['face_rate']:.1%} | {s['hand_rate']:.1%} | {s['min_distance']} | {s['dimensions']} | {s['reason'] or ''} |")
    (out / f"summary_{phase}.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "intake_replay_2026-07-10")
    parser.add_argument("--sample-fps", type=float, default=10.0)
    parser.add_argument("--phase", default="current")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    videos = sorted(args.input.rglob("*.mp4"))[:args.limit]
    all_rows, all_events, summaries = [], [], []
    for index, video in enumerate(videos, 1):
        print(f"[{index}/{len(videos)}] {video.parent.name}/{video.name}", flush=True)
        # VIDEO mode timestamps restart at zero per file, so each replay owns
        # fresh landmarker state just like a fresh browser camera run.
        hands, face = make_landmarkers()
        rows, events, summary = replay(video, hands, face, args.sample_fps)
        hands.close(); face.close()
        all_rows.extend(rows); all_events.extend(events); summaries.append(summary)
    fields = list(all_rows[0]) if all_rows else []
    with (args.output / f"frames_{args.phase}.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fields); writer.writeheader(); writer.writerows(all_rows)
    (args.output / f"events_{args.phase}.json").write_text(json.dumps(all_events, indent=2), encoding="utf-8")
    (args.output / f"videos_{args.phase}.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    write_report(args.output, summaries, args.phase)
    print(json.dumps(Counter(s["result"] for s in summaries), indent=2))


if __name__ == "__main__":
    main()
