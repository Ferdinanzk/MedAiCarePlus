import { useEffect, useRef, useState, useCallback } from 'react';

const CDN_BASE = 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.21';

interface MediaPipeLandmarker {
  detectForVideo: (element: HTMLCanvasElement | HTMLVideoElement, timestamp: number) => unknown;
}

interface VisionModule {
  FilesetResolver: {
    forVisionTasks: (wasmPath: string) => Promise<unknown>;
  };
  HandLandmarker: {
    createFromOptions: (resolver: unknown, options: Record<string, unknown>) => Promise<MediaPipeLandmarker>;
  };
  FaceLandmarker: {
    createFromOptions: (resolver: unknown, options: Record<string, unknown>) => Promise<MediaPipeLandmarker>;
  };
}

interface VisionWindow {
  HandLandmarker?: VisionModule['HandLandmarker'];
  FaceLandmarker?: VisionModule['FaceLandmarker'];
  FilesetResolver?: VisionModule['FilesetResolver'];
}

export function useMediaPipe() {
  const [isLoaded, setIsLoaded] = useState(false);
  const [error, setError] = useState('');
  const handLandmarkerRef = useRef<MediaPipeLandmarker | null>(null);
  const faceLandmarkerRef = useRef<MediaPipeLandmarker | null>(null);
  const loadingRef = useRef(false);

  const initialize = useCallback(async () => {
    if (loadingRef.current) return;
    loadingRef.current = true;
    try {
      let vision: VisionModule;

      // Prefer already-loaded globals (e.g. from tests or another script)
      const w = window as unknown as VisionWindow;
      if (w.HandLandmarker && w.FaceLandmarker && w.FilesetResolver) {
        vision = w as VisionModule;
      } else {
        vision = await import(/* @vite-ignore */ `${CDN_BASE}/vision_bundle.mjs`) as VisionModule;
      }

      const filesetResolver = await vision.FilesetResolver.forVisionTasks(`${CDN_BASE}/wasm`);

      let handLandmarker: MediaPipeLandmarker | null = null;
      let faceLandmarker: MediaPipeLandmarker | null = null;

      try {
        handLandmarker = await vision.HandLandmarker.createFromOptions(filesetResolver, {
          baseOptions: {
            modelAssetPath:
              'https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task',
            delegate: 'GPU',
          },
          runningMode: 'VIDEO',
          numHands: 2,
        });

        faceLandmarker = await vision.FaceLandmarker.createFromOptions(filesetResolver, {
          baseOptions: {
            modelAssetPath:
              'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task',
            delegate: 'GPU',
          },
          runningMode: 'VIDEO',
          numFaces: 1,
        });
      } catch {
        // Fallback to CPU if GPU delegate fails
        handLandmarker = await vision.HandLandmarker.createFromOptions(filesetResolver, {
          baseOptions: {
            modelAssetPath:
              'https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task',
            delegate: 'CPU',
          },
          runningMode: 'VIDEO',
          numHands: 2,
        });

        faceLandmarker = await vision.FaceLandmarker.createFromOptions(filesetResolver, {
          baseOptions: {
            modelAssetPath:
              'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task',
            delegate: 'CPU',
          },
          runningMode: 'VIDEO',
          numFaces: 1,
        });
      }

      handLandmarkerRef.current = handLandmarker;
      faceLandmarkerRef.current = faceLandmarker;
      setIsLoaded(true);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load MediaPipe');
    } finally {
      loadingRef.current = false;
    }
  }, []);

  useEffect(() => {
    initialize();
  }, [initialize]);

  return {
    handLandmarker: handLandmarkerRef,
    faceLandmarker: faceLandmarkerRef,
    isLoaded,
    error,
  };
}
