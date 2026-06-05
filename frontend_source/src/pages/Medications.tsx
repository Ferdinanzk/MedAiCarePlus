import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { getFaceToken } from '../lib/face-auth';
import { getExpiryStatus } from '../lib/expiry';
import TimeSection from '../components/ui/TimeSection';
import {
  Pill,
  Plus,
  ChevronRight,
  AlertTriangle,
  Clock,
  Package,
  Edit3,
  Trash2,
  Loader2,
  X,
  CalendarClock,
  Sun,
  Sunset,
  Moon,
  Bed,
} from 'lucide-react';

interface ScheduleTime {
  morning: boolean;
  noon: boolean;
  night: boolean;
  bedtime: boolean;
  before_meals: boolean;
  after_meals: boolean;
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
}

const SCHEDULE_OPTIONS: { key: keyof ScheduleTime; label: string }[] = [
  { key: 'morning',      label: '早上' },
  { key: 'noon',         label: '中午' },
  { key: 'night',        label: '晚上' },
  { key: 'bedtime',      label: '睡前' },
  { key: 'before_meals', label: '飯前' },
  { key: 'after_meals',  label: '飯後' },
];

const EMPTY_SCHEDULE: ScheduleTime = {
  morning: false, noon: false, night: false,
  bedtime: false, before_meals: false, after_meals: false,
};

const PERIOD_CONFIG = {
  morning:   { icon: Sun,    label: 'Morning',    timeRange: '6:00-10:00',  iconColor: 'text-amber-500',  bgColor: 'bg-amber-50' },
  afternoon: { icon: Sunset, label: 'Afternoon',  timeRange: '12:00-15:00', iconColor: 'text-orange-500', bgColor: 'bg-orange-50' },
  evening:   { icon: Moon,   label: 'Evening',    timeRange: '18:00-21:00', iconColor: 'text-indigo-500', bgColor: 'bg-indigo-50' },
  bedtime:   { icon: Bed,    label: 'Bedtime',    timeRange: '21:00-23:00', iconColor: 'text-purple-500', bgColor: 'bg-purple-50' },
};

