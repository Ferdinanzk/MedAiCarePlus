import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { getFaceToken } from '../lib/face-auth';
import { Pill, CalendarDays, CheckCircle2 } from 'lucide-react';

interface ScheduleItem {
  id: number;
  med_id: number;
  name: string;
  dosage: string | null;
  scheduled_time: string;
  status: string;
  slot_label: string;
  use_before_warning: string | null;
}

interface RawScheduleItem {
  id?: number;
  intk_id?: number;
  intake_id?: number;
  med_id?: number;
  name?: string;
  dosage?: string | null;
  scheduled_time?: string;
  status?: string;
  slot_label?: string;
  use_before_warning?: string | null;
}

function getAuthHeaders(): Record<string, string> {
  const token = getFaceToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export default function Schedule() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const [schedules, setSchedules] = useState<ScheduleItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedDate, setSelectedDate] = useState(new Date().toISOString().split('T')[0]);

  const handleTakeEarly = async (medId: number) => {
    navigate(`/intake?med=${medId}`);
  };

  const fetchSchedule = useCallback(async () => {
    setLoading(true);
    try {
      const headers = getAuthHeaders();
      const response = await fetch('/api/medications/today', {
        headers,
      });
      if (!response.ok) {
        console.error('Failed to fetch schedule:', response.status);
        setSchedules([]);
        setLoading(false);
        return;
      }
      const data = await response.json() as RawScheduleItem[];
      const mapped = data.map((item: RawScheduleItem) => ({
        id: item.id ?? item.intk_id ?? item.intake_id ?? 0,
        med_id: item.med_id ?? 0,
        name: item.name || '',
        dosage: item.dosage ?? null,
        scheduled_time: item.scheduled_time || '',
        status: item.status || 'pending',
        slot_label: item.slot_label || '',
        use_before_warning: item.use_before_warning ?? null,
      }));
      setSchedules(mapped);
    } catch (error) {
      console.error('Error fetching schedule:', error);
      setSchedules([]);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    fetchSchedule();
  }, [selectedDate, fetchSchedule]);

  const weekDays = Array.from({ length: 7 }, (_, i) => {
    const d = new Date();
    d.setDate(d.getDate() + i);
    return {
      date: d.toISOString().split('T')[0],
      label: i === 0 ? t('schedule.today') : d.toLocaleDateString(i18n.language === 'zh-TW' ? 'zh-TW' : 'en', { weekday: 'short' }),
      dayNum: d.getDate(),
    };
  });

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-medical-600"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold text-gray-900">{t('schedule.title')}</h2>

      {/* Date Selector */}
      <div className="flex gap-2 overflow-x-auto pb-2">
        {weekDays.map((d) => (
          <button
            key={d.date}
            onClick={() => setSelectedDate(d.date)}
            className={`flex flex-col items-center justify-center min-w-[64px] h-20 rounded-xl border transition-colors ${
              d.date === selectedDate
                ? 'bg-medical-600 text-white border-medical-600'
                : 'bg-white text-gray-600 border-gray-200 hover:border-medical-300'
            }`}
          >
            <span className="text-base font-medium">{d.label}</span>
            <span className="text-lg font-bold">{d.dayNum}</span>
          </button>
        ))}
      </div>

      {/* Schedule List */}
      {schedules.length === 0 ? (
        <div className="bg-gray-50 rounded-xl p-8 text-center">
          <CalendarDays className="w-12 h-12 text-gray-300 mx-auto mb-3" />
          <p className="text-gray-500 text-base">{t('schedule.noSchedule')}</p>
        </div>
      ) : (
        <div className="space-y-3">
          {schedules.map((s) => (
            <div
              key={s.id}
              className="flex items-center gap-4 p-5 bg-white rounded-2xl border border-gray-100 shadow-sm hover:shadow-md transition-all"
            >
              {/* Time Column */}
              <div className="flex flex-col items-center min-w-[60px]">
                <span className="text-lg font-bold text-[#0057B8]">
                  {new Date(s.scheduled_time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                </span>
                <span className="text-base text-gray-500">{s.slot_label}</span>
              </div>

              {/* Divider */}
              <div className="w-px h-12 bg-gray-200" />

              {/* Medication Info */}
              <div className="flex-1 min-w-0">
                <p className="font-semibold text-gray-900 text-base flex items-center gap-2">
                  <Pill className="w-5 h-5 text-[#0057B8]" />
                  {s.name}
                </p>
                {s.dosage && (
                  <p className="text-base text-gray-600 mt-1">{s.dosage}</p>
                )}
                {s.use_before_warning && (
                  <p className="text-sm text-orange-600 mt-1">{s.use_before_warning}</p>
                )}
              </div>

              {/* Status / Take Action */}
              {s.status === 'pending' ? (
                <button
                  onClick={() => handleTakeEarly(s.med_id)}
                  className="flex items-center justify-center min-w-[48px] min-h-[48px] rounded-full border-2 border-[#0057B8] text-[#0057B8] hover:bg-blue-50 active:scale-95 transition-all"
                  title={t('intake.take')}
                >
                  <CheckCircle2 className="w-6 h-6" />
                </button>
              ) : (
                <div className={`px-3 py-1.5 rounded-full text-base font-medium ${
                  s.status === 'taken' ? 'bg-green-50 text-green-600' :
                  s.status === 'missed' ? 'bg-red-50 text-red-600' :
                  'bg-blue-50 text-blue-600'
                }`}>
                  {s.status === 'taken' ? t('intake.taken') :
                   s.status === 'missed' ? t('intake.missed') :
                   t('intake.pending')}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
