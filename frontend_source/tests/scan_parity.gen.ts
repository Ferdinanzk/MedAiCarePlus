// The golden file that holds app/services/scan_rules.py equal to lib/scan.ts. Regenerate after any change to scan.ts:
//   node --experimental-strip-types tests/scan_parity.gen.ts
// It writes ../tests/scan_parity.json (the repo's tests folder), read by tests/test_scan_rules.py and by
// 'the golden file matches scan.ts' in scan.unit.ts. Fixtures as in scan.unit.ts (made-up names and numbers).
import { writeFileSync } from 'node:fs';
import { draftFrom, draftProblem, markDuplicates, medicationPayload, medicinesOf } from '../src/lib/scan.ts';
import type { ScanResult, ScannedMedicine } from '../src/lib/scan.ts';

export const GOLDEN_FILE = new URL('../../tests/scan_parity.json', import.meta.url);

export interface ParityCase { name: string; today: string; result: ScanResult; active_names: string[] }

const json = <T>(value: T): T => JSON.parse(JSON.stringify(value));

/** What scan.ts makes of one case: the drafts (without `source`), their problems and their payloads, as JSON. */
export function expectedOf({ today, result, active_names }: ParityCase) {
  const drafts = markDuplicates(medicinesOf(result).map((med) => draftFrom(med, result, today)), active_names);
  return json({
    drafts: drafts.map(({ source: _source, ...rest }) => rest),
    problems: drafts.map((draft) => draftProblem(draft, today)),
    payloads: drafts.map((draft) => medicationPayload(draft, result)),
  });
}

const SIX = (on: string[]) => Object.fromEntries(
  ['morning', 'noon', 'night', 'bedtime', 'before_meals', 'after_meals'].map((slot) => [slot, on.includes(slot)]));

const medicine = (changes: Partial<ScannedMedicine>): ScannedMedicine => ({
  med_name: 'VITACOBAL CAPSULES 0', dosage: 'N/A', quantity: '15 錠', pill_count: 15, unit: '錠',
  frequency_text: '一天3次', instructions: '一天3次', times_per_day: 3, interval_hours: null, days: 5,
  amount_each_intake: 'N/A', units_per_dose: 1, form: 'capsule', dose_form: 'solid_oral', as_needed: false,
  schedule_time: SIX(['morning', 'noon', 'night']), schedule_source: 'text', stock: 15,
  total_intake: '15錠 (3次/天 × 1錠/次 × 5天)', intake_time_label: '一天3次 [早上 | 中午 | 晚上]', warning: 'N/A',
  pill_description: 'N/A', clinical_uses: 'N/A', manufacturer: 'N/A', ...changes,
});

const drops = medicine({
  med_name: 'OTOMYX OTIC DROPS.', quantity: '1 瓶', pill_count: 1, unit: '瓶', frequency_text: '一天2次',
  instructions: '一天2次', times_per_day: 2, units_per_dose: null, form: 'ear_drops', dose_form: 'other',
  schedule_time: SIX(['morning', 'night']), stock: 10, total_intake: 'N/A (partial: 2次/天 × 5天)',
});

const scan = (changes: Partial<ScanResult> = {}): ScanResult => {
  const meds = [medicine({}), medicine({ med_name: 'GASTRO F.C. TABLETS', form: 'tablet', unit: 'TAB' }), drops];
  return {
    ...meds[0], hospital: 'N/A', pharmacy: '安心藥局', prescription_no: 'N/A', use_before: 'N/A', physician: 'N/A',
    pharmacist: '林小青', patient_name: '王小華', date_dispensed: '2026-09-28', visit_date: '2026-09-28',
    document_type: 'pharmacy_receipt', medications: meds, ...changes,
  };
};

const TODAY = '2026-10-04';
const current = (changes: Partial<ScanResult> = {}) =>
  scan({ date_dispensed: '2026-10-03', visit_date: '2026-10-03', ...changes });

/** A copy without the given keys (an older server's answer has no `stock`; a medicine may have no `form`). */
const without = (med: ScannedMedicine, ...keys: (keyof ScannedMedicine)[]) => {
  const copy: Partial<ScannedMedicine> = { ...med };
  for (const key of keys) delete copy[key];
  return copy as ScannedMedicine;
};

const withMeds = (meds: ScannedMedicine[], changes: Partial<ScanResult> = {}) => current({ medications: meds, ...changes });

