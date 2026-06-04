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
  onConfirmed: (confidence: number) => void,
  onUncertain?: (confidence: number) => void,
  onError?: (err: string) => void
) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [confidence, setConfidence] = useState(0);
  const [peakConfidence, setPeakConfidence] = useState(0);
  const [isDetecting, setIsDetecting] = useState(false);
  const [status, setStatus] = useState('IDLE');
  const [debug, setDebug] = useState<Record<string, unknown> | null>(null);
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
          setPeakConfidence(Math.round((result.peak_confidence || 0) * 100));
          setStatus(result.status || 'DETECTING');

          // DEBUG: the on-screen meter is the decaying frame_confidence; the
          // event_confidence / peak_confidence are the values that actually
          // drive the confirm/uncertain decision. Log both to diagnose why the
          // visible % stays low.
          if (result.debug) {
            setDebug(result.debug);
            // eslint-disable-next-line no-console
            console.debug('[intake]', result.debug);
          }

          const detectionConfidence = result.confidence || 0;
          // Prefer the explicit decision band; fall back to the legacy
          // event_detected gate if an older backend omits `decision`.
          const decision =
            result.decision ?? (result.event_detected ? 'confirmed' : 'none');

          if (decision === 'confirmed') {
            stop();
            onConfirmed(detectionConfidence);
          } else if (decision === 'uncertain') {
            stop();
            onUncertain?.(detectionConfidence);
          }
        } catch (e: unknown) {
          const msg = e instanceof Error ? e.message : 'Detection request failed';
          onError?.(msg);
        }
      }, 100);
    },
    [sessionId, onConfirmed, onUncertain, onError, stop]
  );

  useEffect(() => {
    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
      }
    };
  }, []);

  return { videoRef, confidence, peakConfidence, isDetecting, status, debug, start, stop };
}
