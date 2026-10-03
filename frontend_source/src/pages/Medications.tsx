import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useNavigate } from 'react-router-dom';
import { getFaceToken } from '../lib/face-auth';
import { getExpiryStatus } from '../lib/expiry';
import { blockLabel, forgetRefusals, medBlock } from '../lib/doses';
import { useNow } from '../hooks/useNow';
import { useOverdoseProtection } from '../hooks/useOverdoseProtection';
import TimeSection from '../components/ui/TimeSection';
import {
  Pill,
  Plus,
  ChevronRight,
  AlertTriangle,
  Clock,
  Package,
  PackagePlus,
  Edit3,
  Trash2,
  Loader2,
  X,
  CalendarClock,
  Sun,
  Sunset,
  Moon,
  Bed,
  Camera,
  Archive,
  RotateCcw,
  ShieldCheck,
} from 'lucide-react';

interface ScheduleTime {
  morning: boolean;
  noon: boolean;
  night: boolean;
  bedtime: boolean;
  before_meals: boolean;
  after_meals: boolean;
  custom_times: string[];
  weekdays: number[] | null;   // ISO 1 (Mon) .. 7 (Sun); null = every day
}

interface Medication {
  id: number;
  name: string;
  dosage: string | null;
  total_pills: number;
  pills_remaining: number;
  instructions: string | null;
  warning: string | null;
  pill_description: string | null;
  use_before: string | null;
  schedule_time: ScheduleTime | null;
  is_active: boolean;
  dose_form?: DoseForm;
  units_per_dose?: number;
  days_left?: number | null;
  run_out_date?: string | null;
  /** Overdose protection limits; null = the default from the schedule. */
  min_interval_minutes?: number | null;
  max_daily_doses?: number | null;
}

type DoseForm = 'solid_oral' | 'liquid' | 'inhaler' | 'injection' | 'topical' | 'other';
const DOSE_FORMS: DoseForm[] = ['solid_oral', 'liquid', 'inhaler', 'injection', 'topical', 'other'];

type PresetKey = 'morning' | 'noon' | 'night' | 'bedtime' | 'before_meals' | 'after_meals';
const SCHEDULE_OPTIONS: { key: PresetKey; label: string }[] = [
  { key: 'morning',      label: '早上' },
  { key: 'noon',         label: '中午' },
  { key: 'night',        label: '晚上' },
  { key: 'bedtime',      label: '睡前' },
  { key: 'before_meals', label: '飯前' },
  { key: 'after_meals',  label: '飯後' },
];
const PRESET_TIMES: Record<'morning' | 'noon' | 'night' | 'bedtime', string> = {
  morning: '08:00', noon: '12:00', night: '20:00', bedtime: '22:00',
};
const WEEKDAYS = [1, 2, 3, 4, 5, 6, 7];
const MAX_TIMES = 8;
const REFILL_SOON_DAYS = 7;

const EMPTY_SCHEDULE: ScheduleTime = {
  morning: false, noon: false, night: false,
  bedtime: false, before_meals: false, after_meals: false,
  custom_times: [], weekdays: null,
};

/** Normalize JSON fields returned by asyncpg into the UI's object shape. */
function normalizeScheduleTime(value: unknown): ScheduleTime | null {
  let candidate = value;
  if (typeof candidate === 'string') {
    try {
      candidate = JSON.parse(candidate);
    } catch {
      return null;
    }
  }
  if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) return null;

  // Older builds spread a serialized schedule string into an object while
  // editing. Recover that payload when it is still present in the database
  // (for example, keys "0", "1", ... containing the JSON characters).
  const raw = candidate as Record<string, unknown>;
  const numericKeys = Object.keys(raw)
    .filter((key) => /^\d+$/.test(key))
    .sort((a, b) => Number(a) - Number(b));
  if (numericKeys.length >= 2 && numericKeys.every((key) => typeof raw[key] === 'string' && String(raw[key]).length <= 1)) {
    try {
      candidate = JSON.parse(numericKeys.map((key) => String(raw[key])).join(''));
    } catch {
      // Fall through to the known boolean fields below.
    }
  }
  if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) return null;
  const source = candidate as Record<string, unknown>;
  const customTimes = Array.isArray(source.custom_times)
    ? source.custom_times.filter((time): time is string => typeof time === 'string' && /^\d{2}:\d{2}$/.test(time))
    : [];
  const weekdays = Array.isArray(source.weekdays)
    ? source.weekdays.filter((day): day is number => Number.isInteger(day) && day >= 1 && day <= 7)
    : [];
  return {
    morning: Boolean(source.morning),
    noon: Boolean(source.noon),
    night: Boolean(source.night),
    bedtime: Boolean(source.bedtime),
    before_meals: Boolean(source.before_meals),
    after_meals: Boolean(source.after_meals),
    custom_times: [...new Set(customTimes)].sort(),
    weekdays: weekdays.length > 0 && weekdays.length < 7 ? [...new Set(weekdays)].sort() : null,
  };
}

