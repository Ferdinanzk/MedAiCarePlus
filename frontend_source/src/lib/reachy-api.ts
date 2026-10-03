import { getFaceAuthHeaders } from './face-auth';

export interface ReachyStatus {
  paired: boolean;
  device_id?: string;
  label?: string;
  auto_record?: boolean;
  last_seen_at?: string | null;
  online?: boolean;
  robot_reachable?: boolean | null;
  landmark_fps?: number | null;
  alert_contacts?: number;
  last_task?: { task_id: string; status: string; slot_time: string; finished_at: string | null } | null;
}

/** A failed request; `message` is the server's `detail`, `body` the rest (e.g. a dose's time with dose_not_due_yet). */
export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, detail: string, body: unknown) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

export async function call<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { ...getFaceAuthHeaders(), ...(init.body ? { 'Content-Type': 'application/json' } : {}), ...init.headers },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ApiError(response.status, typeof body.detail === 'string' ? body.detail : `request_failed_${response.status}`, body);
  }
  return body as T;
}

export const fetchReachyStatus = () => call<ReachyStatus>('/api/reachy/status', { cache: 'no-store' });

export const pairReachy = () => call<{ device_id: string; token: string }>('/api/reachy/pairing', { method: 'POST' });

export const unpairReachy = () => call<ReachyStatus>('/api/reachy/pairing', { method: 'DELETE' });

export const setAutoRecord = (autoRecord: boolean) =>
  call<ReachyStatus>('/api/reachy/settings', { method: 'PATCH', body: JSON.stringify({ auto_record: autoRecord }) });

export const queueReachyTask = (intkId: number) =>
  call<{ task_id: string; status: string }>('/api/reachy/tasks', { method: 'POST', body: JSON.stringify({ intk_id: intkId }) });

export const startCheckin = () =>
  call<{ task_id: string; status: string }>('/api/reachy/checkin', { method: 'POST' });

export type Mood = 'happy' | 'calm' | 'sad' | 'worried' | 'angry' | 'unknown';

export interface ConversationSummary {
  id: string;
  started_at: string;
  ended_at: string | null;
  end_reason: string | null;
  summary: string | null;
  mood: Mood | null;
  risk_flag: boolean;
  language: string;
  patient_turns: number;
  first_words: string | null;
}

/** Stage timings in ms. Patient turn: {robot: {...}}; Reachy turn: {server: {...}, robot: {...}}. */
export type TurnMetrics = Record<string, unknown>;

export interface ConversationTurn {
  turn_id: number;
  role: 'patient' | 'reachy';
  text: string;
  flagged: boolean;
  created_at: string;
  metrics: TurnMetrics | null;
}

export interface ConversationDetail extends Omit<ConversationSummary, 'patient_turns' | 'first_words'> {
  model: string | null;
  turns: ConversationTurn[];
}

export const fetchConversations = (limit = 20, offset = 0) =>
  call<{ items: ConversationSummary[]; total: number; has_more: boolean }>(
    `/api/conversations?limit=${limit}&offset=${offset}`, { cache: 'no-store' });

export const fetchConversation = (id: string) =>
  call<ConversationDetail>(`/api/conversations/${id}`, { cache: 'no-store' });

export const deleteConversation = (id: string) =>
  call<{ deleted: string }>(`/api/conversations/${id}`, { method: 'DELETE' });

export interface StageTiming {
  count: number;
  median_ms: number | null;
  p90_ms: number | null;
}

export interface ConversationMetricsSummary {
  days: number;
  turns: number;
  /** Keyed by stage name, e.g. "stt_ms" or "speech_end_to_first_sound_ms". */
  stages: Record<string, StageTiming>;
  /** Share (0..1) of AI replies that fell back to the fixed line; null when no AI reply was made. */
  fallback_rate: number | null;
  models: { model: string; count: number; median_ms: number | null }[];
}

/** One turn's timings for export: the text itself is never sent, only its length. */
export interface ConversationMetricTurn {
  conversation_id: string;
  turn_id: number;
  role: 'patient' | 'reachy';
  created_at: string;
  text_chars: number;
  metrics: TurnMetrics | null;
}

export const fetchConversationMetricsSummary = (days = 7) =>
  call<ConversationMetricsSummary>(`/api/conversations/metrics/summary?days=${days}`, { cache: 'no-store' });

export const fetchConversationMetricTurns = (days = 7) =>
  call<{ items: ConversationMetricTurn[] }>(`/api/conversations/metrics/turns?days=${days}`, { cache: 'no-store' });

export interface TodayDose {
  id: number;
  med_id: number;
  name: string;
  status: string;
  scheduled_time: string | null;
  /** From when the server lets this dose be started or recorded. */
  due_from: string | null;
  /** When the server stops counting it if not taken (null: never). */
  expires_at?: string | null;
}

export const fetchTodayDoses = () => call<TodayDose[]>('/api/medications/today', { cache: 'no-store' });
