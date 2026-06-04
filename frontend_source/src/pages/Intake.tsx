import { useEffect, useRef, useState, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { useSearchParams, useParams } from 'react-router-dom';
import { getFaceToken } from '../lib/face-auth';
import { aiApi } from '../lib/ai-api';
import { useMediaPipe } from '../hooks/useMediaPipe';
import { useIntakeDetection } from '../hooks/useIntakeDetection';
import { useTTS } from '../hooks/useTTS';
import {
  Pill,
  CheckCircle2,
  XCircle,
  Clock,
  AlertCircle,
  Loader2,
  Camera,
  ScanFace,
  AlertTriangle,
  RefreshCw,
  Volume2,
} from 'lucide-react';

interface IntakeItem {
  id: number;
  medication_id: number;
  medication_name: string;
  dosage: string | null;
  scheduled_time: string | null;
  status: 'pending' | 'taken' | 'skipped' | 'missed';
  pills_remaining: number;
  dosage_amount: number;
  warning?: string | null;
}

interface RawIntakeItem {
  id: number;
  med_id?: number;
  medication_id?: number;
  medication_name?: string;
  name?: string;
  dosage?: string | null;
  scheduled_time?: string | null;
  status?: string;
  pills_remaining?: number;
  dosage_amount?: number;
  warning?: string | null;
}

type Phase = 'list' | 'ready' | 'detecting' | 'pre' | 'post' | 'complete';

function getAuthHeaders(): Record<string, string> {
  const token = getFaceToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export default function Intake() {
  const { t, i18n } = useTranslation();
  const { speak, stop: stopTTS, isSpeaking } = useTTS();
  const [searchParams] = useSearchParams();
  const params = useParams();
  const highlightMedId = searchParams.get('med') || params.medicationId || null;
  const [intakes, setIntakes] = useState<IntakeItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [skippingId, setSkippingId] = useState<number | null>(null);
  const [skipNote, setSkipNote] = useState('');

  // Phase-based flow
  const [phase, setPhase] = useState<Phase>('list');
  const [activeItem, setActiveItem] = useState<IntakeItem | null>(null);
  const [sessionId] = useState(() => crypto.randomUUID());

  // Emotion modal state
  const [, setPreEmotionId] = useState<number | null>(null);

  // AI emotion detection state
  const videoRef = useRef<HTMLVideoElement>(null);
  const mountedRef = useRef(true);
  const cameraStartingRef = useRef(false);
  const [cameraOn, setCameraOn] = useState(false);
  const [aiLoading, setAiLoading] = useState(false);
  const [aiError, setAiError] = useState('');
  const [aiResult, setAiResult] = useState<{
    emotion_type?: string;
    emotion_score?: number;
    probabilities?: Record<string, number>;
  } | null>(null);

  // Detection timing
  const [detectionStartTime, setDetectionStartTime] = useState<number | null>(null);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);

  // Ambiguous-detection confirmation prompt (holds the detected confidence; null = hidden)
  const [uncertainPrompt, setUncertainPrompt] = useState<number | null>(null);

  // Store detection confidence for recording after pre-emotion
  const detectionConfidenceRef = useRef<number>(0);

  // MediaPipe + Detection
  const { handLandmarker, faceLandmarker, isLoaded: mediaPipeLoaded, error: mediaPipeError } = useMediaPipe();
  const {
    videoRef: detectionVideoRef,
    confidence,
    peakConfidence,
    status: detectionStatus,
    debug: detectionDebug,
    start: startDetection,
    stop: stopDetection,
  } = useIntakeDetection(
    sessionId,
    // confirmed: high-confidence delivery -> auto-record as taken
    (conf) => {
      stopCamera();
      stopDetection();
      detectionConfidenceRef.current = conf;
      // Record intake after detection succeeds, then go to post-emotion
      if (activeItem) {
        aiApi.recordIntake({
          intk_id: activeItem.id,
          detection_confidence: conf,
          detection_method: 'auto',
        }).then(() => {
          updateStatus(activeItem.id, 'taken');
        });
      }
      setPhase('post');
      resetAiState();
      startCamera();
    },
    // uncertain: ambiguous event -> ask the patient to confirm before logging.
    // Patient-safety: never silently record a dose that may not have been taken.
    (conf) => {
      stopDetection();
      detectionConfidenceRef.current = conf;
      setUncertainPrompt(conf);
    },
    (err) => setAiError(err)
  );

  const startCamera = useCallback(async () => {
    if (cameraStartingRef.current) return;
    cameraStartingRef.current = true;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user' } });
      if (!mountedRef.current) {
        stream.getTracks().forEach((t) => t.stop());
        return;
      }

      // Wait for DOM to settle after phase transition (200ms)
      await new Promise((r) => setTimeout(r, 200));

      let videoReady = false;

      if (videoRef.current) {
        videoRef.current.srcObject = stream;
        try {
          await videoRef.current.play();
          // Wait for loadedmetadata to confirm frames are flowing
          await new Promise<void>((resolve, reject) => {
            const timeout = setTimeout(() => reject(new Error('video metadata timeout')), 2000);
            const handler = () => {
              clearTimeout(timeout);
              videoRef.current?.removeEventListener('loadedmetadata', handler);
              resolve();
            };
            if (videoRef.current!.readyState >= 1) {
              clearTimeout(timeout);
              resolve(); // already loaded
            } else {
              videoRef.current!.addEventListener('loadedmetadata', handler);
            }
          });
          videoReady = true;
        } catch (e) {
          console.error('[Intake] videoRef play error:', e);
        }
      }

      if (detectionVideoRef.current) {
        detectionVideoRef.current.srcObject = stream;
        try {
          await detectionVideoRef.current.play();
        } catch (e) {
          console.error('[Intake] detectionVideoRef play error:', e);
        }
      }

      // Only mark camera as ON after video confirmed ready
      if (videoReady) {
        setCameraOn(true);
        setAiError('');
      } else {
        setAiError('Camera stream started but video not ready. Please retry.');
        stream.getTracks().forEach((t) => t.stop());
      }
    } catch (err) {
      console.error('[Intake] getUserMedia error:', err);
      if (mountedRef.current) {
        setAiError('Camera access denied or not available');
      }
    } finally {
      cameraStartingRef.current = false;
    }
  }, [detectionVideoRef]);

  const stopCamera = useCallback(() => {
    const el = videoRef.current;
    const detEl = detectionVideoRef.current;
    if (el) {
      const stream = el.srcObject as MediaStream | null;
      el.srcObject = null;
      stream?.getTracks().forEach((t) => t.stop());
    }
    if (detEl && detEl !== el) {
      const stream = detEl.srcObject as MediaStream | null;
      detEl.srcObject = null;
      stream?.getTracks().forEach((t) => t.stop());
    }
    setCameraOn(false);
  }, [detectionVideoRef]);

  useEffect(() => {
    mountedRef.current = true;
    fetchIntakes();
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if ((phase === 'pre' || phase === 'post') && !cameraOn) {
      startCamera();
    } else if (phase === 'list' || phase === 'complete' || phase === 'ready') {
      stopCamera();
    }
  }, [phase, cameraOn, startCamera, stopCamera]);

  // Elapsed timer during detection
  useEffect(() => {
    if (phase !== 'detecting' || !detectionStartTime) {
      setElapsedSeconds(0);
      return;
    }
    const interval = setInterval(() => {
      setElapsedSeconds(Math.floor((Date.now() - detectionStartTime) / 1000));
    }, 1000);
    return () => clearInterval(interval);
  }, [phase, detectionStartTime]);

  // Auto-speak the medication warning when entering the 'ready' phase.
  useEffect(() => {
    if (phase === 'ready' && activeItem) {
      const warningText = activeItem.warning
        ? t('intake.tts.warningPrefix') + activeItem.warning
        : t('intake.tts.genericWarning');
      const lang = i18n.language === 'en' ? 'en-US' : 'zh-TW';
      speak(warningText, lang);
    }
    return () => stopTTS();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, activeItem]);

  // Auto-speak the cheerful encouragement when entering the 'complete' phase.
  useEffect(() => {
    if (phase === 'complete') {
      const lang = i18n.language === 'en' ? 'en-US' : 'zh-TW';
      speak(t('intake.tts.cheerUp'), lang);
    }
    return () => stopTTS();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase]);

  const captureVideoAndAnalyze = async () => {
    if (!videoRef.current || !cameraOn) {
      console.error('[Intake] captureVideoAndAnalyze early return. videoRef=', !!videoRef.current, 'cameraOn=', cameraOn);
      setAiError('Camera not ready. Please retry.');
      return;
    }

    setAiLoading(true);
    setAiError('');
    setAiResult(null);

    const video = videoRef.current;

    // Guard: ensure camera has real frames using loadedmetadata
    try {
      await new Promise<void>((resolve, reject) => {
        if (video.videoWidth && video.videoHeight) {
          resolve(); // already ready
          return;
        }
        const timeout = setTimeout(() => reject(new Error('Camera not ready after 3s')), 3000);
        const handler = () => {
          clearTimeout(timeout);
          video.removeEventListener('loadedmetadata', handler);
          resolve();
        };
        video.addEventListener('loadedmetadata', handler);
      });
    } catch (e) {
      console.error('[Intake] Camera readiness error:', e);
      setAiError('Camera still initializing. Please wait a moment and try again.');
      setAiLoading(false);
      return;
    }

    // Multi-frame capture
    const frames: Blob[] = [];
    const frameCount = 10;
    const intervalMs = 300;

    for (let i = 0; i < frameCount; i++) {
      await new Promise((r) => setTimeout(r, intervalMs));
      const canvas = document.createElement('canvas');
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      const ctx2d = canvas.getContext('2d');
      ctx2d?.drawImage(video, 0, 0);
      const blob = await new Promise<Blob | null>((resolve) =>
        canvas.toBlob((b) => resolve(b), 'image/jpeg', 0.9)
      );
      if (blob) frames.push(blob);
    }

    try {
      const form = new FormData();
      const ctx = phase === 'pre' ? 'pre_ingestion' : 'post_ingestion';
      form.append('context', ctx);
      frames.forEach((blob, idx) => form.append('frames', blob, `frame_${idx}.jpg`));

      const res = await fetch('/api/emotion/analyze-batch', {
        method: 'POST',
        headers: getAuthHeaders(),
        body: form,
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }

      const result = await res.json();
      if (!result.detected) {
        throw new Error(result.error || 'No face detected');
      }

      setAiResult(result);
    } catch (err) {
      console.error('[Intake] Batch analysis error:', err);
      setAiError(err instanceof Error ? err.message : 'Analysis failed');
    } finally {
      setAiLoading(false);
    }
  };

  const fetchIntakes = async () => {
    setLoading(true);
    try {
      const res = await fetch('/api/medications/today', { headers: getAuthHeaders() });
      if (res.ok) {
        const data = await res.json() as RawIntakeItem[];
        setIntakes(data.map((item: RawIntakeItem) => ({
          id: item.id,
          medication_id: item.med_id || item.medication_id || item.id,
          medication_name: item.medication_name ?? item.name ?? '',
          dosage: item.dosage ?? null,
          scheduled_time: item.scheduled_time ?? null,
          status: (item.status || 'pending') as IntakeItem['status'],
          pills_remaining: item.pills_remaining ?? 999,
          dosage_amount: item.dosage_amount ?? 1,
          warning: item.warning ?? null,
        })));
      }
    } catch {
      // network or parse error — leave state unchanged
    }
    setLoading(false);
  };

  const updateStatus = async (id: number, status: 'taken' | 'skipped') => {
    await fetch(`/api/medications/intake/${id}`, {
      method: 'PATCH',
      headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    });
    fetchIntakes();
  };

  const saveEmotion = async (emotionType: string, score: number, _note: string, _intakeId: number | null, ctx?: string): Promise<boolean> => {
    try {
      const res = await fetch('/api/emotion/log', {
        method: 'POST',
        headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ emotion_type: emotionType, emotion_score: score, note: _note || '', context: ctx }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        console.error('Failed to save emotion:', err);
        return false;
      }
      return true;
    } catch (e) {
      console.error('saveEmotion exception:', e);
      return false;
    }
  };

  const confirmAiEmotion = async () => {
    if (!aiResult?.emotion_type || !activeItem) return;
    const ctx = phase === 'pre' ? 'pre_ingestion' : 'post_ingestion';
    const success = await saveEmotion(aiResult.emotion_type, aiResult.emotion_score ?? 0.5, '', null, ctx);
    if (!success) {
      setAiError('Failed to save emotion. Please try again.');
      return;
    }
    if (phase === 'pre') {
      resetAiState();
      setPhase('detecting');
      setDetectionStartTime(Date.now());
      setElapsedSeconds(0);
      // Defer camera start so the detecting-phase video element mounts first,
      // ensuring the stream is attached to the correct DOM node.
      setTimeout(() => {
        startCamera().then(() => {
          if (handLandmarker.current && faceLandmarker.current) {
            startDetection(handLandmarker.current, faceLandmarker.current);
          }
        });
      }, 0);
    } else {
      resetAiState();
      setPhase('complete');
    }
  };

  const resetAiState = () => {
    setAiResult(null);
    setAiError('');
    setAiLoading(false);
  };

  const handleTakeClick = (item: IntakeItem) => {
    if (item.pills_remaining <= 0) return;
    setActiveItem(item);
    setPhase('ready');
    resetAiState();
    setAiError('');
  };

  const setVideoRefs = useCallback((el: HTMLVideoElement | null) => {
    (videoRef as React.MutableRefObject<HTMLVideoElement | null>).current = el;
    (detectionVideoRef as React.MutableRefObject<HTMLVideoElement | null>).current = el;
  }, [detectionVideoRef]);

  const handleReadyConfirm = () => {
    setPhase('pre');
    setDetectionStartTime(null);
    setElapsedSeconds(0);
    resetAiState();
    setAiError('');
    startCamera();
  };

  const handleReadyCancel = () => {
    stopTTS();
    setPhase('list');
    setActiveItem(null);
    resetAiState();
    setAiError('');
  };

  const replayWarning = () => {
    if (!activeItem) return;
    const warningText = activeItem.warning
      ? t('intake.tts.warningPrefix') + activeItem.warning
      : t('intake.tts.genericWarning');
    const lang = i18n.language === 'en' ? 'en-US' : 'zh-TW';
    speak(warningText, lang);
  };

  const replayCheerUp = () => {
    const lang = i18n.language === 'en' ? 'en-US' : 'zh-TW';
    speak(t('intake.tts.cheerUp'), lang);
  };

  const handleManualConfirm = () => {
    stopDetection();
    stopCamera();
    detectionConfidenceRef.current = 0;
    setPhase('post');
    setDetectionStartTime(null);
    setElapsedSeconds(0);
    resetAiState();
    if (activeItem) {
      aiApi.recordIntake({
        intk_id: activeItem.id,
        detection_confidence: 0,
        detection_method: 'manual',
      }).then(() => {
        updateStatus(activeItem.id, 'taken');
      });
    }
    startCamera();
  };

  // Patient confirmed an ambiguous ("uncertain") detection -> record as taken.
  const handleUncertainConfirm = () => {
    const conf = uncertainPrompt ?? detectionConfidenceRef.current ?? 0;
    setUncertainPrompt(null);
    stopCamera();
    setPhase('post');
    setDetectionStartTime(null);
    setElapsedSeconds(0);
    resetAiState();
    if (activeItem) {
      aiApi.recordIntake({
        intk_id: activeItem.id,
        detection_confidence: conf,
        detection_method: 'uncertain_confirmed',
      }).then(() => {
        updateStatus(activeItem.id, 'taken');
      });
    }
    startCamera();
  };

  // Patient said they have not taken it -> resume detection (camera still live).
  const handleUncertainReject = () => {
    setUncertainPrompt(null);
    if (handLandmarker.current && faceLandmarker.current) {
      startDetection(handLandmarker.current, faceLandmarker.current);
    }
  };

  const getGuidanceText = () => {
    if (confidence >= 50) return t('intake.guidanceAlmost');
    if (detectionStatus === 'HAND_AT_MOUTH') return t('intake.guidanceHand');
    return t('intake.guidanceIdle');
  };

  const handleCancelDetection = () => {
    stopTTS();
    stopDetection();
    stopCamera();
    setPhase('list');
    setActiveItem(null);
    setDetectionStartTime(null);
    setElapsedSeconds(0);
  };

  const handleSkipConfirm = async (item: IntakeItem) => {
    await updateStatus(item.id, 'skipped');
    setSkippingId(null);
    setSkipNote('');
  };

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'taken':
        return <CheckCircle2 className="w-5 h-5 text-green-500" />;
      case 'skipped':
        return <XCircle className="w-5 h-5 text-orange-500" />;
      case 'missed':
        return <AlertCircle className="w-5 h-5 text-red-500" />;
      default:
        return <Clock className="w-5 h-5 text-gray-400" />;
    }
  };

  const getStatusBadge = (status: string) => {
    switch (status) {
      case 'taken':
        return <span className="text-2xs font-medium px-2 py-1 rounded-full bg-green-50 text-green-600">{t('intake.taken')}</span>;
      case 'skipped':
        return <span className="text-2xs font-medium px-2 py-1 rounded-full bg-orange-50 text-orange-600">{t('intake.skipped')}</span>;
      case 'missed':
        return <span className="text-2xs font-medium px-2 py-1 rounded-full bg-red-50 text-red-600">{t('intake.missed')}</span>;
      default:
        return <span className="text-2xs font-medium px-2 py-1 rounded-full bg-blue-50 text-blue-600">{t('intake.pending')}</span>;
    }
  };

  // Detection overlay UI
  if (phase === 'detecting' && activeItem) {
    return (
      <div className="fixed inset-0 bg-black z-50 flex items-center justify-center">
        <div className="w-full max-w-lg h-full flex flex-col relative">
          <video
            ref={setVideoRefs}
            autoPlay
            playsInline
            muted
            className="w-full h-full object-cover"
          />
          <div className={`absolute inset-0 flex flex-col items-center justify-center text-white transition-opacity duration-300 ${cameraOn || aiError ? 'opacity-0 pointer-events-none' : 'opacity-100'}`}>
            <Camera className="w-12 h-12 mb-3 opacity-60" />
            <p className="text-sm opacity-80">Camera starting...</p>
          </div>

          <div className={`absolute inset-0 flex flex-col items-center justify-center text-white p-8 transition-opacity duration-300 ${aiError ? 'opacity-100' : 'opacity-0 pointer-events-none'}`}>
            <AlertTriangle className="w-12 h-12 mb-3 text-yellow-400" />
            <p className="text-base text-center mb-6">{aiError}</p>
            <div className="space-y-3 w-full max-w-xs">
              <button
                onClick={() => { setAiError(''); startCamera(); }}
                className="w-full py-4 bg-[#0057B8] text-white text-base rounded-xl font-medium hover:bg-[#003D82] active:scale-95 transition-all touch-target-large"
              >
                {t('intake.retryCamera')}
              </button>
              <button
                onClick={handleManualConfirm}
                className="w-full py-4 bg-white/20 text-white text-base rounded-xl font-medium hover:bg-white/30 active:scale-95 transition-all touch-target-large backdrop-blur-sm"
              >
                {t('intake.skipDetection')}
              </button>
            </div>
          </div>

          {/* Overlay info */}
          <div className="absolute top-0 left-0 right-0 p-4 bg-gradient-to-b from-black/60 to-transparent">
            <h3 className="text-white font-semibold text-lg">{activeItem.medication_name}</h3>
            <p className="text-white/80 text-sm">{getGuidanceText()}</p>
          </div>

          {/* DEBUG panel: the "Confidence" meter below is the decaying live
              signal; peak/event are the real scores driving the decision. */}
          {detectionDebug && (
            <div className="absolute top-20 left-2 z-20 bg-black/70 text-green-300 font-mono text-[10px] leading-tight p-2 rounded-md max-w-[60%] space-y-0.5">
              <div>decision: {String(detectionDebug.decision)} ({String(detectionDebug.decision_reason ?? '')})</div>
              <div>peak: {String(detectionDebug.peak_confidence)} | event: {String(detectionDebug.event_confidence)} | frame: {String(detectionDebug.frame_confidence)}</div>
              <div>confirm@ {String(detectionDebug.confirm_threshold)} | raw: {String(detectionDebug.raw_event_score)}</div>
              <div>style: {String(detectionDebug.event_style)} | contact: {String(detectionDebug.peak_mouth_contact)}</div>
              <div>mouthOpen: {String(detectionDebug.mouth_open)} ({String(detectionDebug.mouth_open_ratio)}) | withdrew: {String(detectionDebug.withdrew_enough)}</div>
              <div>dwell: {String(detectionDebug.dwell)} | nearMouth: {String(detectionDebug.hand_near_mouth)} | hands: {String(detectionDebug.hands)}</div>
              <div>winClosed: {String(detectionDebug.event_window_closed)} | status: {String(detectionDebug.status)}</div>
            </div>
          )}

          {/* Confidence meter */}
          <div className="absolute bottom-24 left-4 right-4">
            <div className="bg-black/50 rounded-xl p-4 backdrop-blur-sm space-y-2">
              <div className="flex justify-between text-white text-sm mb-2">
                <span>Confidence</span>
                <span className="font-mono">{confidence}% <span className="text-white/50">(peak {peakConfidence}%)</span></span>
              </div>
              <div className="w-full h-3 bg-gray-700 rounded-full overflow-hidden">
                <div
                  className="h-full transition-all duration-200 rounded-full"
                  style={{
                    width: `${confidence}%`,
                    backgroundColor: confidence >= 50 ? '#22c55e' : confidence >= 25 ? '#eab308' : '#ef4444',
                  }}
                />
              </div>
              <div className="flex justify-between items-center">
                <p className="text-white/70 text-xs">{detectionStatus}</p>
                <p className="text-white/60 text-xs">{t('intake.elapsed')}: {Math.floor(elapsedSeconds / 60)}:{String(elapsedSeconds % 60).padStart(2, '0')}</p>
              </div>
              {!mediaPipeLoaded && !mediaPipeError && (
                <p className="text-yellow-300 text-xs">Loading MediaPipe...</p>
              )}
              {mediaPipeError && (
                <p className="text-red-300 text-xs">{mediaPipeError}</p>
              )}
            </div>          </div>

          {/* Ambiguous-detection confirmation prompt (patient safety: never auto-log) */}
          {uncertainPrompt !== null && (
            <div className="absolute inset-0 z-10 flex flex-col items-center justify-center bg-black/70 backdrop-blur-sm p-8 text-white">
              <AlertTriangle className="w-12 h-12 mb-4 text-yellow-400" />
              <h3 className="text-xl font-semibold text-center mb-2">需要確認 / Please confirm</h3>
              <p className="text-base text-center mb-8 opacity-90">
                系統偵測到您可能已服藥，但無法完全確定。請確認您是否已服下藥物。
                <br />
                We detected a possible intake but aren't certain. Did you take your medication?
              </p>
              <div className="space-y-3 w-full max-w-xs">
                <button
                  onClick={handleUncertainConfirm}
                  className="w-full py-4 bg-[#0057B8] text-white text-base rounded-xl font-medium hover:bg-[#003D82] active:scale-95 transition-all touch-target-large"
                >
                  是的，我已服藥 / Yes, I took it
                </button>
                <button
                  onClick={handleUncertainReject}
                  className="w-full py-4 bg-white/20 text-white text-base rounded-xl font-medium hover:bg-white/30 active:scale-95 transition-all touch-target-large backdrop-blur-sm"
                >
                  尚未服藥，繼續偵測 / Not yet, keep detecting
                </button>
              </div>
            </div>
          )}

          {/* Cancel + Manual buttons */}
          <div className="absolute bottom-4 left-4 right-4 space-y-2">
            {elapsedSeconds >= 15 && (
              <button
                onClick={handleManualConfirm}
                className="w-full py-4 bg-white/20 text-white text-base rounded-xl font-medium hover:bg-white/30 active:scale-95 transition-all touch-target-large backdrop-blur-sm"
              >
                {t('intake.manualConfirm')}
              </button>
            )}
            <button
              onClick={handleCancelDetection}
              className="w-full py-4 bg-white/10 text-white/80 text-base rounded-xl font-medium hover:bg-white/20 active:scale-95 transition-all touch-target-large backdrop-blur-sm"
            >
              {t('common.cancel')}
            </button>
          </div>
        </div>
      </div>
    );
  }

  // Ready confirmation modal
  if (phase === 'ready' && activeItem) {
    return (
      <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
        <div className="bg-white rounded-2xl p-6 w-full max-w-sm space-y-4 shadow-2xl text-center">
          <div className="w-12 h-12 bg-blue-50 rounded-full flex items-center justify-center mx-auto">
            <Pill className="w-6 h-6 text-[#0057B8]" />
          </div>
          <div>
            <h3 className="text-lg font-semibold text-gray-900">{t('intake.readyTitle')}</h3>
            <p className="text-base text-gray-500 mt-1">
              {activeItem.medication_name}
              {activeItem.dosage ? ` · ${activeItem.dosage}` : ''}
            </p>
          </div>

          {/* Warning box (amber) */}
          {activeItem.warning && (
            <div className="p-3 bg-amber-50 border border-amber-200 rounded-xl text-base text-amber-800 text-left">
              <div className="flex items-start gap-2">
                <AlertTriangle className="w-5 h-5 shrink-0 mt-0.5" />
                <p className="flex-1">{activeItem.warning}</p>
              </div>
              <button
                onClick={replayWarning}
                aria-label={t('intake.tts.speakerLabel')}
                className="mt-3 ml-auto flex items-center gap-2 px-4 py-3 bg-amber-100 text-amber-800 text-base font-medium rounded-lg hover:bg-amber-200 active:scale-95 transition-all touch-target-large"
              >
                <Volume2 className="w-5 h-5" />
                {t('intake.tts.speakerLabel')}
              </button>
            </div>
          )}

          <div className="space-y-3 pt-2">
            <button
              onClick={handleReadyConfirm}
              className="w-full py-4 bg-[#0057B8] text-white text-base rounded-xl font-medium hover:bg-[#003D82] active:scale-95 transition-all touch-target-large"
            >
              {t('intake.readyButton')}
            </button>
            <button
              onClick={handleReadyCancel}
              className="w-full py-4 bg-gray-100 text-gray-700 text-base rounded-xl font-medium hover:bg-gray-200 active:scale-95 transition-all touch-target-large"
            >
              {t('common.cancel')}
            </button>
            {isSpeaking && (
              <p className="flex items-center justify-center gap-2 text-base text-[#0057B8] font-medium">
                <Volume2 className="w-5 h-5 animate-pulse" />
                {t('intake.tts.speakerLabel')}...
              </p>
            )}
          </div>
        </div>
      </div>
    );
  }

  // Completion screen
  if (phase === 'complete' && activeItem) {
    return (
      <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
        <div className="bg-white rounded-2xl p-8 w-full max-w-sm space-y-6 shadow-2xl text-center">
          <div className="w-16 h-16 bg-green-50 rounded-full flex items-center justify-center mx-auto">
            <CheckCircle2 className="w-8 h-8 text-green-500" />
          </div>
          <div>
            <h3 className="text-xl font-semibold text-gray-900">Intake Complete</h3>
            <p className="text-gray-500 mt-1">{activeItem.medication_name} has been recorded.</p>
          </div>

          {/* Encouragement box (green) */}
          <div className="p-3 bg-green-50 border border-green-200 rounded-xl text-base text-green-700 text-left">
            <p>🎉 {t('intake.tts.cheerUp')}</p>
            <button
              onClick={replayCheerUp}
              aria-label={t('intake.tts.speakerLabel')}
              className="mt-3 ml-auto flex items-center gap-2 px-4 py-3 bg-green-100 text-green-700 text-base font-medium rounded-lg hover:bg-green-200 active:scale-95 transition-all touch-target-large"
            >
              <Volume2 className="w-5 h-5" />
              {t('intake.tts.speakerLabel')}
            </button>
          </div>

          <button
            onClick={() => {
              stopTTS();
              setPhase('list');
              setActiveItem(null);
              resetAiState();
            }}
            className="w-full py-4 bg-[#0057B8] text-white text-base rounded-xl font-medium hover:bg-[#003D82] active:scale-95 transition-all touch-target-large"
          >
            Back to Schedule
          </button>
          {isSpeaking && (
            <p className="flex items-center justify-center gap-2 text-base text-green-700 font-medium">
              <Volume2 className="w-5 h-5 animate-pulse" />
              {t('intake.tts.speakerLabel')}...
            </p>
          )}
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-3">
        <Loader2 className="w-8 h-8 animate-spin text-[#0057B8]" />
        <span className="text-gray-500 text-base">{t('common.loading')}</span>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold text-gray-900">{t('intake.title')}</h2>
        <p className="text-base text-gray-500 mt-0.5">{new Date().toLocaleDateString(undefined, { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' })}</p>
      </div>

      {intakes.length === 0 ? (
        <div className="bg-white rounded-xl border border-gray-100 shadow-sm hover:shadow-md transition-all duration-300 hover:border-gray-200 p-8 text-center">
          <Pill className="w-12 h-12 text-gray-300 mx-auto mb-3" />
          <p className="text-gray-500">{t('schedule.noSchedule')}</p>
        </div>
      ) : (
        <div className="space-y-2">
          {intakes.map((item) => (
            <div key={item.id} className="space-y-2">
              <div
                className={`p-4 bg-white rounded-xl border shadow-sm hover:shadow-md transition-all duration-300 flex items-center gap-3 ${
                  highlightMedId && String(item.medication_id) === highlightMedId ? 'ring-2 ring-[#0057B8]' : 'border-gray-100 hover:border-gray-200'
                }`}
              >
                <div className={`w-10 h-10 rounded-full flex items-center justify-center shrink-0 ${
                  item.status === 'taken' ? 'bg-green-50' :
                  item.status === 'missed' ? 'bg-red-50' :
                  item.status === 'skipped' ? 'bg-orange-50' :
                  'bg-blue-50'
                }`}>
                  {getStatusIcon(item.status)}
                </div>
                <div className="flex-1 min-w-0">
                  <p className="font-medium text-gray-900">{item.medication_name}</p>
                  <p className="text-base text-gray-500">{item.dosage} · {item.pills_remaining} pills left</p>
                  {item.scheduled_time && (
                    <p className="text-sm text-gray-400 mt-0.5">
                      {new Date(item.scheduled_time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                    </p>
                  )}
                  {item.pills_remaining <= 0 && (
                    <p className="text-sm text-red-600 mt-1 flex items-center gap-1">
                      <AlertTriangle className="w-3 h-3" />
                      {t('intake.overdoseWarning')}
                    </p>
                  )}
                </div>
                {item.status === 'pending' && skippingId !== item.id && (
                  <div className="flex gap-2 shrink-0">
                    <button
                      onClick={() => handleTakeClick(item)}
                      disabled={item.pills_remaining <= 0 || !mediaPipeLoaded}
                      className="px-4 py-3 bg-[#0057B8] text-white text-base font-medium rounded-lg hover:bg-[#003D82] active:scale-95 transition-all disabled:opacity-50 disabled:cursor-not-allowed touch-target-large"
                    >
                      {item.pills_remaining <= 0 ? t('intake.cannotTake') : t('intake.take')}
                    </button>
                    <button
                      onClick={() => { setSkippingId(item.id); setSkipNote(''); }}
                      className="px-4 py-3 bg-gray-100 text-gray-700 text-base font-medium rounded-lg hover:bg-gray-200 active:scale-95 transition-all touch-target-large"
                    >
                      {t('intake.skip')}
                    </button>
                  </div>
                )}
                {item.status !== 'pending' && getStatusBadge(item.status)}
              </div>

              {/* Skip confirmation panel */}
              {skippingId === item.id && (
                <div className="p-4 bg-gray-50 rounded-xl border border-gray-200 space-y-3">
                  <div>
                    <label className="block text-base font-medium text-gray-700 mb-1">{t('intake.whySkip')}</label>
                    <textarea
                      value={skipNote}
                      onChange={(e) => setSkipNote(e.target.value)}
                      className="w-full px-4 py-2.5 border border-gray-300 rounded-xl focus:ring-2 focus:ring-[#0057B8]/30 focus:border-[#0057B8] outline-none bg-white"
                      rows={2}
                      placeholder="Optional reason..."
                    />
                  </div>
                  <div className="flex gap-3">
                    <button
                      onClick={() => handleSkipConfirm(item)}
                      className="flex-1 py-4 bg-orange-500 text-white text-base rounded-xl font-medium hover:bg-orange-600 active:scale-95 transition-all flex items-center justify-center gap-2 touch-target-large"
                    >
                      {t('intake.skip')}
                    </button>
                    <button
                      onClick={() => { setSkippingId(null); setSkipNote(''); }}
                      className="flex-1 py-4 bg-gray-100 text-gray-700 text-base rounded-xl font-medium hover:bg-gray-200 active:scale-95 transition-all touch-target-large"
                    >
                      {t('common.cancel')}
                    </button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Emotion Modal for pre/post */}
      {(phase === 'pre' || phase === 'post') && activeItem && (
        <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl p-6 w-full max-w-sm space-y-4 shadow-2xl">
            <h3 className="text-lg font-semibold text-gray-900">
              {phase === 'pre' ? t('intake.preEmotion') : t('intake.postEmotion')}
            </h3>
            <p className="text-base text-gray-500">{activeItem.medication_name}</p>

            <div className="relative aspect-[4/3] bg-gray-900 rounded-2xl overflow-hidden">
              <video
                ref={videoRef}
                autoPlay
                playsInline
                muted
                className="w-full h-full object-cover"
              />
              <div className={`absolute inset-0 flex flex-col items-center justify-center text-white transition-opacity duration-300 ${cameraOn ? 'opacity-0 pointer-events-none' : 'opacity-100'}`}>
                <Camera className="w-12 h-12 mb-3 opacity-60" />
                <p className="text-sm opacity-80">Camera starting...</p>
              </div>
              <div className="absolute inset-0 border-2 border-[#0057B8]/40 rounded-xl opacity-50 pointer-events-none" />
              <div className="absolute top-4 left-4 text-white text-sm bg-black/40 px-2 py-1 rounded">
                Look at camera
              </div>
            </div>

            {!aiResult ? (
              <button
                onClick={captureVideoAndAnalyze}
                disabled={aiLoading || !cameraOn}
                className="w-full py-4 bg-[#0057B8] text-white text-base rounded-xl font-medium hover:bg-[#003D82] active:scale-95 transition-all disabled:opacity-50 flex items-center justify-center gap-2 touch-target-large"
              >
                {aiLoading ? (
                  <Loader2 className="w-5 h-5 animate-spin" />
                ) : (
                  <ScanFace className="w-5 h-5" />
                )}
                {aiLoading ? t('common.loading') : 'Detect Emotion'}
              </button>
            ) : (
              <div className="p-4 bg-blue-50 border border-blue-200 rounded-xl space-y-3">
                <div className="text-center">
                  <p className="font-medium text-gray-900">
                    Detected: {t(`emotion.${(aiResult.emotion_type || '').toLowerCase()}`)}
                  </p>
                  <p className="text-base text-gray-600">
                    Confidence: {((aiResult.emotion_score || 0) * 100).toFixed(1)}%
                  </p>
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={confirmAiEmotion}
                    className="flex-1 py-4 bg-[#0057B8] text-white text-base rounded-xl font-medium hover:bg-[#003D82] active:scale-95 transition-all flex items-center justify-center gap-2 touch-target-large"
                  >
                    <CheckCircle2 className="w-4 h-4" />
                    Confirm
                  </button>
                  <button
                    onClick={resetAiState}
                    className="flex-1 py-4 bg-gray-100 text-gray-700 text-base rounded-xl font-medium hover:bg-gray-200 active:scale-95 transition-all flex items-center justify-center gap-2 touch-target-large"
                  >
                    <RefreshCw className="w-4 h-4" />
                    Retake
                  </button>
                </div>
              </div>
            )}

            {aiError && (
              <div className="p-3 bg-red-50 border border-red-200 rounded-lg text-base text-red-600 flex items-center gap-2">
                <AlertTriangle className="w-4 h-4" />
                {aiError}
              </div>
            )}

            <button
              onClick={() => {
                stopTTS();
                stopCamera();
                resetAiState();
                setPhase('list');
                setActiveItem(null);
                setPreEmotionId(null);
              }}
              className="w-full py-4 bg-gray-100 text-gray-700 text-base rounded-xl font-medium hover:bg-gray-200 active:scale-95 transition-all touch-target-large"
            >
              {t('common.cancel')}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
