// The Scan page's drafts and save payloads (lib/scan.ts). No browser needed:
//   node --experimental-strip-types --test tests/scan.unit.ts
// The scan is shaped like the server's answer for a three-row pharmacy receipt (made-up names and numbers).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  autoRecordable, courseStart, doseLimits, draftFrom, draftProblem, lastDayFor, markDuplicates, medicationPayload,
  medicinesOf, sameName, saveErrorKey, slotsForCount, withAsNeeded, withDays, withSlot, withTimesPerDay,
  withoutCustomTime,
} from '../src/lib/scan.ts';
import type { ScanResult, ScannedMedicine } from '../src/lib/scan.ts';

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
/** The receipt dispensed yesterday: its five-day course is still running. */
const current = (changes: Partial<ScanResult> = {}) =>
  scan({ date_dispensed: '2026-10-03', visit_date: '2026-10-03', ...changes });

test('every medicine of the scan becomes a draft; an older server answer is one medicine', () => {
  assert.deepEqual(medicinesOf(scan()).map((m) => m.med_name),
    ['VITACOBAL CAPSULES 0', 'GASTRO F.C. TABLETS', 'OTOMYX OTIC DROPS.']);
  const legacy = scan({ medications: undefined });
  assert.deepEqual(medicinesOf(legacy).map((m) => m.med_name), ['VITACOBAL CAPSULES 0']);
});

test('the course runs from the dispensing date for its days; without days the printed use-before date', () => {
  assert.equal(courseStart(scan(), TODAY), '2026-09-28');
  assert.equal(courseStart(scan({ date_dispensed: 'N/A', visit_date: 'N/A' }), TODAY), TODAY);
  assert.equal(lastDayFor('2026-09-28', 5), '2026-10-02');
  assert.equal(lastDayFor('2026-12-30', 5), '2027-01-03');
  assert.equal(lastDayFor('2026-09-28', null, '2027-03-31'), '2027-03-31');
  assert.equal(lastDayFor('2026-09-28', null, 'N/A'), '');
});

test('a draft starts from what the paper says', () => {
  const draft = draftFrom(drops, current(), TODAY);
  assert.equal(draft.include, true);
  assert.equal(draft.name, 'OTOMYX OTIC DROPS.');
  assert.equal(draft.doseForm, 'other');
  assert.equal(draft.stock, '10');
  assert.equal(draft.unitsPerDose, '1');
  assert.equal(draft.timesPerDay, '2');
  assert.equal(draft.lastDay, '2026-10-07');
  assert.deepEqual(draft.slots, SIX(['morning', 'night']));
  assert.equal(draftProblem(draft, TODAY), null);
});

test('a course that ended by the paper starts unticked and cannot be saved until its last day changes', () => {
  // Dispensed 28 Sep for 5 days: the last day was 2 Oct, two days before the scan.
  const draft = draftFrom(medicine({}), scan(), TODAY);
  assert.equal(draft.lastDay, '2026-10-02');
  assert.equal(draft.include, false);
  assert.equal(draftProblem({ ...draft, include: true }, TODAY), 'lastDayPast');
  assert.equal(draftProblem({ ...draft, lastDay: '' }, TODAY), null);
  assert.equal(draftProblem({ ...draft, lastDay: TODAY }, TODAY), null);       // today is still a day of the course
});

test('the stock is never a count of bottles, and less than one dose on hand cannot be saved', () => {
  // The server could not count the doses of a bottle used as needed: nothing is prefilled.
  const prnDrops = { ...drops, stock: null, as_needed: true, schedule_time: SIX([]) };
  const draft = draftFrom(prnDrops, current(), TODAY);
  assert.equal(draft.stock, '');
  assert.equal(draftProblem(draft, TODAY), 'stockLow');
  assert.equal(draftProblem({ ...draft, stock: '0.5' }, TODAY), 'stockLow');
  assert.equal(draftProblem({ ...draft, stock: '20' }, TODAY), null);
  // An older server answer without `stock`: a tablet's printed count is its stock, a bottle's is not.
  const older = (med: ScannedMedicine) => {
    const copy: Partial<ScannedMedicine> = { ...med };
    delete copy.stock;
    return copy as ScannedMedicine;
  };
  assert.equal(draftFrom(older(drops), current(), TODAY).stock, '');
  assert.equal(draftFrom(older(medicine({})), current(), TODAY).stock, '15');
  // Two tablets a dose need at least two on hand.
  assert.equal(draftProblem({ ...draftFrom(medicine({}), current(), TODAY), stock: '1', unitsPerDose: '2' }, TODAY),
    'stockLow');
});

