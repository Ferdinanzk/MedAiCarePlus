import { useTranslation } from 'react-i18next';
import { ScanFace } from 'lucide-react';
import { EMOTION_CHIP_STYLE, percent, type EmotionClass } from '../lib/dose-emotion';

interface Props {
  dominant: EmotionClass | null | undefined;
  score: number | null | undefined;
  /** The mouth was mostly covered, so the estimate is less reliable: dashed, dimmed and marked in the text. */
  occluded?: boolean;
  /** Few clear frames or a weak top class: shown the same way, with its own mark. */
  uncertain?: boolean;
  /** A shorter chip without the icon (the Emotion page's before/during/after row). */
  compact?: boolean;
  label?: string;
}

/** Facial expression while a dose was taken, in the page's language. Renders nothing without a result. The
 * reliability mark is part of the visible text, not only the tooltip (tablets have no hover). */
export default function DoseEmotionChip({ dominant, score, occluded = false, uncertain = false, compact = false, label }: Props) {
  const { t } = useTranslation();
  if (!dominant) return null;
  const emotion = t(`emotion.${dominant}`);
  const value = percent(score);
  const mark = occluded ? t('doseEmotion.coveredShort') : uncertain ? t('doseEmotion.uncertainShort') : '';
  const title = [
    t('doseEmotion.chipLabel', { emotion, percent: value }),
    occluded ? t('doseEmotion.mostlyCovered') : uncertain ? t('doseEmotion.uncertain') : '',
    t('doseEmotion.disclaimer'),
  ].filter(Boolean).join(' · ');
  return (
    <span
      title={title}
      aria-label={title}
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-sm font-medium ${
        EMOTION_CHIP_STYLE[dominant] ?? EMOTION_CHIP_STYLE.neutral} ${mark ? 'border-dashed opacity-80' : ''}`}
    >
      {!compact && <ScanFace className="h-3.5 w-3.5" aria-hidden="true" />}
      {label ? `${label}: ` : ''}{emotion} {value}%
      {mark && <span className="font-normal">· {mark}</span>}
    </span>
  );
}
