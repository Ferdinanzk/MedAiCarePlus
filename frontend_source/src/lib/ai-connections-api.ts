import { ApiError, call } from './reachy-api';

export type AiKind = 'openrouter' | 'openai' | 'openai_compatible';
export type AiJob = 'reply' | 'safety' | 'after_chat';

export interface AiConnection {
  connection_id: number; name: string; kind: AiKind; base_url: string;
  key_last4: string | null; key_readable: boolean; has_key: boolean; service_name: string;
}
export interface AiProfile { token_param: string; temperature: boolean; reasoning_off: Record<string, unknown> | null }
export interface AiModel {
  model_ref: number; connection_id: number; model_id: string; company: string; region: string; retention: string;
  profile: AiProfile | null; tested: boolean; copied_from_env: boolean; tested_at: string | null;
  test_ms: number | null; last_error: string | null;
}
export interface AiJobChoice { primary_ref: number; fallback_ref: number | null }
export interface AiSettings {
  connections: AiConnection[]; models: AiModel[]; jobs: Record<AiJob, AiJobChoice>;
  seeded_at: string | null; weak_secret: boolean; no_key_in_use: boolean;
}
export interface AiTestResult { ok: boolean; ms: number | null; sample: string | null; profile: AiProfile | null; error: string | null }

export const AI_KINDS: AiKind[] = ['openrouter', 'openai', 'openai_compatible'];
export const AI_JOBS: AiJob[] = ['reply', 'safety', 'after_chat'];

const send = (method: string, body?: unknown): RequestInit =>
  body === undefined ? { method } : { method, body: JSON.stringify(body) };

/** The card; a malformed answer is a load failure, so one card can never take the Settings page down. */
export async function fetchAiSettings(): Promise<AiSettings> {
  const settings = await call<AiSettings>('/api/ai/settings', { cache: 'no-store' });
  if (!Array.isArray(settings?.connections) || !Array.isArray(settings?.models) || !settings?.jobs) {
    throw new ApiError(500, 'loadFailed', settings);
  }
  return settings;
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
