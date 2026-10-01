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
  last_task?: { task_id: string; status: string; slot_time: string; finished_at: string | null } | null;
}

async function call<T>(url: string, init: RequestInit = {}): Promise<T> {
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
