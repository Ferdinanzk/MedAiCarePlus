// scan.ts — what the Scan page does with an OCR result: one editable draft per medicine on the paper, and the
// medication-create payload for each one the user keeps. Pure functions (no i18n, no fetch), so
// tests/scan.unit.ts runs them with `node --experimental-strip-types --test tests/scan.unit.ts`.

export type DoseForm = 'solid_oral' | 'liquid' | 'inhaler' | 'injection' | 'topical' | 'other';
export const DOSE_FORMS: DoseForm[] = ['solid_oral', 'liquid', 'inhaler', 'injection', 'topical', 'other'];

export type TimeSlot = 'morning' | 'noon' | 'night' | 'bedtime';
export type MealSlot = 'before_meals' | 'after_meals';
export type Slot = TimeSlot | MealSlot;
export const TIME_SLOTS: TimeSlot[] = ['morning', 'noon', 'night', 'bedtime'];
export const MEAL_SLOTS: MealSlot[] = ['before_meals', 'after_meals'];
export const SLOTS: Slot[] = [...TIME_SLOTS, ...MEAL_SLOTS];
/** The medication API's limit on dose times a day (app/services/schedule.py MAX_TIMES_PER_DAY). */
export const MAX_TIMES = 8;
// Same as the server's ocr_parsing.slots_for_count: 1 morning, 2 + night, 3 + noon, 4 + bedtime, then these.
const COUNT_SLOTS: Record<number, TimeSlot[]> = {
  1: ['morning'], 2: ['morning', 'night'], 3: ['morning', 'noon', 'night'], 4: ['morning', 'noon', 'night', 'bedtime'],
};
const EXTRA_TIMES = ['16:00', '06:00', '10:00', '14:00'];

export type ScanSchedule = Partial<Record<Slot, boolean>> & { custom_times?: string[] };

/** One medicine line as /api/ocr/parse returns it in `medications`. */
export interface ScannedMedicine {
  med_name: string;
  dosage: string;
  quantity: string;
  pill_count: number | null;
  unit?: string;
  frequency_text?: string;
  instructions: string;
  times_per_day?: number | null;
  /** The upper end of a printed range (一天3-4次 → 4); null when the paper gives one count. */
  max_per_day?: number | null;
  interval_hours?: number | null;
  days?: number | null;
  amount_each_intake: string;
  units_per_dose?: number | null;
  form?: string;
  dose_form?: DoseForm;
  as_needed?: boolean;
  schedule_time: ScanSchedule;
  schedule_source?: 'icons' | 'text' | 'none';
  stock?: number | null;
  total_intake: string;
  intake_time_label: string;
  warning: string;
  pill_description: string;
  clinical_uses: string;
  manufacturer: string;
}

/** The whole answer: the first medicine at the top level (older clients), every medicine in `medications`. */
export interface ScanResult extends ScannedMedicine {
  hospital: string;
  prescription_no: string;
  use_before: string;
  physician: string;
  pharmacist: string;
  patient_name: string;
  date_dispensed: string;
  pharmacy?: string;
  visit_date?: string;
  document_type?: string;
  medications?: ScannedMedicine[];
}

export type DraftStatus = 'idle' | 'saving' | 'saved' | 'failed';

/** One editable medicine card. Number fields are kept as the text the user typed. */
export interface MedicineDraft {
  include: boolean;
  name: string;
  dosage: string;
  doseForm: DoseForm;
  stock: string;
  unitsPerDose: string;
  /** Doses a day; for an as-needed medicine, its fixed times only (maxDaily is its limit). */
  timesPerDay: string;
  /** As needed: the most doses a day (max_daily_doses), never turned into dose times. */
  maxDaily: string;
  days: string;
  lastDay: string;
  slots: Record<Slot, boolean>;
  customTimes: string[];
  asNeeded: boolean;
  /** An active medicine with the same name is already saved: unticked until the user ticks it again. */
  duplicate?: boolean;
  source: ScannedMedicine;
  status: DraftStatus;
  /** The i18n key of why the last save failed (never the server's raw text). */
  errorKey?: string;
}

export const na = (value: string | null | undefined): string | undefined =>
  !value || value.trim() === '' || value.trim() === 'N/A' ? undefined : value;

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

