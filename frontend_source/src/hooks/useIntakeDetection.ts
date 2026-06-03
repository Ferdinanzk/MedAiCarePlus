import { useRef, useState, useCallback, useEffect } from 'react';
import { aiApi } from '../lib/ai-api';

interface Landmark {
  x: number;
  y: number;
  z: number;
}

interface FaceLandmarkerResult {
  faceLandmarks?: Landmark[][];
}

interface HandLandmarkerResult {
  landmarks?: Landmark[][];
}

interface Landmarker {
  detectForVideo: (element: HTMLCanvasElement | HTMLVideoElement, timestamp: number) => unknown;
}

export function useIntakeDetection(
  sessionId: string,
  onDetect: (confidence: number) => void,
  onError?: (err: string) => void
) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [confidence, setConfidence] = useState(0);
  const [isDetecting, setIsDetecting] = useState(false);
  const [status, setStatus] = useState('IDLE');
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const canvasRef = useRef<HTMLCanvasElement>(document.createElement('canvas'));

  const stop = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    setIsDetecting(false);
    setStatus('IDLE');
  }, []);

  const start = useCallback(
    (handLandmarker: Landmarker, faceLandmarker: Landmarker) => {
      if (!videoRef.current || intervalRef.current) return;

      setIsDetecting(true);
      setConfidence(0);
      setStatus('DETECTING');

      intervalRef.current = setInterval(async () => {
        const video = videoRef.current;
        if (!video || video.readyState < 2) return;

        const canvas = canvasRef.current;
        canvas.width = video.videoWidth || 640;
        canvas.height = video.videoHeight || 480;
        const ctx = canvas.getContext('2d');
        if (!ctx) return;
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

        const timestamp = performance.now();

        let faceResults = null;
        let handResults = null;

        try {
          faceResults = faceLandmarker.detectForVideo(canvas, timestamp);
        } catch {
          /* ignore detection errors */
        }
        try {
          handResults = handLandmarker.detectForVideo(canvas, timestamp);
        } catch {
          /* ignore detection errors */
        }

        const faceLandmarks =
          (faceResults as FaceLandmarkerResult)?.faceLandmarks?.[0] || [];

        const handLandmarks =
          (handResults as HandLandmarkerResult)?.landmarks || [];

        if (!faceLandmarks.length) return;

        try {
          const payload = {
            session_id: sessionId,
            width: canvas.width,
            height: canvas.height,
            face_landmarks: faceLandmarks,
            hand_landmarks: handLandmarks,
            timestamp: timestamp / 1000,
          };

          const result = await aiApi.detectIntake(payload);

          if (result.error) {
            onError?.(result.error);
            return;
          }

          const conf = Math.round((result.confidence || 0) * 100);
          setConfidence(conf);
          setStatus(result.status || 'DETECTING');

          const detectionConfidence = result.confidence || 0;
          if (result.event_detected && detectionConfidence >= 0.38) {
            stop();
            onDetect(detectionConfidence);
          }
        } catch (e: unknown) {
          const msg = e instanceof Error ? e.message : 'Detection request failed';
          onError?.(msg);
        }
      }, 200);
    },
    [sessionId, onDetect, onError, stop]
  );

  useEffect(() => {
    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
      }
    };
  }, []);

  return { videoRef, confidence, isDetecting, status, start, stop };
}