test('an as-needed medicine keeps its daily maximum apart from its dose times', () => {
  const prn = medicine({ schedule_time: SIX([]), times_per_day: 3, as_needed: true, interval_hours: 6 });
  let draft = draftFrom(prn, current(), TODAY);
  assert.equal(draft.asNeeded, true);
  assert.equal(draft.maxDaily, '3');
  assert.equal(draft.timesPerDay, '');                 // no fixed times
  draft = { ...draft, maxDaily: '4' };                 // the user corrects the maximum: no times appear
  assert.deepEqual(draft.slots, SIX([]));
  let body = medicationPayload(draft, current());
  assert.equal(body.schedule_time, null);
  assert.equal(body.max_daily_doses, 4);
  assert.equal(body.min_interval_minutes, 360);
  assert.equal(draftProblem({ ...draft, maxDaily: '30' }, TODAY), 'maxDaily');
  // Switched off, it becomes a scheduled medicine; switched on again, the maximum comes back from the paper.
  draft = withAsNeeded(withTimesPerDay(withAsNeeded(draft, false), '2'), true);
  assert.deepEqual(draft.slots, SIX(['morning', 'night']));
  assert.equal(draft.maxDaily, '4');
  body = medicationPayload(withAsNeeded({ ...draft, maxDaily: '' }, true), current());
  assert.equal(body.max_daily_doses, 3);               // the paper's count
  // A range on the paper: the upper end is the maximum.
  assert.equal(draftFrom({ ...prn, max_per_day: 4 }, current(), TODAY).maxDaily, '4');
});

test('a printed range allows its upper count, and every other day keeps two days apart', () => {
  const range = draftFrom(medicine({ times_per_day: 3, max_per_day: 4 }), current(), TODAY);
  assert.deepEqual(doseLimits(range), { max_daily_doses: 4 });
  assert.deepEqual(doseLimits(draftFrom(medicine({}), current(), TODAY)), {});
  const qod = draftFrom(medicine({ times_per_day: null, interval_hours: 48, schedule_time: SIX([]) }), current(), TODAY);
  assert.deepEqual(doseLimits(withSlot(qod, 'morning', true)), { min_interval_minutes: 2760 });
});

test('a medicine already in the list starts unticked, once', () => {
  const drafts = [draftFrom(medicine({}), current(), TODAY), draftFrom(drops, current(), TODAY)];
  assert.equal(sameName('Otomyx Otic Drops', 'OTOMYX OTIC DROPS.'), true);
  assert.equal(sameName('', ''), false);
  let marked = markDuplicates(drafts, ['otomyx otic drops', 'Something else']);
  assert.deepEqual(marked.map((d) => [d.include, d.duplicate === true]), [[true, false], [false, true]]);
  // The user ticks it again: a later check leaves that choice alone. Saved drafts are never marked.
  marked = markDuplicates([marked[0], { ...marked[1], include: true }], ['OTOMYX OTIC DROPS.']);
  assert.equal(marked[1].include, true);
  const saved = { ...drafts[0], status: 'saved' as const };
  assert.deepEqual(markDuplicates([saved], ['VITACOBAL CAPSULES 0']), [saved]);
});

test('a failed save says why in the page language, never the server text', () => {
  assert.equal(saveErrorKey(400, 'asyncpg.exceptions.DataError: invalid input'), 'scan.saveOneFailed');
  assert.equal(saveErrorKey(401, 'Not authenticated'), 'scan.notLoggedIn');
  assert.equal(saveErrorKey(403, 'consent_required'), 'scan.errors.consent_required');
  assert.equal(saveErrorKey(null, undefined), 'scan.saveOneFailed');
});

