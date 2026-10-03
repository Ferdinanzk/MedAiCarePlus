import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Pill } from 'lucide-react';
import DoseEmotionChip from './DoseEmotionChip';
import { fetchMedicationEmotions, OUTCOME_STATUSES, percent, type MedicationEmotion } from '../lib/dose-emotion';

const PHASES = ['before', 'during', 'after'] as const;

/** "During medication": the facial expression the camera saw around each recent dose (Emotion page). */
export default function MedicationEmotions() {
  const { t } = useTranslation();
  const [items, setItems] = useState<MedicationEmotion[] | null>(null);

  // The outcome is how the camera session left the dose; when the dose changed since (an undo, family's answer),
  // its status now is shown beside it, with the History page's words.
  const changedStatus = (item: MedicationEmotion) => {
    if ((OUTCOME_STATUSES[item.outcome] ?? []).includes(item.status)) return null;
    if (item.status === 'pending_confirmation') return t('intake.pendingConfirmation');
    if (item.status === 'pending') return t('history.notRecorded');
    return t(`intake.${item.status}`);
  };

  useEffect(() => {
    let cancelled = false;
    fetchMedicationEmotions(20)
      .then((rows) => { if (!cancelled) setItems(rows); })
      .catch(() => { if (!cancelled) setItems([]); });
    return () => { cancelled = true; };
  }, []);

  if (!items || items.length === 0) return null;
  return (
    <section className="bg-white rounded-2xl p-5 border border-gray-100 shadow-sm space-y-3" aria-labelledby="dose-emotion-title">
      <div>
        <h3 id="dose-emotion-title" className="font-semibold text-gray-900 flex items-center gap-2">
          <Pill className="w-5 h-5 text-[#0057B8]" />
          {t('doseEmotion.title')}
        </h3>
        <p className="text-sm text-gray-500 mt-1">{t('doseEmotion.disclaimer')}</p>
      </div>
      <ul className="space-y-2">
        {items.map((item) => {
          const now = changedStatus(item);
          return (
          <li key={item.id} className="rounded-xl border border-gray-100 p-3 space-y-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0">
                <p className="font-medium text-gray-900 truncate">{item.med_name}</p>
                <p className="text-sm text-gray-500">
                  {new Date(item.scheduled_time).toLocaleString([], { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                  {' · '}{t(`doseEmotion.outcome.${item.outcome}`)}
                  {now && <>{' · '}{t('doseEmotion.statusNow', { status: now })}</>}
                </p>
              </div>
              {item.dominant ? (
                <DoseEmotionChip dominant={item.dominant} score={item.score} occluded={item.mostly_occluded}
                  uncertain={item.uncertain} />
              ) : (
                <span className="text-sm text-gray-500">{t('doseEmotion.none')}</span>
              )}
            </div>
            {item.phases && (
              <div className="flex flex-wrap gap-1.5">
                {PHASES.map((phase) => {
                  const value = item.phases?.[phase];
                  return value?.dominant ? (
                    <DoseEmotionChip key={phase} dominant={value.dominant} score={value.score}
                      occluded={value.occluded} uncertain={value.uncertain} compact label={t(`doseEmotion.${phase}`)} />
                  ) : null;
                })}
              </div>
            )}
            {item.occluded_share != null && item.occluded_share > 0 && (
              <p className={`text-sm ${item.mostly_occluded ? 'text-amber-700' : 'text-gray-500'}`}>
                {item.mostly_occluded ? t('doseEmotion.mostlyCovered') : t('doseEmotion.coveredShare', { percent: percent(item.occluded_share) })}
              </p>
            )}
            {item.dominant && item.uncertain && (
              <p className="text-sm text-amber-700">{t('doseEmotion.uncertain')}</p>
            )}
          </li>
          );
        })}
      </ul>
    </section>
  );
}
