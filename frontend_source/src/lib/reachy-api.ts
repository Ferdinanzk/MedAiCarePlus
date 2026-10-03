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

export async function call<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { ...getFaceAuthHeaders(), ...(init.body ? { 'Content-Type': 'application/json' } : {}), ...init.headers },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `request_failed_${response.status}`);
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

export interface ConversationTurn {
  role: 'patient' | 'reachy';
  text: string;
  flagged: boolean;
  created_at: string;
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

export interface TodayDose {
  id: number;
  name: string;
  status: string;
}

export const fetchTodayDoses = () => call<TodayDose[]>('/api/medications/today', { cache: 'no-store' });