/** Every clock time the schedule creates a dose at. */
function doseTimes(schedule: ScheduleTime | null): string[] {
  if (!schedule) return [];
  const presets = (Object.keys(PRESET_TIMES) as (keyof typeof PRESET_TIMES)[])
    .filter((key) => schedule[key]).map((key) => PRESET_TIMES[key]);
  return [...new Set([...presets, ...schedule.custom_times])].sort();
}

type Period = 'morning' | 'afternoon' | 'evening' | 'bedtime';
function periodOf(time: string): Period {
  const hour = Number(time.slice(0, 2));
  if (hour < 11) return 'morning';
  if (hour < 17) return 'afternoon';
  if (hour < 21) return 'evening';
  return 'bedtime';
}

const PERIOD_CONFIG = {
  morning:   { icon: Sun,    label: 'Morning',    timeRange: '00:00-11:00', iconColor: 'text-amber-500',  bgColor: 'bg-amber-50' },
  afternoon: { icon: Sunset, label: 'Afternoon',  timeRange: '11:00-17:00', iconColor: 'text-orange-500', bgColor: 'bg-orange-50' },
  evening:   { icon: Moon,   label: 'Evening',    timeRange: '17:00-21:00', iconColor: 'text-indigo-500', bgColor: 'bg-indigo-50' },
  bedtime:   { icon: Bed,    label: 'Bedtime',    timeRange: '21:00-24:00', iconColor: 'text-purple-500', bgColor: 'bg-purple-50' },
};

