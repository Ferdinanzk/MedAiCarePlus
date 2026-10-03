import { useState, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { getFaceToken } from '../lib/face-auth';
import { aiApi } from '../lib/ai-api';
import { getExpiryStatus } from '../lib/expiry';
import { forgetRefusals } from '../lib/doses';
import {
  DOSE_FORMS, SLOTS, TIME_SLOTS, autoRecordable, courseStart, draftFrom, draftProblem, isoDate, markDuplicates,
  medicationPayload, medicinesOf, na, saveErrorKey, withAsNeeded, withDays, withSlot, withTimesPerDay,
  withoutCustomTime,
} from '../lib/scan';
import type { DoseForm, MedicineDraft, ScanResult } from '../lib/scan';
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
  Building2,
  X,
  Zap,
  Info,
} from 'lucide-react';

const INPUT = 'w-full px-3 py-2.5 rounded-xl bg-gray-50 border border-gray-200 text-gray-900 text-base focus:outline-none focus:ring-2 focus:ring-[#0057B8]/30 focus:border-[#0057B8] transition-all disabled:opacity-60';
const LABEL = 'block text-sm text-gray-500 mb-1';

function getAuthHeaders(): Record<string, string> {
  const token = getFaceToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** Names of the user's active medicines, or [] when the list can't be read (the check is a help, not a gate). */
async function activeMedicineNames(headers: Record<string, string>): Promise<string[]> {
  if (!headers.Authorization) return [];
  try {
    const res = await fetch('/api/medications', { headers });
    if (!res.ok) return [];
    const list: unknown = await res.json();
    if (!Array.isArray(list)) return [];
    return list
      .filter((med) => med && typeof med === 'object' && med.is_active !== false && typeof med.name === 'string')
      .map((med) => med.name as string);
  } catch {
    return [];
  }
}

export default function Scan() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const videoRef = useRef<HTMLVideoElement>(null);
  const [stream, setStream] = useState<MediaStream | null>(null);
  const [facingMode, setFacingMode] = useState<'environment' | 'user'>('environment');
  const [capturedImage, setCapturedImage] = useState<string | null>(null);
  const [parsing, setParsing] = useState(false);
  const [parsed, setParsed] = useState<ScanResult | null>(null);
  const [drafts, setDrafts] = useState<MedicineDraft[]>([]);
  const [parseError, setParseError] = useState('');
  const [saving, setSaving] = useState(false);
  const [flashOn, setFlashOn] = useState(false);

  const startCamera = async (mode: 'environment' | 'user' = facingMode) => {
    if (stream) {
      stream.getTracks().forEach((t) => t.stop());
      setStream(null);
    }
    try {
      // Ask for more than the usual 640x480 default: small print on a medicine bag needs the pixels.
      const s = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: mode, width: { ideal: 1920 }, height: { ideal: 1080 } },
      });
      setStream(s);
      if (videoRef.current) videoRef.current.srcObject = s;
    } catch {
      alert(t('scan.cameraUnavailable'));
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
    setDrafts([]);
    setParseError('');
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
    setDrafts([]);

    try {
      const file = dataUrlToFile(capturedImage, 'prescription.jpg');
      const result = await aiApi.parsePrescription(file);

      if (result.error) {
        // The server's text is English only; show the page's own words for every reason it gives.
        setParseError(result.code ? t(`scan.errors.${result.code}`, { defaultValue: result.error }) : result.error);
      } else {
        const scan = result as ScanResult;
        const today = isoDate(new Date());
        setParsed(scan);
        setDrafts(medicinesOf(scan).map((med) => draftFrom(med, scan, today)));
        // A medicine already in the list (the same paper scanned again) starts unticked, with a note.
        const existing = await activeMedicineNames(getAuthHeaders());
        if (existing.length > 0) setDrafts((current) => markDuplicates(current, existing));
      }
    } catch (error) {
      const timedOut = error instanceof Error && ['AbortError', 'TimeoutError'].includes(error.name);
      setParseError(t(timedOut ? 'scan.timeout' : 'scan.connectionFailed'));
    } finally {
      setParsing(false);
    }
  };

  const updateDraft = (index: number, change: (draft: MedicineDraft) => MedicineDraft) => {
    setDrafts((current) => current.map((draft, i) => (i === index ? change(draft) : draft)));
  };

  const today = isoDate(new Date());
  const selected = drafts.filter((draft) => draft.include && draft.status !== 'saved');
  const savedCount = drafts.filter((draft) => draft.status === 'saved').length;
  const allSaved = drafts.length > 0 && savedCount > 0 && drafts.every((d) => d.status === 'saved' || !d.include);

  const addSelected = async () => {
    if (!parsed) return;
    const headers = getAuthHeaders();
    if (!headers.Authorization) {
      setParseError(t('scan.notLoggedIn'));
      return;
    }
    if (selected.length === 0) {
      setParseError(t('scan.noneSelected'));
      return;
    }
    // Problems are shown on their cards; nothing is saved until every kept medicine can be.
    if (drafts.some((draft) => draft.include && draft.status !== 'saved' && draftProblem(draft, today))) {
      setParseError(t('scan.fixProblems'));
      return;
    }
    setParseError('');
    setSaving(true);
    // Look again just before saving: a save whose answer was lost may have stored the medicine after all.
    let current = drafts;
    const existing = await activeMedicineNames(headers);
    if (existing.length > 0) {
      const checked = markDuplicates(current, existing);
      if (checked.some((draft, index) => draft.duplicate && !current[index].duplicate)) {
        setParseError(t('scan.duplicatesFound'));
      }
      current = checked;
      setDrafts(checked);
    }
    for (let index = 0; index < current.length; index++) {
      const draft = current[index];
      if (!draft.include || draft.status === 'saved') continue;
      updateDraft(index, (d) => ({ ...d, status: 'saving', errorKey: undefined }));
      let status: MedicineDraft['status'] = 'failed';
      let errorKey: string | undefined;
      try {
        const res = await fetch('/api/medications', {
          method: 'POST',
          headers: { ...headers, 'Content-Type': 'application/json' },
          body: JSON.stringify(medicationPayload(draft, parsed)),
        });
        if (res.ok) {
          status = 'saved';
        } else {
          const err = await res.json().catch(() => ({}));
          // The server's text can be an English database message; it goes to the console, the card says why in
          // the page's language.
          console.warn('Saving a scanned medicine failed', res.status, err?.detail);
          errorKey = saveErrorKey(res.status, err?.detail);
        }
      } catch (error) {
        console.warn('Saving a scanned medicine failed', error);
        errorKey = saveErrorKey(null, undefined);
      }
      updateDraft(index, (d) => ({ ...d, status, errorKey }));
    }
    // New medicines change which doses the server refuses.
    forgetRefusals();
    setSaving(false);
  };

  const start = parsed ? courseStart(parsed, today) : today;
  // A note printed once for the whole paper (◎請按時服藥) is shown once, above the cards, not on every card.
  const warnings = drafts.map((draft) => na(draft.source.warning));
  const sharedWarning = drafts.length > 1 && warnings[0] && warnings.every((w) => w === warnings[0])
    ? warnings[0]
    : undefined;

  const problemText = (draft: MedicineDraft) => {
    const problem = draftProblem(draft, today);
    return problem ? t(`scan.problems.${problem}`) : null;
  };

  const renderDraft = (draft: MedicineDraft, index: number) => {
    const locked = draft.status === 'saved' || draft.status === 'saving' || saving;
    const med = draft.source;
    const printed = [na(med.quantity), na(med.frequency_text ?? med.instructions), med.days ? t('scan.daysCount', { count: med.days }) : undefined]
      .filter(Boolean)
      .join(' · ');
    const timeCount = TIME_SLOTS.filter((slot) => draft.slots[slot]).length + draft.customTimes.length;
    const problem = draft.include && draft.status !== 'saved' ? problemText(draft) : null;
    const ended = draft.lastDay !== '' && draft.lastDay < today;
    return (
      <div
        key={index}
        className={`bg-white rounded-2xl border shadow-sm p-4 space-y-3 transition-all ${
          draft.include ? 'border-gray-200' : 'border-gray-100 opacity-70'
        }`}
      >
        <div className="flex items-start justify-between gap-3">
          <label className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox"
              className="w-5 h-5 accent-[#0057B8]"
              checked={draft.include}
              disabled={locked}
              onChange={(e) => updateDraft(index, (d) => ({ ...d, include: e.target.checked }))}
            />
            <span className="text-base font-medium text-gray-900">{t('scan.include')}</span>
          </label>
          <span className="text-sm text-gray-400 shrink-0">{t('scan.medicineNumber', { n: index + 1 })}</span>
        </div>

        {draft.duplicate && draft.status !== 'saved' && (
          <div className="p-3 bg-amber-50 border border-amber-200 rounded-xl flex items-start gap-2">
            <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
            <p className="text-sm text-amber-800">{t('scan.duplicate')}</p>
          </div>
        )}

        <div>
          <label className={LABEL} htmlFor={`scan-name-${index}`}>{t('medications.name')}</label>
          <input
            id={`scan-name-${index}`}
            className={INPUT}
            value={draft.name}
            disabled={locked}
            onChange={(e) => updateDraft(index, (d) => ({ ...d, name: e.target.value }))}
          />
          {printed && (
            <p className="text-sm text-gray-500 mt-1">
              {t('scan.onPaper')}: {printed}
            </p>
          )}
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={LABEL} htmlFor={`scan-dosage-${index}`}>{t('scan.strength')}</label>
            <input
              id={`scan-dosage-${index}`}
              className={INPUT}
              value={draft.dosage}
              disabled={locked}
              onChange={(e) => updateDraft(index, (d) => ({ ...d, dosage: e.target.value }))}
            />
          </div>
          <div>
            <label className={LABEL} htmlFor={`scan-form-${index}`}>{t('medications.doseForm')}</label>
            <select
              id={`scan-form-${index}`}
              className={INPUT}
              value={draft.doseForm}
              disabled={locked}
              onChange={(e) => updateDraft(index, (d) => ({ ...d, doseForm: e.target.value as DoseForm }))}
            >
              {DOSE_FORMS.map((form) => (
                <option key={form} value={form}>{t(`medications.doseForms.${form}`)}</option>
              ))}
            </select>
          </div>
          <div>
            <label className={LABEL} htmlFor={`scan-stock-${index}`}>
              {draft.doseForm === 'solid_oral' ? t('scan.stock') : t('scan.stockDoses')}
            </label>
            <input
              id={`scan-stock-${index}`}
              className={INPUT}
              type="number"
              inputMode="decimal"
              min={0}
              step="0.5"
              value={draft.stock}
              disabled={locked}
              onChange={(e) => updateDraft(index, (d) => ({ ...d, stock: e.target.value }))}
            />
          </div>
          <div>
            <label className={LABEL} htmlFor={`scan-units-${index}`}>{t('medications.unitsPerDose')}</label>
            <input
              id={`scan-units-${index}`}
              className={INPUT}
              type="number"
              inputMode="decimal"
              min={0.25}
              step="0.25"
              value={draft.unitsPerDose}
              disabled={locked}
              onChange={(e) => updateDraft(index, (d) => ({ ...d, unitsPerDose: e.target.value }))}
            />
          </div>
          {draft.asNeeded ? (
            <div>
              {/* As needed: the daily maximum for overdose protection; it never sets dose times or reminders. */}
              <label className={LABEL} htmlFor={`scan-max-${index}`}>{t('scan.maxDaily')}</label>
              <input
                id={`scan-max-${index}`}
                className={INPUT}
                type="number"
                inputMode="numeric"
                min={1}
                max={24}
                value={draft.maxDaily}
                disabled={locked}
                onChange={(e) => updateDraft(index, (d) => ({ ...d, maxDaily: e.target.value }))}
              />
            </div>
          ) : (
            <div>
              <label className={LABEL} htmlFor={`scan-times-${index}`}>{t('scan.timesPerDay')}</label>
              <input
                id={`scan-times-${index}`}
                className={INPUT}
                type="number"
                inputMode="numeric"
                min={1}
                max={8}
                value={draft.timesPerDay}
                disabled={locked}
                onChange={(e) => updateDraft(index, (d) => withTimesPerDay(d, e.target.value))}
              />
            </div>
          )}
          <div>
            <label className={LABEL} htmlFor={`scan-days-${index}`}>{t('scan.days')}</label>
            <input
              id={`scan-days-${index}`}
              className={INPUT}
              type="number"
              inputMode="numeric"
              min={1}
              max={365}
              value={draft.days}
              disabled={locked}
              onChange={(e) => updateDraft(index, (d) => withDays(d, e.target.value, start))}
            />
          </div>
        </div>

        <div>
          <div className="flex items-center gap-2 mb-2">
            <Clock className="w-4 h-4 text-[#0057B8]" />
            <span className="text-sm text-gray-500">{t('scan.whenToTake')}</span>
            {draft.asNeeded && (
              <span className="px-2.5 py-1 bg-amber-50 text-amber-800 text-sm rounded-full">
                {/^\d+$/.test(draft.maxDaily.trim()) && Number(draft.maxDaily) > 0
                  ? t('scan.asNeededMax', { count: Number(draft.maxDaily) })
                  : t('medications.asNeeded')}
              </span>
            )}
          </div>
          <label className="flex items-center gap-2 mb-2 text-base text-gray-700 cursor-pointer">
            <input
              type="checkbox"
              className="w-4 h-4 accent-[#0057B8]"
              checked={draft.asNeeded}
              disabled={locked}
              onChange={(e) => updateDraft(index, (d) => withAsNeeded(d, e.target.checked))}
            />
            {t('scan.asNeeded')}
          </label>
          <div className="grid grid-cols-3 gap-2">
            {SLOTS.map((slot) => (
              <label
                key={slot}
                className={`flex items-center gap-2 px-3 py-2 rounded-xl border text-base cursor-pointer ${
                  draft.slots[slot] ? 'bg-blue-50 border-[#0057B8] text-[#0057B8]' : 'bg-gray-50 border-gray-200 text-gray-700'
                }`}
              >
                <input
                  type="checkbox"
                  className="w-4 h-4 accent-[#0057B8]"
                  checked={draft.slots[slot]}
                  disabled={locked}
                  onChange={(e) => updateDraft(index, (d) => withSlot(d, slot, e.target.checked))}
                />
                {t(`scan.slots.${slot}`)}
              </label>
            ))}
          </div>
          {draft.customTimes.length > 0 && (
            <div className="flex flex-wrap items-center gap-2 mt-2">
              <span className="text-sm text-gray-500">{t('medications.customTimes')}:</span>
              {draft.customTimes.map((time) => (
                <button
                  key={time}
                  type="button"
                  disabled={locked}
                  onClick={() => updateDraft(index, (d) => withoutCustomTime(d, time))}
                  aria-label={t('medications.removeTime', { time })}
                  className="flex items-center gap-1 px-2.5 py-1 bg-blue-50 text-[#0057B8] text-sm rounded-full disabled:opacity-60"
                >
                  {time}
                  <X className="w-3 h-3" />
                </button>
              ))}
            </div>
          )}
          {med.schedule_source === 'icons' && (
            <p className="text-sm text-gray-500 mt-2">{t('scan.fromIcons')}</p>
          )}
          {!draft.asNeeded && (med.interval_hours ?? 0) > 24 && (
            <p className="text-sm text-amber-700 mt-2">
              {t('scan.everyFewDays', { days: Math.round((med.interval_hours ?? 48) / 24) })}
            </p>
          )}
          {timeCount === 0 && !draft.asNeeded && (
            <p className="text-sm text-amber-700 mt-2">{t('scan.noSchedule')}</p>
          )}
        </div>

        <div>
          <label className={LABEL} htmlFor={`scan-last-${index}`}>{t('scan.lastDay')}</label>
          <input
            id={`scan-last-${index}`}
            className={INPUT}
            type="date"
            value={draft.lastDay}
            disabled={locked}
            onChange={(e) => updateDraft(index, (d) => ({ ...d, lastDay: e.target.value }))}
          />
          <p className={`text-sm mt-1 ${ended ? 'text-amber-700' : 'text-gray-500'}`}>
            {draft.lastDay === ''
              ? t('scan.noLastDay')
              : ended
                ? t('scan.courseEnded', { date: draft.lastDay })
                : t('scan.remindersUntil', { date: draft.lastDay })}
          </p>
        </div>

        {!autoRecordable(draft) && (
          <div className="flex items-start gap-2 text-sm text-gray-600">
            <Info className="w-4 h-4 text-[#0057B8] shrink-0 mt-0.5" />
            <span>{t('medications.reachyConfirmHint')}</span>
          </div>
        )}

        {na(med.warning) && med.warning !== sharedWarning && (
          <div className="p-3 bg-amber-50 border border-amber-200 rounded-xl flex items-start gap-2">
            <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
            <p className="text-sm text-amber-800">{med.warning}</p>
          </div>
        )}
        {(na(med.pill_description) || na(med.clinical_uses)) && (
          <div className="text-sm text-gray-600 space-y-1">
            {na(med.pill_description) && <p>{t('scan.appearance')}: {med.pill_description}</p>}
            {na(med.clinical_uses) && <p>{t('scan.clinicalUses')}: {med.clinical_uses}</p>}
          </div>
        )}

        {problem && <p className="text-sm text-red-600">{problem}</p>}
        {draft.status === 'saved' && (
          <p className="text-sm text-green-700 flex items-center gap-1">
            <Check className="w-4 h-4" />
            {t('scan.saved')}
          </p>
        )}
        {draft.status === 'failed' && (
          <p className="text-sm text-red-600">{t(draft.errorKey ?? 'scan.saveOneFailed')}</p>
        )}
      </div>
    );
  };

  const details: [string, string | undefined][] = parsed
    ? [
        [t('scan.hospital'), na(parsed.hospital)],
        [t('scan.pharmacy'), na(parsed.pharmacy)],
        [t('scan.prescriptionNo'), na(parsed.prescription_no)],
        [t('scan.patient'), na(parsed.patient_name)],
        [t('scan.physician'), na(parsed.physician)],
        [t('scan.pharmacist'), na(parsed.pharmacist)],
        [t('scan.visitDate'), na(parsed.visit_date)],
        [t('scan.dateDispensed'), na(parsed.date_dispensed)],
        [t('scan.useBefore'), na(parsed.use_before)],
      ]
    : [];
  const shownDetails = details.filter(([, value]) => value);

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-semibold text-gray-900">{t('scan.title')}</h2>
        <button
          onClick={() => navigate('/dashboard')}
          aria-label={t('scan.close')}
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
                    {t('scan.alignFrame')}
                  </p>
                </div>

                {/* Top controls */}
                <div className="absolute top-4 left-4 right-4 flex justify-between">
                  <button
                    onClick={() => navigate('/dashboard')}
                    aria-label={t('scan.close')}
                    className="w-10 h-10 bg-black/40 backdrop-blur-sm rounded-full flex items-center justify-center text-white hover:bg-black/60 transition-colors"
                  >
                    <X className="w-5 h-5" />
                  </button>
                  <div className="flex gap-2">
                    <button
                      onClick={() => setFlashOn(!flashOn)}
                      aria-label={t('scan.flash')}
                      className={`w-10 h-10 backdrop-blur-sm rounded-full flex items-center justify-center transition-colors ${
                        flashOn ? 'bg-[#0057B8] text-white' : 'bg-black/40 text-white hover:bg-black/60'
                      }`}
                    >
                      <Zap className="w-5 h-5" />
                    </button>
                    <button
                      onClick={switchCamera}
                      aria-label={t('scan.switchCamera')}
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
                    aria-label={t('scan.capture')}
                    className="w-16 h-16 bg-white rounded-full border-4 border-[#0057B8] flex items-center justify-center shadow-lg active:scale-95 transition-transform"
                  >
                    <div className="w-12 h-12 bg-[#0057B8] rounded-full" />
                  </button>
                </div>
              </>
            )}
          </>
        ) : (
          <img src={capturedImage} alt={t('scan.capturedPhoto')} className="w-full h-full object-cover" />
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

      {/* Every medicine found, each an editable card */}
      {parsed && !parsing && (
        <div className="space-y-4">
          <div className="flex items-start gap-3">
            <Pill className="w-5 h-5 text-[#0057B8] mt-0.5 shrink-0" />
            <div>
              <p className="font-semibold text-gray-900 text-base">
                {t('scan.foundMedicines', { count: drafts.length })}
              </p>
              <p className="text-sm text-gray-500">{t('scan.checkAgainstPaper')}</p>
            </div>
          </div>

          {sharedWarning && (
            <div className="p-3 bg-amber-50 border border-amber-200 rounded-xl flex items-start gap-2">
              <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
              <p className="text-sm text-amber-800">{sharedWarning}</p>
            </div>
          )}

          {drafts.map(renderDraft)}

          {/* Prescription details */}
          {shownDetails.length > 0 && (
            <div className="bg-white rounded-2xl border border-gray-100 shadow-sm px-5 py-4 space-y-2">
              <div className="flex items-center gap-2 mb-2">
                <Building2 className="w-4 h-4 text-[#0057B8]" />
                <p className="text-sm text-gray-500">{t('scan.details')}</p>
              </div>
              {shownDetails.map(([label, value]) => (
                <div key={label} className="flex justify-between gap-3 text-sm">
                  <span className="text-gray-500">{label}</span>
                  <span className="text-gray-900 font-medium text-right max-w-[60%]">{value}</span>
                </div>
              ))}
              {(() => {
                const exp = getExpiryStatus(parsed.use_before);
                if (exp.status !== 'expired' && exp.status !== 'soon') return null;
                const msg =
                  exp.status === 'expired'
                    ? t('medications.expiredOn', { date: exp.iso })
                    : exp.daysLeft === 0
                      ? t('medications.expiresToday', { date: exp.iso })
                      : t('medications.expiresInDays', { days: exp.daysLeft, date: exp.iso });
                return (
                  <div className="mt-2 p-3 bg-amber-50 border border-amber-200 rounded-xl flex items-start gap-2">
                    <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
                    <p className="text-sm text-amber-800">{msg}</p>
                  </div>
                );
              })()}
            </div>
          )}

          {/* Actions */}
          {allSaved ? (
            <div className="space-y-3">
              <div className="py-4 bg-green-50 text-green-700 text-base rounded-xl font-medium flex items-center justify-center gap-2">
                <Check className="w-4 h-4" />
                {t('scan.savedAll', { count: savedCount })}
              </div>
              <div className="flex gap-3">
                <button
                  onClick={() => navigate('/medications')}
                  className="flex-1 py-4 bg-[#0057B8] text-white text-base rounded-xl font-medium hover:bg-[#003D82] active:scale-95 transition-all touch-target-large"
                >
                  {t('scan.viewMedications')}
                </button>
                <button
                  onClick={retake}
                  className="flex-1 py-4 bg-gray-100 text-gray-700 text-base rounded-xl font-medium hover:bg-gray-200 active:scale-95 transition-all touch-target-large"
                >
                  {t('scan.scanAnother')}
                </button>
              </div>
            </div>
          ) : (
            <div className="flex gap-3">
              <button
                onClick={addSelected}
                disabled={saving || selected.length === 0}
                className="flex-1 py-4 bg-[#0057B8] text-white text-base rounded-xl font-medium hover:bg-[#003D82] active:scale-95 transition-all flex items-center justify-center gap-2 touch-target-large disabled:opacity-60"
              >
                {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
                {t('scan.addSelected', { count: selected.length })}
              </button>
              <button
                onClick={retake}
                disabled={saving}
                className="flex-1 py-4 bg-gray-100 text-gray-700 text-base rounded-xl font-medium hover:bg-gray-200 active:scale-95 transition-all touch-target-large disabled:opacity-60"
              >
                {t('scan.scanAnother')}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