export function isoDate(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${date.getFullYear()}-${month}-${day}`;
}

export function addDays(iso: string, days: number): string {
  const [year, month, day] = iso.split('-').map(Number);
  return isoDate(new Date(year, month - 1, day + days));
}

/** Every medicine of a scan: `medications`, or the top level as one medicine (an older server). */
export function medicinesOf(result: ScanResult): ScannedMedicine[] {
  return Array.isArray(result.medications) && result.medications.length > 0 ? result.medications : [result];
}

/** The default dose times for N doses a day, as the server's parser sets them. */
export function slotsForCount(count: number): { slots: Record<TimeSlot, boolean>; customTimes: string[] } {
  const slots: Record<TimeSlot, boolean> = { morning: false, noon: false, night: false, bedtime: false };
  if (!Number.isInteger(count) || count < 1) return { slots, customTimes: [] };
  for (const slot of COUNT_SLOTS[Math.min(count, 4)]) slots[slot] = true;
  const customTimes = count > 4 ? EXTRA_TIMES.slice(0, Math.min(count, MAX_TIMES) - 4) : [];
  return { slots, customTimes };
}

export function timesPerDayOf(draft: Pick<MedicineDraft, 'slots' | 'customTimes'>): number {
  return TIME_SLOTS.filter((slot) => draft.slots[slot]).length + draft.customTimes.length;
}

/** The course's first day: the dispensing date, else the visit date, else today. */
export function courseStart(result: Pick<ScanResult, 'date_dispensed' | 'visit_date'>, today: string): string {
  for (const date of [result.date_dispensed, result.visit_date]) {
    if (date && ISO_DATE.test(date)) return date;
  }
  return today;
}

/** The last day of reminders: start + days − 1 when the days are known, else the printed use-before date. */
export function lastDayFor(start: string, days: number | null, useBefore?: string): string {
  if (days && days > 0) return addDays(start, days - 1);
  return useBefore && ISO_DATE.test(useBefore) ? useBefore : '';
}

const countText = (count: number) => (count > 0 ? String(count) : '');

/** The most doses a day of an as-needed medicine: the upper end of a printed range, else its count. */
const printedMaximum = (med: ScannedMedicine) => med.max_per_day ?? med.times_per_day ?? null;

export function draftFrom(med: ScannedMedicine, result: ScanResult, today: string): MedicineDraft {
  const schedule = med.schedule_time || {};
  const slots = Object.fromEntries(SLOTS.map((slot) => [slot, schedule[slot] === true])) as Record<Slot, boolean>;
  const customTimes = (schedule.custom_times || []).filter((time) => /^\d{2}:\d{2}$/.test(time));
  const days = med.days ?? null;
  const doseForm: DoseForm = med.dose_form && DOSE_FORMS.includes(med.dose_form) ? med.dose_form : 'solid_oral';
  // The server's suggested stock; null means it could not count doses (a bottle used as needed, nothing printed).
  // Only an answer without `stock` (an older server) falls back to the printed count, and only for tablets: the
  // count of a bottle is bottles, not doses.
  const stock = med.stock !== undefined ? med.stock : doseForm === 'solid_oral' ? med.pill_count : null;
  const asNeeded = med.as_needed === true;
  const lastDay = lastDayFor(courseStart(result, today), days, result.use_before);
  const maximum = printedMaximum(med);
  return {
    // A course that ended by the paper's dates starts unticked: saving it would only remind for the rest of today.
    include: !(lastDay !== '' && lastDay < today),
    name: na(med.med_name) ?? '',
    dosage: na(med.dosage) ?? '',
    doseForm,
    stock: stock != null ? String(stock) : '',
    unitsPerDose: String(med.units_per_dose ?? 1),
    timesPerDay: asNeeded
      ? countText(timesPerDayOf({ slots, customTimes }))
      : med.times_per_day ? String(med.times_per_day) : '',
    maxDaily: asNeeded && maximum ? String(maximum) : '',
    days: days ? String(days) : '',
    lastDay,
    slots,
    customTimes,
    asNeeded,
    source: med,
    status: 'idle',
  };
}

/** A draft after the user typed a number of doses a day: that count's usual times; the meal marks stay. */
export function withTimesPerDay(draft: MedicineDraft, text: string): MedicineDraft {
  const count = Number(text);
  if (text.trim() === '' || !Number.isInteger(count) || count < 1 || count > MAX_TIMES) {
    return { ...draft, timesPerDay: text };
  }
  const { slots, customTimes } = slotsForCount(count);
  return { ...draft, timesPerDay: text, slots: { ...draft.slots, ...slots }, customTimes };
}

/** As needed on or off. The dose times stay as they are; on, the paper's daily maximum fills an empty limit. */
export function withAsNeeded(draft: MedicineDraft, on: boolean): MedicineDraft {
  const maximum = printedMaximum(draft.source);
  return {
    ...draft,
    asNeeded: on,
    maxDaily: on && draft.maxDaily === '' && maximum ? String(maximum) : draft.maxDaily,
    timesPerDay: countText(timesPerDayOf(draft)),
  };
}

export function withSlot(draft: MedicineDraft, slot: Slot, on: boolean): MedicineDraft {
  const next = { ...draft, slots: { ...draft.slots, [slot]: on } };
  if (!(TIME_SLOTS as Slot[]).includes(slot)) return next;
  return { ...next, timesPerDay: countText(timesPerDayOf(next)) };
}

export function withoutCustomTime(draft: MedicineDraft, time: string): MedicineDraft {
  const next = { ...draft, customTimes: draft.customTimes.filter((value) => value !== time) };
  return { ...next, timesPerDay: countText(timesPerDayOf(next)) };
}

export function withDays(draft: MedicineDraft, text: string, start: string): MedicineDraft {
  const days = Number(text);
  const valid = text.trim() !== '' && Number.isInteger(days) && days >= 1 && days <= 365;
  return { ...draft, days: text, lastDay: valid ? addDays(start, days - 1) : draft.lastDay };
}

/** A medicine name compared without case, spaces or dots ("Gastro F.C. tablets" = "GASTRO FC TABLETS"). */
export function sameName(a: string, b: string): boolean {
  const key = (name: string) => name.normalize('NFKC').toUpperCase().replace(/[\s.．·・]+/g, '');
  return key(a) !== '' && key(a) === key(b);
}

/**
 * Drafts after a look at the user's active medicines: an unsaved draft whose name is already among them is marked
 * a duplicate and unticked, once. Ticking it again keeps it (the user decided). Saved drafts are left alone.
 */
export function markDuplicates(drafts: MedicineDraft[], activeNames: string[]): MedicineDraft[] {
  return drafts.map((draft) => {
    if (draft.status === 'saved' || draft.duplicate) return draft;
    if (!activeNames.some((name) => sameName(name, draft.name))) return draft;
    return { ...draft, duplicate: true, include: false };
  });
}

export type DraftProblem =
  | 'name' | 'stock' | 'unitsPerDose' | 'stockLow' | 'maxDaily' | 'days' | 'tooManyTimes' | 'lastDay' | 'lastDayPast';

/** What keeps a draft from being saved, or null. `today` (YYYY-MM-DD) also refuses a last day that has passed. */
export function draftProblem(draft: MedicineDraft, today?: string): DraftProblem | null {
  if (!draft.name.trim()) return 'name';
  const stock = draft.stock.trim() === '' ? 0 : Number(draft.stock);
  if (!Number.isFinite(stock) || stock < 0 || stock >= 1_000_000) return 'stock';
  const units = draft.unitsPerDose.trim() === '' ? 1 : Number(draft.unitsPerDose);
  if (!Number.isFinite(units) || units <= 0 || units >= 100) return 'unitsPerDose';
  // With less than one dose on hand no dose can be recorded: the camera, Reachy and "Take now" all refuse.
  if (stock < units) return 'stockLow';
  if (draft.asNeeded && draft.maxDaily.trim() !== '') {
    const most = Number(draft.maxDaily);
    if (!Number.isInteger(most) || most < 1 || most > 24) return 'maxDaily';
  }
  if (draft.days.trim() !== '') {
    const days = Number(draft.days);
    if (!Number.isInteger(days) || days < 1 || days > 365) return 'days';
  }
  if (timesPerDayOf(draft) > MAX_TIMES) return 'tooManyTimes';
  if (draft.lastDay !== '' && !ISO_DATE.test(draft.lastDay)) return 'lastDay';
  if (today && draft.lastDay !== '' && draft.lastDay < today) return 'lastDayPast';
  return null;
}

/** The i18n key for a failed save: the page's own words, never the server's raw text. */
export function saveErrorKey(status: number | null, detail: unknown): string {
  if (status === 401) return 'scan.notLoggedIn';
  if (status === 403 && detail === 'consent_required') return 'scan.errors.consent_required';
  return 'scan.saveOneFailed';
}

/** Only one tablet or capsule a dose may be recorded by the camera alone (robot and browser). */
export function autoRecordable(draft: Pick<MedicineDraft, 'doseForm' | 'unitsPerDose'>): boolean {
  const units = draft.unitsPerDose.trim() === '' ? 1 : Number(draft.unitsPerDose);
  return draft.doseForm === 'solid_oral' && units === 1;
}

const round2 = (value: number) => Math.round(value * 100) / 100;
/** How early a dose may be recorded (the server's DOSE_EARLY_MINUTES), kept as slack in a days-long interval. */
const DOSE_EARLY_MINUTES = 120;

/** Overdose-protection limits from the paper (null = the server's default from the dose times). */
export function doseLimits(draft: MedicineDraft): { max_daily_doses?: number | null; min_interval_minutes?: number | null } {
  const med = draft.source;
  const interval = med.interval_hours ?? null;
  if (draft.asNeeded) {
    // Taken only when needed: at most N a day, at least the printed interval apart.
    const most = Number(draft.maxDaily);
    return {
      max_daily_doses: draft.maxDaily.trim() !== '' && Number.isInteger(most) ? Math.min(24, Math.max(1, most)) : null,
      min_interval_minutes: interval ? Math.min(2880, Math.max(30, interval * 60)) : null,
    };
  }
  const limits: { max_daily_doses?: number; min_interval_minutes?: number } = {};
  // 一天3-4次: reminders for 3, a fourth dose allowed.
  if (med.max_per_day && med.max_per_day > timesPerDayOf(draft)) limits.max_daily_doses = Math.min(24, med.max_per_day);
  // Every other day (QOD): the schedule reminds daily, so the gap keeps a dose from being taken a day early.
  if (interval && interval > 24) {
    limits.min_interval_minutes = Math.min(2880, Math.max(30, interval * 60 - DOSE_EARLY_MINUTES));
  }
  return limits;
}

/** The POST /api/medications body for one kept draft. */
export function medicationPayload(draft: MedicineDraft, result: ScanResult) {
  const med = draft.source;
  const stock = draft.stock.trim() === '' ? 0 : round2(Number(draft.stock));
  const units = draft.unitsPerDose.trim() === '' ? 1 : round2(Number(draft.unitsPerDose));
  const hasSchedule = timesPerDayOf(draft) > 0 || MEAL_SLOTS.some((slot) => draft.slots[slot]);
  const days = draft.days.trim() === '' ? null : Number(draft.days);
  const timesPerDay = timesPerDayOf(draft) || (med.times_per_day ?? null);
  const printedUseBefore = result.use_before && ISO_DATE.test(result.use_before) ? result.use_before : undefined;
  return {
    name: draft.name.trim(),
    dosage: draft.dosage.trim() || null,
    total_pills: Math.round(stock),
    pills_remaining: stock,
    instructions: na(med.instructions) ?? null,
    warning: na(med.warning) ?? null,
    pill_description: na(med.pill_description) ?? null,
    use_before: draft.lastDay || null,
    is_active: true,
    dose_form: draft.doseForm,
    units_per_dose: units,
    schedule_time: hasSchedule
      ? {
          morning: draft.slots.morning, noon: draft.slots.noon, night: draft.slots.night,
          bedtime: draft.slots.bedtime, before_meals: draft.slots.before_meals,
          after_meals: draft.slots.after_meals, custom_times: draft.customTimes,
        }
      : null,
    ...doseLimits(draft),
    prescription_meta: {
      // use_before above ends the reminders. course_end says it is the course's last day, not an expiry date (the
      // Medications and Schedule pages word it so); the paper's own use-before date is kept apart.
      course_end: draft.lastDay && draft.lastDay !== printedUseBefore ? draft.lastDay : undefined,
      printed_use_before: printedUseBefore,
      as_needed: draft.asNeeded || undefined,
      clinical_uses: na(med.clinical_uses),
      manufacturer: na(med.manufacturer),
      hospital: na(result.hospital),
      pharmacy: na(result.pharmacy),
      prescription_no: na(result.prescription_no),
      physician: na(result.physician),
      pharmacist: na(result.pharmacist),
      patient_name: na(result.patient_name),
      date_dispensed: na(result.date_dispensed),
      visit_date: na(result.visit_date),
      quantity: na(med.quantity),
      amount_each_intake: na(med.amount_each_intake),
      frequency_text: na(med.frequency_text),
      times_per_day: timesPerDay,
      days,
      form: med.form,
      total_intake_calc: na(med.total_intake),
      intake_time_label: na(med.intake_time_label),
    },
  };
}
