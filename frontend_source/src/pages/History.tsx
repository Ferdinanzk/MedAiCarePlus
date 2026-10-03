import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Pill, Smile, TrendingUp, Calendar, Flame, Loader2, ChevronLeft, ChevronRight } from 'lucide-react';
import DoseEmotionChip from '../components/DoseEmotionChip';
import type { DoseEmotion } from '../lib/dose-emotion';

function getAuthHeaders(): Record<string, string> {
  const token = localStorage.getItem('face_auth_token');
  return token ? { Authorization: `Bearer ${token}` } : {};
}

type Tab = 'medications' | 'emotions' | 'adherence';
type Range = '7' | '30' | 'custom';
const PAGE_SIZE = 50;

interface IntakeRecord {
  id: number;
  medication_name: string;
  dosage: string | null;
  status: string;
  scheduled_time: string;
  taken_at: string | null;
  /** Facial expression while it was taken (a camera session's result); absent from an older server. */
  emotion?: DoseEmotion | null;
}

interface IntakePage {
  items: IntakeRecord[];
  total: number;
  offset: number;
  has_more: boolean;
}

interface DayCounts {
  day: string;
  due: number;
  taken: number;
  missed: number;
  skipped: number;
  awaiting: number;
  overdue: number;
}

interface Summary {
  start: string;
  end: string;
  due: number;
  taken: number;
  adherence: number | null;
  streak_days: number;
  days: DayCounts[];
}

interface EmotionRecord {
  id: number;
  emotion_type: string;
  emotion_score: number;
  recorded_at: string;
}

