/**
 * Which of today's doses may be started now, and what to say when the server refuses one. The server allows a
 * scheduled dose from `due_from` (DOSE_EARLY_MINUTES before its time, or halfway from the same medicine's previous
 * dose if that is later). While the patient's overdose protection is on (the default) it also refuses a dose too soon
 * after the last one taken, past the day's maximum, or missed past halfway to the next one: 409 `dose_not_due_yet`,
 * `dose_too_soon`, `daily_max_reached` or `dose_expired`. These helpers keep the UI from offering or picking such a
 * dose where the page can tell beforehand (3 Oct 2026: test alerts sent just after midnight used that day's 08:00,
 * 12:00 and 20:00 doses, which were then recorded as taken).
 */
import type { TFunction } from 'i18next';

export interface DoseTiming {
  id?: number;
  med_id?: number;
  status: string;
  scheduled_time?: string | null;
  due_from?: string | null;
  /** From /api/medications/today: when the server stops counting the dose if not taken (null: never). */
  expires_at?: string | null;
}

export const isOpen = (dose: DoseTiming) => dose.status === 'pending' || dose.status === 'missed';

/** Rows without `due_from` (no time, or an ad-hoc dose made now) are always due. */
export function isDue(dose: DoseTiming, now = Date.now()): boolean {
  return !dose.due_from || Date.parse(dose.due_from) <= now;
}

/**
 * When a dose not taken stops counting ("never double up"): halfway to the same medicine's next scheduled dose. The
 * server's `expires_at` says so for every dose, the last of the day (halfway to tomorrow's first) and ad-hoc ones
 * (never) included. Without it (an older server), halfway to the next dose in the day's list; the last dose of the
 * day is then left to the server.
 */
export function expiresAt(dose: DoseTiming, doses: DoseTiming[]): number | null {
  if (dose.expires_at !== undefined) return dose.expires_at ? Date.parse(dose.expires_at) : null;
  if (!dose.scheduled_time || dose.med_id == null) return null;
  const at = Date.parse(dose.scheduled_time);
  const next = Math.min(...doses
    .filter(other => other.med_id === dose.med_id && other.scheduled_time)
    .map(other => Date.parse(other.scheduled_time as string))
    .filter(time => time > at));
  return Number.isFinite(next) ? at + (next - at) / 2 : null;
}

/** The server's 409 body for a dose it will not start or record. */
export type RefusalDetail = 'dose_not_due_yet' | 'dose_too_soon' | 'daily_max_reached' | 'dose_expired';
const REFUSALS: string[] = ['dose_not_due_yet', 'dose_too_soon', 'daily_max_reached', 'dose_expired'];

export interface DoseRefusal {
  detail: RefusalDetail;
  intk_id?: number | null;
  med_name?: string | null;
  scheduled_time?: string | null;
  due_from?: string | null;
  last_taken_at?: string | null;
  next_allowed_at?: string | null;
  /** One short sentence for the patient, in the patient's language (`language`: 'zh-TW' or 'en'). */
  reply?: string | null;
  language?: string | null;
}

/** Same language family: the server writes zh-TW or en, the page may run zh-TW, zh or en-US. */
const sameLanguage = (a: string, b: string) => a.slice(0, 2).toLowerCase() === b.slice(0, 2).toLowerCase();

/** The server's sentence suits a page in `language` (unknown on either side: it does). */
export function hasReplyFor(refusal: DoseRefusal, language?: string): boolean {
  return Boolean(refusal.reply) && (!language || !refusal.language || sameLanguage(language, refusal.language));
}

export function doseRefusal(body: unknown): DoseRefusal | null {
  if (!body || typeof body !== 'object') return null;
  const detail = (body as { detail?: unknown }).detail;
  return typeof detail === 'string' && REFUSALS.includes(detail) ? body as DoseRefusal : null;
}

