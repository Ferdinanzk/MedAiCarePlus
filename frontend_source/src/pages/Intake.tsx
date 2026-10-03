import { useCallback, useEffect, useRef, useState } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
import { Bot, Camera, CheckCircle2, Clock, Pill, ScanFace, ShieldCheck, TriangleAlert, XCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { ApiError, fetchReachyStatus, queueReachyTask } from '../lib/reachy-api';
import { getFaceAuthHeaders } from '../lib/face-auth';
import { blockLabel, blockMessage, blockOf, doseRefusal, dueDose, forgetRefusals, isOpen, refusalMessage, rememberRefusal } from '../lib/doses';
import { useNow } from '../hooks/useNow';
import { useOverdoseProtection } from '../hooks/useOverdoseProtection';

interface IntakeItem {
  id: number;
  med_id: number;
  name: string;
  dosage: string | null;
  scheduled_time: string | null;
  due_from?: string | null;
  expires_at?: string | null;
  status: 'pending' | 'missed' | 'taken' | 'skipped' | 'pending_confirmation';
  pills_remaining: number;
  warning: string | null;
}

interface Candidate {
  event_id: string;
  decision: 'confirmed' | 'uncertain';
  confidence: number;
  ready: boolean;
}

interface MonitorStatus {
  session_id: string;
  generation: string;
  name: string;
  intk_id: number;
  frame_seq: number;
  identity_status: string;
  identity_distance: number | null;
  emotion: { emotion_type: string; emotion_score: number; probabilities: Record<string, number> } | null;
  /** The face was last scored with the mouth covered: still analysed for the dose, not shown live. */
  emotion_occluded?: boolean;
  detector:{ stage: string; decision: string; event_confidence: number } | null;
  candidate: Candidate | null;
  recorded: { event_id: string; status: string; emotion_id?: number } | null;
}

interface RecentEvent { event_id: string; intk_id: number; recorded_at: string }
interface WorkerResult {
  type: string;
  frame_seq: number;
  generation: string;
  timestamp: number;
  width: number;
  height: number;
  faces: unknown[];
  hands: unknown[];
  poses: unknown[];
  error?: string;
}

const MAX_FRAME_WIDTH = 640;
const MAX_FRAME_HEIGHT = 480;
const FRAME_ERROR_MESSAGE = 'Camera frame unavailable. Retrying…';

async function api<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(url, { ...init, headers: { ...getFaceAuthHeaders(), ...init.headers } });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ApiError(response.status, typeof body.detail === 'string' ? body.detail : `Request failed (${response.status})`, body);
  }
  return body as T;
}

function detectorMessage(stage: string | undefined, name: string, engineReady: boolean): string {
  if (!engineReady) return 'Loading camera models';
  const possessive = name.endsWith('s') ? `${name}'` : `${name}'s`;
  switch (stage) {
    case 'WAITING_FOR_PEARL': return `Waiting for ${possessive} face`;
    case 'APPROACHING': return 'Monitoring pill movement';
    case 'AT_MOUTH': return 'Checking the pill at the mouth';
    case 'OCCLUDED': return 'Hand near face; checking intake';
    case 'WITHDRAWING': return 'Checking pill withdrawal';
    default: return 'Waiting for intake movement';
  }
}

