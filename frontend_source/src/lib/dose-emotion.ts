import { call } from './reachy-api';

/** The seven expression classes of the seed-43 model (app/services/emotion_service.py LABELS). */
export const EMOTION_CLASSES = ['angry', 'disgust', 'fear', 'happy', 'sad', 'surprise', 'neutral'] as const;
export type EmotionClass = typeof EMOTION_CLASSES[number];

export type DoseEmotionOutcome =
  'recorded' | 'taken_other' | 'sent_to_family' | 'patient_claim' | 'skipped' | 'unresolved';
export type DoseEmotionBasis = 'event' | 'event_during' | 'session' | 'occluded_only' | 'none';

/** Facial expression while a dose was taken, as /api/medications/today and /api/history/intakes rows carry it
 * (only for a dose taken or waiting for family whose camera session recorded it, saw it taken, or sent it on). */
export interface DoseEmotion {
  dominant: EmotionClass | null;
  score: number | null;
  occluded_share: number | null;
  /** The mouth was covered most of the time (a hand at the mouth), or only covered faces were scored. */
  mostly_occluded: boolean;
  /** Thin evidence: few clear frames behind the result, or a weak top class. */
  uncertain: boolean;
  basis: DoseEmotionBasis;
  basis_samples: number;
  samples: number;
  unoccluded_samples: number;
  outcome: DoseEmotionOutcome;
}

export interface DoseEmotionPhase {
  n: number;
  n_unoccluded: number;
  occluded_share: number | null;
  probabilities: Record<EmotionClass, number> | null;
  dominant: EmotionClass | null;
  score: number | null;
  /** Every face in this phase had its mouth covered: the result is less reliable. */
  occluded: boolean;
  /** Few clear frames in this phase, or a weak top class. */
  uncertain?: boolean;
}

export interface DoseEmotionPhases {
  before: DoseEmotionPhase;
  during: DoseEmotionPhase;
  after: DoseEmotionPhase;
  event_seconds: number;
}

/** One dose's result for the Emotion page (/api/emotion/medication). */
export interface MedicationEmotion {
  id: number;
  intk_id: number;
  med_name: string;
  scheduled_time: string;
  /** The dose's status now; `outcome` is how the camera session left it (an undo or family's answer may differ). */
  status: string;
  outcome: DoseEmotionOutcome;
  client_type: 'browser' | 'reachy';
  resolved_at: string;
  created_at: string;
  dominant: EmotionClass | null;
  score: number | null;
  probabilities: Record<EmotionClass, number> | null;
  occluded_share: number | null;
  mostly_occluded: boolean;
  uncertain: boolean;
  basis: DoseEmotionBasis;
  basis_samples: number;
  samples: number;
  unoccluded_samples: number;
  phases: DoseEmotionPhases | null;
}

/** The dose statuses a camera session's outcome leaves the dose in; any other status now means it changed since. */
export const OUTCOME_STATUSES: Record<DoseEmotionOutcome, string[]> = {
  recorded: ['taken'],
  taken_other: ['taken'],
  sent_to_family: ['pending_confirmation', 'taken'],
  patient_claim: ['pending_confirmation', 'taken'],
  skipped: ['skipped'],
  unresolved: ['pending', 'missed'],
};

export const fetchMedicationEmotions = (limit = 20) =>
  call<MedicationEmotion[]>(`/api/emotion/medication?limit=${limit}`, { cache: 'no-store' });

/** Tailwind classes per class, matching the History page's emotion colours. */
export const EMOTION_CHIP_STYLE: Record<EmotionClass, string> = {
  angry: 'bg-red-100 text-red-700 border-red-200',
  disgust: 'bg-lime-100 text-lime-700 border-lime-200',
  fear: 'bg-purple-100 text-purple-700 border-purple-200',
  happy: 'bg-green-100 text-green-700 border-green-200',
  sad: 'bg-blue-100 text-blue-700 border-blue-200',
  surprise: 'bg-yellow-100 text-yellow-700 border-yellow-200',
  neutral: 'bg-gray-100 text-gray-700 border-gray-200',
};

export function percent(value: number | null | undefined): number {
  return Math.round((value ?? 0) * 100);
}
