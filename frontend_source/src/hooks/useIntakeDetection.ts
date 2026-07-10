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
  const runningRef = useRef(false);
  const generationRef = useRef(0);
  const frameSeqRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const activeSessionRef = useRef<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const canvasRef = useRef<HTMLCanvasElement>(document.createElement('canvas'));

  const stop = useCallback(() => {
    runningRef.current = false;
    generationRef.current += 1;
    abortRef.current?.abort();
    abortRef.current = null;
    const activeSession = activeSessionRef.current;
    activeSessionRef.current = null;
    if (activeSession) void aiApi.endIntakeSession(activeSession);
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = null;
    setIsDetecting(false);
    setStatus('IDLE');
  }, []);

  const start = useCallback(
    (handLandmarker: Landmarker, faceLandmarker: Landmarker) => {
      if (!videoRef.current || runningRef.current) return;

      runningRef.current = true;
      const generation = ++generationRef.current;
      const activeSession = `${sessionId}:${generation}`;
      activeSessionRef.current = activeSession;
      frameSeqRef.current = 0;
      setIsDetecting(true);
      setConfidence(0);
      setStatus('DETECTING');

      const loop = async () => {
        if (!runningRef.current || generation !== generationRef.current) return;
        const video = videoRef.current;
        if (!video || video.readyState < 2) {
          timerRef.current = setTimeout(loop, 100);
          return;
        }

        const canvas = canvasRef.current;
        canvas.width = video.videoWidth || 640;
        canvas.height = video.videoHeight || 480;
        const ctx = canvas.getContext('2d');
        if (!ctx) {
          timerRef.current = setTimeout(loop, 100);
          return;
        }
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

        try {
          const frameSeq = ++frameSeqRef.current;
          const payload = {
            session_id: activeSession,
            frame_seq: frameSeq,
            width: canvas.width,
            height: canvas.height,
            face_landmarks: faceLandmarks,
            hand_landmarks: handLandmarks,
            timestamp: timestamp / 1000,
          };

          const controller = new AbortController();
          abortRef.current = controller;
          const result = await aiApi.detectIntake(payload, controller.signal);
          if (!runningRef.current || generation !== generationRef.current || result.frame_seq !== frameSeq) return;

          if (result.error) {
            onError?.(result.error);
            return;
          }

          const conf = Math.round((result.confidence || 0) * 100);
          setConfidence(conf);
          setPeakConfidence(Math.round((result.peak_confidence || 0) * 100));
          setStatus(result.stage || result.status || 'DETECTING');

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
          if (e instanceof DOMException && e.name === 'AbortError') return;
          const msg = e instanceof Error ? e.message : 'Detection request failed';
          onError?.(msg);
        } finally {
          if (runningRef.current && generation === generationRef.current) {
            timerRef.current = setTimeout(loop, 100);
          }
        }
      };
      void loop();
    },
    [sessionId, onConfirmed, onUncertain, onError, stop]
  );

  useEffect(() => {
    return () => {
      runningRef.current = false;
      abortRef.current?.abort();
      if (timerRef.current) clearTimeout(timerRef.current);
      const activeSession = activeSessionRef.current;
      activeSessionRef.current = null;
      if (activeSession) void aiApi.endIntakeSession(activeSession);
    };
  }, []);

  return { videoRef, confidence, peakConfidence, isDetecting, status, debug, start, stop };
}
