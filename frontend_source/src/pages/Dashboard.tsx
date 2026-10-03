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
  Bot,
  AlertTriangle,
} from 'lucide-react';
import ServiceCard from '../components/ui/ServiceCard';
import DoseEmotionChip from '../components/DoseEmotionChip';
import type { DoseEmotion } from '../lib/dose-emotion';
import { MoodBadge } from './Conversations';
import { fetchConversations, startCheckin, type ConversationSummary } from '../lib/reachy-api';
import { blockLabel, blockOf, type DoseBlock } from '../lib/doses';
import { useNow } from '../hooks/useNow';
import { useOverdoseProtection } from '../hooks/useOverdoseProtection';

interface TodayMedication {
  id: number;
  med_id?: number;
  name: string;
  dosage: string | null;
  status: 'pending' | 'taken' | 'skipped' | 'missed' | 'pending_confirmation';
  scheduled_time: string | null;
  due_from: string | null;
  expires_at?: string | null;
  taken_at: string | null;
  pills_remaining: number;
  units_per_dose: number;
  /** Facial expression while it was taken (a camera session's result), when there was one. */
  emotion?: DoseEmotion | null;
}

type TodayGroup = 'due' | 'later' | 'awaiting' | 'done';
const GROUP_ORDER: TodayGroup[] = ['due', 'later', 'awaiting', 'done'];

/** A dose missed past halfway to the next one is closed: it is not made up (overdose protection). */
function groupOf(dose: TodayMedication, now: number, block: DoseBlock | null): TodayGroup {
  if (dose.status === 'taken' || dose.status === 'skipped' || block?.reason === 'expired') return 'done';
  if (dose.status === 'pending_confirmation') return 'awaiting';
  if (dose.status === 'missed') return 'due';
  return dose.scheduled_time && new Date(dose.scheduled_time).getTime() > now ? 'later' : 'due';
}

function clock(value: string | null): string {
  return value ? new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
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
  units_per_dose?: number | null;
  status?: string;
  scheduled_time?: string | null;
  due_from?: string | null;
  expires_at?: string | null;
  taken_at?: string | null;
  emotion?: DoseEmotion | null;
}

