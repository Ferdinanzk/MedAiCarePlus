import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  ComposedChart, Bar, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from 'recharts';
import { Activity } from 'lucide-react';
import { getFaceToken } from '../lib/face-auth';

interface TrendPoint {
  date: string;
  avg_mood: number | null;
  mood_samples: number;
  dominant_emotion: string | null;
  taken: number;
  total: number;
  adherence_pct: number | null;
}

const MOOD_KEY: Record<number, string> = { 1: 'angry', 2: 'sad', 3: 'neutral', 4: 'happy' };
const MOOD_EMOJI: Record<number, string> = { 1: '😣', 2: '😟', 3: '🙂', 4: '😄' };
const EMOTION_COLOR: Record<string, string> = {
  Happy: '#10b981', Neutral: '#64748b', Sad: '#3b82f6', Angry: '#ef4444',
};
// Soft tile theming for the mood summary, keyed by rounded average mood (1-4).
const MOOD_TILE: Record<number, string> = {
  1: 'from-rose-50 to-red-50 border-rose-100',
  2: 'from-sky-50 to-blue-50 border-sky-100',
  3: 'from-slate-50 to-gray-50 border-slate-100',
  4: 'from-emerald-50 to-teal-50 border-emerald-100',
};

export default function EmotionTrendChart() {
  const { t, i18n } = useTranslation();
  const [range, setRange] = useState<'week' | 'month'>('week');
  const [points, setPoints] = useState<TrendPoint[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    const fetchData = async () => {
      setLoading(true);
      const token = getFaceToken();
      const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
      try {
        const resp = await fetch(`/api/analytics/emotion-trend?range=${range}`, { headers });
        if (resp.ok) {
          const data = await resp.json();
          if (active) setPoints(data.points || []);
        }
      } catch {
        /* network error — leave state unchanged */
      }
      if (active) setLoading(false);
    };
    fetchData();
    return () => { active = false; };
  }, [range]);

  const hasData = points.some((p) => p.avg_mood !== null || p.total > 0);

  // ── Period summary ──────────────────────────────────────────────────────────
  const moodVals = points.filter((p) => p.avg_mood != null).map((p) => p.avg_mood as number);
  const avgMood = moodVals.length ? moodVals.reduce((a, b) => a + b, 0) / moodVals.length : null;
  const moodRound = avgMood != null ? Math.round(avgMood) : null;
  const totalTaken = points.reduce((a, p) => a + p.taken, 0);
  const totalDoses = points.reduce((a, p) => a + p.total, 0);
  const adhPct = totalDoses > 0 ? Math.round((totalTaken / totalDoses) * 100) : null;

  const fmtAxisDate = (d: string) => {
    const dt = new Date(d + 'T00:00:00');
    return range === 'week'
      ? dt.toLocaleDateString(i18n.language, { weekday: 'short' })
      : dt.toLocaleDateString(i18n.language, { month: 'numeric', day: 'numeric' });
  };

  const moodTick = (v: number) => MOOD_EMOJI[v] || '';

  const CustomTooltip = ({ active, payload }: { active?: boolean; payload?: { payload: TrendPoint }[] }) => {
    if (!active || !payload || !payload.length) return null;
    const p = payload[0].payload;
    const moodInt = p.avg_mood != null ? Math.round(p.avg_mood) : null;
    return (
      <div className="bg-white/95 backdrop-blur border border-gray-200 rounded-xl shadow-lg px-3.5 py-2.5 text-xs space-y-1">
        <p className="font-semibold text-gray-900">
          {new Date(p.date + 'T00:00:00').toLocaleDateString(i18n.language, { weekday: 'short', month: 'short', day: 'numeric' })}
        </p>
        <p className="flex items-center gap-1.5 text-gray-600">
          <span className="text-sm">{moodInt ? MOOD_EMOJI[moodInt] : '—'}</span>
          {p.dominant_emotion ? t(`emotion.${p.dominant_emotion.toLowerCase()}`) : t('dashboard.notEnoughData')}
          {p.mood_samples ? <span className="text-gray-400">· {p.mood_samples}</span> : null}
        </p>
        <p className="flex items-center gap-1.5 text-gray-600">
          <span className="inline-block w-2.5 h-2.5 rounded-sm bg-[#0057B8]/70" />
          {t('dashboard.doses')}: {p.total > 0 ? `${p.adherence_pct}% (${p.taken}/${p.total})` : '—'}
        </p>
      </div>
    );
  };

  // Mood dot tinted by that day's dominant emotion; nothing when there's no reading.
  const renderDot = (props: { cx?: number; cy?: number; index?: number; payload?: TrendPoint }) => {
    const { cx, cy, index, payload } = props;
    if (!payload || payload.avg_mood == null || cx == null || cy == null) {
      return <g key={`dot-empty-${index}`} />;
    }
    const color = (payload.dominant_emotion && EMOTION_COLOR[payload.dominant_emotion]) || '#10b981';
    return (
      <g key={`dot-${index}`}>
        <circle cx={cx} cy={cy} r={6} fill={color} opacity={0.18} />
        <circle cx={cx} cy={cy} r={3.5} fill={color} stroke="#fff" strokeWidth={1.5} />
      </g>
    );
  };

  return (
    <section className="relative overflow-hidden rounded-2xl border border-gray-100 bg-gradient-to-b from-white to-slate-50/60 shadow-sm hover:shadow-md transition-all duration-300">
      {/* soft top accent */}
      <div className="absolute inset-x-0 top-0 h-1 bg-gradient-to-r from-emerald-400 via-sky-400 to-[#0057B8]" />

      <div className="p-5">
        {/* Header */}
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2.5">
            <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-emerald-100 to-sky-100 flex items-center justify-center">
              <Activity className="w-5 h-5 text-emerald-600" />
            </div>
            <div>
              <h3 className="font-semibold text-gray-900 leading-tight">{t('dashboard.moodTrend')}</h3>
              <p className="text-2xs text-gray-400 leading-tight">
                {range === 'week' ? t('dashboard.last7Days') : t('dashboard.last30Days')}
              </p>
            </div>
          </div>
          <div className="flex bg-gray-100 rounded-lg p-0.5">
            {(['week', 'month'] as const).map((r) => (
              <button
                key={r}
                onClick={() => setRange(r)}
                className={`px-3 py-1 text-sm rounded-md transition-all ${
                  range === r ? 'bg-white text-[#0057B8] shadow-sm font-medium' : 'text-gray-500 hover:text-gray-700'
                }`}
              >
                {t(`dashboard.${r}`)}
              </button>
            ))}
          </div>
        </div>

        {loading ? (
          <div className="h-64 flex items-center justify-center text-gray-400 text-sm">{t('common.loading')}</div>
        ) : !hasData ? (
          <div className="h-64 flex flex-col items-center justify-center text-gray-400 text-sm gap-2">
            <span className="text-4xl">🌱</span>
            {t('dashboard.notEnoughData')}
          </div>
        ) : (
          <>
            {/* Summary strip */}
            <div className="grid grid-cols-2 gap-3 mb-4">
              <div className={`rounded-xl bg-gradient-to-br border p-3 flex items-center gap-3 ${moodRound ? MOOD_TILE[moodRound] : 'from-gray-50 to-gray-50 border-gray-100'}`}>
                <span className="text-3xl leading-none">{moodRound ? MOOD_EMOJI[moodRound] : '—'}</span>
                <div className="min-w-0">
                  <p className="text-2xs uppercase tracking-wide text-gray-400">{t('dashboard.avgMood')}</p>
                  <p className="text-base font-semibold text-gray-900 truncate">
                    {moodRound ? t(`emotion.${MOOD_KEY[moodRound]}`) : '—'}
                  </p>
                </div>
              </div>
              <div className="rounded-xl bg-gradient-to-br from-blue-50 to-indigo-50 border border-blue-100 p-3 flex items-center gap-3">
                <div className="w-9 h-9 rounded-full bg-[#0057B8]/10 flex items-center justify-center shrink-0">
                  <span className="text-sm font-bold text-[#0057B8]">{adhPct != null ? `${adhPct}` : '–'}</span>
                </div>
                <div className="min-w-0">
                  <p className="text-2xs uppercase tracking-wide text-gray-400">{t('dashboard.adherence')}</p>
                  <p className="text-base font-semibold text-gray-900">
                    {adhPct != null ? `${adhPct}%` : '—'}
                  </p>
                </div>
              </div>
            </div>

            <ResponsiveContainer width="100%" height={196}>
              <ComposedChart data={points} margin={{ top: 6, right: 6, bottom: 2, left: -22 }}>
                <defs>
                  <linearGradient id="moodStroke" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#10b981" />
                    <stop offset="40%" stopColor="#14b8a6" />
                    <stop offset="70%" stopColor="#3b82f6" />
                    <stop offset="100%" stopColor="#ef4444" />
                  </linearGradient>
                  <linearGradient id="moodFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#10b981" stopOpacity={0.22} />
                    <stop offset="100%" stopColor="#3b82f6" stopOpacity={0.02} />
                  </linearGradient>
                  <linearGradient id="adhBar" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#0057B8" stopOpacity={0.28} />
                    <stop offset="100%" stopColor="#0057B8" stopOpacity={0.08} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" vertical={false} />
                <XAxis
                  dataKey="date"
                  tickFormatter={fmtAxisDate}
                  tick={{ fontSize: 11, fill: '#9ca3af' }}
                  tickLine={false}
                  axisLine={{ stroke: '#e5e7eb' }}
                  interval="preserveStartEnd"
                  minTickGap={range === 'month' ? 20 : 4}
                />
                <YAxis
                  yAxisId="mood"
                  domain={[0.6, 4.4]}
                  ticks={[1, 2, 3, 4]}
                  tickFormatter={moodTick}
                  tick={{ fontSize: 15 }}
                  tickLine={false}
                  axisLine={false}
                  width={34}
                />
                <YAxis yAxisId="adh" orientation="right" domain={[0, 100]} hide />
                <Tooltip content={<CustomTooltip />} cursor={{ fill: '#0057B8', fillOpacity: 0.04 }} />
                <Bar
                  yAxisId="adh"
                  dataKey="adherence_pct"
                  fill="url(#adhBar)"
                  radius={[4, 4, 0, 0]}
                  barSize={range === 'week' ? 26 : 8}
                  isAnimationActive
                  animationDuration={700}
                />
                <Area
                  yAxisId="mood"
                  type="monotone"
                  dataKey="avg_mood"
                  stroke="url(#moodStroke)"
                  strokeWidth={3}
                  fill="url(#moodFill)"
                  connectNulls
                  dot={renderDot}
                  activeDot={{ r: 5, strokeWidth: 0 }}
                  isAnimationActive
                  animationDuration={900}
                />
              </ComposedChart>
            </ResponsiveContainer>

            {/* Legend */}
            <div className="flex items-center justify-center gap-5 mt-2 text-2xs text-gray-500">
              <span className="flex items-center gap-1.5">
                <span className="inline-block w-4 h-0.5 rounded-full bg-gradient-to-r from-emerald-500 to-sky-500" />
                {t('dashboard.mood')}
              </span>
              <span className="flex items-center gap-1.5">
                <span className="inline-block w-2.5 h-2.5 rounded-sm bg-[#0057B8]/30" />
                {t('dashboard.doses')}
              </span>
            </div>
          </>
        )}
      </div>
    </section>
  );
}