/** The dose_not_due_yet body with both times (the Reachy card names them in its own words). */
export interface NotDueYet {
  detail: 'dose_not_due_yet';
  scheduled_time: string;
  due_from: string;
}

export function notDueYet(body: unknown): NotDueYet | null {
  const value = body as Partial<NotDueYet> | null;
  return value && value.detail === 'dose_not_due_yet' && typeof value.scheduled_time === 'string'
    && typeof value.due_from === 'string' ? value as NotDueYet : null;
}

/** Why a dose cannot be started now, as far as the page can tell before asking the server. */
export type DoseBlock =
  | { reason: 'not_due'; until: number }
  | { reason: 'expired' }
  | { reason: 'too_soon' | 'daily_max'; until: number; refusal: DoseRefusal };

type MedBlock = Extract<DoseBlock, { reason: 'too_soon' | 'daily_max' }>;

// What the server last refused, kept for this page load so the buttons say it before the next try: a minimum gap or
// daily maximum blocks every dose of that medicine until next_allowed_at, an expired dose stays expired.
const medBlocks = new Map<number, MedBlock>();
const expiredDoses = new Set<number>();

export function rememberRefusal(refusal: DoseRefusal, medId?: number | null, now = Date.now()): void {
  if (refusal.detail === 'dose_expired' && refusal.intk_id != null) expiredDoses.add(refusal.intk_id);
  if (medId == null) return;
  const next = refusal.next_allowed_at ? Date.parse(refusal.next_allowed_at) : NaN;
  if (refusal.detail === 'dose_too_soon' && Number.isFinite(next)) {
    medBlocks.set(medId, { reason: 'too_soon', until: next, refusal });
  } else if (refusal.detail === 'daily_max_reached') {
    const midnight = new Date(now);
    midnight.setHours(24, 0, 0, 0);
    medBlocks.set(medId, { reason: 'daily_max', until: Number.isFinite(next) ? next : midnight.getTime(), refusal });
  }
}

/** After anything that changes what the server would say: an undo, the switch, a medicine's limits. */
export function forgetRefusals(): void {
  medBlocks.clear();
  expiredDoses.clear();
}

/** Inclusive, like the server: at halfway the earlier dose is missed and the later one is due. Also a dose the
 * server called expired (the last one of the day, whose next dose the page cannot see). */
function expired(dose: DoseTiming, doses: DoseTiming[], now: number): boolean {
  const expires = expiresAt(dose, doses);
  return (expires !== null && now >= expires) || (dose.id != null && expiredDoses.has(dose.id));
}

/** A remembered gap or daily maximum for a medicine. `protection` false: the server blocks nothing. */
export function medBlock(medId: number, now = Date.now(), protection = true): MedBlock | null {
  const block = protection ? medBlocks.get(medId) : undefined;
  return block && block.until > now ? block : null;
}

/** From the dose's own timing: not due yet (due_from) or expired. */
function timingBlock(dose: DoseTiming, doses: DoseTiming[], now: number, protection: boolean): DoseBlock | null {
  if (!protection || !isOpen(dose)) return null;
  if (expired(dose, doses, now)) return { reason: 'expired' };
  if (!isDue(dose, now)) return { reason: 'not_due', until: Date.parse(dose.due_from as string) };
  return null;
}

/**
 * Why an open dose cannot be started now, or null. `doses` is the day's list (for the next dose of the same
 * medicine). With `protection` off nothing is blocked: the server then allows any open dose.
 */
export function blockOf(dose: DoseTiming, doses: DoseTiming[], now = Date.now(), protection = true): DoseBlock | null {
  const timing = timingBlock(dose, doses, now, protection);
  if (timing?.reason === 'expired' || !isOpen(dose) || dose.med_id == null) return timing;
  const remembered = medBlock(dose.med_id, now, protection);
  if (!remembered || !timing) return remembered ?? timing;
  return timing.until > remembered.until ? timing : remembered;
}

