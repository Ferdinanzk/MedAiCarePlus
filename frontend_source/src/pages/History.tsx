import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Pill, Smile, TrendingUp, Calendar, Flame, Loader2 } from 'lucide-react';

function getAuthHeaders(): Record<string, string> {
  const token = localStorage.getItem('face_auth_token');
  return token ? { Authorization: `Bearer ${token}` } : {};
}

type Tab = 'medications' | 'emotions' | 'adherence';

interface IntakeRecord {
  id: number;
  medication_name: string;
  status: string;
  scheduled_time: string;
}

interface EmotionRecord {
  id: number;
  emotion_type: string;
  emotion_score: number;
  recorded_at: string;
}

export default function History() {
  const { t } = useTranslation();
  const [tab, setTab] = useState<Tab>('medications');
  const [intakes, setIntakes] = useState<IntakeRecord[]>([]);
  const [emotions, setEmotions] = useState<EmotionRecord[]>([]);
  const [streak, setStreak] = useState(0);
  const [loading, setLoading] = useState(true);

  const fetchData = useCallback(async () => {
    setLoading(true);
    const headers = getAuthHeaders();

    if (tab === 'medications' || tab === 'adherence') {
      try {
        const res = await fetch('/api/history/intakes', { headers });
        if (res.ok) {
          const data: IntakeRecord[] = await res.json();
          setIntakes(data);

          let currentStreak = 0;
          const dates = [...new Set(data.map((m) => (m.scheduled_time || '').split('T')[0]))].sort().reverse();
          for (const date of dates) {
            const dayRecords = data.filter((m) => m.scheduled_time?.startsWith(date));
            const allTaken = dayRecords.length > 0 && dayRecords.every((m) => m.status === 'taken');
            if (allTaken) currentStreak++;
            else break;
          }
          setStreak(currentStreak);
        }
      } catch {
        // network error — leave state unchanged
      }
    }

    if (tab === 'emotions') {
      try {
        const res = await fetch('/api/history/emotions', { headers });
        if (res.ok) {
          const data: EmotionRecord[] = await res.json();
          setEmotions(data);
        }
      } catch {
        // network error — leave state unchanged
      }
    }

    setLoading(false);
  }, [tab]);

  useEffect(() => {
    fetchData();
  }, [tab, fetchData]);

  const tabs: { key: Tab; label: string; icon: typeof Pill }[] = [
    { key: 'medications', label: t('history.medications'), icon: Pill },
    { key: 'emotions', label: t('history.emotions'), icon: Smile },
    { key: 'adherence', label: t('history.adherence'), icon: TrendingUp },
  ];

  const emotionColors: Record<string, string> = {
    Angry: 'bg-red-100 text-red-700',
    Sad: 'bg-blue-100 text-blue-700',
    Neutral: 'bg-gray-100 text-gray-700',
    Happy: 'bg-green-100 text-green-700',
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
      <h2 className="text-2xl font-bold text-gray-900">{t('history.title')}</h2>

      {/* Streak Card */}
      <div className="bg-gradient-to-r from-[#0057B8] to-[#003D82] rounded-2xl p-5 text-white shadow-hero">
        <div className="flex items-center gap-3">
          <div className="w-12 h-12 bg-white/20 rounded-xl flex items-center justify-center">
            <Flame className="w-6 h-6" />
          </div>
          <div>
            <p className="text-3xl font-bold">{streak}</p>
            <p className="text-base opacity-90">{t('history.streak')}</p>
          </div>
        </div>
      </div>

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

      {/* Medications Tab */}
      {tab === 'medications' && (
        <div className="space-y-2">
          {intakes.length === 0 ? (
            <div className="bg-white rounded-xl border border-gray-100 shadow-sm hover:shadow-md transition-all duration-300 hover:border-gray-200 p-8 text-center text-gray-500">{t('history.noMedicationHistory')}</div>
          ) : (
            intakes.map((item) => (
              <div
                key={item.id}
                className="flex items-center gap-3 p-4 bg-white rounded-xl border border-gray-100 shadow-sm hover:shadow-md transition-all duration-300 hover:border-gray-200"
              >
                <div
                  className={`w-2 h-2 rounded-full shrink-0 ${
                    item.status === 'taken'
                      ? 'bg-green-500'
                      : item.status === 'skipped'
                      ? 'bg-orange-500'
                      : item.status === 'missed'
                      ? 'bg-red-500'
                      : 'bg-gray-300'
                  }`}
                />
                <div className="flex-1 min-w-0">
                  <p className="font-medium text-gray-900">{item.medication_name}</p>
                  <p className="text-sm text-gray-500">
                    {new Date(item.scheduled_time).toLocaleString()}
                  </p>
                </div>
                <span
                  className={`px-2.5 py-1 rounded-full text-2xs font-medium capitalize ${
                    item.status === 'taken'
                      ? 'bg-green-50 text-green-700'
                      : item.status === 'skipped'
                      ? 'bg-orange-50 text-orange-700'
                      : item.status === 'missed'
                      ? 'bg-red-50 text-red-700'
                      : 'bg-gray-100 text-gray-700'
                  }`}
                >
                  {item.status}
                </span>
              </div>
            ))
          )}
        </div>
      )}

      {/* Emotions Tab */}
      {tab === 'emotions' && (
        <div className="space-y-2">
          {emotions.length === 0 ? (
            <div className="bg-white rounded-xl border border-gray-100 shadow-sm hover:shadow-md transition-all duration-300 hover:border-gray-200 p-8 text-center text-gray-500">{t('history.noEmotionRecords')}</div>
          ) : (
            emotions.map((item) => (
              <div
                key={item.id}
                className="flex items-center gap-3 p-4 bg-white rounded-xl border border-gray-100 shadow-sm hover:shadow-md transition-all duration-300 hover:border-gray-200"
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

      {/* Adherence Tab */}
      {tab === 'adherence' && (
        <div className="space-y-4">
          {(() => {
            const grouped: Record<string, { total: number; taken: number }> = {};
            intakes.forEach((i) => {
              const date = i.scheduled_time.split('T')[0];
              if (!grouped[date]) grouped[date] = { total: 0, taken: 0 };
              grouped[date].total++;
              if (i.status === 'taken') grouped[date].taken++;
            });
            const dates = Object.keys(grouped).sort().reverse();
            return dates.length === 0 ? (
              <div className="bg-white rounded-xl border border-gray-100 shadow-sm hover:shadow-md transition-all duration-300 hover:border-gray-200 p-8 text-center text-gray-500">{t('history.noData')}</div>
            ) : (
              dates.map((date) => {
                const rate = Math.round((grouped[date].taken / grouped[date].total) * 100);
                return (
                  <div key={date} className="bg-white rounded-xl p-4 border border-gray-100 shadow-sm hover:shadow-md transition-all duration-300 hover:border-gray-200">
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center gap-2">
                        <Calendar className="w-4 h-4 text-gray-400" />
                        <span className="text-base font-medium text-gray-900">{date}</span>
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
                      {grouped[date].taken}/{grouped[date].total} taken
                    </p>
                  </div>
                );
              })
            );
          })()}
        </div>
      )}
    </div>
  );
}
