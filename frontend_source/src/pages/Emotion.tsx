import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { getFaceToken } from '../lib/face-auth';
import { aiApi } from '../lib/ai-api';
import { Smile, Frown, Meh, Angry, Sparkles, ShieldAlert, TrendingUp, Camera, ScanFace, Loader2, AlertTriangle, CheckCircle2, XCircle } from 'lucide-react';

type EmotionLabel = 'happy' | 'sad' | 'angry' | 'neutral' | 'surprised' | 'disgust';

interface EmotionRecord {
  id: number;
  emotion_type: EmotionLabel;
  emotion_score: number;
  recorded_at: string;
}

function getAuthHeaders(): Record<string, string> {
  const token = getFaceToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

const emotions = [
  { type: 'happy' as const, label: 'happy', icon: Smile, color: 'bg-green-50 text-green-600 border-green-200', bar: 'bg-green-400' },
  { type: 'neutral' as const, label: 'neutral', icon: Meh, color: 'bg-gray-50 text-gray-600 border-gray-200', bar: 'bg-gray-400' },
  { type: 'sad' as const, label: 'sad', icon: Frown, color: 'bg-blue-50 text-blue-600 border-blue-200', bar: 'bg-blue-400' },
  { type: 'angry' as const, label: 'angry', icon: Angry, color: 'bg-red-50 text-red-600 border-red-200', bar: 'bg-red-400' },
  { type: 'surprised' as const, label: 'surprised', icon: Sparkles, color: 'bg-amber-50 text-amber-700 border-amber-200', bar: 'bg-amber-400' },
  { type: 'disgust' as const, label: 'disgust', icon: ShieldAlert, color: 'bg-purple-50 text-purple-700 border-purple-200', bar: 'bg-purple-400' },
];

export default function Emotion() {
  const { t } = useTranslation();
  const videoRef = useRef<HTMLVideoElement>(null);
  const [history, setHistory] = useState<EmotionRecord[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [note, setNote] = useState('');
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [aiMode, setAiMode] = useState(false);
  const [cameraOn, setCameraOn] = useState(false);
  const [aiLoading, setAiLoading] = useState(false);
  const [aiError, setAiError] = useState('');
  const [aiResult, setAiResult] = useState<{ emotion_type?: string; emotion_score?: number; probabilities?: Record<string, number> } | null>(null);
  const [alertStatus, setAlertStatus] = useState<'sent' | 'failed' | null>(null);

  useEffect(() => {
    fetchHistory();
    return () => {
      stopCamera();
    };
  }, []);

  useEffect(() => {
    if (aiMode && !cameraOn) {
      startCamera();
    } else if (!aiMode) {
      stopCamera();
    }
  }, [aiMode, cameraOn]);

  const startCamera = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user' } });
      setCameraOn(true);
      if (videoRef.current) {
        videoRef.current.srcObject = stream;
      }
    } catch {
      setAiError('Camera access denied or not available');
    }
  };

  const stopCamera = () => {
    const stream = videoRef.current?.srcObject as MediaStream | null;
    stream?.getTracks().forEach((t) => t.stop());
    setCameraOn(false);
  };

  const handleAiDetect = async () => {
    if (!videoRef.current || !cameraOn) return;
    setAiLoading(true);
    setAiError('');
    setAiResult(null);

    try {
      const canvas = document.createElement('canvas');
      canvas.width = videoRef.current.videoWidth;
      canvas.height = videoRef.current.videoHeight;
      const ctx = canvas.getContext('2d');
      if (!ctx) throw new Error('Canvas not available');
      ctx.drawImage(videoRef.current, 0, 0);

      const blob = await new Promise<Blob>((resolve) =>
        canvas.toBlob((b) => resolve(b!), 'image/jpeg', 0.9)
      );
      const file = new File([blob], 'emotion.jpg', { type: 'image/jpeg' });
      const result = await aiApi.analyzeEmotion(file);

      if (result.error) {
        setAiError(result.error);
      } else if (result.detected && result.emotion_type) {
        setAiResult(result);
        setSelected(result.emotion_type);
      } else {
        setAiError('No face detected. Please try again.');
      }
    } catch (e: unknown) {
      setAiError(e instanceof Error ? e.message : 'Emotion detection failed');
    }
    setAiLoading(false);
  };

  const fetchHistory = async () => {
    setLoading(true);
    const res = await fetch('/api/emotion/history', { headers: getAuthHeaders() });
    if (res.ok) {
      const data = await res.json() as EmotionRecord[];
      setHistory(data.map((r) => ({ ...r, recorded_at: r.recorded_at || new Date().toISOString() })));
    }
    setLoading(false);
  };

  const handleSubmit = async () => {
    if (!selected) return;
    setSubmitting(true);
    setAlertStatus(null);
    const score = aiResult?.emotion_score ?? 0.5;
    const res = await fetch('/api/emotion/log', {
      method: 'POST',
      headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ emotion_type: selected, emotion_score: score, note: note || '' }),
    });
    if (!res.ok) {
      setAlertStatus('failed');
    } else {
      // Backend handles LINE notifications server-side
      setAlertStatus(['sad', 'angry', 'disgust'].includes(selected) ? 'sent' : null);
    }
    setSelected(null);
    setNote('');
    setAiResult(null);
    setAiMode(false);
    fetchHistory();
    setSubmitting(false);
  };

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
      <h2 className="text-2xl font-bold text-gray-900">{t('emotion.title')}</h2>

      {/* Mode Toggle */}
      <div className="flex gap-2">
        <button
          onClick={() => { setAiMode(false); setAiError(''); setAiResult(null); }}
          className={`flex-1 py-3 rounded-xl text-base font-medium transition-all ${
            !aiMode ? 'bg-[#0057B8] text-white' : 'bg-white border border-gray-200 text-gray-600'
          }`}
        >
          {t('emotion.manual')}
        </button>
        <button
          onClick={() => { setAiMode(true); setSelected(null); setAiError(''); }}
          className={`flex-1 py-3 rounded-xl text-base font-medium transition-all flex items-center justify-center gap-2 ${
            aiMode ? 'bg-[#0057B8] text-white' : 'bg-white border border-gray-200 text-gray-600'
          }`}
        >
          <ScanFace className="w-4 h-4" />
          {t('emotion.aiDetect')}
        </button>
      </div>

      {aiMode ? (
        <div className="space-y-4">
          <div className="relative aspect-[4/3] bg-gray-900 rounded-2xl overflow-hidden">
            <video
              ref={videoRef}
              autoPlay
              playsInline
              muted
              className="w-full h-full object-cover"
            />
            {!cameraOn && (
              <div className="absolute inset-0 flex flex-col items-center justify-center text-white">
                <Camera className="w-12 h-12 mb-3 opacity-60" />
                <p className="text-base opacity-80">Camera starting...</p>
              </div>
            )}
            <div className="absolute inset-0 border-2 border-[#0057B8]/40 rounded-xl opacity-50 pointer-events-none" />
          </div>

          <button
            onClick={handleAiDetect}
            disabled={aiLoading || !cameraOn}
            className="w-full py-4 bg-[#0057B8] text-white text-base rounded-xl font-medium hover:bg-[#003D82] active:scale-95 transition-all disabled:opacity-50 flex items-center justify-center gap-2 touch-target-large"
          >
            {aiLoading ? <Loader2 className="w-5 h-5 animate-spin" /> : <ScanFace className="w-5 h-5" />}
            {aiLoading ? t('common.loading') : t('emotion.detectEmotion')}
          </button>

          {aiError && (
            <div className="p-3 bg-red-50 border border-red-200 rounded-lg text-base text-red-600 flex items-center gap-2">
              <AlertTriangle className="w-4 h-4" />
              {aiError}
            </div>
          )}

          {aiResult && (
            <div className="p-4 bg-blue-50 border border-blue-200 rounded-xl">
              <p className="font-medium text-gray-900">
                Detected: {t(`emotion.${(aiResult.emotion_type || '').toLowerCase()}`)}
              </p>
              <p className="text-base text-gray-600">
                Confidence: {((aiResult.emotion_score || 0) * 100).toFixed(1)}%
              </p>
            </div>
          )}
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-3">
          {emotions.map((e) => {
            const Icon = e.icon;
            const isSelected = selected === e.type;
            return (
              <button
                key={e.type}
                onClick={() => setSelected(e.type)}
                className={`p-4 rounded-xl border-2 flex flex-col items-center gap-2 transition-all active:scale-95 ${
                  isSelected ? `${e.color} border-current` : 'bg-white border-gray-200 hover:border-gray-300'
                }`}
              >
                <Icon className={`w-8 h-8 ${isSelected ? 'scale-110' : ''} transition-transform`} />
                <span className="font-medium">{t(`emotion.${e.label}`)}</span>
              </button>
            );
          })}
        </div>
      )}

      {selected && (
        <div className="space-y-3">
          <div>
            <label className="block text-base font-medium text-gray-700 mb-1">{t('emotion.note')}</label>
            <textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              className="w-full px-4 py-2.5 border border-gray-300 rounded-xl focus:ring-2 focus:ring-[#0057B8]/30 focus:border-[#0057B8] outline-none bg-white"
              rows={3}
              placeholder="..."
            />
          </div>
          <button
            onClick={handleSubmit}
            disabled={submitting}
            className="w-full py-4 bg-[#0057B8] text-white text-base rounded-xl font-medium hover:bg-[#003D82] active:scale-95 transition-all disabled:opacity-50 touch-target-large"
          >
            {submitting ? t('common.loading') : t('emotion.submit')}
          </button>
        </div>
      )}

      {/* Alert status */}
      {alertStatus === 'sent' && (
        <div className="p-3 bg-green-50 border border-green-200 rounded-lg text-base text-green-700 flex items-center gap-2">
          <CheckCircle2 className="w-4 h-4" />
          {t('emotion.lowEmotionWarning')}
        </div>
      )}
      {alertStatus === 'failed' && (
        <div className="p-3 bg-orange-50 border border-orange-200 rounded-lg text-base text-orange-700 flex items-center gap-2">
          <XCircle className="w-4 h-4" />
          {t('emotion.alertFailed')}
        </div>
      )}

      {/* History Chart */}
      {history.length > 0 && (
        <div className="bg-white rounded-2xl p-5 border border-gray-100 shadow-sm hover:shadow-md transition-all duration-300 hover:border-gray-200">
          <div className="flex items-center justify-between mb-4">
            <h3 className="font-semibold text-gray-900 flex items-center gap-2">
              <TrendingUp className="w-5 h-5 text-[#0057B8]" />
              {t('emotion.history')}
            </h3>
          </div>
          <div className="flex items-end gap-2 h-32">
            {[...history].reverse().map((record) => {
              const emotion = emotions.find((e) => e.type === record.emotion_type);
              const height = Math.max(20, record.emotion_score * 100);
              return (
                <div
                  key={record.id}
                  className="flex-1 flex flex-col items-center gap-1"
                >
                  <div
                    className={`w-full rounded-t-lg ${emotion?.bar || 'bg-gray-400'} transition-all`}
                    style={{ height: `${height}%` }}
                  />
                  <span className="text-2xs text-gray-400">
                    {new Date(record.recorded_at).toLocaleDateString('zh-TW', { month: 'numeric', day: 'numeric' })}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
