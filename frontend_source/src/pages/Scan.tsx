import { useState, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { getFaceToken } from '../lib/face-auth';
import { aiApi } from '../lib/ai-api';
import {
  Camera,
  RotateCcw,
  Check,
  Scan as ScanIcon,
  Loader2,
  Pill,
  AlertTriangle,
  SwitchCamera,
  Clock,
  FileText,
  Building2,
  X,
  Zap,
} from 'lucide-react';

interface OCRResult {
  med_name: string;
  dosage: string;
  quantity: string;
  pill_count: number | null;
  amount_each_intake: string;
  total_intake: string;
  schedule_time: Record<string, boolean>;
  instructions: string;
  warning: string;
  pill_description: string;
  intake_time_label: string;
  clinical_uses: string;
  manufacturer: string;
  hospital: string;
  prescription_no: string;
  use_before: string;
  physician: string;
  pharmacist: string;
  patient_name: string;
  date_dispensed: string;
  error?: string;
}

const SCHEDULE_LABELS: Record<string, string> = {
  morning: '早上',
  noon: '中午',
  night: '晚上',
  bedtime: '睡前',
  before_meals: '飯前',
  after_meals: '飯後',
};

function getAuthHeaders(): Record<string, string> {
  const token = getFaceToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export default function Scan() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const videoRef = useRef<HTMLVideoElement>(null);
  const [stream, setStream] = useState<MediaStream | null>(null);
  const [facingMode, setFacingMode] = useState<'environment' | 'user'>('environment');
  const [capturedImage, setCapturedImage] = useState<string | null>(null);
  const [parsing, setParsing] = useState(false);
  const [parsed, setParsed] = useState<OCRResult | null>(null);
  const [parseError, setParseError] = useState('');
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [flashOn, setFlashOn] = useState(false);

  const startCamera = async (mode: 'environment' | 'user' = facingMode) => {
    if (stream) {
      stream.getTracks().forEach((t) => t.stop());
      setStream(null);
    }
    try {
      const s = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: mode },
      });
      setStream(s);
      if (videoRef.current) videoRef.current.srcObject = s;
    } catch {
      alert('Camera access denied or not available');
    }
  };

  const switchCamera = async () => {
    const next = facingMode === 'environment' ? 'user' : 'environment';
    setFacingMode(next);
    await startCamera(next);
  };

  const capture = () => {
    if (!videoRef.current) return;
    const canvas = document.createElement('canvas');
    canvas.width = videoRef.current.videoWidth;
    canvas.height = videoRef.current.videoHeight;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.drawImage(videoRef.current, 0, 0);
    setCapturedImage(canvas.toDataURL('image/jpeg'));
    stream?.getTracks().forEach((t) => t.stop());
    setStream(null);
  };

  const retake = () => {
    setCapturedImage(null);
    setParsed(null);
    setParseError('');
    setSaved(false);
    startCamera();
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

  const parseImage = async () => {
    if (!capturedImage) return;
    setParsing(true);
    setParseError('');
    setParsed(null);

    const file = dataUrlToFile(capturedImage, 'prescription.jpg');
    const result = await aiApi.parsePrescription(file);

    if (result.error) {
      setParseError(result.error);
    } else {
      setParsed(result as OCRResult);
    }
    setParsing(false);
  };

  const addToMedications = async () => {
    if (!parsed) return;
    setSaving(true);
    const headers = getAuthHeaders();
    if (!headers.Authorization) {
      setParseError('Not logged in');
      setSaving(false);
      return;
    }

    const nA = (v: string | undefined) => (!v || v === 'N/A' ? undefined : v);

    const payload = {
      name: parsed.med_name || 'Unknown',
      dosage: nA(parsed.dosage),
      total_pills: Number(parsed.pill_count) || 0,
      pills_remaining: Number(parsed.pill_count) || 0,
      instructions: nA(parsed.instructions),
      warning: nA(parsed.warning),
      pill_description: nA(parsed.pill_description),
      use_before: nA(parsed.use_before),
      schedule_time: parsed.schedule_time,
      prescription_meta: {
        clinical_uses: nA(parsed.clinical_uses),
        manufacturer: nA(parsed.manufacturer),
        hospital: nA(parsed.hospital),
        prescription_no: nA(parsed.prescription_no),
        physician: nA(parsed.physician),
        pharmacist: nA(parsed.pharmacist),
        patient_name: nA(parsed.patient_name),
        date_dispensed: nA(parsed.date_dispensed),
        quantity: nA(parsed.quantity),
        amount_each_intake: nA(parsed.amount_each_intake),
        total_intake_calc: nA(parsed.total_intake),
        intake_time_label: nA(parsed.intake_time_label),
      },
    };

    const res = await fetch('/api/medications', {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (res.ok) {
      setSaved(true);
    } else {
      const err = await res.json().catch(() => ({}));
      setParseError(err.detail || 'Failed to save medication');
    }
    setSaving(false);
  };

  const activeSchedule = parsed
    ? Object.entries(parsed.schedule_time || {}).filter(([, v]) => v)
    : [];

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-semibold text-gray-900">{t('scan.title')}</h2>
        <button
          onClick={() => navigate('/dashboard')}
          className="p-2 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded-lg transition-colors"
        >
          <X className="w-5 h-5" />
        </button>
      </div>

      {/* Camera / Preview */}
      <div className="relative aspect-[4/3] bg-gray-900 rounded-2xl overflow-hidden shadow-sm hover:shadow-md transition-all duration-300">
        {!capturedImage ? (
          <>
            <video
              ref={videoRef}
              autoPlay
              playsInline
              className="w-full h-full object-cover"
            />

            {/* No stream yet — start prompt */}
            {!stream && (
              <div className="absolute inset-0 flex flex-col items-center justify-center text-white">
                <Camera className="w-12 h-12 mb-3 opacity-60" />
                <p className="text-base opacity-80 mb-4">{t('scan.instructions')}</p>
                <button
                  onClick={() => startCamera()}
                  className="px-6 py-4 bg-[#0057B8] rounded-xl text-base font-medium hover:bg-[#003D82] active:scale-95 transition-all flex items-center gap-2 touch-target-large"
                >
                  <ScanIcon className="w-4 h-4" />
                  {t('scan.capture')}
                </button>
              </div>
            )}

            {/* Stream active */}
            {stream && (
              <>
                {/* Corner brackets for alignment */}
                <div className="absolute inset-6 pointer-events-none">
                  <div className="absolute top-0 left-0 w-8 h-8 border-t-2 border-l-2 border-white/60 rounded-tl-lg" />
                  <div className="absolute top-0 right-0 w-8 h-8 border-t-2 border-r-2 border-white/60 rounded-tr-lg" />
                  <div className="absolute bottom-0 left-0 w-8 h-8 border-b-2 border-l-2 border-white/60 rounded-bl-lg" />
                  <div className="absolute bottom-0 right-0 w-8 h-8 border-b-2 border-r-2 border-white/60 rounded-br-lg" />
                </div>

                {/* Instruction overlay */}
                <div className="absolute top-16 left-0 right-0 text-center">
                  <p className="text-white text-base font-medium bg-black/30 inline-block px-4 py-1.5 rounded-full backdrop-blur-sm">
                    Align prescription within frame
                  </p>
                </div>

                {/* Top controls */}
                <div className="absolute top-4 left-4 right-4 flex justify-between">
                  <button
                    onClick={() => navigate('/dashboard')}
                    className="w-10 h-10 bg-black/40 backdrop-blur-sm rounded-full flex items-center justify-center text-white hover:bg-black/60 transition-colors"
                  >
                    <X className="w-5 h-5" />
                  </button>
                  <div className="flex gap-2">
                    <button
                      onClick={() => setFlashOn(!flashOn)}
                      className={`w-10 h-10 backdrop-blur-sm rounded-full flex items-center justify-center transition-colors ${
                        flashOn ? 'bg-[#0057B8] text-white' : 'bg-black/40 text-white hover:bg-black/60'
                      }`}
                    >
                      <Zap className="w-5 h-5" />
                    </button>
                    <button
                      onClick={switchCamera}
                      className="w-10 h-10 bg-black/40 backdrop-blur-sm rounded-full flex items-center justify-center text-white hover:bg-black/60 transition-colors"
                    >
                      <SwitchCamera className="w-5 h-5" />
                    </button>
                  </div>
                </div>

                {/* Capture shutter */}
                <div className="absolute bottom-6 left-0 right-0 flex justify-center">
                  <button
                    onClick={capture}
                    className="w-16 h-16 bg-white rounded-full border-4 border-[#0057B8] flex items-center justify-center shadow-lg active:scale-95 transition-transform"
                  >
                    <div className="w-12 h-12 bg-[#0057B8] rounded-full" />
                  </button>
                </div>
              </>
            )}
          </>
        ) : (
          <img src={capturedImage} alt="Captured" className="w-full h-full object-cover" />
        )}
      </div>

      {/* Post-capture actions */}
      {capturedImage && !parsed && !parsing && (
        <div className="flex gap-3">
          <button
            onClick={retake}
            className="flex-1 py-4 bg-gray-100 text-gray-700 text-base rounded-xl font-medium hover:bg-gray-200 active:scale-95 transition-all flex items-center justify-center gap-2 touch-target-large"
          >
            <RotateCcw className="w-4 h-4" />
            {t('scan.retake')}
          </button>
          <button
            onClick={parseImage}
            className="flex-1 py-4 bg-[#0057B8] text-white text-base rounded-xl font-medium hover:bg-[#003D82] active:scale-95 transition-all flex items-center justify-center gap-2 touch-target-large"
          >
            <ScanIcon className="w-4 h-4" />
            {t('scan.confirm')}
          </button>
        </div>
      )}

      {/* Parsing spinner */}
      {parsing && (
        <div className="flex flex-col items-center py-8 gap-3">
          <Loader2 className="w-8 h-8 text-[#0057B8] animate-spin" />
          <p className="text-gray-600 text-base">{t('scan.parsing')}</p>
        </div>
      )}

      {/* Error */}
      {parseError && (
        <div className="p-4 bg-red-50 border border-red-200 rounded-xl text-base text-red-600 flex items-center gap-2">
          <AlertTriangle className="w-4 h-4 shrink-0" />
          {parseError}
        </div>
      )}

      {/* Parsed Result */}
      {parsed && !parsing && (
        <div className="bg-white rounded-2xl border border-gray-100 shadow-sm hover:shadow-md transition-all duration-300 hover:border-gray-200 divide-y divide-gray-100">

          {/* Header — medication name */}
          <div className="p-5 flex items-start gap-3">
            <Pill className="w-5 h-5 text-[#0057B8] mt-0.5 shrink-0" />
            <div>
              <p className="text-sm text-gray-400 uppercase tracking-wide mb-0.5">Medication</p>
              <p className="font-semibold text-gray-900 text-base">{parsed.med_name || 'Unknown'}</p>
              {parsed.dosage && parsed.dosage !== 'N/A' && (
                <p className="text-sm text-[#0057B8] font-medium mt-0.5">{parsed.dosage}</p>
              )}
            </div>
          </div>

          {/* Dosage & Quantity */}
          <div className="px-5 py-4 grid grid-cols-2 gap-4">
            <div>
              <p className="text-sm text-gray-400 uppercase tracking-wide mb-1">Quantity</p>
              <p className="text-sm font-medium text-gray-900">{parsed.quantity || 'N/A'}</p>
            </div>
            <div>
              <p className="text-sm text-gray-400 uppercase tracking-wide mb-1">Per Dose</p>
              <p className="text-sm font-medium text-gray-900">{parsed.amount_each_intake || 'N/A'}</p>
            </div>
            <div>
              <p className="text-sm text-gray-400 uppercase tracking-wide mb-1">Total</p>
              <p className="text-sm font-medium text-gray-900">{parsed.total_intake || 'N/A'}</p>
            </div>
            {parsed.pill_description && parsed.pill_description !== 'N/A' && (
              <div>
                <p className="text-sm text-gray-400 uppercase tracking-wide mb-1">Appearance</p>
                <p className="text-sm font-medium text-gray-900">{parsed.pill_description}</p>
              </div>
            )}
          </div>

          {/* Schedule */}
          {activeSchedule.length > 0 && (
            <div className="px-5 py-4">
              <div className="flex items-center gap-2 mb-2">
                <Clock className="w-4 h-4 text-[#0057B8]" />
                <p className="text-sm text-gray-400 uppercase tracking-wide">Schedule</p>
              </div>
              <div className="flex flex-wrap gap-2">
                {activeSchedule.map(([key]) => (
                  <span
                    key={key}
                    className="px-2.5 py-1 bg-blue-50 text-[#0057B8] text-2xs font-medium rounded-full"
                  >
                    {SCHEDULE_LABELS[key] || key}
                  </span>
                ))}
              </div>
              {parsed.instructions && parsed.instructions !== 'N/A' && (
                <p className="text-sm text-gray-500 mt-2">{parsed.instructions}</p>
              )}
            </div>
          )}

          {/* Warning */}
          {parsed.warning && parsed.warning !== 'N/A' && (
            <div className="px-5 py-4">
              <div className="p-3 bg-amber-50 border border-amber-200 rounded-xl">
                <div className="flex items-start gap-2">
                  <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
                  <p className="text-base text-amber-800">{parsed.warning}</p>
                </div>
              </div>
            </div>
          )}

          {/* Clinical uses */}
          {parsed.clinical_uses && parsed.clinical_uses !== 'N/A' && (
            <div className="px-5 py-4">
              <div className="flex items-center gap-2 mb-1">
                <FileText className="w-4 h-4 text-[#0057B8]" />
                <p className="text-sm text-gray-400 uppercase tracking-wide">Clinical Uses</p>
              </div>
              <p className="text-base text-gray-600">{parsed.clinical_uses}</p>
            </div>
          )}

          {/* Prescription meta */}
          <div className="px-5 py-4 space-y-2">
            <div className="flex items-center gap-2 mb-2">
              <Building2 className="w-4 h-4 text-[#0057B8]" />
              <p className="text-sm text-gray-400 uppercase tracking-wide">Prescription Info</p>
            </div>
            {[
              ['Hospital', parsed.hospital],
              ['Prescription No.', parsed.prescription_no],
              ['Patient', parsed.patient_name],
              ['Physician', parsed.physician],
              ['Pharmacist', parsed.pharmacist],
              ['Date Dispensed', parsed.date_dispensed],
              ['Use Before', parsed.use_before],
              ['Manufacturer', parsed.manufacturer],
            ]
              .filter(([, v]) => v && v !== 'N/A')
              .map(([label, value]) => (
                <div key={label} className="flex justify-between text-sm">
                  <span className="text-gray-500">{label}</span>
                  <span className="text-gray-900 font-medium text-right max-w-[60%]">{value}</span>
                </div>
              ))}
          </div>

          {/* Actions */}
          <div className="p-5 flex gap-3">
            {saved ? (
              <div className="flex-1 py-4 bg-green-50 text-green-700 text-base rounded-xl font-medium flex items-center justify-center gap-2 touch-target-large">
                <Check className="w-4 h-4" />
                Added to Medications
              </div>
            ) : (
              <button
                onClick={addToMedications}
                disabled={saving}
                className="flex-1 py-4 bg-[#0057B8] text-white text-base rounded-xl font-medium hover:bg-[#003D82] active:scale-95 transition-all flex items-center justify-center gap-2 touch-target-large disabled:opacity-60"
              >
                {saving ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Check className="w-4 h-4" />
                )}
                {t('scan.addToMeds')}
              </button>
            )}
            <button
              onClick={retake}
              className="flex-1 py-4 bg-gray-100 text-gray-700 text-base rounded-xl font-medium hover:bg-gray-200 active:scale-95 transition-all touch-target-large"
            >
              {t('scan.scanAnother')}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