export default function Dashboard() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  // Ticks, so a dose that becomes due while the page is open is enabled without a reload.
  const now = useNow();
  const protection = useOverdoseProtection();
  const blockFor = (dose: TodayMedication) => blockOf(dose, medications, now, protection !== false);
  const [medications, setMedications] = useState<TodayMedication[]>([]);
  const [emotions, setEmotions] = useState<EmotionRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [progressRate, setProgressRate] = useState(0);
  const [takenCount, setTakenCount] = useState(0);
  const [greeting, setGreeting] = useState('');
  const [actionError, setActionError] = useState('');
  const [busyDose, setBusyDose] = useState<number | null>(null);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [checkinNotice, setCheckinNotice] = useState('');

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
          med_id: item.med_id,
          name: item.name || '',
          dosage: item.dosage ?? null,
          pills_remaining: item.pills_remaining ?? 0,
          units_per_dose: item.units_per_dose ?? 1,
          status: (item.status || 'pending') as TodayMedication['status'],
          scheduled_time: item.scheduled_time ?? null,
          due_from: item.due_from ?? null,
          // Undefined from an older server: doses.ts then works expiry out from the day's list.
          expires_at: item.expires_at,
          taken_at: item.taken_at ?? null,
          emotion: item.emotion ?? null,
        }));
        setMedications(formatted);

        // Today's progress: doses taken out of all of today's doses (later ones included).
        const taken = formatted.filter((m) => m.status === 'taken').length;
        const total = formatted.length;
        setTakenCount(taken);
        setProgressRate(total > 0 ? Math.round((taken / total) * 100) : 0);
      }
    } catch {
      // network error — leave state unchanged
    }

    // Recent check-in conversations with Reachy
    try {
      setConversations((await fetchConversations(3)).items);
    } catch {
      // no conversations yet or network error
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

  const skipDose = async (dose: TodayMedication) => {
    const token = localStorage.getItem('face_auth_token');
    setBusyDose(dose.id);
    setActionError('');
    try {
      const resp = await fetch(`/api/medications/intake/${dose.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
        body: JSON.stringify({ status: 'skipped' }),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        setActionError(body.detail === 'awaiting_caregiver_confirmation'
          ? t('intake.pendingConfirmation') : t('dashboard.actionFailed'));
      }
      await fetchTodayData();
    } finally {
      setBusyDose(null);
    }
  };

  const talkToReachy = async () => {
    setCheckinNotice('');
    try {
      await startCheckin();
      setCheckinNotice(t('conversations.checkinQueued'));
    } catch (cause) {
      const code = cause instanceof Error ? cause.message : '';
      setCheckinNotice(code === 'checkin_consent_required' ? t('conversations.enableCheckins')
        : code === 'robot_not_paired' ? t('conversations.notPaired') : t('dashboard.actionFailed'));
    }
  };

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'taken':
        return <CheckCircle2 className="w-5 h-5 text-green-500" />;
      case 'skipped':
        return <XCircle className="w-5 h-5 text-orange-500" />;
      case 'missed':
        return <AlertCircle className="w-5 h-5 text-red-500" />;
      case 'pending_confirmation':
        return <Clock className="w-5 h-5 text-amber-500" />;
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
      case 'pending_confirmation':
        return <span className="text-sm font-medium px-2 py-1 rounded-full bg-amber-50 text-amber-700">{t('intake.pendingConfirmation')}</span>;
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
            <p className="text-blue-100 text-base">{t('dashboard.todayProgress')}</p>
            {medications.length === 0 ? (
              <div className="mt-1">
                <p className="text-xl font-medium">{t('dashboard.noMedsYet')}</p>
                <p className="text-base text-blue-200 mt-1">{t('dashboard.addMedsPrompt')}</p>
              </div>
            ) : (
              <h2 className="text-4xl font-bold mt-1">{progressRate}%</h2>
            )}
            <p className="text-blue-100 text-lg mt-2 font-medium">
              {takenCount} {t('dashboard.of')} {medications.length} {t('dashboard.medicationsTaken')}
            </p>

            {/* Progress bar */}
            <div className="w-full h-2 bg-white/20 rounded-full mt-4 overflow-hidden">
              <div
                className="h-full bg-white rounded-full transition-all duration-500"
                style={{ width: `${progressRate}%` }}
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

      {/* Today: every dose of the day, grouped by what it needs */}
      <section className="space-y-3" aria-labelledby="today-title">
        <div className="flex items-center justify-between px-1">
          <h3 id="today-title" className="text-lg font-semibold text-gray-900">{t('dashboard.todayMedications')}</h3>
          <button
            onClick={() => navigate('/medications')}
            className="text-base text-[#0057B8] font-medium flex items-center gap-1 hover:text-[#003D82] transition-colors"
          >
            {t('common.viewAll')} <ChevronRight className="w-4 h-4" />
          </button>
        </div>

        {actionError && <p role="alert" className="rounded-xl bg-red-50 p-3 text-sm text-red-700">{actionError}</p>}

        {protection === false && (
          <p role="status" className="rounded-xl bg-amber-50 border border-amber-200 p-3 text-sm text-amber-900">
            {t('overdose.offNotice')}{' '}
            <button onClick={() => navigate('/settings')} className="font-medium underline">{t('family.goToSettings')}</button>
          </p>
        )}

        {medications.length === 0 ? (
          <div className="bg-white rounded-xl border border-gray-100 p-8 text-center shadow-sm">
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
          (() => {
            const groups: Record<TodayGroup, TodayMedication[]> = { due: [], later: [], awaiting: [], done: [] };
            medications.forEach((dose) => groups[groupOf(dose, now, blockFor(dose))].push(dose));
            return GROUP_ORDER.filter((group) => groups[group].length > 0).map((group) => (
              <div key={group} className="space-y-2">
                <h4 className={`px-1 text-sm font-semibold uppercase tracking-wide ${
                  group === 'due' ? 'text-[#0057B8]' : group === 'awaiting' ? 'text-amber-700' : 'text-gray-500'}`}>
                  {t(`dashboard.today.${group}`)} · {groups[group].length}
                </h4>
                {groups[group].map((dose) => {
                  const canTake = dose.pills_remaining >= dose.units_per_dose;
                  // A later dose can be started from due_from on; the server refuses it before then, and (with
                  // overdose protection on) a dose missed past halfway to the next or one it just refused.
                  const block = blockFor(dose);
                  // An expired dose shows as missed even before the missed-dose job has marked it.
                  const shown = block?.reason === 'expired' ? 'missed' : dose.status;
                  return (
                    <div
                      key={dose.id}
                      className={`flex flex-wrap items-center gap-3 p-4 rounded-xl border shadow-sm ${
                        group === 'due' ? 'bg-white border-blue-200' : 'bg-white border-gray-100'}`}
                    >
                      <div className={`w-10 h-10 rounded-full flex items-center justify-center shrink-0 ${
                        shown === 'taken' ? 'bg-green-50' :
                        shown === 'missed' ? 'bg-red-50' :
                        shown === 'skipped' ? 'bg-orange-50' :
                        'bg-blue-50'
                      }`}>
                        {getStatusIcon(shown)}
                      </div>
                      <div className="flex-1 min-w-0">
                        <p className="font-medium text-gray-900 text-base truncate">
                          <span className="tabular-nums text-gray-500 mr-2">{clock(dose.scheduled_time)}</span>{dose.name}
                        </p>
                        <p className="text-sm text-gray-600">
                          {dose.dosage ? `${dose.dosage} · ` : ''}{t('dashboard.unitsPerDose', { count: dose.units_per_dose })}
                          {dose.status === 'taken' && dose.taken_at ? ` · ${t('history.takenAt', { time: clock(dose.taken_at) })}` : ''}
                        </p>
                        {dose.emotion?.dominant && (
                          <div className="mt-1">
                            <DoseEmotionChip dominant={dose.emotion.dominant} score={dose.emotion.score}
                              occluded={dose.emotion.mostly_occluded} uncertain={dose.emotion.uncertain} />
                          </div>
                        )}
                      </div>
                      {group === 'due' || group === 'later' ? (
                        <div className="flex gap-2 shrink-0">
                          <button
                            onClick={() => navigate(`/intake?intake=${dose.id}&start=1`)}
                            disabled={!canTake || !!block}
                            className="px-4 py-2 bg-[#0057B8] text-white text-base font-medium rounded-lg hover:bg-[#003D82] active:scale-95 transition-all touch-target-large disabled:bg-gray-200 disabled:text-gray-600"
                          >
                            {!canTake ? t('intake.cannotTake') : block ? blockLabel(block, t) : t('intake.take')}
                          </button>
                          {group === 'due' && (
                            <button
                              onClick={() => void skipDose(dose)}
                              disabled={busyDose === dose.id}
                              className="px-4 py-2 border border-gray-200 text-gray-700 text-base rounded-lg hover:bg-gray-50 touch-target-large disabled:opacity-50"
                            >
                              {t('intake.skip')}
                            </button>
                          )}
                        </div>
                      ) : block?.reason === 'expired' ? (
                        <span className="text-sm font-medium px-2 py-1 rounded-full bg-red-50 text-red-600">{blockLabel(block, t)}</span>
                      ) : (
                        getStatusBadge(dose.status)
                      )}
                    </div>
                  );
                })}
              </div>
            ));
          })()
        )}
      </section>

      {/* Conversations with Reachy */}
      <section className="space-y-3" aria-labelledby="conversations-title">
        <div className="flex items-center justify-between px-1">
          <h3 id="conversations-title" className="text-lg font-semibold text-gray-900">{t('conversations.recent')}</h3>
          <button
            onClick={() => navigate('/conversations')}
            className="text-base text-[#0057B8] font-medium flex items-center gap-1 hover:text-[#003D82] transition-colors"
          >
            {t('common.viewAll')} <ChevronRight className="w-4 h-4" />
          </button>
        </div>
        <button
          onClick={() => void talkToReachy()}
          className="w-full flex items-center justify-center gap-2 py-4 rounded-xl border border-[#0057B8] text-[#0057B8] text-base font-medium hover:bg-blue-50 transition-colors touch-target-large"
        >
          <Bot className="w-5 h-5" />{t('conversations.talkNow')}
        </button>
        {checkinNotice && <p role="status" className="rounded-xl bg-blue-50 p-3 text-sm text-blue-800">{checkinNotice}</p>}
        {conversations.length === 0 ? (
          <p className="px-1 text-base text-gray-500">{t('conversations.empty')}</p>
        ) : conversations.map((item) => (
          <button
            key={item.id}
            onClick={() => navigate('/conversations')}
            className={`w-full text-left flex items-start gap-3 p-4 bg-white rounded-xl border shadow-sm hover:shadow-md transition-all ${
              item.risk_flag ? 'border-red-200' : 'border-gray-100'}`}
          >
            <div className="w-10 h-10 bg-blue-50 rounded-full flex items-center justify-center shrink-0">
              <Bot className="w-5 h-5 text-[#0057B8]" />
            </div>
            <div className="flex-1 min-w-0 space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm text-gray-500">{new Date(item.started_at).toLocaleString()}</span>
                <MoodBadge mood={item.mood} />
                {item.risk_flag && <AlertTriangle className="w-4 h-4 text-red-600" aria-label={t('conversations.safetyAlert')} />}
              </div>
              <p className="text-base text-gray-800 truncate">
                {item.summary || item.first_words || t('conversations.noAnswer')}
              </p>
            </div>
          </button>
        ))}
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
                Disgust: 'bg-lime-400',
                Fear: 'bg-purple-400',
                Sad: 'bg-blue-400',
                Surprise: 'bg-yellow-400',
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
