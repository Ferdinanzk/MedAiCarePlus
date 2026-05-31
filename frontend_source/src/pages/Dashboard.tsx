import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { getFaceSession } from '../lib/face-auth';
import {
  Pill,
  Users,
  Activity,
  FileText,
  Plus,
  ChevronRight,
  Clock,
  CheckCircle2,
  XCircle,
  AlertCircle,
  Heart,
} from 'lucide-react';
import ServiceCard from '../components/ui/ServiceCard';

interface TodayMedication {
  id: number;
  medication_id?: number;
  name: string;
  dosage: string | null;
  status: 'pending' | 'taken' | 'skipped' | 'missed';
  scheduled_time: string | null;
  pills_remaining: number;
}

interface EmotionRecord {
  id: number;
  emotion_type: string;
  emotion_score: number;
  recorded_at: string;
}

interface RawMedItem {
  id?: number;
  med_id?: number;
  name?: string;
  dosage?: string | null;
  pills_remaining?: number;
  status?: string;
  scheduled_time?: string | null;
}

export default function Dashboard() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [medications, setMedications] = useState<TodayMedication[]>([]);
  const [emotions, setEmotions] = useState<EmotionRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [adherenceRate, setAdherenceRate] = useState(0);
  const [takenCount, setTakenCount] = useState(0);
  const [greeting, setGreeting] = useState('');

  const getGreeting = useCallback(() => {
    const hour = new Date().getHours();
    if (hour < 12) return t('dashboard.goodMorning') || 'Good morning';
    if (hour < 18) return t('dashboard.goodAfternoon') || 'Good afternoon';
    return t('dashboard.goodEvening') || 'Good evening';
  }, [t]);

  useEffect(() => {
    fetchTodayData();
    setGreeting(getGreeting());
  }, [getGreeting]);

  const fetchTodayData = async () => {
    setLoading(true);
    const faceUser = getFaceSession();
    const token = faceUser ? localStorage.getItem('face_auth_token') : null;
    const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};

    // Fetch today's medications with intake status
    try {
      const resp = await fetch('/api/medications/today', { headers });
      if (resp.ok) {
        const medsData = await resp.json() as RawMedItem[];
        const formatted = medsData.map((item: RawMedItem) => ({
          id: item.id ?? item.med_id ?? 0,
          medication_id: item.med_id,
          name: item.name || '',
          dosage: item.dosage ?? null,
          pills_remaining: item.pills_remaining ?? 0,
          status: (item.status || 'pending') as TodayMedication['status'],
          scheduled_time: item.scheduled_time ?? null,
        }));
        setMedications(formatted);

        // Calculate adherence
        const taken = formatted.filter((m) => m.status === 'taken').length;
        const total = formatted.length;
        setTakenCount(taken);
        setAdherenceRate(total > 0 ? Math.round((taken / total) * 100) : 0);
      }
    } catch {
      // network error — leave state unchanged
    }

    // Fetch recent emotions
    try {
      const resp = await fetch('/api/history/emotions', { headers });
      if (resp.ok) {
        const emotionData: EmotionRecord[] = await resp.json();
        setEmotions(emotionData.slice(0, 7));
      }
    } catch {
      // network error — leave state unchanged
    }

    setLoading(false);
  };

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'taken':
        return <CheckCircle2 className="w-5 h-5 text-green-500" />;
      case 'skipped':
        return <XCircle className="w-5 h-5 text-orange-500" />;
      case 'missed':
        return <AlertCircle className="w-5 h-5 text-red-500" />;
      default:
        return <Clock className="w-5 h-5 text-gray-400" />;
    }
  };

  const getStatusBadge = (status: string) => {
    switch (status) {
      case 'taken':
        return <span className="text-sm font-medium px-2 py-1 rounded-full bg-green-50 text-green-600">{t('intake.taken')}</span>;
      case 'skipped':
        return <span className="text-sm font-medium px-2 py-1 rounded-full bg-orange-50 text-orange-600">{t('intake.skipped')}</span>;
      case 'missed':
        return <span className="text-sm font-medium px-2 py-1 rounded-full bg-red-50 text-red-600">{t('intake.missed')}</span>;
      default:
        return <span className="text-sm font-medium px-2 py-1 rounded-full bg-blue-50 text-blue-600">{t('intake.pending')}</span>;
    }
  };

  const faceUser = getFaceSession();
  const userName = faceUser?.name || '';

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-3">
        <div className="animate-spin rounded-full h-10 w-10 border-b-2 border-[#0057B8]"></div>
        <span className="text-gray-500 text-base">{t('common.loading')}</span>
      </div>
    );
  }

  return (
    <div className="space-y-6 lg:space-y-8 fade-in">
      {/* Greeting */}
      <div className="px-1">
        <p className="text-lg text-gray-600 font-medium">{greeting}{userName ? `, ${userName}` : ''}</p>
        <h1 className="text-2xl lg:text-3xl font-bold text-gray-900 mt-0.5">{t('dashboard.title')}</h1>
      </div>

      {/* Hero Card */}
      <div className="bg-gradient-to-r from-[#0057B8] to-[#003D82] rounded-2xl p-8 text-white shadow-lg shadow-blue-500/10 hover:shadow-xl hover:shadow-blue-500/15 transition-all duration-300">
        <div className="flex justify-between items-start">
          <div className="flex-1">
            <p className="text-blue-100 text-base">{t('dashboard.adherence')}</p>
            {medications.length === 0 ? (
              <div className="mt-1">
                <p className="text-xl font-medium">{t('dashboard.noMedsYet')}</p>
                <p className="text-base text-blue-200 mt-1">{t('dashboard.addMedsPrompt')}</p>
              </div>
            ) : (
              <h2 className="text-4xl font-bold mt-1">{adherenceRate}%</h2>
            )}
            <p className="text-blue-100 text-lg mt-2 font-medium">
              {takenCount} {t('dashboard.of')} {medications.length} {t('dashboard.medicationsTaken')}
            </p>

            {/* Progress bar */}
            <div className="w-full h-2 bg-white/20 rounded-full mt-4 overflow-hidden">
              <div
                className="h-full bg-white rounded-full transition-all duration-500"
                style={{ width: `${adherenceRate}%` }}
              />
            </div>
          </div>
          <div className="bg-white/20 rounded-full p-3 ml-4">
            <Heart className="w-8 h-8 text-white" />
          </div>
        </div>

        {/* Quick Actions */}
        <div className="flex gap-3 mt-6">
          <button
            onClick={() => navigate('/medications')}
            className="flex-1 bg-white/20 backdrop-blur rounded-xl py-4 text-base font-medium hover:bg-white/30 active:scale-95 transition-all flex items-center justify-center gap-2 touch-target-large"
          >
            <Plus className="w-4 h-4" />
            {t('medications.addNew')}
          </button>
        </div>
      </div>

      {/* Services Grid */}
      <section className="slide-up">
        <h3 className="text-lg font-semibold text-gray-900 mb-3 px-1">{t('dashboard.services') || 'Services'}</h3>
        <div className="grid grid-cols-2 gap-4 lg:gap-6">
          <ServiceCard
            icon={Pill}
            title={t('medications.title')}
            subtitle={t('dashboard.activeMedications', { count: medications.length })}
            color="bg-blue-50 text-[#0057B8]"
            onClick={() => navigate('/medications')}
          />
          <ServiceCard
            icon={Users}
            title={t('family.title')}
            subtitle={t('dashboard.familySubtitle') || 'Manage contacts'}
            color="bg-green-50 text-green-600"
            onClick={() => navigate('/family')}
          />
          <ServiceCard
            icon={Activity}
            title={t('emotion.title')}
            subtitle={t('dashboard.vitalsSubtitle') || 'Track mood'}
            color="bg-purple-50 text-purple-600"
            onClick={() => navigate('/emotion')}
          />
          <ServiceCard
            icon={FileText}
            title={t('history.title')}
            subtitle={t('dashboard.reportsSubtitle') || 'View all'}
            color="bg-orange-50 text-orange-600"
            onClick={() => navigate('/history')}
          />
        </div>
      </section>

      {/* Today's Medications Preview */}
      <section className="space-y-3">
        <div className="flex items-center justify-between px-1">
          <h3 className="text-lg font-semibold text-gray-900">{t('dashboard.todayMedications') || "Today's Medications"}</h3>
          <button
            onClick={() => navigate('/medications')}
            className="text-base text-[#0057B8] font-medium flex items-center gap-1 hover:text-[#003D82] transition-colors"
          >
            {t('common.viewAll')} <ChevronRight className="w-4 h-4" />
          </button>
        </div>

        {medications.length === 0 ? (
          <div className="bg-white rounded-xl border border-gray-100 p-8 text-center shadow-sm hover:shadow-md transition-all duration-300">
            <Pill className="w-12 h-12 text-gray-300 mx-auto mb-3" />
            <p className="text-gray-500 text-base">{t('dashboard.noMedications')}</p>
            <button
              onClick={() => navigate('/medications')}
              className="mt-4 px-6 py-4 bg-[#0057B8] text-white text-base font-medium rounded-xl hover:bg-[#003D82] active:scale-95 transition-all touch-target-large"
            >
              {t('medications.addNew')}
            </button>
          </div>
        ) : (
          <div className="space-y-2">
            {medications.slice(0, 3).map((med) => (
              <div
                key={med.id}
                className="flex items-center gap-3 p-4 bg-white rounded-xl border border-gray-100 shadow-sm hover:shadow-md hover:border-gray-200 transition-all duration-300"
              >
                <div className={`w-10 h-10 rounded-full flex items-center justify-center shrink-0 ${
                  med.status === 'taken' ? 'bg-green-50' :
                  med.status === 'missed' ? 'bg-red-50' :
                  med.status === 'skipped' ? 'bg-orange-50' :
                  'bg-blue-50'
                }`}>
                  {getStatusIcon(med.status)}
                </div>
                <div className="flex-1 min-w-0">
                  <p className="font-medium text-gray-900 text-base truncate">{med.name}</p>
                  <p className="text-base text-gray-600 font-medium">{med.dosage} · {med.pills_remaining} {t('medications.pillsLeft')}</p>
                </div>
                {getStatusBadge(med.status)}
                {med.status === 'pending' && (
                  <button
                    onClick={() => navigate(`/intake?med=${med.medication_id || med.id}`)}
                    className="px-4 py-2 bg-[#0057B8] text-white text-base font-medium rounded-lg hover:bg-[#003D82] active:scale-95 transition-all shrink-0 touch-target-large"
                  >
                    {t('intake.take')}
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Recent Emotions */}
      {emotions.length > 0 && (
        <section className="bg-white rounded-2xl border border-gray-100 shadow-sm p-5 hover:shadow-md transition-all duration-300">
          <div className="flex items-center justify-between mb-4">
            <h3 className="font-semibold text-gray-900">{t('emotion.history')}</h3>
            <span className="text-base text-gray-500 font-medium">{t('dashboard.last7Days') || 'Last 7 days'}</span>
          </div>
          <div className="flex items-end gap-2 h-24">
            {emotions.map((emotion) => {
              const colors: Record<string, string> = {
                Angry: 'bg-red-400',
                Sad: 'bg-blue-400',
                Neutral: 'bg-gray-400',
                Happy: 'bg-green-400',
              };
              return (
                <div
                  key={emotion.id}
                  className="flex-1 flex flex-col items-center gap-1"
                >
                  <div
                    className={`w-full rounded-t-lg ${colors[emotion.emotion_type] || 'bg-gray-400'} transition-all`}
                    style={{ height: `${Math.max(20, emotion.emotion_score * 100)}%` }}
                  />
                  <span className="text-sm text-gray-400">
                    {new Date(emotion.recorded_at).toLocaleDateString('zh-TW', { weekday: 'narrow' })}
                  </span>
                </div>
              );
            })}
          </div>
        </section>
      )}
    </div>
  );
}