function getAuthHeaders(): Record<string, string> {
  const token = getFaceToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** 30 -> "30", 29.5 -> "29.5" */
function amount(value: number | null | undefined): string {
  return String(Math.round(Number(value ?? 0) * 100) / 100);
}

const hours = (minutes: number) => amount(minutes / 60);

/**
 * The server's minimum gap when none is set (overdose protection): half the shortest gap between the day's dose
 * times, counting the one past midnight (once a day: 12 h), or 4 h for a medicine without times.
 */
function defaultGapMinutes(times: string[]): number {
  if (times.length === 0) return 240;
  const minutes = times.map((time) => Number(time.slice(0, 2)) * 60 + Number(time.slice(3, 5))).sort((a, b) => a - b);
  return Math.min(...minutes.map((value, i) => (i + 1 < minutes.length ? minutes[i + 1] : minutes[0] + 1440) - value)) / 2;
}

/** Blank = null, the default; otherwise minutes within the column's 30..2880. */
function gapMinutes(text: string): number | null {
  const value = Number(text);
  if (text.trim() === '' || !Number.isFinite(value) || value <= 0) return null;
  return Math.min(2880, Math.max(30, Math.round(value * 60)));
}

/** Blank = null, the default; otherwise 1..24. */
function dailyMax(text: string): number | null {
  const value = Number(text);
  if (text.trim() === '' || !Number.isFinite(value) || value < 1) return null;
  return Math.min(24, Math.round(value));
}

const INPUT = 'w-full px-4 py-3 rounded-xl bg-gray-50 border border-gray-200 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#0057B8]/30 focus:border-[#0057B8] transition-all';
const LABEL = 'block text-sm font-medium text-gray-500 uppercase tracking-wide mb-1.5';

export default function Medications() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [medications, setMedications] = useState<Medication[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [newTime, setNewTime] = useState('');
  const [supplyFor, setSupplyFor] = useState<Medication | null>(null);
  const [supply, setSupply] = useState({ quantity: 30, note: '' });
  const [notice, setNotice] = useState('');
  const [formError, setFormError] = useState('');
  // A Take Now the server refused (too soon, daily maximum) stays labelled until it is allowed again.
  const now = useNow();
  const protection = useOverdoseProtection();

  const [form, setForm] = useState({
    name: '',
    dosage: '',
    total_pills: 30,
    pills_remaining: 30,
    instructions: '',
    warning: '',
    pill_description: '',
    use_before: '',
    is_active: true,
    schedule_time: { ...EMPTY_SCHEDULE },
    dose_form: 'solid_oral' as DoseForm,
    units_per_dose: 1,
    // Text, so blank means "use the default".
    min_gap_hours: '',
    max_daily: '',
  });

  useEffect(() => { fetchMedications(); }, []);

  const fetchMedications = async () => {
    setLoading(true);
    const headers = getAuthHeaders();
    const resp = await fetch('/api/medications', { headers }).catch(() => null);
    if (resp?.ok) {
      const rows = await resp.json() as Array<Omit<Medication, 'schedule_time'> & { schedule_time?: unknown }>;
      setMedications(rows.map((row) => ({
        ...row,
        schedule_time: normalizeScheduleTime(row.schedule_time),
      })));
    }
    setLoading(false);
  };

  const resetForm = () => {
    setForm({ name: '', dosage: '', total_pills: 30, pills_remaining: 30, instructions: '', warning: '', pill_description: '', use_before: '', is_active: true, schedule_time: { ...EMPTY_SCHEDULE }, dose_form: 'solid_oral', units_per_dose: 1, min_gap_hours: '', max_daily: '' });
    setEditingId(null);
    setNewTime('');
    setFormError('');
    setShowForm(false);
  };

  const handleEdit = (med: Medication) => {
    setForm({
      name: med.name,
      dosage: med.dosage || '',
      total_pills: med.total_pills,
      pills_remaining: Number(med.pills_remaining),
      instructions: med.instructions || '',
      warning: med.warning || '',
      pill_description: med.pill_description || '',
      use_before: med.use_before || '',
      is_active: med.is_active,
      schedule_time: normalizeScheduleTime(med.schedule_time) || { ...EMPTY_SCHEDULE },
      dose_form: med.dose_form || 'solid_oral',
      units_per_dose: Number(med.units_per_dose ?? 1),
      min_gap_hours: med.min_interval_minutes ? hours(med.min_interval_minutes) : '',
      max_daily: med.max_daily_doses ? String(med.max_daily_doses) : '',
    });
    setEditingId(med.id);
    setFormError('');
    setShowForm(true);
  };

  const post = (url: string, body?: unknown) => fetch(url, {
    method: 'POST',
    headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  const handleStop = async (med: Medication) => {
    if (!confirm(t('medications.stopConfirm', { name: med.name }))) return;
    await post(`/api/medications/${med.id}/archive`);
    fetchMedications();
  };

  const handleResume = async (med: Medication) => {
    await post(`/api/medications/${med.id}/reactivate`);
    fetchMedications();
  };

  const handleDelete = async (med: Medication) => {
    if (!confirm(t('medications.deleteConfirm'))) return;
    const resp = await fetch(`/api/medications/${med.id}`, { method: 'DELETE', headers: getAuthHeaders() });
    if (resp.status === 409) setNotice(t('medications.historyKept', { name: med.name }));
    fetchMedications();
  };

  const handleAddSupply = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!supplyFor || !(supply.quantity > 0)) return;
    setSaving(true);
    const resp = await post(`/api/medications/${supplyFor.id}/supply`,
      { quantity: Math.round(supply.quantity * 100) / 100, note: supply.note || null });
    setSaving(false);
    if (resp.ok) {
      setNotice(t('medications.supplyAdded', { quantity: amount(supply.quantity), name: supplyFor.name }));
      setSupplyFor(null);
      fetchMedications();
    }
  };

  const handleTakeNow = (med: Medication) => {
    if (med.pills_remaining < Number(med.units_per_dose ?? 1) || medBlock(med.id, now, protection !== false)) return;
    navigate(`/intake?med=${med.id}&start=1`);
  };

  const toggleSchedule = (key: PresetKey) => {
    setForm(f => ({ ...f, schedule_time: { ...f.schedule_time, [key]: !f.schedule_time[key] } }));
  };

  const addCustomTime = () => {
    if (!/^\d{2}:\d{2}$/.test(newTime)) return;
    setForm(f => {
      const times = [...new Set([...f.schedule_time.custom_times, newTime])].sort();
      if (doseTimes({ ...f.schedule_time, custom_times: times }).length > MAX_TIMES) return f;
      return { ...f, schedule_time: { ...f.schedule_time, custom_times: times } };
    });
    setNewTime('');
  };

  const removeCustomTime = (time: string) => {
    setForm(f => ({ ...f, schedule_time: { ...f.schedule_time,
      custom_times: f.schedule_time.custom_times.filter((value) => value !== time) } }));
  };

  const toggleWeekday = (day: number) => {
    setForm(f => {
      const current = f.schedule_time.weekdays ?? WEEKDAYS;
      const next = current.includes(day) ? current.filter((d) => d !== day) : [...current, day].sort();
      if (next.length === 0) return f;   // at least one day
      return { ...f, schedule_time: { ...f.schedule_time, weekdays: next.length === 7 ? null : next } };
    });
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    const headers = getAuthHeaders();
    if (!headers.Authorization) { setSaving(false); return; }

    const schedule = normalizeScheduleTime(form.schedule_time);
    const hasSchedule = !!schedule && (doseTimes(schedule).length > 0 || schedule.before_meals || schedule.after_meals);
    const payload = {
      name: form.name,
      dosage: form.dosage || null,
      total_pills: form.total_pills,
      pills_remaining: Math.round(form.pills_remaining * 100) / 100,
      instructions: form.instructions || null,
      warning: form.warning || null,
      pill_description: form.pill_description || null,
      use_before: form.use_before || null,
      is_active: form.is_active,
      dose_form: form.dose_form,
      units_per_dose: form.units_per_dose,
      schedule_time: hasSchedule && schedule
        ? { morning: schedule.morning, noon: schedule.noon, night: schedule.night, bedtime: schedule.bedtime,
            before_meals: schedule.before_meals, after_meals: schedule.after_meals,
            custom_times: schedule.custom_times,
            ...(schedule.weekdays ? { weekdays: schedule.weekdays } : {}) }
        : null,
      // Always sent, so clearing a field on edit returns that limit to the default.
      min_interval_minutes: gapMinutes(form.min_gap_hours),
      max_daily_doses: dailyMax(form.max_daily),
    };

    const url = editingId ? `/api/medications/${editingId}` : '/api/medications';
    const method = editingId ? 'PATCH' : 'POST';
    const resp = await fetch(url, { method, headers: { ...headers, 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
      .catch(() => null);

    setSaving(false);
    if (!resp?.ok) {
      setFormError(t('medications.saveFailed'));
      return;
    }
    // New limits or times change what the server refuses.
    forgetRefusals();
    resetForm();
    fetchMedications();
  };

  const activeMeds = medications.filter(m => m.is_active);
  const groupedMeds: Record<Period, Medication[]> = { morning: [], afternoon: [], evening: [], bedtime: [] };
  for (const med of activeMeds) {
    const periods = new Set(doseTimes(med.schedule_time).map(periodOf));
    periods.forEach((period) => groupedMeds[period].push(med));
  }
  const unscheduledMeds = activeMeds.filter(m => doseTimes(m.schedule_time).length === 0);
  const inactiveMeds = medications.filter(m => !m.is_active);

  const scheduleSummary = (med: Medication) => {
    const times = doseTimes(med.schedule_time);
    if (times.length === 0) return null;
    const days = med.schedule_time?.weekdays;
    const dayText = days ? days.map((d) => t(`medications.weekday.${d}`)).join(' ') : t('medications.everyDay');
    return `${times.join(' · ')} — ${dayText}`;
  };

  const limitsSummary = (med: Medication) => [
    med.min_interval_minutes != null ? t('medications.limitGap', { hours: hours(med.min_interval_minutes) }) : null,
    med.max_daily_doses != null ? t('medications.limitDaily', { count: med.max_daily_doses }) : null,
  ].filter(Boolean).join(' · ');

  const renderCard = (med: Medication) => {
    const canTake = med.pills_remaining >= Number(med.units_per_dose ?? 1);
    const block = medBlock(med.id, now, protection !== false);
    const lowSupply = med.days_left != null && med.days_left <= REFILL_SOON_DAYS;
    const summary = scheduleSummary(med);
    const limits = limitsSummary(med);
    return (
      <div
        key={med.id}
        className="flex items-center gap-3 p-4 bg-white rounded-xl border border-gray-100 shadow-sm hover:shadow-md hover:border-gray-200 transition-all duration-300"
      >
        <div className="w-10 h-10 bg-blue-50 rounded-full flex items-center justify-center shrink-0">
          <Pill className="w-5 h-5 text-[#0057B8]" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <p className="font-semibold text-gray-900 text-base">{med.name}</p>
            {med.warning && <AlertTriangle className="w-3.5 h-3.5 text-amber-500 shrink-0" />}
          </div>
          {med.dosage && <p className="text-sm text-gray-500 font-medium mt-0.5">{med.dosage}</p>}
          <div className="flex items-center gap-3 mt-1 text-sm text-gray-400 flex-wrap">
            <span className="flex items-center gap-1">
              <Package className="w-3 h-3" />
              {amount(med.pills_remaining)}/{med.total_pills}
            </span>
            {summary && (
              <span className="flex items-center gap-1">
                <Clock className="w-3 h-3 shrink-0" />
                {summary}
              </span>
            )}
          </div>
          {limits && (
            <p className="text-sm mt-1 text-gray-500 flex items-center gap-1">
              <ShieldCheck className="w-3 h-3 shrink-0" />{limits}
            </p>
          )}
          {med.days_left != null && med.run_out_date && (
            <p className={`text-sm mt-1 ${lowSupply ? 'text-amber-700 font-medium' : 'text-gray-500'}`}>
              {t('medications.daysLeft', { days: med.days_left, date: med.run_out_date })}
            </p>
          )}
          {med.use_before && (() => {
            const exp = getExpiryStatus(med.use_before);
            const alert = exp.status === 'expired' || exp.status === 'soon';
            return (
              <p className={`text-sm mt-1 flex items-center gap-1 ${alert ? 'text-amber-600 font-medium' : 'text-gray-400'}`}>
                <CalendarClock className="w-3 h-3 shrink-0" />
                {med.use_before}
                {exp.status === 'expired' && <span>· {t('medications.expiredOn', { date: exp.iso })}</span>}
                {exp.status === 'soon' && (
                  <span>· {exp.daysLeft === 0
                    ? t('medications.expiresToday', { date: exp.iso })
                    : t('medications.expiresInDays', { days: exp.daysLeft, date: exp.iso })}</span>
                )}
              </p>
            );
          })()}
        </div>
        <div className="flex items-center gap-1 shrink-0 flex-wrap justify-end">
          <button
            onClick={() => handleTakeNow(med)}
            disabled={!canTake || !!block}
            aria-label={`${!canTake ? t('intake.cannotTake') : block ? blockLabel(block, t) : t('intake.take')}: ${med.name}`}
            className="flex items-center gap-1 px-3 py-2 text-sm font-medium text-white bg-[#0057B8] hover:bg-[#003D82] rounded-lg transition-colors disabled:bg-gray-200 disabled:text-gray-600 disabled:cursor-not-allowed"
          >
            <Camera className="w-4 h-4" />
            {!canTake ? t('intake.cannotTake') : block ? blockLabel(block, t) : t('intake.take')}
          </button>
          <button
            onClick={() => { setSupply({ quantity: med.total_pills || 30, note: '' }); setSupplyFor(med); }}
            aria-label={`${t('medications.addSupply')}: ${med.name}`}
            title={t('medications.addSupply')}
            className={`p-2 rounded-lg transition-colors ${lowSupply ? 'text-amber-600 bg-amber-50 hover:bg-amber-100' : 'text-gray-400 hover:text-[#0057B8] hover:bg-blue-50'}`}
          >
            <PackagePlus className="w-4 h-4" />
          </button>
          <button
            onClick={() => handleEdit(med)}
            aria-label={`${t('medications.edit')}: ${med.name}`}
            className="p-2 text-gray-400 hover:text-[#0057B8] hover:bg-blue-50 rounded-lg transition-colors"
          >
            <Edit3 className="w-4 h-4" />
          </button>
          <button
            onClick={() => handleStop(med)}
            aria-label={`${t('medications.stop')}: ${med.name}`}
            title={t('medications.stop')}
            className="p-2 text-gray-400 hover:text-red-500 hover:bg-red-50 rounded-lg transition-colors"
          >
            <Archive className="w-4 h-4" />
          </button>
        </div>
      </div>
    );
  };

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-3">
        <Loader2 className="w-8 h-8 animate-spin text-[#0057B8]" />
        <span className="text-gray-500 text-sm">{t('common.loading')}</span>
      </div>
    );
  }

  const formTimes = doseTimes(form.schedule_time);
  const formGap = hours(defaultGapMinutes(formTimes));
  // Limits that contradict the medicine's own schedule refuse some of its scheduled doses every day. A pharmacist
  // may mean that, so it is a warning, not a block.
  const setGap = gapMinutes(form.min_gap_hours);
  const setMax = dailyMax(form.max_daily);
  const closestGap = formTimes.length > 0 ? defaultGapMinutes(formTimes) * 2 : null;
  const limitWarnings = [
    setMax !== null && setMax < formTimes.length
      ? t('medications.limitsMaxBelowTimes', { max: setMax, count: formTimes.length, refused: formTimes.length - setMax })
      : null,
    setGap !== null && closestGap !== null && setGap > closestGap
      ? t('medications.limitsGapOverTimes', { hours: hours(setGap), gap: hours(closestGap) })
      : null,
  ].filter((text): text is string => text !== null);

  return (
    <div className="space-y-5 relative">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-gray-900">{t('medications.title')}</h2>
          <p className="text-base text-gray-500 mt-0.5">{new Date().toLocaleDateString(undefined, { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' })}</p>
        </div>
      </div>

      {notice && (
        <div role="status" className="flex items-start justify-between gap-3 rounded-xl bg-blue-50 border border-blue-200 p-4 text-blue-800">
          <span>{notice}</span>
          <button onClick={() => setNotice('')} aria-label={t('common.close')} className="shrink-0"><X className="w-4 h-4" /></button>
        </div>
      )}

      {/* ── Add supply dialog ── */}
      {supplyFor && (
        <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 backdrop-blur-sm px-4 pb-4">
          <form onSubmit={handleAddSupply} className="w-full max-w-sm bg-white rounded-2xl shadow-2xl border border-gray-100 p-5 space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="font-semibold text-gray-900 flex items-center gap-2">
                <PackagePlus className="w-5 h-5 text-[#0057B8]" />{t('medications.supplyTitle', { name: supplyFor.name })}
              </h3>
              <button type="button" onClick={() => setSupplyFor(null)} aria-label={t('common.cancel')} className="p-1.5 text-gray-400 hover:text-gray-600 rounded-lg">
                <X className="w-5 h-5" />
              </button>
            </div>
            <p className="text-sm text-gray-500">{t('medications.supplyCurrent', { amount: amount(supplyFor.pills_remaining) })}</p>
            <div>
              <label htmlFor="supply-quantity" className={LABEL}>{t('medications.supplyQuantity')}</label>
              <input id="supply-quantity" type="number" min={0.5} max={9999} step={0.5} required
                value={supply.quantity} onChange={e => setSupply({ ...supply, quantity: Number(e.target.value) })} className={INPUT} />
            </div>
            <div>
              <label htmlFor="supply-note" className={LABEL}>{t('medications.supplyNote')}</label>
              <input id="supply-note" type="text" maxLength={200} value={supply.note}
                onChange={e => setSupply({ ...supply, note: e.target.value })} className={INPUT}
                placeholder={t('medications.supplyNotePlaceholder')} />
            </div>
            <div className="flex gap-3">
              <button type="submit" disabled={saving}
                className="flex-1 py-3 rounded-xl bg-[#0057B8] text-white font-semibold hover:bg-[#003D82] disabled:opacity-60 flex items-center justify-center gap-2">
                {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : t('medications.addSupply')}
              </button>
              <button type="button" onClick={() => setSupplyFor(null)} className="flex-1 py-3 bg-gray-100 text-gray-700 rounded-xl font-medium hover:bg-gray-200">
                {t('common.cancel')}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* ── Manual Input Form Modal ── */}
      {showForm && (
        <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 backdrop-blur-sm px-4 pb-4">
          <div className="w-full max-w-lg bg-white rounded-2xl shadow-2xl max-h-[90vh] flex flex-col border border-gray-100">
            {/* Modal header */}
            <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
              <div className="flex items-center gap-2">
                <Pill className="w-5 h-5 text-[#0057B8]" />
                <h3 className="font-semibold text-gray-900">
                  {editingId ? t('medications.edit') : t('medications.addNew')}
                </h3>
              </div>
              <button onClick={resetForm} className="p-1.5 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded-lg transition-colors">
                <X className="w-5 h-5" />
              </button>
            </div>

            {/* Scrollable form body */}
            <form onSubmit={handleSubmit} className="overflow-y-auto flex-1 divide-y divide-gray-100">
              {/* Medication name + dosage */}
              <div className="px-5 py-4 space-y-3">
                <div>
                  <label className={LABEL}>{t('medications.name')} *</label>
                  <input
                    type="text"
                    value={form.name}
                    onChange={e => setForm({ ...form, name: e.target.value })}
                    className={INPUT}
                    placeholder="e.g. Allegra (Fexofenadine) 60mg/tab"
                    required
                  />
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className={LABEL}>{t('medications.dosage')}</label>
                    <input
                      type="text"
                      value={form.dosage}
                      onChange={e => setForm({ ...form, dosage: e.target.value })}
                      className={INPUT}
                      placeholder="e.g. 60mg/tab"
                    />
                  </div>
                  <div>
                    <label className={LABEL}>外觀 Appearance</label>
                    <input
                      type="text"
                      value={form.pill_description}
                      onChange={e => setForm({ ...form, pill_description: e.target.value })}
                      className={INPUT}
                      placeholder="e.g. 橢圓形橘色"
                    />
                  </div>
                </div>
                {/* Reachy records automatically only one tablet/capsule per prompt. */}
                <div className="grid grid-cols-2 gap-3 mt-3">
                  <div>
                    <label htmlFor="dose-form" className={LABEL}>{t('medications.doseForm')}</label>
                    <select
                      id="dose-form"
                      value={form.dose_form}
                      onChange={e => setForm({ ...form, dose_form: e.target.value as DoseForm })}
                      className={INPUT}
                    >
                      {DOSE_FORMS.map(value => <option key={value} value={value}>{t(`medications.doseForms.${value}`)}</option>)}
                    </select>
                  </div>
                  <div>
                    <label htmlFor="units-per-dose" className={LABEL}>{t('medications.unitsPerDose')}</label>
                    <input
                      id="units-per-dose"
                      type="number"
                      min={0.25}
                      max={20}
                      step={0.25}
                      value={form.units_per_dose}
                      onChange={e => setForm({ ...form, units_per_dose: Number(e.target.value) })}
                      className={INPUT}
                    />
                  </div>
                </div>
                {!(form.dose_form === 'solid_oral' && form.units_per_dose === 1) && (
                  <p className="mt-2 text-sm text-amber-700">{t('medications.reachyConfirmHint')}</p>
                )}
              </div>

              {/* Quantity */}
              <div className="px-5 py-4 grid grid-cols-2 gap-3">
                <div>
                  <label className={LABEL}>總數量 Total Pills</label>
                  <input
                    type="number"
                    value={form.total_pills}
                    onChange={e => setForm({ ...form, total_pills: parseInt(e.target.value) || 0 })}
                    className={INPUT}
                    min={0}
                  />
                </div>
                <div>
                  <label className={LABEL}>{t('medications.pillsLeft')}</label>
                  <input
                    type="number"
                    value={form.pills_remaining}
                    onChange={e => setForm({ ...form, pills_remaining: parseFloat(e.target.value) || 0 })}
                    className={INPUT}
                    min={0}
                    step={0.25}
                  />
                </div>
              </div>

              {/* Schedule */}
              <div className="px-5 py-4 space-y-3">
                <div>
                  <label className={LABEL}>服藥時間 Schedule</label>
                  <div className="flex flex-wrap gap-2 mt-1">
                    {SCHEDULE_OPTIONS.map(({ key, label }) => (
                      <button
                        key={key}
                        type="button"
                        onClick={() => toggleSchedule(key)}
                        aria-pressed={form.schedule_time[key]}
                        className={`px-4 py-2 rounded-full text-base font-medium border transition-all ${
                          form.schedule_time[key]
                            ? 'bg-[#0057B8] border-[#0057B8] text-white shadow-md'
                            : 'bg-white border-gray-200 text-gray-500 hover:border-gray-300'
                        }`}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </div>
                <div>
                  <label htmlFor="custom-time" className={LABEL}>{t('medications.customTimes')}</label>
                  <div className="flex gap-2">
                    <input id="custom-time" type="time" value={newTime} onChange={e => setNewTime(e.target.value)}
                      className={`${INPUT} flex-1`} />
                    <button type="button" onClick={addCustomTime} disabled={!newTime || formTimes.length >= MAX_TIMES}
                      className="px-4 rounded-xl border border-[#0057B8] text-[#0057B8] font-medium disabled:opacity-40">
                      {t('medications.addTime')}
                    </button>
                  </div>
                  {form.schedule_time.custom_times.length > 0 && (
                    <div className="flex flex-wrap gap-2 mt-2">
                      {form.schedule_time.custom_times.map((time) => (
                        <span key={time} className="flex items-center gap-1 px-3 py-1.5 rounded-full bg-blue-50 text-[#0057B8] font-medium">
                          {time}
                          <button type="button" onClick={() => removeCustomTime(time)}
                            aria-label={t('medications.removeTime', { time })} className="p-0.5"><X className="w-3.5 h-3.5" /></button>
                        </span>
                      ))}
                    </div>
                  )}
                </div>
                <div>
                  <span className={LABEL}>{t('medications.days')}</span>
                  <div className="flex flex-wrap gap-1.5" role="group" aria-label={t('medications.days')}>
                    {WEEKDAYS.map((day) => {
                      const on = (form.schedule_time.weekdays ?? WEEKDAYS).includes(day);
                      return (
                        <button key={day} type="button" onClick={() => toggleWeekday(day)} aria-pressed={on}
                          className={`w-11 h-11 rounded-full text-sm font-medium border transition-all ${
                            on ? 'bg-[#0057B8] border-[#0057B8] text-white' : 'bg-white border-gray-200 text-gray-500'}`}>
                          {t(`medications.weekday.${day}`)}
                        </button>
                      );
                    })}
                  </div>
                  <p className="text-sm text-gray-500 mt-1.5">
                    {formTimes.length === 0 ? t('medications.noTimesYet')
                      : `${formTimes.join(' · ')} — ${form.schedule_time.weekdays
                        ? form.schedule_time.weekdays.map((d) => t(`medications.weekday.${d}`)).join(' ')
                        : t('medications.everyDay')}`}
                  </p>
                </div>
              </div>

              {/* Safety limits for overdose protection; blank = the server's default from the schedule */}
              <div className="px-5 py-4 space-y-3">
                <p className="flex items-center gap-2 text-base font-semibold text-gray-800">
                  <ShieldCheck className="w-5 h-5 text-[#0057B8]" />{t('medications.safetyLimits')}
                </p>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="min-gap-hours" className={LABEL}>{t('medications.minGapHours')}</label>
                    <input
                      id="min-gap-hours"
                      type="number"
                      inputMode="decimal"
                      min={0.5}
                      max={48}
                      step="any"
                      value={form.min_gap_hours}
                      onChange={e => setForm({ ...form, min_gap_hours: e.target.value })}
                      className={INPUT}
                      placeholder={formGap}
                    />
                  </div>
                  <div>
                    <label htmlFor="max-daily-doses" className={LABEL}>{t('medications.maxDaily')}</label>
                    <input
                      id="max-daily-doses"
                      type="number"
                      inputMode="numeric"
                      min={1}
                      max={24}
                      step={1}
                      value={form.max_daily}
                      onChange={e => setForm({ ...form, max_daily: e.target.value })}
                      className={INPUT}
                      placeholder={formTimes.length > 0 ? String(formTimes.length) : t('medications.noLimit')}
                    />
                  </div>
                </div>
                <p className="text-sm text-gray-500">
                  {formTimes.length > 0
                    ? t('medications.limitsDefault', { hours: formGap, count: formTimes.length })
                    : t('medications.limitsDefaultUnscheduled', { hours: formGap })}
                </p>
                <p className={`text-sm ${formTimes.length > 0 ? 'text-gray-500' : 'text-amber-700 font-medium'}`}>
                  {t('medications.limitsPharmacist')}
                </p>
                {limitWarnings.map((text) => (
                  <p key={text} role="status" className="flex items-start gap-2 text-sm font-medium text-amber-700">
                    <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />{text}
                  </p>
                ))}
              </div>

              {/* Instructions + use before */}
              <div className="px-5 py-4 space-y-3">
                <div>
                  <label className={LABEL}>{t('medications.instructions')}</label>
                  <textarea
                    value={form.instructions}
                    onChange={e => setForm({ ...form, instructions: e.target.value })}
                    className={INPUT}
                    rows={2}
                    placeholder="e.g. 每天兩次，早晚飯後使用"
                  />
                </div>
                <div>
                  <label className={LABEL}>有效期限 Use Before</label>
                  <input
                    type="text"
                    value={form.use_before}
                    onChange={e => setForm({ ...form, use_before: e.target.value })}
                    className={INPUT}
                    placeholder="e.g. 114年08月22日"
                  />
                  {(() => {
                    const exp = getExpiryStatus(form.use_before);
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
              </div>

              {/* Warning */}
              <div className="px-5 py-4">
                <label className={LABEL}>{t('medications.warning')}</label>
                <textarea
                  value={form.warning}
                  onChange={e => setForm({ ...form, warning: e.target.value })}
                  className="w-full px-4 py-3 rounded-xl bg-amber-50 border border-amber-200 text-gray-900 placeholder-amber-400/60 focus:outline-none focus:ring-2 focus:ring-amber-400/30 focus:border-amber-400 transition-all"
                  rows={2}
                  placeholder="注意事項及警語..."
                />
              </div>

              {/* Active toggle */}
              <div className="px-5 py-3 flex items-center justify-between">
                <span className="text-base text-gray-700">{t('medications.active')}</span>
                <button
                  type="button"
                  onClick={() => setForm(f => ({ ...f, is_active: !f.is_active }))}
                  aria-pressed={form.is_active}
                  className={`relative w-11 h-6 rounded-full transition-colors ${form.is_active ? 'bg-[#0057B8]' : 'bg-gray-200'}`}
                >
                  <span className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow transition-transform ${form.is_active ? 'translate-x-5' : ''}`} />
                </button>
              </div>

              {formError && (
                <p role="alert" className="mx-5 my-3 rounded-xl bg-red-50 border border-red-200 p-3 text-sm text-red-700">{formError}</p>
              )}

              {/* Action buttons */}
              <div className="px-5 py-4 flex gap-3">
                <button
                  type="submit"
                  disabled={saving}
                  className="flex-1 py-4 rounded-xl bg-[#0057B8] text-white text-base font-semibold hover:bg-[#003D82] active:scale-95 transition-all disabled:opacity-60 flex items-center justify-center gap-2 touch-target-large"
                >
                  {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : t('common.save')}
                </button>
                <button
                  type="button"
                  onClick={resetForm}
                  className="flex-1 py-4 bg-gray-100 text-gray-700 text-base rounded-xl font-medium hover:bg-gray-200 active:scale-95 transition-all touch-target-large"
                >
                  {t('common.cancel')}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Time-Grouped Medications */}
      {medications.length === 0 ? (
        <div className="flex flex-col items-center py-12 text-center">
          <Pill className="w-16 h-16 text-gray-200 mb-4" />
          <h3 className="text-xl font-semibold text-gray-900 mb-2">{t('medications.title')}</h3>
          <p className="text-base text-gray-500 mb-6 max-w-xs">{t('common.noMedications')}</p>
        </div>
      ) : (
        <div className="space-y-6">
          {(Object.entries(groupedMeds) as [Period, Medication[]][]).map(([period, meds]) => {
            if (meds.length === 0) return null;
            const config = PERIOD_CONFIG[period];
            return (
              <TimeSection
                key={period}
                title={config.label}
                timeRange={config.timeRange}
                icon={config.icon}
                iconColor={config.iconColor}
                bgColor={config.bgColor}
              >
                <div className="space-y-2">{meds.map(renderCard)}</div>
              </TimeSection>
            );
          })}

          {/* Unscheduled medications */}
          {unscheduledMeds.length > 0 && (
            <TimeSection
              title={t('medications.unscheduled')}
              timeRange={t('medications.asNeeded')}
              icon={Clock}
              iconColor="text-gray-500"
              bgColor="bg-gray-50"
            >
              <div className="space-y-2">{unscheduledMeds.map(renderCard)}</div>
            </TimeSection>
          )}

          {/* Stopped medications: history kept; resume or (with no history) delete */}
          {inactiveMeds.length > 0 && (
            <div className="pt-4 border-t border-gray-100">
              <h4 className="text-sm font-medium text-gray-400 uppercase tracking-wide mb-3">{t('medications.stopped')}</h4>
              <div className="space-y-2">
                {inactiveMeds.map((med) => (
                  <div
                    key={med.id}
                    className="flex items-center gap-3 p-4 bg-gray-50 rounded-xl border border-gray-100"
                  >
                    <div className="w-10 h-10 bg-gray-100 rounded-full flex items-center justify-center shrink-0">
                      <Pill className="w-5 h-5 text-gray-400" />
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="font-semibold text-gray-500 text-base">{med.name}</p>
                      {med.dosage && <p className="text-sm text-gray-400">{med.dosage}</p>}
                    </div>
                    <div className="flex items-center gap-1 shrink-0">
                      <button
                        onClick={() => handleResume(med)}
                        className="flex items-center gap-1 px-3 py-2 text-sm font-medium text-[#0057B8] border border-[#0057B8] rounded-lg hover:bg-blue-50 transition-colors"
                      >
                        <RotateCcw className="w-4 h-4" />{t('medications.resume')}
                      </button>
                      <button
                        onClick={() => handleEdit(med)}
                        aria-label={`${t('medications.edit')}: ${med.name}`}
                        className="p-2 text-gray-400 hover:text-[#0057B8] hover:bg-blue-50 rounded-lg transition-colors"
                      >
                        <Edit3 className="w-4 h-4" />
                      </button>
                      <button
                        onClick={() => handleDelete(med)}
                        aria-label={`${t('common.delete')}: ${med.name}`}
                        className="p-2 text-gray-400 hover:text-red-500 hover:bg-red-50 rounded-lg transition-colors"
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Link to Schedule */}
      <Link
        to="/schedule"
        className="flex items-center justify-between p-4 bg-white rounded-2xl border border-gray-100 shadow-sm hover:shadow-md hover:border-gray-200 transition-all duration-300"
      >
        <span className="font-medium text-gray-900 text-base">{t('schedule.title')}</span>
        <ChevronRight className="w-5 h-5 text-gray-400" />
      </Link>

      {/* FAB - Add Medication */}
      <button
        onClick={() => { resetForm(); setShowForm(true); }}
        className="fixed right-4 bottom-24 sm:bottom-6 w-14 h-14 bg-[#0057B8] text-white rounded-full shadow-fab hover:bg-[#003D82] active:scale-95 transition-all flex items-center justify-center z-40"
        aria-label={t('medications.addNew')}
      >
        <Plus className="w-6 h-6" />
      </button>
    </div>
  );
}