test('times a day and the slot boxes follow each other; meal marks stay', () => {
  let draft = draftFrom(medicine({ schedule_time: SIX(['morning', 'after_meals']), times_per_day: 1 }), scan(), TODAY);
  draft = withTimesPerDay(draft, '3');
  assert.deepEqual(draft.slots, SIX(['morning', 'noon', 'night', 'after_meals']));
  draft = withSlot(draft, 'noon', false);
  assert.equal(draft.timesPerDay, '2');
  draft = withSlot(draft, 'before_meals', true);
  assert.equal(draft.timesPerDay, '2');
  draft = withTimesPerDay(draft, '6');
  assert.deepEqual(draft.customTimes, ['16:00', '06:00']);
  draft = withoutCustomTime(draft, '06:00');
  assert.equal(draft.timesPerDay, '5');
  assert.deepEqual(slotsForCount(1).slots, { morning: true, noon: false, night: false, bedtime: false });
  assert.equal(withTimesPerDay(draft, '').timesPerDay, '');
});

test('changing the days moves the last day', () => {
  const draft = withDays(draftFrom(medicine({}), scan(), TODAY), '7', '2026-09-28');
  assert.equal(draft.lastDay, '2026-10-04');
  assert.equal(withDays(draft, 'x', '2026-09-28').lastDay, '2026-10-04');
});

test('problems keep a draft from being saved', () => {
  const draft = draftFrom(medicine({}), scan(), TODAY);
  assert.equal(draftProblem(draft), null);
  assert.equal(draftProblem({ ...draft, name: ' ' }), 'name');
  assert.equal(draftProblem({ ...draft, stock: '-1' }), 'stock');
  assert.equal(draftProblem({ ...draft, unitsPerDose: '0' }), 'unitsPerDose');
  assert.equal(draftProblem({ ...draft, days: '1.5' }), 'days');
  assert.equal(draftProblem({ ...draft, customTimes: ['06:00', '10:00', '14:00', '16:00', '18:00', '23:00'] }),
    'tooManyTimes');
});

test('only one tablet or capsule a dose can be recorded by the camera alone', () => {
  const capsule = draftFrom(medicine({}), scan(), TODAY);
  assert.equal(autoRecordable(capsule), true);
  assert.equal(autoRecordable({ ...capsule, unitsPerDose: '2' }), false);
  assert.equal(autoRecordable(draftFrom(drops, scan(), TODAY)), false);
});

test('the save payload fits the medication API', () => {
  const result = current();
  const body = medicationPayload(draftFrom(drops, result, TODAY), result);
  assert.equal(body.name, 'OTOMYX OTIC DROPS.');
  assert.equal(body.dose_form, 'other');                 // never auto-recorded
  assert.equal(body.units_per_dose, 1);
  assert.equal(body.pills_remaining, 10);
  assert.equal(body.total_pills, 10);
  assert.equal(body.use_before, '2026-10-07');
  assert.equal(body.dosage, null);
  assert.deepEqual(body.schedule_time, { ...SIX(['morning', 'night']), custom_times: [] });
  assert.equal(body.prescription_meta.pharmacy, '安心藥局');
  assert.equal(body.prescription_meta.hospital, undefined);
  assert.equal(body.prescription_meta.days, 5);
  // use_before ends the reminders; course_end marks it as the course's last day, not an expiry date.
  assert.equal(body.prescription_meta.course_end, '2026-10-07');
  assert.equal(body.prescription_meta.printed_use_before, undefined);
  assert.equal('max_daily_doses' in body, false);
  // Without days the last day is the printed use-before date: that one is an expiry, not a course end.
  const printed = current({ use_before: '2027-03-31' });
  const noDays = medicationPayload(draftFrom(medicine({ days: null }), printed, TODAY), printed);
  assert.equal(noDays.use_before, '2027-03-31');
  assert.equal(noDays.prescription_meta.course_end, undefined);
  assert.equal(noDays.prescription_meta.printed_use_before, '2027-03-31');
});

test('an unscheduled medicine saves without a schedule; an as-needed one keeps its daily maximum', () => {
  const result = current();
  const none = draftFrom(medicine({ schedule_time: SIX([]), times_per_day: null }), result, TODAY);
  assert.equal(medicationPayload(none, result).schedule_time, null);
  const prn = draftFrom(medicine({ schedule_time: SIX([]), times_per_day: 3, as_needed: true, interval_hours: 6 }),
    result, TODAY);
  const body = medicationPayload(prn, result);
  assert.equal(body.schedule_time, null);
  assert.equal(body.max_daily_doses, 3);
  assert.equal(body.min_interval_minutes, 360);
});
