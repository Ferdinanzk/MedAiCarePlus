import { isThinkingChoice } from './ai-thinking';
import { ApiError, call } from './reachy-api';

export type AiKind = 'openrouter' | 'openai' | 'openai_compatible';
export type AiJob = 'reply' | 'safety' | 'after_chat';

export interface AiConnection {
  connection_id: number; name: string; kind: AiKind; base_url: string;
  key_last4: string | null; key_readable: boolean; has_key: boolean; service_name: string;
}
/** A job's thinking level (ai_job.thinking). 'off' sends every request as before thinking levels existed. */
export type AiThinkingLevel = 'off' | 'low' | 'medium' | 'high';
/** What Test found about thinking (ai_profile.detect_thinking): the setting it sends ('reasoning_effort',
 *  'openrouter_effort', or null when the model can't think), the levels the model accepts, its time at each in ms,
 *  whether it always thinks (it refuses to stop), and whether a thinking request may carry a temperature. */
export interface AiThinking { param: string | null; levels: string[]; ms: Record<string, number>; always: boolean; temperature: boolean }
/** A profile saved before thinking levels existed has no `thinking`: that model can't think. */
export interface AiProfile {
  token_param: string; temperature: boolean; reasoning_off: Record<string, unknown> | null; thinking?: AiThinking;
}
export interface AiModel {
  model_ref: number; connection_id: number; model_id: string; company: string; region: string; retention: string;
  profile: AiProfile | null; tested: boolean; copied_from_env: boolean; tested_at: string | null;
  test_ms: number | null; last_error: string | null;
}
export interface AiJobChoice { primary_ref: number; fallback_ref: number | null; thinking: AiThinkingLevel }
export interface AiSettings {
  connections: AiConnection[]; models: AiModel[]; jobs: Record<AiJob, AiJobChoice>;
  /** Per job: true when its main or backup model is tested and its connection has a readable key (as saved). */
  jobs_usable: Record<string, boolean>;
  seeded_at: string | null; weak_secret: boolean; no_key_in_use: boolean;
}
export interface AiTestResult { ok: boolean; ms: number | null; sample: string | null; profile: AiProfile | null; error: string | null }

export const AI_KINDS: AiKind[] = ['openrouter', 'openai', 'openai_compatible'];
export const AI_JOBS: AiJob[] = ['reply', 'safety', 'after_chat'];

const send = (method: string, body?: unknown): RequestInit =>
  body === undefined ? { method } : { method, body: JSON.stringify(body) };

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
const isNumber = (value: unknown) => typeof value === 'number' && Number.isFinite(value);
/** No `thinking` (a profile saved before thinking levels) is fine; a present one needs every part the card reads. */
const isThinking = (value: unknown) => value === undefined || value === null || (isObject(value)
  && (value.param === null || typeof value.param === 'string') && Array.isArray(value.levels)
  && value.levels.every(level => typeof level === 'string') && isObject(value.ms) && Object.values(value.ms).every(isNumber)
  && typeof value.always === 'boolean');
const hasThinking = (choice: unknown) => isObject(choice) && isThinkingChoice(choice.thinking);

/** True only when every part the card reads has the shape it expects. */
function isAiSettings(value: unknown): value is AiSettings {
  if (!isObject(value) || !Array.isArray(value.connections) || !Array.isArray(value.models) || !isObject(value.jobs)
    || !isObject(value.jobs_usable)) return false;
  const jobs = value.jobs;
  const usable = value.jobs_usable;
  return value.connections.every(c => isObject(c) && isNumber(c.connection_id) && typeof c.name === 'string' && typeof c.kind === 'string')
    && value.models.every(m => isObject(m) && isNumber(m.model_ref) && isNumber(m.connection_id) && typeof m.model_id === 'string'
      && (m.profile === null || (isObject(m.profile) && isThinking(m.profile.thinking))))
    && AI_JOBS.every(job => hasThinking(jobs[job]))
    && AI_JOBS.every(job => {
      const choice = jobs[job];
      return isObject(choice) && isNumber(choice.primary_ref) && (choice.fallback_ref === null || isNumber(choice.fallback_ref))
        && typeof usable[job] === 'boolean';
    });
}

/** The card; a malformed answer is a load failure, so one card can never take the Settings page down. */
export async function fetchAiSettings(): Promise<AiSettings> {
  const settings = await call<AiSettings>('/api/ai/settings', { cache: 'no-store' });
  if (!isAiSettings(settings)) throw new ApiError(500, 'loadFailed', settings);
  return settings;
}

/**
 * After a change other than "Save jobs", keep each job choice the person picked but has not saved yet, as long as
 * it still points at tested models; otherwise take the server's value for that job.
 */
export function mergeJobs(
  current: Record<AiJob, AiJobChoice>, server: Record<AiJob, AiJobChoice>, testedRefs: number[],
): Record<AiJob, AiJobChoice> {
  const merged = { ...server };
  for (const job of AI_JOBS) {
    const choice = current[job];
    if (choice && testedRefs.includes(choice.primary_ref) && (choice.fallback_ref === null || testedRefs.includes(choice.fallback_ref))) {
      merged[job] = choice;
    }
  }
  return merged;
}

export const addConnection = (body: { name: string; kind: AiKind; base_url?: string; api_key?: string; service_name: string }) =>
  call<AiSettings>('/api/ai/connections', send('POST', body));

export const updateConnection = (id: number, body: Partial<{ name: string; base_url: string; api_key: string; service_name: string }>) =>
  call<AiSettings>(`/api/ai/connections/${id}`, send('PATCH', body));

export const deleteConnection = (id: number) => call<AiSettings>(`/api/ai/connections/${id}`, send('DELETE'));

export const fetchRemoteModels = (id: number) => call<{ models: string[] }>(`/api/ai/connections/${id}/remote-models`);

export const addModel = (body: { connection_id: number; model_id: string; company: string; region: string; retention: string }) =>
  call<AiSettings>('/api/ai/models', send('POST', body));

export const updateModel = (ref: number, body: { company: string; region: string; retention: string }) =>
  call<AiSettings>(`/api/ai/models/${ref}`, send('PATCH', body));

export const deleteModel = (ref: number) => call<AiSettings>(`/api/ai/models/${ref}`, send('DELETE'));

export const testModel = (ref: number) => call<AiTestResult & { settings: AiSettings }>(`/api/ai/models/${ref}/test`, send('POST'));

export const saveJobs = (jobs: Record<AiJob, AiJobChoice>) => call<AiSettings>('/api/ai/jobs', send('PUT', jobs));
