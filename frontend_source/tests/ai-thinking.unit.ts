// Which thinking levels a job offers and which level each model is really sent (lib/ai-thinking.ts). No browser needed:
//   node --experimental-strip-types --test tests/ai-thinking.unit.ts
// The rule must match the server's ai_profile.effective_level: the nearest lower level the model accepts, else off,
// and never off for a model that always thinks (GPT-6 Astra and 6.1 Sol refuse reasoning_effort "none").
import assert from 'node:assert/strict';
import { test } from 'node:test';
import type { AiModel } from '../src/lib/ai-connections-api.ts';
import {
  JOB_CALL_LIMIT_MS, acceptedLevels, isThinkingChoice, nearestLevel, offeredLevels, sentLevel, thinkingMs, tooSlow,
} from '../src/lib/ai-thinking.ts';

const tested = (thinking?: unknown): AiModel => ({
  model_ref: 1, connection_id: 1, model_id: 'gpt-6-luna', company: 'OpenAI', region: 'USA', retention: '30 days',
  profile: { token_param: 'max_completion_tokens', temperature: true, reasoning_off: { reasoning_effort: 'none' },
    ...(thinking === undefined ? {} : { thinking }) } as AiModel['profile'],
  tested: true, copied_from_env: false, tested_at: '2026-10-11T09:00:00+00:00', test_ms: 900, last_error: null,
});
const untested: AiModel = { ...tested(), profile: null, tested: false, tested_at: null, test_ms: null };
const thinks = (levels: string[], ms: Record<string, number> = {}, always = false) =>
  tested({ param: 'reasoning_effort', levels, ms, always, temperature: false });
const cantThink = tested({ param: null, levels: [], ms: {}, always: false, temperature: false });

test('a model can think only when Test saved a setting to send and at least one level', () => {
  assert.deepEqual(acceptedLevels(undefined), []);
  assert.deepEqual(acceptedLevels(untested), []);
  assert.deepEqual(acceptedLevels(tested()), []);   // a profile saved before thinking levels existed
  assert.deepEqual(acceptedLevels(cantThink), []);
  assert.deepEqual(acceptedLevels(tested({ param: null, levels: ['low'], ms: {}, always: false, temperature: false })), []);
  // lowest first, and only the card's levels (xhigh is never offered)
  assert.deepEqual(acceptedLevels(thinks(['xhigh', 'high', 'low'])), ['low', 'high']);
});

test('a job level the model refuses becomes the nearest lower level it accepts, else off', () => {
  const lowAndHigh = thinks(['low', 'high']);
  assert.equal(sentLevel(lowAndHigh, 'off'), 'off');
  assert.equal(sentLevel(lowAndHigh, 'low'), 'low');
  assert.equal(sentLevel(lowAndHigh, 'medium'), 'low');
  assert.equal(sentLevel(lowAndHigh, 'high'), 'high');
  assert.equal(sentLevel(thinks(['medium']), 'low'), 'off');
  assert.equal(sentLevel(cantThink, 'high'), 'off');
  assert.equal(sentLevel(untested, 'medium'), 'off');
});

test('a model that always thinks is sent its lowest level for off and for anything below it', () => {
  const astra = thinks(['medium', 'high'], { medium: 5200 }, true);
  assert.equal(sentLevel(astra, 'off'), 'medium');
  assert.equal(sentLevel(astra, 'low'), 'medium');
  assert.equal(sentLevel(astra, 'high'), 'high');
  // the job keeps off when it moves to such a model: off is always offered
  assert.equal(nearestLevel(astra, 'off'), 'off');
  assert.equal(nearestLevel(astra, 'low'), 'off');
  assert.equal(nearestLevel(astra, 'high'), 'high');
});

test('the time shown is the time at the level really sent', () => {
  const luna = thinks(['low', 'medium', 'high'], { low: 1500, medium: 2300 });
  assert.equal(thinkingMs(luna, 'off'), null);
  assert.equal(thinkingMs(luna, 'medium'), 2300);
  assert.equal(thinkingMs(luna, 'high'), null);   // no time saved for high
  assert.equal(thinkingMs(thinks(['low'], { low: 1900 }), 'high'), 1900);
  assert.equal(thinkingMs(thinks(['low', 'high'], { low: 2700 }, true), 'off'), 2700);
});

test("a time over the job's per-call limit is too slow; one exactly at it is not", () => {
  assert.deepEqual(JOB_CALL_LIMIT_MS, { reply: 6000, safety: 2500, after_chat: 12000 });
  const at = (ms: number) => thinks(['low'], { low: ms });
  assert.equal(tooSlow('reply', at(6000), 'low'), false);
  assert.equal(tooSlow('reply', at(6001), 'low'), true);
  assert.equal(tooSlow('safety', at(2700), 'low'), true);
  assert.equal(tooSlow('after_chat', at(9300), 'low'), false);
  assert.equal(tooSlow('safety', at(9000), 'off'), false);   // off is not timed for a model that can stop thinking
});

test("a job offers off, its main model's levels, and its own saved level", () => {
  assert.deepEqual(offeredLevels(thinks(['low', 'medium', 'high']), 'off'), ['off', 'low', 'medium', 'high']);
  assert.deepEqual(offeredLevels(cantThink, 'off'), ['off']);
  assert.deepEqual(offeredLevels(thinks(['low']), 'high'), ['off', 'low', 'high']);
  assert.deepEqual(offeredLevels(undefined, 'medium'), ['off', 'medium']);
});

test('only off, low, medium and high are job levels', () => {
  for (const level of ['off', 'low', 'medium', 'high']) assert.equal(isThinkingChoice(level), true);
  for (const level of ['max', 'xhigh', '', null, undefined, 1]) assert.equal(isThinkingChoice(level), false);
});