function getAuthHeaders(): Record<string, string> {
  const token = getFaceToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export default function Medications() {
  const { t } = useTranslation();
  const [medications, setMedications] = useState<Medication[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);

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
  });

  useEffect(() => { fetchMedications(); }, []);

  const fetchMedications = async () => {
    setLoading(true);
    const headers = getAuthHeaders();
    const resp = await fetch('/api/medications', { headers }).catch(() => null);
    if (resp?.ok) setMedications(await resp.json());
    setLoading(false);
  };

  const resetForm = () => {
    setForm({ name: '', dosage: '', total_pills: 30, pills_remaining: 30, instructions: '', warning: '', pill_description: '', use_before: '', is_active: true, schedule_time: { ...EMPTY_SCHEDULE } });
    setEditingId(null);
    setShowForm(false);
  };

  const handleEdit = (med: Medication) => {
    setForm({
      name: med.name,
      dosage: med.dosage || '',
      total_pills: med.total_pills,
      pills_remaining: med.pills_remaining,
      instructions: med.instructions || '',
      warning: med.warning || '',
      pill_description: med.pill_description || '',
      use_before: med.use_before || '',
      is_active: med.is_active,
      schedule_time: med.schedule_time ? { ...EMPTY_SCHEDULE, ...med.schedule_time } : { ...EMPTY_SCHEDULE },
    });
    setEditingId(med.id);
    setShowForm(true);
  };

  const handleDelete = async (id: number) => {
    if (!confirm(t('medications.deleteConfirm'))) return;
    const headers = getAuthHeaders();
    await fetch(`/api/medications/${id}`, { method: 'DELETE', headers });
    fetchMedications();
  };

  const toggleSchedule = (key: keyof ScheduleTime) => {
    setForm(f => ({ ...f, schedule_time: { ...f.schedule_time, [key]: !f.schedule_time[key] } }));
  };

  const hasSchedule = Object.values(form.schedule_time).some(Boolean);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    const headers = getAuthHeaders();
    if (!headers.Authorization) { setSaving(false); return; }

    const payload = {
      name: form.name,
      dosage: form.dosage || null,
      total_pills: form.total_pills,
      pills_remaining: form.pills_remaining,
      instructions: form.instructions || null,
      warning: form.warning || null,
      pill_description: form.pill_description || null,
      use_before: form.use_before || null,
      is_active: form.is_active,
      schedule_time: hasSchedule ? form.schedule_time : null,
    };

    const url = editingId ? `/api/medications/${editingId}` : '/api/medications';
    const method = editingId ? 'PATCH' : 'POST';
    await fetch(url, { method, headers: { ...headers, 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });

    setSaving(false);
    resetForm();
    fetchMedications();
  };

  // Group medications by time period
  const groupedMeds = {
    morning: medications.filter(m => m.schedule_time?.morning && m.is_active),
    afternoon: medications.filter(m => m.schedule_time?.noon && m.is_active),
    evening: medications.filter(m => m.schedule_time?.night && m.is_active),
    bedtime: medications.filter(m => m.schedule_time?.bedtime && m.is_active),
  };

  const unscheduledMeds = medications.filter(m => {
    const s = m.schedule_time;
    return m.is_active && (!s || (!s.morning && !s.noon && !s.night && !s.bedtime));
  });

  const inactiveMeds = medications.filter(m => !m.is_active);

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-3">
        <Loader2 className="w-8 h-8 animate-spin text-[#0057B8]" />
        <span className="text-gray-500 text-sm">{t('common.loading')}</span>
      </div>
    );
  }

  return (
    <div className="space-y-5 relative">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-gray-900">{t('medications.title')}</h2>
          <p className="text-base text-gray-500 mt-0.5">{new Date().toLocaleDateString(undefined, { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' })}</p>
        </div>
      </div>

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
                  <label className="block text-sm font-medium text-gray-500 uppercase tracking-wide mb-1.5">{t('medications.name')} *</label>
                  <input
                    type="text"
                    value={form.name}
                    onChange={e => setForm({ ...form, name: e.target.value })}
                    className="w-full px-4 py-3 rounded-xl bg-gray-50 border border-gray-200 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#0057B8]/30 focus:border-[#0057B8] transition-all"
                    placeholder="e.g. Allegra (Fexofenadine) 60mg/tab"
                    required
                  />
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-sm font-medium text-gray-500 uppercase tracking-wide mb-1.5">{t('medications.dosage')}</label>
                    <input
                      type="text"
                      value={form.dosage}
                      onChange={e => setForm({ ...form, dosage: e.target.value })}
                      className="w-full px-4 py-3 rounded-xl bg-gray-50 border border-gray-200 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#0057B8]/30 focus:border-[#0057B8] transition-all"
                      placeholder="e.g. 60mg/tab"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-500 uppercase tracking-wide mb-1.5">外觀 Appearance</label>
                    <input
                      type="text"
                      value={form.pill_description}
                      onChange={e => setForm({ ...form, pill_description: e.target.value })}
                      className="w-full px-4 py-3 rounded-xl bg-gray-50 border border-gray-200 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#0057B8]/30 focus:border-[#0057B8] transition-all"
                      placeholder="e.g. 橢圓形橘色"
                    />
                  </div>
                </div>
              </div>

              {/* Quantity */}
              <div className="px-5 py-4 grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-medium text-gray-500 uppercase tracking-wide mb-1.5">總數量 Total Pills</label>
                  <input
                    type="number"
                    value={form.total_pills}
                    onChange={e => setForm({ ...form, total_pills: parseInt(e.target.value) || 0 })}
                    className="w-full px-4 py-3 rounded-xl bg-gray-50 border border-gray-200 text-gray-900 focus:outline-none focus:ring-2 focus:ring-[#0057B8]/30 focus:border-[#0057B8] transition-all"
                    min={0}
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-500 uppercase tracking-wide mb-1.5">{t('medications.pillsLeft')}</label>
                  <input
                    type="number"
                    value={form.pills_remaining}
                    onChange={e => setForm({ ...form, pills_remaining: parseInt(e.target.value) || 0 })}
                    className="w-full px-4 py-3 rounded-xl bg-gray-50 border border-gray-200 text-gray-900 focus:outline-none focus:ring-2 focus:ring-[#0057B8]/30 focus:border-[#0057B8] transition-all"
                    min={0}
                  />
                </div>
              </div>

              {/* Schedule */}
              <div className="px-5 py-4">
                <label className="block text-sm font-medium text-gray-500 uppercase tracking-wide mb-1.5">服藥時間 Schedule</label>
                <div className="flex flex-wrap gap-2 mt-1">
                  {SCHEDULE_OPTIONS.map(({ key, label }) => (
                    <button
                      key={key}
                      type="button"
                      onClick={() => toggleSchedule(key)}
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

              {/* Instructions + use before */}
              <div className="px-5 py-4 space-y-3">
                <div>
                  <label className="block text-sm font-medium text-gray-500 uppercase tracking-wide mb-1.5">{t('medications.instructions')}</label>
                  <textarea
                    value={form.instructions}
                    onChange={e => setForm({ ...form, instructions: e.target.value })}
                    className="w-full px-4 py-3 rounded-xl bg-gray-50 border border-gray-200 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#0057B8]/30 focus:border-[#0057B8] transition-all"
                    rows={2}
                    placeholder="e.g. 每天兩次，早晚飯後使用"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-500 uppercase tracking-wide mb-1.5">有效期限 Use Before</label>
                  <input
                    type="text"
                    value={form.use_before}
                    onChange={e => setForm({ ...form, use_before: e.target.value })}
                    className="w-full px-4 py-3 rounded-xl bg-gray-50 border border-gray-200 text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#0057B8]/30 focus:border-[#0057B8] transition-all"
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
                <label className="block text-sm font-medium text-gray-500 uppercase tracking-wide mb-1.5">{t('medications.warning')}</label>
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
                  className={`relative w-11 h-6 rounded-full transition-colors ${form.is_active ? 'bg-[#0057B8]' : 'bg-gray-200'}`}
                >
                  <span className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow transition-transform ${form.is_active ? 'translate-x-5' : ''}`} />
                </button>
              </div>

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
          {Object.entries(groupedMeds).map(([period, meds]) => {
            if (meds.length === 0) return null;
            const config = PERIOD_CONFIG[period as keyof typeof PERIOD_CONFIG];
            const Icon = config.icon;
            return (
              <TimeSection
                key={period}
                title={config.label}
                timeRange={config.timeRange}
                icon={Icon}
                iconColor={config.iconColor}
                bgColor={config.bgColor}
              >
                <div className="space-y-2">
                  {meds.map((med) => (
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
                        <div className="flex items-center gap-3 mt-1 text-sm text-gray-400">
                          <span className="flex items-center gap-1">
                            <Package className="w-3 h-3" />
                            {med.pills_remaining}/{med.total_pills}
                          </span>
                          {med.instructions && (
                            <span className="flex items-center gap-1 truncate max-w-[180px]">
                              <Clock className="w-3 h-3 shrink-0" />
                              {med.instructions}
                            </span>
                          )}
                        </div>
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
                      <div className="flex items-center gap-1 shrink-0">
                        <button
                          onClick={() => handleEdit(med)}
                          className="p-2 text-gray-400 hover:text-[#0057B8] hover:bg-blue-50 rounded-lg transition-colors"
                        >
                          <Edit3 className="w-4 h-4" />
                        </button>
                        <button
                          onClick={() => handleDelete(med.id)}
                          className="p-2 text-gray-400 hover:text-red-500 hover:bg-red-50 rounded-lg transition-colors"
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              </TimeSection>
            );
          })}

          {/* Unscheduled medications */}
          {unscheduledMeds.length > 0 && (
            <TimeSection
              title="Unscheduled"
              timeRange="As needed"
              icon={Clock}
              iconColor="text-gray-500"
              bgColor="bg-gray-50"
            >
              <div className="space-y-2">
                {unscheduledMeds.map((med) => (
                  <div
                    key={med.id}
                    className="flex items-center gap-3 p-4 bg-white rounded-xl border border-gray-100 shadow-sm hover:shadow-md hover:border-gray-200 transition-all duration-300"
                  >
                    <div className="w-10 h-10 bg-gray-50 rounded-full flex items-center justify-center shrink-0">
                      <Pill className="w-5 h-5 text-gray-500" />
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="font-semibold text-gray-900 text-base">{med.name}</p>
                      {med.dosage && <p className="text-sm text-gray-500">{med.dosage}</p>}
                    </div>
                    <div className="flex items-center gap-1 shrink-0">
                      <button
                        onClick={() => handleEdit(med)}
                        className="p-2 text-gray-400 hover:text-[#0057B8] hover:bg-blue-50 rounded-lg transition-colors"
                      >
                        <Edit3 className="w-4 h-4" />
                      </button>
                      <button
                        onClick={() => handleDelete(med.id)}
                        className="p-2 text-gray-400 hover:text-red-500 hover:bg-red-50 rounded-lg transition-colors"
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </TimeSection>
          )}

          {/* Inactive medications */}
          {inactiveMeds.length > 0 && (
            <div className="pt-4 border-t border-gray-100">
              <h4 className="text-sm font-medium text-gray-400 uppercase tracking-wide mb-3">Inactive</h4>
              <div className="space-y-2">
                {inactiveMeds.map((med) => (
                  <div
                    key={med.id}
                    className="flex items-center gap-3 p-4 bg-gray-50 rounded-xl border border-gray-100 opacity-60"
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
                        onClick={() => handleEdit(med)}
                        className="p-2 text-gray-400 hover:text-[#0057B8] hover:bg-blue-50 rounded-lg transition-colors"
                      >
                        <Edit3 className="w-4 h-4" />
                      </button>
                      <button
                        onClick={() => handleDelete(med.id)}
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
