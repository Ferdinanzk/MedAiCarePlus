/**
 * photo_capture.js
 *
 * Browser port of photo_capture.py:
 *   - Uses MediaPipe Pose Landmarker (WASM, loaded from CDN)
 *   - Guides user through 3 shots: FRONT, LEFT side, RIGHT side
 *   - Detects orientation from nose + ear landmark visibility
 *   - Draws skeleton overlay + crop guide on a canvas
 *   - SPACE (or Capture button) to accept a shot when orientation matches
 *   - On completion, 3 JPEG blobs are ready for upload
 */

import {
  PoseLandmarker,
  FilesetResolver,
  DrawingUtils,
} from "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/vision_bundle.mjs";

// ── Constants ──────────────────────────────────────────────────────────────────
const SHOT_LABELS    = ["FRONT", "LEFT side", "RIGHT side"];
const SHOT_TARGETS   = ["front", "left", "right"];
const CROP_RATIO_W   = 2, CROP_RATIO_H = 3;
const CROP_MARGIN    = 2.0;

// MediaPipe pose landmark indices
const NOSE      = 0;
const LEFT_EAR  = 7;
const RIGHT_EAR = 8;

// ── State ──────────────────────────────────────────────────────────────────────
let poseLandmarker = null;
let stream         = null;
let animFrame      = null;
let shotIndex      = 0;
let capturedBlobs  = [];  // 3 Blobs when done
let lastLandmarks  = null;

// ── DOM refs (set by init) ─────────────────────────────────────────────────────
let videoEl, overlayCanvas, captureBtn, statusEl, progressEl, reviewEl;

// ── Public entry point ────────────────────────────────────────────────────────
export async function initPhotoCapture({ video, canvas, captureButton, status, progress, review }) {
  videoEl     = video;
  overlayCanvas = canvas;
  captureBtn  = captureButton;
  statusEl    = status;
  progressEl  = progress;
  reviewEl    = review;

  setStatus("Loading pose model…", "text-muted");

  const vision = await FilesetResolver.forVisionTasks(
    "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm"
  );
  poseLandmarker = await PoseLandmarker.createFromOptions(vision, {
    baseOptions: {
      modelAssetPath:
        "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task",
      delegate: "GPU",
    },
    runningMode: "VIDEO",
    numPoses: 1,
  });

  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { width: 640, height: 480, facingMode: "user" },
      audio: false,
    });
    videoEl.srcObject = stream;
    await new Promise(r => videoEl.onloadedmetadata = r);
    videoEl.play();
  } catch (e) {
    setStatus("Camera error: " + e.message, "text-danger");
    return;
  }

  setStatus(`Shot 1/3 — Face ${SHOT_LABELS[0]}`, "text-primary fw-bold");
  updateProgress();
  loop();
}

// ── Detection loop ─────────────────────────────────────────────────────────────
function loop() {
  animFrame = requestAnimationFrame(loop);
  if (videoEl.readyState < 2) return;

  const ctx = overlayCanvas.getContext("2d");
  overlayCanvas.width  = videoEl.videoWidth;
  overlayCanvas.height = videoEl.videoHeight;
  ctx.drawImage(videoEl, 0, 0);

  const result = poseLandmarker.detectForVideo(videoEl, performance.now());
  lastLandmarks = result.landmarks?.[0] || null;

  if (lastLandmarks) {
    drawSkeleton(ctx, lastLandmarks);
    const cropRect = computeCrop(lastLandmarks, overlayCanvas.width, overlayCanvas.height);
    if (cropRect) drawCropGuide(ctx, cropRect);

    const orient = detectOrientation(lastLandmarks);
    const target = SHOT_TARGETS[shotIndex];
    updateOrientationHint(orient, target);
    captureBtn.disabled = (orient !== target);
  } else {
    setStatus("No pose detected — step back or improve lighting", "text-warning");
    captureBtn.disabled = true;
  }
}

// ── Capture ────────────────────────────────────────────────────────────────────
export async function captureShot() {
  if (!lastLandmarks) return;
  const orient = detectOrientation(lastLandmarks);
  if (orient !== SHOT_TARGETS[shotIndex]) return;

  const cropRect = computeCrop(lastLandmarks, overlayCanvas.width, overlayCanvas.height);
  if (!cropRect) return;

  const [x, y, w, h] = cropRect;

  // Grab a clean frame from the video (no overlay)
  const tmpCanvas = document.createElement("canvas");
  tmpCanvas.width = w; tmpCanvas.height = h;
  const tmpCtx = tmpCanvas.getContext("2d");
  tmpCtx.drawImage(videoEl, x, y, w, h, 0, 0, w, h);

  const blob = await new Promise(r => tmpCanvas.toBlob(r, "image/jpeg", 0.92));
  capturedBlobs.push(blob);
  shotIndex++;

  if (shotIndex < 3) {
    setStatus(`Shot ${shotIndex + 1}/3 — Face ${SHOT_LABELS[shotIndex]}`, "text-primary fw-bold");
    updateProgress();
    flashFeedback();
  } else {
    // All 3 done
    cancelAnimationFrame(animFrame);
    stopCamera();
    showReview();
  }
}

// ── Orientation detection (mirrors photo_capture.py logic) ────────────────────
function detectOrientation(lms) {
  const nose  = lms[NOSE];
  const lEar  = lms[LEFT_EAR];
  const rEar  = lms[RIGHT_EAR];

  if ((nose.visibility ?? 1) < 0.3) return null;

  const lVis = (lEar.visibility ?? 1) > 0.3;
  const rVis = (rEar.visibility ?? 1) > 0.3;

  if (lVis && rVis) {
    const earCx   = (lEar.x + rEar.x) / 2;
    const earSpan = Math.abs(lEar.x - rEar.x);
    if (earSpan > 0 && Math.abs(nose.x - earCx) / earSpan < 0.2) return "front";
    return nose.x < earCx ? "right" : "left";
  }
  if (lVis) return "left";
  if (rVis) return "right";
  return null;
}

