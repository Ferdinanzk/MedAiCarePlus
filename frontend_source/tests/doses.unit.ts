// Which real dose the app picks or offers (lib/doses.ts). No browser needed:
//   node --experimental-strip-types --test tests/doses.unit.ts
// The day is the user's real allegra schedule on 3 Oct 2026 (Taipei): 08:00, 12:00, 20:00, 22:00, with the
// due_from the server sends (two hours before, and 21:00 for 22:00: halfway from the 20:00 dose).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  blockOf, doseRefusal, dueDose, expiresAt, forgetRefusals, hasReplyFor, isDue, isOpen, medBlock, nextDose,
  notDueYet, refusalMessage, rememberRefusal,
} from '../src/lib/doses.ts';

const at = (hhmm: string) => Date.parse(`2026-10-03T${hhmm}:00+08:00`);
const iso = (hhmm: string) => `2026-10-03T${hhmm}:00+08:00`;
const dose = (id: number, hhmm: string, from: string, status = 'pending', med_id = 1) => ({
  id, med_id, name: 'allegra', status, scheduled_time: iso(hhmm), due_from: iso(from),
});
const day = (status: Record<number, string> = {}) => [
  dose(5, '08:00', '06:00', status[5]), dose(6, '12:00', '10:00', status[6]),
  dose(7, '20:00', '18:00', status[7]), dose(8, '22:00', '21:00', status[8]),
];
// A stand-in for i18next's t: the key, so a test can tell which sentence was chosen.
const t = ((key: string) => key) as never;

test('just after midnight no dose is due, and the next one is named with its time', () => {
  for (const moment of ['00:05', '00:09', '00:17']) {           // the three test alerts of 3 Oct 2026
    assert.equal(dueDose(day(), at(moment)), undefined);
    assert.equal(nextDose(day(), at(moment))?.id, 5);
  }
});

test('the window boundary is inclusive, like the server', () => {
  assert.equal(dueDose(day(), at('05:59')), undefined);
  assert.equal(dueDose(day(), at('06:00'))?.id, 5);
  assert.equal(isDue(dose(6, '12:00', '10:00'), at('10:00')), true);
  assert.equal(isDue(dose(6, '12:00', '10:00'), at('09:59')), false);
});

test('nearest to its time first; the open 08:00 dose expires when the 12:00 one becomes due', () => {
  assert.equal(dueDose(day(), at('07:00'))?.id, 5);
  assert.equal(dueDose(day(), at('09:59'))?.id, 5);
  assert.equal(dueDose(day(), at('10:00'))?.id, 6);             // halfway: 08:00 is missed, not made up
  assert.equal(dueDose(day(), at('10:00'), false)?.id, 5);      // protection off: both two hours away, the earlier
  assert.equal(dueDose(day(), at('10:01'), false)?.id, 6);      // 12:00 is nearer than the open 08:00
  assert.equal(dueDose(day({ 5: 'missed' }), at('12:30'))?.id, 6);
});

test('the 22:00 dose is not used right after the 20:00 one', () => {
  const evening = day({ 5: 'taken', 6: 'taken', 7: 'taken' });
  assert.equal(dueDose(evening, at('20:05')), undefined);
  assert.equal(nextDose(evening, at('20:05'))?.due_from, '2026-10-03T21:00:00+08:00');
  assert.equal(dueDose(evening, at('21:00'))?.id, 8);
});

test('only pending and missed doses are open; a day with every dose closed has none', () => {
  assert.deepEqual(['pending', 'missed', 'taken', 'skipped', 'pending_confirmation'].map(status => isOpen({ status })),
    [true, true, false, false, false]);
  const done = day({ 5: 'taken', 6: 'taken', 7: 'skipped', 8: 'pending_confirmation' });
  assert.equal(dueDose(done, at('23:59')), undefined);
  assert.equal(nextDose(done, at('23:59')), undefined);
  const missed2000 = day({ 5: 'taken', 6: 'taken', 8: 'taken' });
  assert.equal(dueDose(missed2000, at('23:59')), undefined);      // the 20:00 dose expired at 21:00
  assert.equal(dueDose(missed2000, at('23:59'), false)?.id, 7);   // protection off: it can still be taken
});

