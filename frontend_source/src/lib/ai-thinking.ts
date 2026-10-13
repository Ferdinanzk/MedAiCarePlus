// Thinking levels on the AI connections card (spec 2026-10-11 D6): what a job's Thinking list offers, the level each
// model is really sent, and its time in Test. No browser needed, so tests/ai-thinking.unit.ts runs it in Node.
import type { AiJob, AiModel, AiThinkingLevel } from './ai-connections-api';

/** The levels above off, lowest first (ai_profile.THINKING_LEVELS). */
export const THINKING_LEVELS: AiThinkingLevel[] = ['low', 'medium', 'high'];
/** Every value a job's thinking can have (ai_job.thinking's CHECK), lowest first. */
export const THINKING_CHOICES: AiThinkingLevel[] = ['off', ...THINKING_LEVELS];

/** The longest one model call of each job may take, in ms: conversation.py CALL_TIMEOUT (6 s), RISK_CALL_TIMEOUT
 *  (2.5 s) and SUMMARY_CALL_TIMEOUT (12 s). A main model slower than this in Test gets a warning on its job. */
export const JOB_CALL_LIMIT_MS: Record<AiJob, number> = { reply: 6000, safety: 2500, after_chat: 12000 };

export const isThinkingChoice = (value: unknown): value is AiThinkingLevel =>
  THINKING_CHOICES.includes(value as AiThinkingLevel);

const rank = (level: AiThinkingLevel) => THINKING_CHOICES.indexOf(level);

/** The levels the model accepts, lowest first. None when it is untested, was tested before thinking levels existed,
 *  or can't think (Test saved no setting to send). */
export function acceptedLevels(model: AiModel | undefined): AiThinkingLevel[] {
  const thinking = model?.profile?.thinking;
  if (!thinking?.param) return [];
  return THINKING_LEVELS.filter(level => thinking.levels.includes(level));
}

/** The level to keep when the job's main model changes to `model`: `level` if it accepts it, else the nearest lower
 *  level it accepts, else off. */
export function nearestLevel(model: AiModel | undefined, level: AiThinkingLevel): AiThinkingLevel {
  const lower = acceptedLevels(model).filter(name => rank(name) <= rank(level));
  return lower.length ? lower[lower.length - 1] : 'off';
}

/** The level the server really sends `model` when its job is set to `level` (ai_profile.effective_level): like
 *  nearestLevel, except that a model that always thinks never gets off; it gets its lowest level. */
export function sentLevel(model: AiModel | undefined, level: AiThinkingLevel): AiThinkingLevel {
  const nearest = nearestLevel(model, level);
  const lowest = acceptedLevels(model)[0];
  return nearest === 'off' && model?.profile?.thinking?.always && lowest ? lowest : nearest;
}

/** The model's time in Test, in ms, at the level it is sent for `level`; null at off or when that level has no time. */
export function thinkingMs(model: AiModel | undefined, level: AiThinkingLevel): number | null {
  const sent = sentLevel(model, level);
  const ms = sent === 'off' ? undefined : model?.profile?.thinking?.ms[sent];
  return typeof ms === 'number' ? ms : null;
}

/** True when that time is over what one call of the job may take. */
export function tooSlow(job: AiJob, model: AiModel | undefined, level: AiThinkingLevel): boolean {
  const ms = thinkingMs(model, level);
  return ms !== null && ms > JOB_CALL_LIMIT_MS[job];
}

/** What the job's Thinking list offers: off, each level its main model accepts, and the job's own level even when
 *  that model doesn't accept it (saved with another main model, or before a new Test), so the list shows what is saved. */
export function offeredLevels(main: AiModel | undefined, level: AiThinkingLevel): AiThinkingLevel[] {
  const accepted = acceptedLevels(main);
  return THINKING_CHOICES.filter(name => name === 'off' || name === level || accepted.includes(name));
}
