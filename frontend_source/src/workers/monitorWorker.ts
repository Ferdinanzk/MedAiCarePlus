import { FilesetResolver, FaceLandmarker, HandLandmarker, PoseLandmarker } from '@mediapipe/tasks-vision';

const faceIndices = [1, 10, 13, 14, 33, 61, 152, 263, 291];
const handIndices = [0, 4, 5, 6, 8, 9, 10, 12, 14, 16, 17, 18, 20];
const MAX_FRAME_WIDTH = 640;
const MAX_FRAME_HEIGHT = 480;
let face: FaceLandmarker | null = null;
let hand: HandLandmarker | null = null;
let pose: PoseLandmarker | null = null;
let mode = 'worker-cpu';
let canvas = new OffscreenCanvas(640, 480);
let context = canvas.getContext('2d', { alpha: false });

async function initialize(delegate: 'GPU' | 'CPU') {
  face?.close();
  hand?.close();
  pose?.close();
  face = null;
  hand = null;
  pose = null;
  const resolver = await FilesetResolver.forVisionTasks('/wasm');
  try {
    face = await FaceLandmarker.createFromOptions(resolver, {
      baseOptions: { modelAssetPath: '/models/face_landmarker.task', delegate },
      runningMode: 'VIDEO', numFaces: 4,
    });
    hand = await HandLandmarker.createFromOptions(resolver, {
      baseOptions: { modelAssetPath: '/models/hand_landmarker.task', delegate },
      runningMode: 'VIDEO', numHands: 8,
    });
    pose = await PoseLandmarker.createFromOptions(resolver, {
      baseOptions: { modelAssetPath: '/models/pose_landmarker_lite.task', delegate },
      runningMode: 'VIDEO', numPoses: 4,
    });
    mode = delegate === 'GPU' ? 'worker-gpu' : 'worker-cpu';
  } catch (error) {
    face?.close();
    hand?.close();
    pose?.close();
    face = hand = pose = null;
    throw error;
  }
}

const pair = (point: { x: number; y: number }) => [point.x, point.y];
const visible = (point: { x: number; y: number; visibility?: number }) =>
  [point.x, point.y, point.visibility ?? 0];

self.onmessage = async (event: MessageEvent) => {
  const message = event.data;
  if (message.type === 'init') {
    try {
      try {
        await initialize('GPU');
      } catch {
        await initialize('CPU');
      }
      self.postMessage({ type: 'ready', mode });
    } catch (error) {
      self.postMessage({ type: 'error', error: String(error) });
    }
    return;
  }
  if (message.type !== 'frame') return;
  let bitmap: ImageBitmap | null = null;
  try {
    if (!context || !face || !hand || !pose) throw new Error('Camera models are not ready');
    const width = Number(message.width);
    const height = Number(message.height);
    if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1 ||
        width > MAX_FRAME_WIDTH || height > MAX_FRAME_HEIGHT) {
      throw new Error('Camera frame dimensions are invalid');
    }
    if (canvas.width !== width || canvas.height !== height) {
      canvas = new OffscreenCanvas(width, height);
      context = canvas.getContext('2d', { alpha: false });
    }
    if (!context) throw new Error('Canvas context unavailable');
    if (message.frame_data) {
      const data = new Uint8ClampedArray(message.frame_data as ArrayBuffer);
      if (data.length !== width * height * 4) throw new Error('Camera frame data is incomplete');
      context.putImageData(new ImageData(data, width, height), 0, 0);
    } else {
      // Keep compatibility with older clients that may still send a bitmap.
      bitmap = message.bitmap as ImageBitmap;
      if (!bitmap) throw new Error('Camera frame is unavailable');
      context.drawImage(bitmap, 0, 0, width, height);
    }
    const timestamp = message.timestamp as number;
    const faces = face.detectForVideo(canvas, timestamp).faceLandmarks.map((landmarks) => {
      const xs = landmarks.map((point) => point.x);
      const ys = landmarks.map((point) => point.y);
      const x = Math.max(0, Math.min(...xs));
      const y = Math.max(0, Math.min(...ys));
      return {
        box: [x, y, Math.min(1, Math.max(...xs)) - x, Math.min(1, Math.max(...ys)) - y],
        points: faceIndices.map((index) => pair(landmarks[index])),
      };
    });
    const hands = hand.detectForVideo(canvas, timestamp).landmarks.map(
      (landmarks) => handIndices.map((index) => pair(landmarks[index]))
    );
    const poses = pose.detectForVideo(canvas, timestamp).landmarks.map((landmarks) => ({
      nose: pair(landmarks[0]),
      shoulders: [pair(landmarks[11]), pair(landmarks[12])],
      wrists: [visible(landmarks[15]), visible(landmarks[16])],
    }));
    self.postMessage({
      type: 'result', frame_seq: message.frame_seq, generation: message.generation,
      timestamp: timestamp / 1000, width: canvas.width, height: canvas.height,
      faces, hands, poses, mode,
    });
  } catch (error) {
    self.postMessage({ type: 'error', error: String(error) });
  } finally {
    bitmap?.close();
  }
};