test('a row without due_from (no time, or an ad-hoc dose made now) is always due', () => {
  assert.equal(isDue({ status: 'pending', scheduled_time: null, due_from: null }, at('00:00')), true);
  assert.equal(blockOf({ status: 'pending', scheduled_time: null, due_from: null }, [], at('00:00')), null);
});

test('a missed dose expires halfway to the same medicine\'s next dose; the last one of the day is the server\'s', () => {
  assert.equal(expiresAt(dose(5, '08:00', '06:00'), day()), at('10:00'));
  assert.equal(expiresAt(dose(7, '20:00', '18:00'), day()), at('21:00'));
  assert.equal(expiresAt(dose(8, '22:00', '21:00'), day()), null);
  assert.deepEqual(blockOf(day()[0], day(), at('10:00')), { reason: 'expired' });
  assert.equal(blockOf(day({ 5: 'taken', 6: 'taken', 7: 'taken' })[3], day(), at('23:59')), null);
  // Another medicine's dose does not count as "the next one".
  const other = [dose(5, '08:00', '06:00'), dose(9, '09:00', '07:00', 'pending', 2)];
  assert.equal(expiresAt(other[0], other), null);
  assert.equal(blockOf(other[0], other, at('09:30')), null);
});

test("the server's expires_at wins: the last dose of the day, and ad-hoc doses that never expire", () => {
  // 22:00 expires at 03:00, halfway to tomorrow's 08:00, which the day's list does not have.
  const bedtime = { ...dose(8, '22:00', '21:00'), expires_at: '2026-10-04T03:00:00+08:00' };
  assert.equal(expiresAt(bedtime, day()), Date.parse('2026-10-04T03:00:00+08:00'));
  assert.deepEqual(blockOf(bedtime, day(), Date.parse('2026-10-04T03:00:00+08:00')), { reason: 'expired' });
  assert.equal(blockOf(bedtime, day(), Date.parse('2026-10-04T02:59:00+08:00')), null);
  // null: never (an ad-hoc dose, or no later dose), even with a later dose in the list.
  assert.equal(expiresAt({ ...dose(5, '08:00', '06:00'), expires_at: null }, day()), null);
  assert.equal(blockOf({ ...dose(5, '08:00', '06:00'), expires_at: null }, day(), at('11:00')), null);
});

test('with protection off nothing is blocked (the behaviour before 3 Oct), but a pill still counts for a due dose', () => {
  assert.equal(blockOf(day()[1], day(), at('00:05'), false), null);
  assert.equal(blockOf(day()[0], day(), at('11:00'), false), null);
  // Like the server's Take Now: no due dose means an ad-hoc one, not the 08:00 dose eight hours early.
  assert.equal(dueDose(day(), at('00:05'), false), undefined);
  assert.equal(nextDose(day(), at('00:05'))?.id, 5);
});

test('a not-due dose says until when', () => {
  assert.deepEqual(blockOf(day()[1], day(), at('09:00')), { reason: 'not_due', until: at('10:00') });
});

test("the server's 409 bodies are recognised, nothing else is", () => {
  const body = { detail: 'dose_not_due_yet', intk_id: 5, scheduled_time: '2026-10-03T08:00:00+08:00',
                 due_from: '2026-10-03T06:00:00+08:00' };
  assert.equal(notDueYet(body)?.due_from, body.due_from);
  assert.equal(notDueYet({ detail: 'busy_other_client' }), null);
  assert.equal(notDueYet(null), null);
  for (const detail of ['dose_not_due_yet', 'dose_too_soon', 'daily_max_reached', 'dose_expired']) {
    assert.equal(doseRefusal({ detail, intk_id: 6 })?.detail, detail);
  }
  assert.equal(doseRefusal({ detail: 'busy_other_client' }), null);
  assert.equal(doseRefusal('dose_too_soon'), null);
  assert.equal(doseRefusal(null), null);
});