function distance(dose: DoseTiming, now: number): number {
  return dose.scheduled_time ? Math.abs(Date.parse(dose.scheduled_time) - now) : 0;
}

const scheduledAt = (dose: DoseTiming) => (dose.scheduled_time ? Date.parse(dose.scheduled_time) : 0);

/**
 * The open dose a pill taken now counts for: due, not expired (while protection is on), nearest to its time first
 * and the earlier one on a tie. The same choice as the server's Take Now, which keeps the due window even with
 * protection off (it then refuses nothing, but a pill with no due dose becomes an ad-hoc dose rather than using a
 * later one). At 10:01 with 08:00 still open and 12:00 due, that is 12:00 (with protection on, 08:00 expired at
 * 10:00): the pill counts for the noon dose, so its reminder does not prompt a second pill two hours later.
 */
export function dueDose<T extends DoseTiming>(doses: T[], now = Date.now(), protection = true): T | undefined {
  return doses.filter(dose => isOpen(dose) && isDue(dose, now) && !(protection && expired(dose, doses, now)))
    .sort((a, b) => distance(a, now) - distance(b, now) || scheduledAt(a) - scheduledAt(b))[0];
}

/** The next open dose that is not due yet, earliest first. */
export function nextDose<T extends DoseTiming>(doses: T[], now = Date.now()): T | undefined {
  return doses.filter(dose => isOpen(dose) && !isDue(dose, now))
    .sort((a, b) => scheduledAt(a) - scheduledAt(b))[0];
}

export function clockTime(value: string | number | null | undefined): string {
  return value == null || value === '' ? ''
    : new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/**
 * What to tell the person: the server's own sentence, or the same in this page's words. The server writes in the
 * patient's language (that of the robot's check-ins); a page running in another language (`language`, i18n's) says
 * it in its own words instead.
 */
export function refusalMessage(refusal: DoseRefusal, t: TFunction, language?: string): string {
  if (hasReplyFor(refusal, language)) return refusal.reply as string;
  const name = refusal.med_name || t('overdose.thisMedicine');
  switch (refusal.detail) {
    case 'dose_not_due_yet':
      return t('intake.doseNotDueYet', { time: clockTime(refusal.scheduled_time), from: clockTime(refusal.due_from) });
    case 'dose_too_soon': {
      const next = clockTime(refusal.next_allowed_at);
      const last = clockTime(refusal.last_taken_at);
      if (last) return next ? t('overdose.tooSoonUntil', { name, last, next }) : t('overdose.tooSoon', { name, last });
      return next ? t('overdose.tooSoonRecentlyUntil', { name, next }) : t('overdose.tooSoonRecently', { name });
    }
    case 'daily_max_reached':
      return t('overdose.dailyMax', { name });
    case 'dose_expired':
      return refusal.scheduled_time ? t('overdose.expired', { name, time: clockTime(refusal.scheduled_time) })
        : t('overdose.expiredNoTime', { name });
  }
}

/** The full sentence for a dose the page already knows it cannot start. */
export function blockMessage(block: DoseBlock, dose: DoseTiming & { name?: string }, t: TFunction,
                             language?: string): string {
  switch (block.reason) {
    case 'not_due':
      return t('intake.doseNotDueYet', { time: clockTime(dose.scheduled_time), from: clockTime(block.until) });
    case 'expired':
      return refusalMessage({ detail: 'dose_expired', med_name: dose.name, scheduled_time: dose.scheduled_time }, t);
    default:
      return refusalMessage(block.refusal, t, language);
  }
}

/** A few words for the place of the dose's button. */
export function blockLabel(block: DoseBlock, t: TFunction): string {
  switch (block.reason) {
    case 'not_due':
      return t('intake.notDueYet', { time: clockTime(block.until) });
    case 'expired':
      return t('overdose.label.expired');
    case 'too_soon':
      return t('overdose.label.tooSoon', { time: clockTime(block.until) });
    case 'daily_max':
      return t('overdose.label.dailyMax');
  }
}