function isoDay(date: Date): string {
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${date.getFullYear()}-${m}-${d}`;
}

function daysAgo(days: number): string {
  const date = new Date();
  date.setDate(date.getDate() - days);
  return isoDay(date);
}

const STATUS_STYLE: Record<string, { dot: string; badge: string }> = {
  taken: { dot: 'bg-green-500', badge: 'bg-green-50 text-green-700' },
  skipped: { dot: 'bg-orange-500', badge: 'bg-orange-50 text-orange-700' },
  missed: { dot: 'bg-red-500', badge: 'bg-red-50 text-red-700' },
  pending_confirmation: { dot: 'bg-amber-500', badge: 'bg-amber-50 text-amber-700' },
  pending: { dot: 'bg-gray-300', badge: 'bg-gray-100 text-gray-700' },
};

export default function History() {
  const { t } = useTranslation();
  const [tab, setTab] = useState<Tab>('medications');
  const [range, setRange] = useState<Range>('30');
  const [customStart, setCustomStart] = useState(daysAgo(29));
  const [customEnd, setCustomEnd] = useState(isoDay(new Date()));
  const [page, setPage] = useState<IntakePage | null>(null);
  const [offset, setOffset] = useState(0);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [emotions, setEmotions] = useState<EmotionRecord[]>([]);
  const [loading, setLoading] = useState(true);

  const start = range === 'custom' ? customStart : daysAgo(Number(range) - 1);
  const end = range === 'custom' ? customEnd : isoDay(new Date());
  const validRange = !!start && !!end && start <= end;

  const statusLabel = (status: string) => {
    if (status === 'pending_confirmation') return t('intake.pendingConfirmation');
    if (status === 'pending') return t('history.notRecorded');
    return t(`intake.${status}`);
  };

  const fetchData = useCallback(async () => {
    setLoading(true);
    const headers = getAuthHeaders();
    try {
      if (validRange) {
        const query = `start=${start}&end=${end}`;
        const summaryRes = await fetch(`/api/history/summary?${query}`, { headers });
        if (summaryRes.ok) setSummary(await summaryRes.json());
        if (tab === 'medications') {
          const res = await fetch(`/api/history/intakes?${query}&limit=${PAGE_SIZE}&offset=${offset}`, { headers });
          if (res.ok) setPage(await res.json());
        }
      }
      if (tab === 'emotions') {
        const res = await fetch('/api/history/emotions', { headers });
        if (res.ok) setEmotions(await res.json());
      }
    } catch {
      // network error — leave state unchanged
    }
    setLoading(false);
  }, [tab, start, end, offset, validRange]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const chooseRange = (next: Range) => {
    setRange(next);
    setOffset(0);
  };

  const tabs: { key: Tab; label: string; icon: typeof Pill }[] = [
    { key: 'medications', label: t('history.medications'), icon: Pill },
    { key: 'emotions', label: t('history.emotions'), icon: Smile },
    { key: 'adherence', label: t('history.adherence'), icon: TrendingUp },
  ];

  const emotionColors: Record<string, string> = {
    Angry: 'bg-red-100 text-red-700',
    Disgust: 'bg-lime-100 text-lime-700',
    Fear: 'bg-purple-100 text-purple-700',
    Sad: 'bg-blue-100 text-blue-700',
    Surprise: 'bg-yellow-100 text-yellow-700',
    Neutral: 'bg-gray-100 text-gray-700',
    Happy: 'bg-green-100 text-green-700',
  };

  const rangeButton = (value: Range, label: string) => (
    <button
      key={value}
      onClick={() => chooseRange(value)}
      aria-pressed={range === value}
      className={`px-4 py-2 rounded-full text-base font-medium border transition-all ${
        range === value ? 'bg-[#0057B8] border-[#0057B8] text-white' : 'bg-white border-gray-200 text-gray-600'}`}
    >
      {label}
    </button>
  );

  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-bold text-gray-900">{t('history.title')}</h2>

      {/* Period */}
      {tab !== 'emotions' && (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-2">
            {rangeButton('7', t('history.last7'))}
            {rangeButton('30', t('history.last30'))}
            {rangeButton('custom', t('history.custom'))}
          </div>
          {range === 'custom' && (
            <div className="flex flex-wrap items-end gap-3">
              <label className="text-sm text-gray-600">
                {t('history.from')}
                <input type="date" value={customStart} max={customEnd}
                  onChange={e => { setCustomStart(e.target.value); setOffset(0); }}
                  className="block mt-1 px-3 py-2 rounded-xl bg-gray-50 border border-gray-200" />
              </label>
              <label className="text-sm text-gray-600">
                {t('history.to')}
                <input type="date" value={customEnd} min={customStart} max={isoDay(new Date())}
                  onChange={e => { setCustomEnd(e.target.value); setOffset(0); }}
                  className="block mt-1 px-3 py-2 rounded-xl bg-gray-50 border border-gray-200" />
              </label>
            </div>
          )}
        </div>
      )}

      {/* Summary */}
      {tab !== 'emotions' && summary && (
        <div className="bg-gradient-to-r from-[#0057B8] to-[#003D82] rounded-2xl p-5 text-white shadow-hero grid grid-cols-2 gap-4">
          <div>
            <p className="text-3xl font-bold">{summary.adherence == null ? '—' : `${Math.round(summary.adherence)}%`}</p>
            <p className="text-base opacity-90">
              {summary.due > 0 ? t('history.dosesTaken', { taken: summary.taken, due: summary.due }) : t('history.noDueDoses')}
            </p>
          </div>
          <div className="flex items-center gap-3">
            <div className="w-12 h-12 bg-white/20 rounded-xl flex items-center justify-center">
              <Flame className="w-6 h-6" />
            </div>
            <div>
              <p className="text-3xl font-bold">{summary.streak_days}</p>
              <p className="text-base opacity-90">{t('history.streak')}</p>
            </div>
          </div>
        </div>
      )}

      {/* Tabs */}
      <div className="flex gap-2">
        {tabs.map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`flex-1 flex flex-col items-center gap-1 py-3 rounded-xl border text-base font-medium transition-all ${
              tab === key
                ? 'bg-[#0057B8] text-white border-[#0057B8]'
                : 'bg-white text-gray-600 border-gray-200 hover:border-gray-300'
            }`}
          >
            <Icon className="w-5 h-5" />
            {label}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="flex flex-col items-center justify-center h-40 gap-3">
          <Loader2 className="w-8 h-8 animate-spin text-[#0057B8]" />
          <span className="text-gray-500 text-base">{t('common.loading')}</span>
        </div>
      ) : (
        <>
          {/* Medications Tab: past doses only, newest first */}
          {tab === 'medications' && (
            <div className="space-y-2">
              {!page || page.items.length === 0 ? (
                <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-8 text-center text-gray-500">{t('history.noMedicationHistory')}</div>
              ) : (
                <>
                  {page.items.map((item) => {
                    const style = STATUS_STYLE[item.status] || STATUS_STYLE.pending;
                    return (
                      <div
                        key={item.id}
                        className="flex items-center gap-3 p-4 bg-white rounded-xl border border-gray-100 shadow-sm"
                      >
                        <div className={`w-2 h-2 rounded-full shrink-0 ${style.dot}`} />
                        <div className="flex-1 min-w-0">
                          <p className="font-medium text-gray-900">{item.medication_name}{item.dosage ? ` · ${item.dosage}` : ''}</p>
                          <p className="text-sm text-gray-500">
                            {new Date(item.scheduled_time).toLocaleString()}
                            {item.status === 'taken' && item.taken_at && (
                              <> · {t('history.takenAt', { time: new Date(item.taken_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) })}</>
                            )}
                          </p>
                        </div>
                        <div className="flex flex-col items-end gap-1 shrink-0">
                          <span className={`px-2.5 py-1 rounded-full text-2xs font-medium ${style.badge}`}>
                            {statusLabel(item.status)}
                          </span>
                          {item.emotion?.dominant && (
                            <DoseEmotionChip dominant={item.emotion.dominant} score={item.emotion.score}
                              occluded={item.emotion.mostly_occluded} uncertain={item.emotion.uncertain} />
                          )}
                        </div>
                      </div>
                    );
                  })}
                  <div className="flex items-center justify-between pt-2">
                    <button
                      onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                      disabled={offset === 0}
                      className="flex items-center gap-1 px-4 py-2 rounded-xl border text-base disabled:opacity-40"
                    >
                      <ChevronLeft className="w-4 h-4" />{t('history.newer')}
                    </button>
                    <span className="text-sm text-gray-500">
                      {t('history.showing', { from: page.offset + 1, to: page.offset + page.items.length, total: page.total })}
                    </span>
                    <button
                      onClick={() => setOffset(offset + PAGE_SIZE)}
                      disabled={!page.has_more}
                      className="flex items-center gap-1 px-4 py-2 rounded-xl border text-base disabled:opacity-40"
                    >
                      {t('history.older')}<ChevronRight className="w-4 h-4" />
                    </button>
                  </div>
                </>
              )}
            </div>
          )}

          {/* Emotions Tab */}
          {tab === 'emotions' && (
            <div className="space-y-2">
              {emotions.length === 0 ? (
                <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-8 text-center text-gray-500">{t('history.noEmotionRecords')}</div>
              ) : (
                emotions.map((item) => (
                  <div
                    key={item.id}
                    className="flex items-center gap-3 p-4 bg-white rounded-xl border border-gray-100 shadow-sm"
                  >
                    <span className={`px-3 py-1 rounded-full text-2xs font-medium ${emotionColors[item.emotion_type] || 'bg-gray-100 text-gray-700'}`}>
                      {t(`emotion.${item.emotion_type.toLowerCase()}`)}
                    </span>
                    <div className="flex-1 min-w-0">
                      <p className="text-sm text-gray-500">
                        {new Date(item.recorded_at).toLocaleString()}
                      </p>
                    </div>
                    <div className="w-16 h-1.5 bg-gray-100 rounded-full overflow-hidden">
                      <div
                        className="h-full bg-[#0057B8] rounded-full"
                        style={{ width: `${item.emotion_score * 100}%` }}
                      />
                    </div>
                  </div>
                ))
              )}
            </div>
          )}

          {/* Adherence Tab: per day, due doses only */}
          {tab === 'adherence' && (
            <div className="space-y-4">
              {!summary || summary.days.length === 0 ? (
                <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-8 text-center text-gray-500">{t('history.noData')}</div>
              ) : (
                [...summary.days].reverse().map((day) => {
                  const rate = day.due > 0 ? Math.round((day.taken / day.due) * 100) : 0;
                  return (
                    <div key={day.day} className="bg-white rounded-xl p-4 border border-gray-100 shadow-sm">
                      <div className="flex items-center justify-between mb-2">
                        <div className="flex items-center gap-2">
                          <Calendar className="w-4 h-4 text-gray-400" />
                          <span className="text-base font-medium text-gray-900">{day.day}</span>
                        </div>
                        <span className={`text-base font-bold ${rate >= 80 ? 'text-green-600' : rate >= 50 ? 'text-amber-600' : 'text-red-600'}`}>
                          {rate}%
                        </span>
                      </div>
                      <div className="w-full h-2 bg-gray-100 rounded-full overflow-hidden">
                        <div
                          className={`h-full rounded-full transition-all ${
                            rate >= 80 ? 'bg-green-500' : rate >= 50 ? 'bg-amber-500' : 'bg-red-500'
                          }`}
                          style={{ width: `${rate}%` }}
                        />
                      </div>
                      <p className="text-sm text-gray-500 mt-1">
                        {t('history.dosesTaken', { taken: day.taken, due: day.due })}
                        {day.missed > 0 && ` · ${t('intake.missed')} ${day.missed}`}
                        {day.skipped > 0 && ` · ${t('intake.skipped')} ${day.skipped}`}
                        {day.awaiting > 0 && ` · ${t('intake.pendingConfirmation')} ${day.awaiting}`}
                        {day.overdue > 0 && ` · ${t('history.notRecorded')} ${day.overdue}`}
                      </p>
                    </div>
                  );
                })
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