export const CASES: ParityCase[] = [
  { name: 'receipt dispensed 28 Sep: every course ended', today: TODAY, result: scan(), active_names: [] },
  { name: 'receipt dispensed 3 Oct: every course runs', today: TODAY, result: current(), active_names: [] },
  { name: 'an older server answer: the top level is the one medicine', today: TODAY,
    result: current({ medications: undefined }), active_names: [] },
  { name: 'as needed: a 3-4 range, every 6 hours; without a maximum or interval; 1.33 hours', today: TODAY,
    result: withMeds([
      medicine({ schedule_time: SIX([]), times_per_day: 3, max_per_day: 4, as_needed: true, interval_hours: 6 }),
      medicine({ med_name: 'PRN PLAIN', schedule_time: SIX([]), times_per_day: null, as_needed: true }),
      medicine({ med_name: 'PRN ODD', schedule_time: SIX(['bedtime']), times_per_day: 2, as_needed: true,
        interval_hours: 1.33 }),
      medicine({ med_name: 'PRN TOO MANY', schedule_time: SIX([]), max_per_day: 30, as_needed: true }),
    ]), active_names: [] },
  { name: 'scheduled: a 3-4 range; every other day (interval 48) in the morning', today: TODAY,
    result: withMeds([
      medicine({ times_per_day: 3, max_per_day: 4 }),
      medicine({ med_name: 'QOD', times_per_day: null, interval_hours: 48, schedule_time: SIX(['morning']) }),
    ]), active_names: [] },
  { name: 'stock absent vs stock null, form absent vs form null', today: TODAY,
    result: withMeds([
      without(medicine({}), 'stock'), without(drops, 'stock'), medicine({ stock: null }), { ...drops, stock: null },
      without(medicine({ med_name: 'NO FORM' }), 'form'), medicine({ med_name: 'NULL FORM', form: null as never }),
    ]), active_names: [] },
  { name: 'half rounding: stock 2.5, units 1.005; stock 0.125, units 2.675', today: TODAY,
    result: withMeds([
      medicine({ stock: 2.5, units_per_dose: 1.005 }),
      medicine({ med_name: 'EIGHTHS', stock: 0.125, units_per_dose: 0.0625 }),
      medicine({ med_name: 'LIQUID', dose_form: 'liquid', stock: 12.5, units_per_dose: 2.675 }),
    ]), active_names: [] },
  { name: 'duplicates: dots, case, full-width, middle dots, a BOM, and an empty name', today: TODAY,
    result: withMeds([
      medicine({ med_name: 'Gastro F.C. tablets' }), medicine({ med_name: 'ＧＡＳＴＲＯ\u3000ＦＣ' }),
      medicine({ med_name: 'Vita·Cobal' }), medicine({ med_name: 'N/A' }), drops,
    ]), active_names: ['GASTRO FC TABLETS', 'gastro f.c.', 'VITA・COBAL\ufeff', '', 'Something else'] },
  { name: 'no slots and no meal marks: no schedule; the printed count falls through', today: TODAY,
    result: withMeds([
      medicine({ schedule_time: SIX([]), times_per_day: null }),
      medicine({ med_name: 'COUNT ONLY', schedule_time: SIX([]), times_per_day: 2 }),
      medicine({ med_name: 'MEALS ONLY', schedule_time: SIX(['after_meals']), times_per_day: null }),
      medicine({ med_name: 'NO SCHEDULE', schedule_time: null as never }),
    ]), active_names: [] },
  { name: 'days 0 and null: the printed use-before date', today: TODAY,
    result: withMeds([medicine({ days: 0 }), medicine({ med_name: 'NULL DAYS', days: null })],
      { use_before: '2027-03-31' }), active_names: [] },
  { name: 'dispensed 2026-02-30: JS rolls it over', today: '2026-03-01',
    result: withMeds([medicine({})], { date_dispensed: '2026-02-30', visit_date: 'N/A' }), active_names: [] },
  { name: 'dispensed 2026-13-01: JS rolls it over', today: '2026-12-20',
    result: withMeds([medicine({})], { date_dispensed: '2026-13-01', visit_date: 'N/A' }), active_names: [] },
  { name: 'use_before N/A, no days, a bad dispensing date: the visit date; untrimmed fields', today: TODAY,
    result: withMeds([
      medicine({ days: null, dosage: ' 500mg ', instructions: '  一天3次  ', warning: ' 飯後 ' }),
    ], { use_before: 'N/A', date_dispensed: '2026/10/03', visit_date: '2026-10-03', hospital: '  台大醫院 ',
      prescription_no: ' ', physician: '' }), active_names: [] },
  { name: 'six times a day with custom times; nine is too many', today: TODAY,
    result: withMeds([
      medicine({ times_per_day: 6, schedule_time: {
        ...SIX(['morning', 'noon', 'night', 'bedtime']), custom_times: ['16:00', '06:00', 'bad', '6:00'] } }),
      medicine({ med_name: 'NINE', times_per_day: 9, schedule_time: {
        ...SIX(['morning', 'noon', 'night', 'bedtime']),
        custom_times: ['16:00', '06:00', '10:00', '14:00', '23:00'] } }),
    ]), active_names: [] },
  { name: 'problems: units 0, stock too big, days too many, days 1.5', today: TODAY,
    result: withMeds([
      medicine({ units_per_dose: 0 }), medicine({ stock: 2_000_000 }), medicine({ days: 400 }),
      medicine({ days: 1.5 }),
    ]), active_names: [] },
];

if (import.meta.main) {
  const cases = CASES.map((c) => {
    const input = json(c);
    return { ...input, expected: expectedOf(input) };
  });
  writeFileSync(GOLDEN_FILE, `${JSON.stringify({ cases }, null, 2)}\n`);
  console.log(`wrote ${cases.length} cases to ${GOLDEN_FILE.pathname}`);
}