// ── Crop calculation (mirrors compute_face_crop) ───────────────────────────────
function computeCrop(lms, fw, fh) {
  const nose = lms[NOSE];
  if ((nose.visibility ?? 1) < 0.3) return null;

  const lEar = lms[LEFT_EAR], rEar = lms[RIGHT_EAR];
  const nx = nose.x * fw, ny = nose.y * fh;
  let faceRadius;

  const lVis = (lEar.visibility ?? 1) > 0.3;
  const rVis = (rEar.visibility ?? 1) > 0.3;

  if (lVis && rVis) {
    faceRadius = Math.max(Math.abs(nx - lEar.x * fw), Math.abs(nx - rEar.x * fw));
  } else if (lVis) {
    faceRadius = Math.abs(nx - lEar.x * fw);
  } else if (rVis) {
    faceRadius = Math.abs(nx - rEar.x * fw);
  } else {
    faceRadius = fw * 0.06;
  }
  faceRadius = Math.max(faceRadius, 30);

  const cropH = Math.round(faceRadius * 2 * CROP_MARGIN);
  const cropW = Math.round(cropH * CROP_RATIO_W / CROP_RATIO_H);
  let cx = Math.round(nx);
  let cy = Math.round(ny - faceRadius * 0.1);

  let x1 = cx - cropW >> 1, y1 = cy - cropH >> 1;
  let x2 = x1 + cropW,       y2 = y1 + cropH;

  if (x1 < 0) { x2 -= x1; x1 = 0; }
  if (y1 < 0) { y2 -= y1; y1 = 0; }
  if (x2 > fw) { x1 -= (x2 - fw); x2 = fw; }
  if (y2 > fh) { y1 -= (y2 - fh); y2 = fh; }
  x1 = Math.max(0, x1); y1 = Math.max(0, y1);

  return [x1, y1, x2 - x1, y2 - y1];   // [x, y, w, h]
}

// ── Drawing helpers ────────────────────────────────────────────────────────────
function drawSkeleton(ctx, lms) {
  const drawUtils = new DrawingUtils(ctx);
  drawUtils.drawConnectors(lms, PoseLandmarker.POSE_CONNECTIONS,
    { color: "#00C8FF", lineWidth: 2 });
  drawUtils.drawLandmarks(lms, { color: "#00FF00", radius: 3 });
}

function drawCropGuide(ctx, [x, y, w, h]) {
  // Dim outside
  ctx.save();
  ctx.fillStyle = "rgba(0,0,0,0.45)";
  const cw = ctx.canvas.width, ch = ctx.canvas.height;
  ctx.fillRect(0, 0, cw, y);
  ctx.fillRect(0, y + h, cw, ch - y - h);
  ctx.fillRect(0, y, x, h);
  ctx.fillRect(x + w, y, cw - x - w, h);
  ctx.restore();
  ctx.strokeStyle = "#00FF00";
  ctx.lineWidth = 2;
  ctx.strokeRect(x, y, w, h);
}

function flashFeedback() {
  overlayCanvas.style.filter = "brightness(2)";
  setTimeout(() => overlayCanvas.style.filter = "", 150);
}

// ── UI helpers ─────────────────────────────────────────────────────────────────
function setStatus(msg, cls) {
  if (!statusEl) return;
  statusEl.textContent = msg;
  statusEl.className = `small mb-2 ${cls}`;
}

function updateOrientationHint(orient, target) {
  if (!orient) {
    setStatus("No pose detected — step back or improve lighting", "text-warning");
    captureBtn.disabled = true;
    return;
  }
  if (orient === target) {
    setStatus(`✓ Good! Facing ${orient.toUpperCase()} — press Capture`, "text-success fw-bold");
    captureBtn.disabled = false;
  } else {
    setStatus(`Facing ${orient.toUpperCase()} — please turn ${target.toUpperCase()}`, "text-warning");
    captureBtn.disabled = true;
  }
}

function updateProgress() {
  if (!progressEl) return;
  progressEl.innerHTML = SHOT_LABELS.map((label, i) => {
    const done    = i < shotIndex;
    const current = i === shotIndex;
    const cls = done ? "bg-success" : current ? "bg-primary" : "bg-secondary";
    return `<span class="badge ${cls} me-1">${i + 1}. ${label}</span>`;
  }).join("");
}

function stopCamera() {
  stream?.getTracks().forEach(t => t.stop());
}

function showReview() {
  if (!reviewEl) return;
  reviewEl.classList.remove("d-none");
  const imgs = reviewEl.querySelectorAll(".review-img");
  capturedBlobs.forEach((blob, i) => {
    if (imgs[i]) imgs[i].src = URL.createObjectURL(blob);
  });
  setStatus("Review your photos, enter your name and save.", "text-primary");
}

// ── Upload (called by form submit) ─────────────────────────────────────────────
export async function uploadPhotos(faceLabel) {
  if (capturedBlobs.length !== 3) {
    alert("Please complete all 3 shots first.");
    return false;
  }
  const fd = new FormData();
  fd.append("face_label", faceLabel);
  capturedBlobs.forEach((blob, i) =>
    fd.append("photos", new File([blob], `${faceLabel}-${i}.jpg`, { type: "image/jpeg" }))
  );
  const resp = await fetch("/auth/register-photos", { method: "POST", body: fd });
  const data = await resp.json();
  if (data.error) { alert("Photo save error: " + data.error); return false; }
  return true;
}