test("the server's reply is shown as it is; without one the page words it", () => {
  const reply = '這個藥您凌晨12點05分已經吃過了，請先不要再吃。';
  assert.equal(refusalMessage({ detail: 'dose_too_soon', reply }, t), reply);
  assert.equal(refusalMessage({ detail: 'dose_too_soon', med_name: 'allegra', last_taken_at: iso('00:05'),
                                next_allowed_at: iso('06:05') }, t), 'overdose.tooSoonUntil');
  assert.equal(refusalMessage({ detail: 'dose_too_soon', last_taken_at: iso('00:05') }, t), 'overdose.tooSoon');
  assert.equal(refusalMessage({ detail: 'dose_too_soon' }, t), 'overdose.tooSoonRecently');
  assert.equal(refusalMessage({ detail: 'daily_max_reached', med_name: 'allegra' }, t), 'overdose.dailyMax');
  assert.equal(refusalMessage({ detail: 'dose_expired', scheduled_time: iso('08:00') }, t), 'overdose.expired');
  assert.equal(refusalMessage({ detail: 'dose_not_due_yet', scheduled_time: iso('08:00'), due_from: iso('06:00') }, t),
    'intake.doseNotDueYet');
});

test("the server's sentence is in the patient's language; a page in another one words it itself", () => {
  const zh = { detail: 'dose_too_soon' as const, reply: '這個藥您凌晨12點05分已經吃過了，請先不要再吃。', language: 'zh-TW',
               last_taken_at: iso('00:05') };
  assert.equal(refusalMessage(zh, t, 'zh-TW'), zh.reply);
  assert.equal(refusalMessage(zh, t, 'zh'), zh.reply);
  assert.equal(refusalMessage(zh, t, 'en'), 'overdose.tooSoon');
  assert.equal(refusalMessage({ ...zh, reply: 'You already took it.', language: 'en' }, t, 'en-US'), 'You already took it.');
  assert.equal(refusalMessage({ ...zh, language: undefined }, t, 'en'), zh.reply);   // an older server: as it is
  assert.equal(hasReplyFor(zh, 'en'), false);
  assert.equal(hasReplyFor(zh), true);
});

test('a refused gap or daily maximum blocks that medicine until the server allows it again', () => {
  forgetRefusals();
  rememberRefusal({ detail: 'dose_too_soon', intk_id: 6, last_taken_at: iso('09:00'), next_allowed_at: iso('13:00') },
    1, at('12:00'));
  const taken0900 = day({ 5: 'taken' });
  assert.equal(blockOf(taken0900[1], taken0900, at('12:00'))?.reason, 'too_soon');
  assert.equal(blockOf(taken0900[1], taken0900, at('13:00')), null);
  assert.equal(blockOf(taken0900[1], taken0900, at('12:00'), false), null);     // protection off
  assert.equal(medBlock(2, at('12:00')), null);                                 // another medicine
  // The later of the two wins: 20:00 is not due until 18:00, after the gap ends at 13:00.
  assert.deepEqual(blockOf(taken0900[2], taken0900, at('12:30')), { reason: 'not_due', until: at('18:00') });

  // A daily maximum without a time lasts until the device's midnight; an expired dose stays expired.
  rememberRefusal({ detail: 'daily_max_reached', intk_id: 8 }, 3, at('21:00'));
  const midnight = new Date(at('21:00')).setHours(24, 0, 0, 0);
  assert.equal(medBlock(3, midnight - 60_000)?.reason, 'daily_max');
  assert.equal(medBlock(3, midnight), null);
  rememberRefusal({ detail: 'dose_expired', intk_id: 8 }, 1, at('23:00'));
  assert.deepEqual(blockOf(day()[3], day(), at('23:00')), { reason: 'expired' });

  // A too-soon answer without next_allowed_at is not remembered: the server is asked again.
  rememberRefusal({ detail: 'dose_too_soon', intk_id: 7 }, 4, at('12:00'));
  assert.equal(medBlock(4, at('12:00')), null);

  forgetRefusals();
  assert.equal(blockOf(taken0900[1], taken0900, at('12:00')), null);
  assert.equal(blockOf(day()[3], day(), at('23:00')), null);
});