export default function Intake() {
  const params = useParams();
  const [searchParams] = useSearchParams();
  const highlight = params.medicationId || searchParams.get('med');
  const doseParam = searchParams.get('intake');
  const startNow = searchParams.get('start') === '1';
  const [items, setItems] = useState<IntakeItem[]>([]);
  const [active, setActive] = useState<IntakeItem | null>(null);
  const [status, setStatus] = useState<MonitorStatus | null>(null);
  const [recent, setRecent] = useState<RecentEvent | null>(null);
  const [cameraReady, setCameraReady] = useState(false);
  const [engineReady, setEngineReady] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const { t, i18n } = useTranslation();
  // Ticks, so a row's camera and Reachy buttons appear once its dose becomes due.
  const clockNow = useNow();
  // Null while loading; doses are checked as if it were on until then.
  const protection = useOverdoseProtection();
  const guarded = protection !== false;
  const [reachyPaired, setReachyPaired] = useState(false);
  const [reachyNotice, setReachyNotice] = useState('');
  const videoRef = useRef<HTMLVideoElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const workerRef = useRef<Worker | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const sessionRef = useRef<MonitorStatus | null>(null);
  const sequenceRef = useRef(0);
  const workerBusyRef = useRef(false);
  const landmarkBusyRef = useRef(false);
  const visionBusyRef = useRef(false);
  const lastVisionRef = useRef(0);
  const blobRef = useRef<{ sequence: number; promise: Promise<Blob | null> } | null>(null);
  const latestStatusFrameRef = useRef(0);
  const latestStatusRef = useRef<MonitorStatus | null>(null);
  const lastRecordedEventRef = useRef<string | null>(null);
  const refreshInFlightRef = useRef<Promise<void> | null>(null);
  const refreshQueuedRef = useRef(false);
  const autoStartedRef = useRef(false);
  const frameFailureRef = useRef({ count: 0, lastReportedAt: 0 });
  const frameRetryAfterRef = useRef(0);
  // The medicine of the running session, for a refusal that arrives with a camera frame.
  const activeMedRef = useRef<number | null>(null);

  const reportFrameError = useCallback((cause: unknown) => {
    const failure = frameFailureRef.current;
    failure.count += 1;
    const now = Date.now();
    frameRetryAfterRef.current = now + Math.min(1000, 100 * (2 ** Math.min(failure.count - 1, 3)));
    // A camera allocation/read failure is recoverable. Keep retrying, but do
    // not turn a transient browser resource limit into a render/error loop.
    if (failure.count === 1 || now - failure.lastReportedAt >= 5000) {
      failure.lastReportedAt = now;
      setError(FRAME_ERROR_MESSAGE);
    }
    void cause;
  }, []);

  const clearFrameError = useCallback(() => {
    const failure = frameFailureRef.current;
    if (!failure.count) return;
    failure.count = 0;
    frameRetryAfterRef.current = 0;
    setError((current) => current === FRAME_ERROR_MESSAGE ? '' : current);
  }, []);

  const refresh = useCallback(async () => {
    if (refreshInFlightRef.current) {
      refreshQueuedRef.current = true;
      return refreshInFlightRef.current;
    }
    const request = (async () => {
      do {
        refreshQueuedRef.current = false;
        try {
          const rows = await api<IntakeItem[]>('/api/medications/today');
          setItems(rows);
          const events = await api<RecentEvent[]>('/api/intake/monitor/recent');
          setRecent(events[0] || null);
        } catch (cause) {
          setError(cause instanceof Error ? cause.message : 'Could not load medication schedule');
        }
      } while (refreshQueuedRef.current);
    })();
    refreshInFlightRef.current = request;
    try {
      await request;
    } finally {
      if (refreshInFlightRef.current === request) refreshInFlightRef.current = null;
    }
  }, []);

  const releaseResources = useCallback(() => {
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = null;
    workerRef.current?.terminate();
    workerRef.current = null;
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    if (videoRef.current) videoRef.current.srcObject = null;
    workerBusyRef.current = false;
    landmarkBusyRef.current = false;
    visionBusyRef.current = false;
    blobRef.current = null;
    frameFailureRef.current = { count: 0, lastReportedAt: 0 };
    frameRetryAfterRef.current = 0;
  }, []);

  useEffect(() => {
    let active = true;
    fetchReachyStatus().then(status => { if (active) setReachyPaired(status.paired); }, () => undefined);
    return () => { active = false; };
  }, []);

  /**
   * An error for the person. A dose the server refused (not due, too soon, daily maximum, missed) gets the server's
   * sentence, and the refusal is kept so the dose's buttons say it before the next try.
   */
  const describe = useCallback((cause: unknown, fallback: string, medId?: number | null) => {
    const refusal = cause instanceof ApiError ? doseRefusal(cause.body) : null;
    if (refusal) {
      rememberRefusal(refusal, medId);
      return refusalMessage(refusal, t, i18n.language);
    }
    return cause instanceof Error ? cause.message : fallback;
  }, [t, i18n.language]);

  const startWithReachy = async (item: IntakeItem) => {
    setError('');
    try {
      await queueReachyTask(item.id);
      setReachyNotice(t('reachy.taskQueued', { name: item.name }));
    } catch (cause) {
      setError(describe(cause, 'Could not start Reachy', item.med_id));
    }
  };

  useEffect(() => {
    void refresh();
    return () => {
      releaseResources();
      const session = sessionRef.current;
      if (session) {
        void api('/api/intake/monitor/end', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: session.session_id, generation: session.generation }),
        }).catch(() => {});
      }
      sessionRef.current = null;
    };
  }, [refresh, releaseResources]);

  const stop = useCallback(() => {
    const session = sessionRef.current;
    sessionRef.current = null;
    activeMedRef.current = null;
    releaseResources();
    setActive(null);
    setStatus(null);
    setCameraReady(false);
    setEngineReady(false);
    if (session) {
      void api('/api/intake/monitor/end', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: session.session_id, generation: session.generation }),
      }).catch(() => {});
    }
    void refresh();
  }, [releaseResources, refresh]);

  const acceptStatus = (next: MonitorStatus, session: MonitorStatus) => {
    if (sessionRef.current?.generation !== session.generation) return;
    // Landmark and image requests can complete in a different order. Keep a
    // response from an older frame from moving the UI back to an earlier state.
    if (next.frame_seq < latestStatusFrameRef.current) return;
    if (next.frame_seq === latestStatusFrameRef.current && latestStatusRef.current?.recorded && !next.recorded) return;
    latestStatusFrameRef.current = next.frame_seq;
    latestStatusRef.current = next;
    setStatus(next);
    const recordedEventId = next.recorded?.status === 'taken' ? next.recorded.event_id : null;
    if (recordedEventId && lastRecordedEventRef.current !== recordedEventId) {
      lastRecordedEventRef.current = recordedEventId;
      setRecent({ event_id: recordedEventId, intk_id: next.intk_id, recorded_at: new Date().toISOString() });
      void refresh();
    }
  };

  /** A frame request failed. If the server refused to record the dose (e.g. taken too soon after the last one),
   * say why and end the session: the camera cannot record this dose. */
  const frameRefused = (cause: unknown, fallback: string) => {
    const refused = cause instanceof ApiError && doseRefusal(cause.body) !== null;
    setError(describe(cause, fallback, activeMedRef.current));
    if (refused) stop();
  };

  const sendVision = async (session: MonitorStatus, sequence: number, blob: Blob) => {
    if (visionBusyRef.current) return;
    visionBusyRef.current = true;
    try {
      const body = new FormData();
      body.append('session_id', session.session_id);
      body.append('generation', session.generation);
      body.append('frame_seq', String(sequence));
      body.append('file', blob, 'frame.jpg');
      const result = await api<MonitorStatus>('/api/intake/monitor/vision', { method: 'POST', body });
      acceptStatus(result, session);
    } catch (cause) {
      if (sessionRef.current?.generation === session.generation) frameRefused(cause, 'Face verification failed');
    } finally {
      visionBusyRef.current = false;
    }
  };

  const handleResult = async (message: WorkerResult, session: MonitorStatus) => {
    workerBusyRef.current = false;
    if (message.type === 'error') {
      reportFrameError(message.error || 'Camera model failed');
      return;
    }
    if (message.type !== 'result' || message.generation !== session.generation ||
        sessionRef.current?.generation !== session.generation) return;
    landmarkBusyRef.current = true;
    try {
      const result = await api<MonitorStatus>('/api/intake/monitor/landmarks', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: session.session_id, generation: session.generation,
          frame_seq: message.frame_seq, timestamp: message.timestamp,
          width: message.width, height: message.height,
          faces: message.faces, hands: message.hands, poses: message.poses,
        }),
      });
      acceptStatus(result, session);
      const pending = blobRef.current;
      if (pending?.sequence === message.frame_seq) {
        blobRef.current = null;
        const blob = await pending.promise;
        if (blob && sessionRef.current?.generation === session.generation) {
          void sendVision(session, message.frame_seq, blob);
        }
      }
    } catch (cause) {
      if (sessionRef.current?.generation === session.generation) frameRefused(cause, 'Intake detector failed');
    } finally {
      landmarkBusyRef.current = false;
    }
  };

  const tick = async (session: MonitorStatus) => {
    const video = videoRef.current;
    if (sessionRef.current?.generation !== session.generation || !video ||
        video.readyState < 2 || workerBusyRef.current || landmarkBusyRef.current ||
        Date.now() < frameRetryAfterRef.current) return;
    const worker = workerRef.current;
    const canvas = canvasRef.current;
    if (!worker || !canvas) return;
    workerBusyRef.current = true;
    try {
      // Read a bounded frame from one reusable canvas. Transferring the pixel
      // buffer keeps at most one frame in flight and avoids allocating an
      // ImageBitmap for every timer tick (which can fail on mobile browsers).
      const context = canvas.getContext('2d', { alpha: false, willReadFrequently: true });
      if (!context) throw new Error('Camera canvas is unavailable');
      const sourceWidth = video.videoWidth || video.clientWidth || MAX_FRAME_WIDTH;
      const sourceHeight = video.videoHeight || video.clientHeight || MAX_FRAME_HEIGHT;
      const scale = Math.min(1, MAX_FRAME_WIDTH / sourceWidth, MAX_FRAME_HEIGHT / sourceHeight);
      const width = Math.max(1, Math.round(sourceWidth * scale));
      const height = Math.max(1, Math.round(sourceHeight * scale));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      context.drawImage(video, 0, 0, width, height);
      const sequence = ++sequenceRef.current;
      const now = performance.now();
      const captureVision = !visionBusyRef.current && now - lastVisionRef.current >= 200;
      const pixels = context.getImageData(0, 0, width, height);
      worker.postMessage(
        { type: 'frame', frame_data: pixels.data.buffer, width, height,
          frame_seq: sequence, generation: session.generation, timestamp: now },
        [pixels.data.buffer]
      );
      // Register the JPEG only after the worker owns the frame successfully;
      // a failed post cannot leave an orphaned blob waiting for a result.
      if (captureVision) {
        blobRef.current = { sequence, promise: new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', .75)) };
        lastVisionRef.current = now;
      }
      clearFrameError();
    } catch (cause) {
      workerBusyRef.current = false;
      reportFrameError(cause);
    }
  };

  const start = async (item: IntakeItem) => {
    if (loading || sessionRef.current) return;
    setLoading(true);
    setError('');
    setActive(item);
    activeMedRef.current = item.med_id;
    latestStatusFrameRef.current = 0;
    latestStatusRef.current = null;
    lastRecordedEventRef.current = null;
    try {
      const session = await api<MonitorStatus>('/api/intake/monitor/start', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ intk_id: item.id }),
      });
      sessionRef.current = session;
      latestStatusFrameRef.current = session.frame_seq;
      latestStatusRef.current = session;
      setStatus(session);
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: false, video: { facingMode: 'user', width: { ideal: 640 }, height: { ideal: 480 } },
      });
      streamRef.current = stream;
      if (!videoRef.current) throw new Error('Camera view is unavailable');
      videoRef.current.srcObject = stream;
      await videoRef.current.play();
      setCameraReady(true);
      // The MediaPipe WASM loader uses importScripts, so this must stay a
      // classic worker. Vite emits this worker as an IIFE bundle.
      const worker = new Worker(new URL('../workers/monitorWorker.ts', import.meta.url));
      workerRef.current = worker;
      worker.onmessage = (event: MessageEvent<WorkerResult>) => {
        if (event.data.type === 'ready') {
          setEngineReady(true);
          timerRef.current = setInterval(() => void tick(session), 66);
        } else if (event.data.type === 'error') {
          setError(event.data.error || 'Camera model failed');
          void stop();
        } else {
          void handleResult(event.data, session);
        }
      };
      worker.onerror = (event) => {
        setError(event.message || 'Camera worker could not start');
        void stop();
      };
      worker.postMessage({ type: 'init' });
    } catch (cause) {
      setError(describe(cause, 'Could not start monitoring', item.med_id));
      stop();
    } finally {
      setLoading(false);
    }
  };

  // Medication cards can request an immediate camera session. Resolve the
  // highlighted medication to an existing due row (nearest to its time; never
  // one that is not due yet or missed past halfway to the next), or ask the backend
  // for a locked pending row at the current time when no scheduled dose is due or
  // the medication is unscheduled; the backend may refuse that too (too soon, daily maximum).
  // The Today screen opens one exact scheduled dose (?intake=<intk_id>&start=1).
  // Both wait for the protection switch: with it off, a dose that is not due yet may start.
  useEffect(() => {
    if (!startNow || !doseParam || autoStartedRef.current || active || loading || items.length === 0
        || protection === null) return;
    autoStartedRef.current = true;
    const dose = items.find((item) => String(item.id) === doseParam);
    if (dose && isOpen(dose) && dose.pills_remaining > 0) {
      const block = blockOf(dose, items, Date.now(), protection);
      if (block) setError(blockMessage(block, dose, t, i18n.language));
      else void start(dose);
    } else {
      setError('This dose is no longer waiting to be taken');
    }
  }, [active, doseParam, items, loading, protection, start, startNow, t, i18n.language]);

  useEffect(() => {
    if (!startNow || doseParam || !highlight || autoStartedRef.current || active || loading || protection === null) return;
    const medId = Number(highlight);
    if (!Number.isInteger(medId) || medId <= 0) {
      autoStartedRef.current = true;
      setError('The selected medication could not be found');
      return;
    }
    const due = dueDose(items.filter((item) => item.med_id === medId && item.pills_remaining > 0), Date.now(), protection);
    autoStartedRef.current = true;
    if (due) {
      void start(due);
      return;
    }
    void api<IntakeItem>(`/api/medications/${medId}/intake-now`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
    }).then((item) => {
      setItems((current) => current.some((candidate) => candidate.id === item.id)
        ? current
        : [...current, item].sort((a, b) =>
          (a.scheduled_time || '').localeCompare(b.scheduled_time || '')));
      return start(item);
    }).catch((cause) => {
      setError(describe(cause, 'Could not prepare this dose', medId));
    });
  }, [active, describe, highlight, items, loading, protection, start, startNow]);

  const outcome = async (eventId: string, choice: string) => {
    const session = sessionRef.current;
    if (!session) return;
    try {
      const result = await api<MonitorStatus>('/api/intake/monitor/outcome', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: session.session_id, generation: session.generation,
                               event_id: eventId, outcome: choice }),
      });
      acceptStatus(result, session);
      if (choice === 'undo') {
        forgetRefusals();
        stop();
      }
    } catch (cause) {
      const refused = cause instanceof ApiError && doseRefusal(cause.body) !== null;
      setError(describe(cause, 'Could not save correction', activeMedRef.current));
      // "Yes, taken" was refused (e.g. too soon after the last dose): this session cannot record it.
      if (refused) stop();
    }
  };

  const undoRecent = async () => {
    if (!recent) return;
    try {
      await api('/api/intake/monitor/undo', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ event_id: recent.event_id }),
      });
      // With that dose gone, a gap or daily maximum the server reported may no longer hold.
      forgetRefusals();
      setRecent(null);
      if (sessionRef.current) stop();
      void refresh();
    } catch (cause) { setError(describe(cause, 'Could not undo dose')); }
  };

  const manual = async (item: IntakeItem) => {
    try {
      await api('/api/intake/record', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ intk_id: item.id, detection_method: 'manual' }),
      });
      if (sessionRef.current) stop();
      void refresh();
    } catch (cause) { setError(describe(cause, 'Could not save dose', item.med_id)); }
  };

  const skip = async (item: IntakeItem) => {
    try {
      await api(`/api/medications/intake/${item.id}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: 'skipped' }),
      });
      void refresh();
    } catch (cause) { setError(describe(cause, 'Could not skip dose')); }
  };

  const candidate = status?.candidate;
  const recorded = status?.recorded?.status === 'taken';
  const name = status?.name || 'the signed-in person';
  return (
    <div className="max-w-5xl mx-auto px-4 py-8 space-y-6">
      <div>
        <p className="text-sm font-semibold tracking-wide uppercase text-blue-700">Medication intake</p>
        <h1 className="text-3xl font-bold text-slate-900 mt-1">One camera, one care session</h1>
        <p className="text-slate-600 mt-2">The camera checks who is taking the dose, the intake gesture, and their facial expression together.</p>
      </div>
      {reachyNotice && <div role="status" className="flex items-start gap-2 rounded-xl bg-blue-50 border border-blue-200 p-4 text-blue-800">
        <Bot className="w-5 h-5 shrink-0" />{reachyNotice}
      </div>}
      {error && <div role="alert" className="flex items-start gap-2 rounded-xl bg-red-50 border border-red-200 p-4 text-red-700">
        <TriangleAlert className="w-5 h-5 shrink-0" />{error === 'busy_other_client' ? t('reachy.busyOtherClient') : error}
      </div>}
      {recent && <div className="flex flex-wrap justify-between items-center gap-3 rounded-xl bg-emerald-50 border border-emerald-200 p-4">
        <span className="flex items-center gap-2 text-emerald-800"><CheckCircle2 className="w-5 h-5" /> Dose recorded</span>
        <button onClick={() => void undoRecent()} className="font-medium underline text-emerald-900">Not taken — undo</button>
      </div>}
      <div className={active ? 'grid gap-5 lg:grid-cols-[minmax(0,1.4fr)_minmax(260px,1fr)]' : 'hidden'}>
        <div className="rounded-2xl bg-slate-950 overflow-hidden relative aspect-[4/3]">
          <video ref={videoRef} autoPlay playsInline muted className="w-full h-full object-cover" />
          {!cameraReady && <div className="absolute inset-0 flex items-center justify-center text-white gap-2">
            <Camera className="w-6 h-6" /> Starting camera…
          </div>}
        </div>
        <div className="rounded-2xl border bg-white p-5 space-y-4">
          <div className="flex justify-between items-start gap-2">
            <div>
              <p className="text-sm text-slate-500">Monitoring</p>
              <h2 className="text-xl font-semibold">{active?.name}</h2>
              <p className="text-sm text-slate-500">
                {active?.dosage || 'Dose'} · {active?.pills_remaining} pills left
                {active?.scheduled_time && <> · <Clock className="inline w-3 h-3" /> {new Date(active.scheduled_time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</>}
              </p>
            </div>
            <button onClick={stop} className="text-sm font-medium text-slate-600 underline">End session</button>
          </div>
          <div className="flex items-center gap-2"><ShieldCheck className="w-5 h-5 text-blue-700" />
            <span>{name}: {status?.identity_status === 'verified' ? 'Face verified' : status?.identity_status || 'Searching for face'}</span>
          </div>
          <div className="flex items-center gap-2"><ScanFace className="w-5 h-5 text-violet-700" />
            <span>{status?.emotion
              ? `${t(`emotion.${status.emotion.emotion_type.toLowerCase()}`)} · ${Math.round(status.emotion.emotion_score * 100)}%`
              : status?.emotion_occluded ? t('doseEmotion.liveCovered') : t('doseEmotion.liveUnavailable')}</span>
          </div>
          <div className="flex items-center gap-2"><Pill className="w-5 h-5 text-cyan-700" />
            <span>{recorded ? 'Dose recorded' : candidate?.ready ? 'Intake event detected' :
              detectorMessage(status?.detector?.stage, name, engineReady)}</span>
          </div>
          {active?.warning && <p className="text-sm rounded-lg p-3 bg-amber-50 text-amber-900">{active.warning}</p>}
          {candidate?.ready && !recorded && candidate.decision === 'uncertain' && <div className="rounded-xl bg-amber-50 p-4 space-y-3">
            <p className="font-medium">Did you take this medication?</p>
            <div className="flex gap-2">
              <button onClick={() => void outcome(candidate.event_id, 'taken_confirmed')} className="flex-1 rounded-lg p-3 bg-blue-700 text-white">Yes, taken</button>
              <button onClick={() => void outcome(candidate.event_id, 'not_taken')} className="flex-1 rounded-lg p-3 bg-white border">No</button>
            </div>
          </div>}
          {!recorded && <button onClick={() => active && void manual(active)} className="text-sm text-slate-600 underline">
            Mark this dose taken manually
          </button>}
        </div>
      </div>
      <div className="space-y-3">
        <h2 className="text-xl font-semibold text-slate-900">Today's doses</h2>
        {items.length === 0 && <p className="text-slate-500">No medication is scheduled today.</p>}
        {items.map((item) => {
          // Not due yet, missed past halfway to the next dose, or refused by the server a moment ago.
          const block = blockOf(item, items, clockNow, guarded);
          return <div key={item.id} className={`rounded-xl border bg-white p-4 flex flex-wrap items-center gap-4 justify-between ${String(item.id) === doseParam || (!doseParam && String(item.med_id) === highlight) ? 'ring-2 ring-blue-600' : ''}`}>
          <div className="flex items-center gap-3 min-w-0">
            <div className="rounded-full bg-blue-50 p-3 text-blue-700"><Pill className="w-5 h-5" /></div>
            <div><p className="font-semibold text-slate-900">{item.name}</p>
              <p className="text-sm text-slate-500">{item.dosage || 'Dose'} · {item.pills_remaining} pills left
                {item.scheduled_time && <> · <Clock className="inline w-3 h-3" /> {new Date(item.scheduled_time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</>}
              </p></div>
          </div>
          {isOpen(item) && item.pills_remaining > 0 ?
            <div className="flex flex-wrap items-center gap-2">
              {!block ? <>
                <button disabled={loading || !!active} onClick={() => void start(item)}
                  className="rounded-lg bg-blue-700 text-white px-4 py-3 disabled:opacity-50">Start camera</button>
                {reachyPaired && <button disabled={loading || !!active} onClick={() => void startWithReachy(item)}
                  className="rounded-lg border border-blue-700 text-blue-700 px-4 py-3 flex items-center gap-2 disabled:opacity-50">
                  <Bot className="w-4 h-4" />{t('reachy.useReachy')}</button>}
              </> : <span className={`flex items-center gap-1 px-2 text-sm ${block.reason === 'not_due' ? 'text-slate-600' : 'text-amber-800 font-medium'}`}>
                {block.reason === 'not_due' ? <Clock className="w-4 h-4" /> : <TriangleAlert className="w-4 h-4" />}{blockLabel(block, t)}</span>}
              {/* Skipping a later dose takes no pill, so it stays allowed. */}
              <button disabled={!!active} onClick={() => void skip(item)}
                className="rounded-lg border px-4 py-3 disabled:opacity-50">Skip</button>
            </div> :
            <span className="flex items-center gap-1 text-sm font-medium">{item.status === 'taken' ?
              <CheckCircle2 className="w-4 h-4 text-green-600" /> : item.status === 'pending_confirmation' ?
              <Clock className="w-4 h-4 text-amber-600" /> : <XCircle className="w-4 h-4" />}
              {item.status === 'pending_confirmation' ? t('intake.pendingConfirmation') : item.status}</span>}
        </div>;
        })}
      </div>
      <canvas ref={canvasRef} className="hidden" />
    </div>
  );
}
