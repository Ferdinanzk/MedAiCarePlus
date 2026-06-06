import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { aiApi } from '../lib/ai-api';
import { setFaceSession } from '../lib/face-auth';
import { Camera, ScanFace, Loader2, Mail, Lock, LogIn } from 'lucide-react';
import { Link } from 'react-router-dom';
import LanguageSwitcher from '../components/LanguageSwitcher';

export default function Login() {
  const { t } = useTranslation();
  const videoRef = useRef<HTMLVideoElement>(null);
  const mountedRef = useRef(true);
  const startingRef = useRef(false);
  const [isFaceMode, setIsFaceMode] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [cameraOn, setCameraOn] = useState(false);
  const [videoReady, setVideoReady] = useState(false);
  const [faceResult, setFaceResult] = useState<{
    identified?: boolean;
    name?: string;
    distance?: number;
    face_count?: number;
  } | null>(null);

  // Email login state
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');

  useEffect(() => {
    mountedRef.current = true;
    if (isFaceMode) {
      startCamera();
    } else {
      stopCamera();
    }
    return () => {
      mountedRef.current = false;
      stopCamera();
    };
  }, [isFaceMode]);

  const startCamera = async () => {
    if (startingRef.current) return;
    startingRef.current = true;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: 'user' },
      });
      if (!mountedRef.current) {
        stream.getTracks().forEach((t) => t.stop());
        return;
      }
      setCameraOn(true);
      if (videoRef.current) {
        videoRef.current.srcObject = stream;
      }
    } catch {
      if (mountedRef.current) {
        setError('Camera access denied or not available');
      }
    } finally {
      startingRef.current = false;
    }
  };

  const stopCamera = () => {
    const el = videoRef.current;
    if (el) {
      const stream = el.srcObject as MediaStream | null;
      el.srcObject = null;
      stream?.getTracks().forEach((t) => t.stop());
    }
    setCameraOn(false);
    setVideoReady(false);
  };

  const debounceRef = useRef(false);

  const handleFaceLogin = async () => {
    if (!videoRef.current || !cameraOn || !videoReady) return;
    if (debounceRef.current) return;
    debounceRef.current = true;
    setTimeout(() => { debounceRef.current = false; }, 300);

    const video = videoRef.current;
    if (!video.videoWidth || !video.videoHeight || video.readyState < 2) {
      setError('Camera not ready yet, please wait a moment.');
      return;
    }
    setLoading(true);
    setError('');
    setFaceResult(null);

    try {
      const canvas = document.createElement('canvas');
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      const ctx = canvas.getContext('2d');
      if (!ctx) throw new Error('Canvas not available');
      ctx.drawImage(video, 0, 0);

      const blob = await new Promise<Blob>((resolve) =>
        canvas.toBlob((b) => resolve(b!), 'image/jpeg', 0.9)
      );
      const file = new File([blob], 'face.jpg', { type: 'image/jpeg' });

      let result;
      try {
        result = await aiApi.faceLogin(file);
      } catch (e: unknown) {
        console.error('[faceLogin] network/parse error:', e);
        throw e;
      }

      if (result.error && result.error.includes('HTTP')) {
        setError('Face recognition server unavailable. Please try again later.');
      } else if (result.identified && result.name && result.token) {
        setFaceSession({ name: result.name, loginAt: new Date().toISOString(), u_id: result.u_id }, result.token);
        window.location.href = '/dashboard';
      } else if (result.identified && result.name) {
        setError('Login error: session token missing. Please try again.');
      } else {
        setError('Face not recognized. Try email login below.');
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Face recognition failed');
    }
    setLoading(false);
  };

  const handleEmailLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');
    try {
      const res = await fetch('/api/auth/email-login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email.trim().toLowerCase(), password }),
      });
      const data = await res.json();
      if (!res.ok || !data.success) {
        setError(data.error || 'Invalid email or password');
      } else {
        setFaceSession(
          { name: data.user.name, loginAt: new Date().toISOString(), u_id: data.user.id },
          data.token
        );
        window.location.href = '/dashboard';
      }
    } catch {
      setError('Network error. Please try again.');
    }
    setLoading(false);
  };

  return (
    <div className="min-h-screen bg-[#F8F9FA] flex flex-col">
      {/* Header */}
      <header className="bg-white border-b border-gray-100 safe-area-top">
        <div className="max-w-md mx-auto px-4 h-14 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 bg-[#0057B8] rounded-lg flex items-center justify-center">
              <span className="text-white font-bold text-sm">M+</span>
            </div>
            <span className="font-semibold text-gray-900">MedAiCarePlus</span>
          </div>
          <LanguageSwitcher />
        </div>
      </header>

      <div className="flex-1 flex flex-col items-center justify-center px-4 py-8">
        <div className="w-full max-w-sm mx-auto">
          {/* Logo & Welcome */}
          <div className="text-center mb-8">
            <img src="/healsmart-logo.png" alt="HealSmart" className="w-60 max-w-[75%] mx-auto mb-4" />
            <h1 className="text-2xl font-bold text-gray-900">{t('login.welcome')}</h1>
          </div>

          {/* Mode toggle: Email first, Face second */}
          <div className="flex bg-gray-100 rounded-xl p-1 mb-4">
            <button
              onClick={() => { setIsFaceMode(false); setError(''); }}
              className={`flex-1 py-2.5 rounded-lg text-sm font-medium transition-all flex items-center justify-center gap-1.5 ${
                !isFaceMode ? 'bg-white text-[#0057B8] shadow-sm' : 'text-gray-500 hover:text-gray-700'
              }`}
            >
              <Mail className="w-4 h-4" />
              Email
            </button>
            <button
              onClick={() => { setIsFaceMode(true); setError(''); }}
              className={`flex-1 py-2.5 rounded-lg text-sm font-medium transition-all flex items-center justify-center gap-1.5 ${
                isFaceMode ? 'bg-white text-[#0057B8] shadow-sm' : 'text-gray-500 hover:text-gray-700'
              }`}
            >
              <ScanFace className="w-4 h-4" />
              Face
            </button>
          </div>

          {/* Login Card */}
          <div className="bg-white rounded-2xl shadow-sm hover:shadow-md transition-all duration-300 border border-gray-100 hover:border-gray-200 p-6">

            {isFaceMode ? (
              <div className="text-center">
                <div className="aspect-square max-w-[260px] mx-auto bg-gray-50 rounded-xl flex items-center justify-center mb-5 relative overflow-hidden border border-gray-200">
                  <video
                    ref={videoRef}
                    autoPlay
                    playsInline
                    muted
                    onLoadedMetadata={() => setVideoReady(true)}
                    className="absolute inset-0 w-full h-full object-cover"
                  />
                  <Camera className={`w-16 h-16 text-gray-300 transition-opacity duration-300 ${cameraOn ? 'opacity-0' : 'opacity-100'}`} />
                  <div className="absolute inset-4 pointer-events-none">
                    <div className="absolute top-0 left-0 w-6 h-6 border-t-2 border-l-2 border-[#0057B8]/40 rounded-tl-lg" />
                    <div className="absolute top-0 right-0 w-6 h-6 border-t-2 border-r-2 border-[#0057B8]/40 rounded-tr-lg" />
                    <div className="absolute bottom-0 left-0 w-6 h-6 border-b-2 border-l-2 border-[#0057B8]/40 rounded-bl-lg" />
                    <div className="absolute bottom-0 right-0 w-6 h-6 border-b-2 border-r-2 border-[#0057B8]/40 rounded-br-lg" />
                  </div>
                </div>

                <p className="text-base text-gray-500 mb-4">Position your face within the frame</p>

                {faceResult?.identified ? (
                  <div className="mb-4 p-3 bg-green-50 border border-green-200 rounded-xl">
                    <p className="text-green-700 font-medium">Welcome back, {faceResult.name}!</p>
                    <p className="text-sm text-green-600 mt-1">
                      Confidence: {((1 - (faceResult.distance || 0)) * 100).toFixed(1)}%
                    </p>
                    <p className="text-sm text-gray-500 mt-2">Logging you in...</p>
                  </div>
                ) : (
                  <button
                    onClick={handleFaceLogin}
                    disabled={loading || !cameraOn || !videoReady}
                    className="w-full py-4 rounded-xl bg-[#0057B8] text-white text-base font-semibold hover:bg-[#003D82] active:scale-95 transition-all flex items-center justify-center gap-2 touch-target-large disabled:opacity-50"
                  >
                    {loading ? (
                      <>
                        <Loader2 className="w-5 h-5 animate-spin" />
                        {t('common.loading')}
                      </>
                    ) : (
                      <>
                        <ScanFace className="w-5 h-5" />
                        Login with Face
                      </>
                    )}
                  </button>
                )}
              </div>
            ) : (
              <form onSubmit={handleEmailLogin} className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1.5">Email</label>
                  <div className="relative">
                    <Mail className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
                    <input
                      type="email"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      className="w-full pl-10 pr-4 py-4 rounded-xl bg-gray-50 border border-gray-200 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#0057B8]/30 focus:border-[#0057B8] transition-all min-h-[48px]"
                      placeholder="your@email.com"
                      required
                    />
                  </div>
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1.5">Password</label>
                  <div className="relative">
                    <Lock className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
                    <input
                      type="password"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      className="w-full pl-10 pr-4 py-4 rounded-xl bg-gray-50 border border-gray-200 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#0057B8]/30 focus:border-[#0057B8] transition-all min-h-[48px]"
                      placeholder="••••••••"
                      required
                    />
                  </div>
                </div>
                <button
                  type="submit"
                  disabled={loading}
                  className="w-full py-4 rounded-xl bg-[#0057B8] text-white text-base font-semibold hover:bg-[#003D82] active:scale-95 transition-all flex items-center justify-center gap-2 touch-target-large disabled:opacity-50"
                >
                  {loading ? (
                    <><Loader2 className="w-5 h-5 animate-spin" />{t('common.loading')}</>
                  ) : (
                    <><LogIn className="w-5 h-5" />Sign In</>
                  )}
                </button>
              </form>
            )}

            {error && (
              <div className="mt-4 p-3 bg-red-50 border border-red-200 rounded-xl text-base text-red-600">
                {error}
              </div>
            )}
          </div>

          <p className="text-center text-base text-gray-500 mt-6">
            {t('register.noAccount')}{' '}
            <Link to="/register" className="text-[#0057B8] hover:text-[#003D82] font-medium transition-colors">
              {t('register.link')}
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
