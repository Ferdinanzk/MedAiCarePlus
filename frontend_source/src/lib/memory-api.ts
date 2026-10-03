import { call } from './reachy-api';

export type MemoryKind = 'name' | 'person' | 'like' | 'routine' | 'event';

export interface MemoryFact {
  kind: MemoryKind;
  subject: string;
  text: string;
  event_date: string | null;
  source: 'chat' | 'patient';
  learned_at: string;
  conversation_ids: string[];
}

export interface MemoryList { enabled: boolean; items: MemoryFact[] }
export interface MemoryInput { kind: MemoryKind; text: string; event_date?: string | null }

const where = (kind: MemoryKind, subject: string) =>
  `kind=${encodeURIComponent(kind)}&subject=${encodeURIComponent(subject)}`;

export const fetchMemory = () => call<MemoryList>('/api/memory', { cache: 'no-store' });

export const addMemory = (input: MemoryInput) =>
  call<MemoryFact>('/api/memory', { method: 'POST', body: JSON.stringify(input) });

export const updateMemory = (kind: MemoryKind, subject: string, input: { text: string; event_date?: string | null }) =>
  call<MemoryFact>(`/api/memory/fact?${where(kind, subject)}`, { method: 'PATCH', body: JSON.stringify(input) });

export const deleteMemoryFact = (kind: MemoryKind, subject: string) =>
  call<{ deleted: number }>(`/api/memory/fact?${where(kind, subject)}`, { method: 'DELETE' });

export const deleteAllMemory = () => call<{ deleted: number }>('/api/memory?confirm=all', { method: 'DELETE' });
