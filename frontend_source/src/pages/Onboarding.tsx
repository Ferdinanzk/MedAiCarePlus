import { useState, useRef, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { aiApi } from '../lib/ai-api';
import { getFaceToken, faceLogout } from '../lib/face-auth';
import {
  Camera,
  Users,
  CheckCircle2,
  ChevronRight,
  Loader2,
} from 'lucide-react';

type Step = 1 | 2 | 3;

/**
 * Crop the center...
 * Matches the reference photo_capture.py 3:2 logic but portrait-orientated
 * for web camera (taller than wide).
 */
function cropPortraitCenter(srcCanvas: HTMLCanvasElement): HTMLCanvasElement {
  const srcW = srcCanvas.width;
  const srcH = srcCanvas.height;
  // Portrait ratio: width : height = 2 : 3
  // So for given width, height = width * 3 / 2
  // We want the largest 2:3 rectangle centered in the frame
  let cropW: number;
  let cropH: number;
  if (srcW / srcH <= 2 / 3) {
    // Frame is taller/narrower than 2:3 → width limited
    cropW = srcW;
    cropH = (cropW * 3) / 2;
  } else {
    // Frame is wider/shorter than 2:3 → height limited
    cropH = srcH;
    cropW = (cropH * 2) / 3;
  }
  cropW = Math.round(cropW);
  cropH = Math.round(cropH);
  const x = Math.round((srcW - cropW) / 2);
  const y = Math.round((srcH - cropH) / 2);
  const dstCanvas = document.createElement('canvas');
  dstCanvas.width = cropW;
  dstCanvas.height = cropH;
  const dstCtx = dstCanvas.getContext('2d');
  if (!dstCtx) return srcCanvas; // fallback
  dstCtx.drawImage(srcCanvas, x, y, cropW, cropH, 0, 0, cropW, cropH);
  return dstCanvas;
}

function getSavedStep(): Step {
  if (localStorage.getItem('onboarding_complete')) return 3;
  if (localStorage.getItem('onboarding_face_done')) return 2;
  return 1;
}

function setStorage(key: string, value: string) {
  localStorage.setItem(key, value);
  window.dispatchEvent(new StorageEvent('storage', { key }));
}

const STAGE_ORDER = ['front', 'left', 'right'] as const;
type PoseDir = 'front' | 'left' | 'right';

export default function Onboarding() {
  useTranslation();
  const videoRef = useRef<HTMLVideoElement>(null);
  const sampleCanvasRef = useRef<HTMLCanvasElement>(null);
  const [step, setStep] = useState<Step>(getSavedStep());
  const [loading, setLoading] = useState(false);
  const [hasContacts, setHasContacts] = useState(false);
  const [sessionExpired, setSessionExpired] = useState(false);

  // Step 1: Face enrollment with pose detection
  const [poseStageIdx, setPoseStageIdx] = useState(0);       // 0=front, 1=left, 2=right
  const [capturedPhotos, setCapturedPhotos] = useState<string[]>([]);
  const [cameraOn, setCameraOn] = useState(false);
  const [poseResult, setPoseResult] = useState<{ detected: boolean; direction: string; stub: boolean; face_size_ratio?: number } | null>(null);
  const [proximity, setProximity] = useState<'too_close' | 'too_far' | 'good' | null>(null);
  const [countdown, setCountdown] = useState<number | null>(null);  // 3,2,1 or null
  const [enrolling, setEnrolling] = useState(false);
  const stagePhotosRef = useRef<string[]>([]);
  const holdStartRef = useRef<number | null>(null);
  const poseIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const prevFrameRef = useRef<ImageData | null>(null);
  const poseStageIdxRef = useRef(0);

  // Keep ref in sync with state to avoid stale closure in interval
  useEffect(() => {
    poseStageIdxRef.current = poseStageIdx;
  }, [poseStageIdx]);

  // Step 2: Family contact
  const [familyName, setFamilyName] = useState('');
  const [verificationCode, setVerificationCode] = useState('');
  const [familyError, setFamilyError] = useState('');

  // Mark onboarding complete when reaching the Done screen (step 3).
  // Persist to the backend too so the wizard never reappears after the
  // localStorage flags are lost (logout / new device / cleared cache).
  useEffect(() => {
    if (step === 3) {
      setStorage('onboarding_complete', '1');
      const headers = getAuthHeaders();
      if (headers.Authorization) {
        fetch('/api/auth/onboarding-complete', { method: 'POST', headers }).catch(() => {});
      }
    }
  }, [step]);

  function getAuthHeaders(): Record<string, string> {
    const token = getFaceToken();
    return token ? { Authorization: `Bearer ${token}` } : {};
  }

  const handleLogoutAndLogin = () => {
    faceLogout();
    localStorage.removeItem('onboarding_complete');
    localStorage.removeItem('onboarding_face_done');
    localStorage.removeItem('onboarding_family_done');
    window.location.href = '/login';
  };

  // On mount: query backend to detect already-completed steps. The backend is
  // the source of truth — localStorage is only a cache. This stops the wizard
  // from forcing a re-enroll of a face that is already in the gallery after
  // logout / new device / cleared storage.
  useEffect(() => {
    const checkBackend = async () => {
      const headers = getAuthHeaders();
      if (!headers.Authorization) {
        setSessionExpired(true);
        return;
      }

      // Face already enrolled? Skip the 3-pose capture step.
      try {
        const faceRes = await fetch('/api/face/enrollment-status', { headers });
        if (faceRes.status === 401) {
          setSessionExpired(true);
          return;
        }
        if (faceRes.ok) {
          const face = await faceRes.json();
          if (face.enrolled) setStorage('onboarding_face_done', '1');
        }
      } catch { /* offline — fall back to localStorage */ }

      // Whole wizard already finished on a previous device/session?
      let serverComplete = false;
      try {
        const obRes = await fetch('/api/auth/onboarding-status', { headers });
        if (obRes.ok) {
          const ob = await obRes.json();
          if (ob.face_enrolled) setStorage('onboarding_face_done', '1');
          if (ob.onboarding_complete) {
            serverComplete = true;
            setStorage('onboarding_complete', '1');
          }
        }
      } catch { /* offline — fall back to localStorage */ }

      const contactsRes = await fetch('/api/family/contacts', { headers });

      if (contactsRes.status === 401) {
        setSessionExpired(true);
        return;
      }

      if (contactsRes.ok) {
        const contacts = await contactsRes.json();
        if (contacts.length > 0) {
          setHasContacts(true);
          // Legacy users predate the onboarding_complete column. Having a
          // linked contact means they finished setup before — migrate them.
          if (!serverComplete) {
            serverComplete = true;
            setStorage('onboarding_complete', '1');
            fetch('/api/auth/onboarding-complete', { method: 'POST', headers }).catch(() => {});
          }
        }
      }

      // If the backend says onboarding is already done, leave the wizard.
      if (serverComplete) {
        window.location.replace('/dashboard');
        return;
      }

      setStep(getSavedStep());
    };
    checkBackend();
  }, []);

  // Cleanup camera on unmount
  useEffect(() => {
    const videoEl = videoRef.current;
    return () => {
      // Inline cleanup to avoid stale closure / dependency issues
      if (poseIntervalRef.current) {
        clearInterval(poseIntervalRef.current);
        poseIntervalRef.current = null;
      }
      const stream = videoEl?.srcObject as MediaStream | null;
      stream?.getTracks().forEach((t) => t.stop());
      if (videoEl) videoEl.srcObject = null;
      setCameraOn(false);
      holdStartRef.current = null;
      prevFrameRef.current = null;
    };
  }, []);

  // ── Pose detection helpers ─────────────────────────────────────────────────

  const stopPoseLoop = () => {
    if (poseIntervalRef.current) {
      clearInterval(poseIntervalRef.current);
      poseIntervalRef.current = null;
    }
  };

  // Pixel-diff fallback for stub mode (same approach as Register.tsx)
  const checkStabilityStub = () => {
    const canvas = sampleCanvasRef.current;
    const video = videoRef.current;
    if (!canvas || !video) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.drawImage(video, 0, 0, 80, 60);
    const frame = ctx.getImageData(0, 0, 80, 60);
    const prev = prevFrameRef.current;
    if (prev) {
      let diff = 0;
      for (let i = 0; i < frame.data.length; i += 4) {
        diff +=
          Math.abs(frame.data[i] - prev.data[i]) +
          Math.abs(frame.data[i + 1] - prev.data[i + 1]) +
          Math.abs(frame.data[i + 2] - prev.data[i + 2]);
      }
      const avgDiff = diff / (frame.data.length / 4) / 3;
      if (avgDiff < 12) {
        if (!holdStartRef.current) holdStartRef.current = Date.now();
        const elapsed = Date.now() - holdStartRef.current;
        setCountdown(Math.max(0, 3 - Math.floor(elapsed / 1000)));
        if (elapsed >= 3000) {
          holdStartRef.current = null;
          setCountdown(null);
          captureAndAdvance();
        }
      } else {
        holdStartRef.current = null;
        setCountdown(null);
      }
    }
    prevFrameRef.current = frame;
  };

  const captureAndAdvance = () => {
    const video = videoRef.current;
    if (!video) return;
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth || 640;
    canvas.height = video.videoHeight || 480;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.drawImage(video, 0, 0);
    const cropped = cropPortraitCenter(canvas);  // Auto-crop to portrait 2:3
    const dataUrl = cropped.toDataURL('image/jpeg');
    stagePhotosRef.current = [...stagePhotosRef.current, dataUrl];
    setCapturedPhotos([...stagePhotosRef.current]);

    const nextIdx = stagePhotosRef.current.length;
    if (nextIdx >= 3) {
      // All 3 captured
      stopPoseLoop();
      handleEnroll(stagePhotosRef.current);
    } else {
      setPoseStageIdx(nextIdx);
      holdStartRef.current = null;
      prevFrameRef.current = null;
    }
  };

  const startPoseLoop = () => {
    stopPoseLoop();
    poseIntervalRef.current = setInterval(async () => {
      const video = videoRef.current;
      if (!video || !video.srcObject || video.videoWidth === 0 || video.readyState < 2) return;

      // Take a frame snapshot
      const canvas = document.createElement('canvas');
      canvas.width = video.videoWidth || 320;
      canvas.height = video.videoHeight || 240;
      const ctx = canvas.getContext('2d');
      if (!ctx) return;
      ctx.drawImage(video, 0, 0);

      try {
        const cropped = cropPortraitCenter(canvas);
        const blob = await new Promise<Blob>((r) => cropped.toBlob((b) => r(b!), 'image/jpeg', 0.7));
        const file = new File([blob], 'pose.jpg', { type: 'image/jpeg' });
        const form = new FormData();
        form.append('file', file);

        const res = await fetch('/api/face/check-pose', { method: 'POST', body: form });
        if (!res.ok) return;
        const result = await res.json();
        setPoseResult(result);

        // Proximity feedback (only when model is live, not stub)
        if (!result.stub && result.detected && typeof result.face_size_ratio === 'number') {
          if (result.face_size_ratio < 0.20) {
            setProximity('too_far');
          } else if (result.face_size_ratio > 0.65) {
            setProximity('too_close');
          } else {
            setProximity('good');
          }
        } else if (!result.detected) {
          setProximity(null);
        }

        const targetDir = STAGE_ORDER[poseStageIdxRef.current] as PoseDir;

        if (result.stub) {
          // Stub mode: use pixel-diff stability
          checkStabilityStub();
          return;
        }

        // Proximity check for live mode: must be good proximity (0.20 <= face_size_ratio <= 0.65)
        const isGoodProximity = result.stub || typeof result.face_size_ratio !== 'number'
          ? true
          : (result.face_size_ratio >= 0.20 && result.face_size_ratio <= 0.65);

        if (result.detected && result.direction === targetDir && isGoodProximity) {
          if (!holdStartRef.current) holdStartRef.current = Date.now();
          const elapsed = Date.now() - holdStartRef.current;
          const remaining = Math.max(0, 3 - Math.floor(elapsed / 1000));
          setCountdown(remaining);
          if (elapsed >= 3000) {
            // Auto-capture
            holdStartRef.current = null;
            setCountdown(null);
            captureAndAdvance();
          }
        } else {
          holdStartRef.current = null;
          setCountdown(null);
        }
      } catch {
        // Network error — try stub fallback
        checkStabilityStub();
      }
    }, 800);
  };

  const startCamera = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user' } });
      if (videoRef.current) videoRef.current.srcObject = stream;
      setCameraOn(true);
      stagePhotosRef.current = [];
      setCapturedPhotos([]);
      setPoseStageIdx(0);
      holdStartRef.current = null;
      prevFrameRef.current = null;
      // Start pose detection loop after camera stabilizes
      setTimeout(() => startPoseLoop(), 1500);
    } catch {
      alert('Camera access denied. Please allow camera permissions.');
    }
  };

  const stopCamera = () => {
    stopPoseLoop();
    const stream = videoRef.current?.srcObject as MediaStream | null;
    stream?.getTracks().forEach((t) => t.stop());
    if (videoRef.current) videoRef.current.srcObject = null;
    setCameraOn(false);
    holdStartRef.current = null;
    prevFrameRef.current = null;
    setProximity(null);
  };

  const dataUrlToFile = (dataUrl: string, filename: string): File => {
    const arr = dataUrl.split(',');
    const mime = arr[0].match(/:(.*?);/)?.[1] || 'image/jpeg';
    const bstr = atob(arr[1]);
    let n = bstr.length;
    const u8arr = new Uint8Array(n);
    while (n--) u8arr[n] = bstr.charCodeAt(n);
    return new File([u8arr], filename, { type: mime });
  };

  const handleEnroll = async (photos: string[]) => {
    setEnrolling(true);
    const faceSession = JSON.parse(localStorage.getItem('face_auth_user') || '{}');
    const faceLabel = faceSession.name || 'user';
    const files = photos.map((p, i) => dataUrlToFile(p, `face-${i}.jpg`));
    await aiApi.enrollFace(faceLabel, files);
    setEnrolling(false);
    setStorage('onboarding_face_done', '1');
    stopCamera();
    setStep(2);
  };

  // ── Family ─────────────────────────────────────────────────────────────────

  const handleFamily = async () => {
    if (!familyName) return;
    setLoading(true);
    setFamilyError('');

    try {
      const headers = getAuthHeaders();
      if (!headers.Authorization) {
        setFamilyError('Not logged in. Please complete face registration first.');
        setLoading(false);
        return;
      }

      const res = await fetch('/api/family/contacts', {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: familyName, relationship: 'user' }),
      });

      const data = await res.json();

      if (res.status === 401) {
        setSessionExpired(true);
        setLoading(false);
        return;
      }

      if (!res.ok || !data.code) {
        setFamilyError(data.detail || 'Could not generate code. Please try again.');
        setLoading(false);
        return;
      }

      setVerificationCode(data.code);
      setStorage('onboarding_family_done', '1');
      setHasContacts(true);
    } catch {
      setFamilyError('Network error. Please check your connection.');
    }

    setLoading(false);
  };

  // ── Steps config ───────────────────────────────────────────────────────────

  const steps = [
    { num: 1, label: 'Face', icon: Camera },
    { num: 2, label: 'Family', icon: Users },
    { num: 3, label: 'Done', icon: CheckCircle2 },
  ];

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <div className="min-h-screen bg-[#f8f9ff] flex flex-col">
      <header className="bg-white border-b border-[#dce9ff] sticky top-0 z-50">
        <div className="max-w-lg mx-auto px-4 h-14 flex items-center justify-center">
          <span className="font-semibold text-[#0b1c30]">Setup Wizard</span>
        </div>
      </header>

      <main className="flex-1 max-w-lg mx-auto w-full px-4 py-6">
        {/* Session expired banner */}
        {sessionExpired && (
          <div className="mb-4 p-4 bg-amber-50 border border-amber-300 rounded-xl flex flex-col gap-3">
            <p className="text-sm font-medium text-amber-800">Session expired. Please log in again to continue.</p>
            <button
              onClick={handleLogoutAndLogin}
              className="w-full py-2 bg-amber-500 text-white rounded-xl font-medium hover:bg-amber-600 transition-colors"
            >
              Log in again
            </button>
          </div>
        )}

        {/* Step indicators */}
        <div className="flex justify-between mb-6">
          {steps.map(({ num, label }) => (
            <div key={num} className={`flex flex-col items-center gap-1 ${step >= num ? 'text-[#006d36]' : 'text-[#6d7b6d]'}`}>
              <div className={`w-8 h-8 rounded-full flex items-center justify-center text-sm font-bold ${
                step > num ? 'bg-[#006d36] text-white' : step === num ? 'bg-[#4ade80] text-[#006d36]' : 'bg-[#eff4ff]'
              }`}>
                {step > num ? <CheckCircle2 className="w-4 h-4" /> : num}
              </div>
              <span className="text-sm">{label}</span>
            </div>
          ))}
        </div>

        {/* ── Step 1: Face Enrollment ── */}
        {step === 1 && (
          <div className="space-y-4">
            <h2 className="text-xl font-semibold text-[#0b1c30]">Face Enrollment</h2>
            <p className="text-sm text-[#3d4a3e]">We'll capture 3 photos to recognize you at login.</p>

            {/* Camera view */}
            <div className="relative aspect-square bg-[#213145] rounded-2xl overflow-hidden">
              <video ref={videoRef} autoPlay playsInline muted className="w-full h-full object-cover" />
              <canvas ref={sampleCanvasRef} width="80" height="60" className="hidden" />

              {!cameraOn ? (
                <div className="absolute inset-0 flex flex-col items-center justify-center text-white gap-4">
                  <Camera className="w-12 h-12 opacity-60" />
                  <button
                    onClick={startCamera}
                    className="px-6 py-3 bg-[#006d36] text-white text-base font-semibold rounded-xl hover:bg-[#005229] transition-colors"
                  >
                    Start Camera
                  </button>
                </div>
              ) : (
                <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
                  {/* Direction instruction */}
                  <div className="absolute top-4 left-0 right-0 flex justify-center z-10">
                    <div className="px-4 py-2 bg-black/70 rounded-xl">
                      <p className="text-white text-2xl font-bold text-center">
                        {poseStageIdx === 0 ? 'Look straight ahead' :
                         poseStageIdx === 1 ? 'Turn slightly LEFT' :
                                              'Turn slightly RIGHT'}
                      </p>
                    </div>
                  </div>

                  {/* Oval guide — 2:3 portrait crop indicator */}
                  <div
                    className={`w-48 h-[288px] rounded-full border-4 transition-colors duration-300 ${
                      countdown !== null
                        ? 'border-green-400'
                        : proximity === 'too_close'
                        ? 'border-red-400'
                        : proximity === 'too_far'
                        ? 'border-orange-400'
                        : proximity === 'good'
                        ? 'border-green-400'
                        : 'border-white/60'
                    }`}
                    style={{ boxShadow: '0 0 0 9999px rgba(0,0,0,0.4)' }}
                  />

                  {/* Countdown */}
                  {countdown !== null && (
                    <div className="absolute text-7xl font-bold text-white drop-shadow-xl">
                      {countdown === 0 ? '📸' : countdown}
                    </div>
                  )}

                  {/* Status bar */}
                  <div className="absolute bottom-4 left-0 right-0 flex justify-center">
                    <div className={`px-4 py-2 rounded-xl transition-colors ${
                      countdown !== null
                        ? 'bg-green-600/80'
                        : proximity === 'too_close'
                        ? 'bg-red-600/80'
                        : proximity === 'too_far'
                        ? 'bg-orange-500/80'
                        : 'bg-black/60'
                    }`}>
                      <p className="text-white text-base font-medium text-center">
                        {countdown !== null
                          ? 'Hold still...'
                          : proximity === 'too_close'
                          ? '↔ Move back a little'
                          : proximity === 'too_far'
                          ? '→ Come closer to camera'
                          : poseResult?.detected
                            ? 'Face detected — turn to position'
                            : 'Position your face in the oval'}
                      </p>
                    </div>
                  </div>
                </div>
              )}
            </div>

            {/* Photo progress thumbnails */}
            <div className="flex gap-3 justify-center">
              {['Front', 'Left', 'Right'].map((label, i) => (
                <div key={i} className="flex flex-col items-center gap-1">
                  {capturedPhotos[i] ? (
                    <img
                      src={capturedPhotos[i]}
                      alt={label}
                      className="w-16 h-16 rounded-lg object-cover border-2 border-[#006d36]"
                    />
                  ) : (
                    <div className={`w-16 h-16 rounded-lg border-2 border-dashed flex items-center justify-center ${
                      poseStageIdx === i && cameraOn ? 'border-[#006d36] bg-green-50' : 'border-gray-300 bg-gray-100'
                    }`}>
                      <Camera className="w-5 h-5 text-gray-300" />
                    </div>
                  )}
                  <span className="text-sm text-gray-500">{label}</span>
                </div>
              ))}
            </div>

            {/* Skip option for face enrollment */}
            <button
              type="button"
              onClick={() => {
                setStorage('onboarding_face_done', '1');
                stopCamera();
                setStep(2);
              }}
              className="w-full py-3 bg-gray-100 text-gray-600 rounded-xl font-medium hover:bg-gray-200 transition-colors"
            >
              Skip for now
            </button>

            {enrolling && (
              <div className="flex items-center justify-center gap-2 py-2">
                <Loader2 className="w-5 h-5 animate-spin text-[#006d36]" />
                <span className="text-base text-gray-600">Enrolling face...</span>
              </div>
            )}
          </div>
        )}

        {/* ── Step 2: Family Contact ── */}
        {step === 2 && (
          <div className="space-y-4">
            <h2 className="text-lg font-semibold text-[#0b1c30]">Step 2: Link Your LINE</h2>
            <p className="text-sm text-[#3d4a3e]">Sync your own LINE to receive medication reminders.</p>

            {verificationCode ? (
              <div className="bg-[#f8f9ff] rounded-xl p-6 text-center space-y-4">
                <p className="text-base font-medium text-[#3d4a3e]">
                  1. Scan this QR code with your LINE to add the bot:
                </p>
                <img
                  src="/line-bot-qr.png"
                  alt="LINE Bot QR Code"
                  className="w-44 h-44 mx-auto rounded-xl border border-[#dce9ff] shadow-sm"
                />
                <p className="text-base font-medium text-[#3d4a3e]">
                  2. Then send this code to the bot:
                </p>
                <div className="bg-white border-2 border-[#006d36] rounded-2xl p-4">
                  <p className="text-4xl font-bold text-[#006d36] tracking-[0.3em]">{verificationCode}</p>
                </div>
                <p className="text-sm text-[#6d7b6d]">
                  Once you send this code, your LINE will be linked automatically.
                </p>
                <button
                  onClick={() => setStep(3)}
                  className="w-full py-4 bg-[#006d36] text-white text-base font-semibold rounded-xl hover:bg-[#005229] transition-colors flex items-center justify-center gap-2"
                >
                  Continue <ChevronRight className="w-4 h-4" />
                </button>
              </div>
            ) : hasContacts ? (
              <div className="bg-green-50 border border-green-200 rounded-xl p-6 text-center space-y-4">
                <p className="text-sm font-medium text-green-800">Family contact already linked</p>
                <button
                  onClick={() => setStep(3)}
                  className="w-full py-3 bg-[#006d36] text-white rounded-xl font-medium hover:bg-[#005227] transition-colors flex items-center justify-center gap-2"
                >
                  Next <ChevronRight className="w-4 h-4" />
                </button>
              </div>
            ) : (
              <>
                <input
                  type="text"
                  value={familyName}
                  onChange={(e) => setFamilyName(e.target.value)}
                  placeholder="Your name"
                  className="w-full px-4 py-3 border border-[#bccabb] rounded-xl focus:ring-2 focus:ring-[#006d36] outline-none"
                />
                <button
                  onClick={handleFamily}
                  disabled={!familyName || loading}
                  className="w-full py-3 bg-[#006d36] text-white rounded-xl font-medium hover:bg-[#005227] transition-colors disabled:opacity-50 flex items-center justify-center gap-2"
                >
                  {loading ? <Loader2 className="w-5 h-5 animate-spin" /> : 'Generate Code'}
                </button>

                {familyError && (
                  <div className="p-3 bg-red-50 border border-red-200 rounded-xl text-sm text-red-600">
                    {familyError}
                  </div>
                )}

                {/* Skip option */}
                <button
                  onClick={() => { setStorage('onboarding_family_done', '1'); setStorage('onboarding_complete', '1'); setStep(3); }}
                  className="w-full py-3 bg-gray-100 text-gray-600 rounded-xl font-medium hover:bg-gray-200 transition-colors"
                >
                  Skip for now
                </button>
              </>
            )}
          </div>
        )}

        {/* ── Step 3: Done ── */}
        {step === 3 && (
          <div className="text-center space-y-4 py-8">
            <div className="w-16 h-16 bg-green-100 rounded-full flex items-center justify-center mx-auto">
              <CheckCircle2 className="w-8 h-8 text-green-600" />
            </div>
            <h2 className="text-xl font-bold text-[#0b1c30]">Setup Complete!</h2>
            <p className="text-[#3d4a3e]">You're ready to start tracking your medications.</p>
            <button
              onClick={() => { window.location.href = '/dashboard'; }}
              className="w-full py-3 bg-[#006d36] text-white rounded-xl font-medium hover:bg-[#005227] transition-colors"
            >
              Go to Dashboard
            </button>
          </div>
        )}
      </main>
    </div>
  );
}
