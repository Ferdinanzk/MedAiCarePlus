import { useEffect, useState } from 'react';
import { faceLogout, getFaceAuthHeaders } from './face-auth';

export type LegalKind = 'core' | 'robot' | 'memory';
export type LegalLanguage = 'en' | 'zh-TW';
export type LegalBlock =
  | { type: 'p'; text: string }
  | { type: 'list'; items: string[] }
  | { type: 'table'; header: string[]; rows: string[][] };

export interface LegalNotice {
  kind: LegalKind;
  version: string;
  language: LegalLanguage;
  title: string;
  what_changed: string[];
  sections: { id: string; heading: string; blocks: LegalBlock[] }[];
}

export interface LegalResponse {
  kind: LegalKind;
  terms_version: string;
  language: LegalLanguage;
  sha256: string;
  complete: boolean;
  document: LegalNotice;
}

export interface ConsentStatus {
  terms_version: string;
  core_current: boolean;
  robot_current: boolean;
  scopes: Record<string, {
    granted: boolean;
    terms_version: string;
    kind: LegalKind;
    consent_id: number;
    created_at: string;
  }>;
}

export interface ConsentInput {
  kind: LegalKind;
  terms_version: string;
  language: LegalLanguage;
  document_sha256: string;
  scopes: Record<string, boolean>;
  source: 'reconsent' | 'settings' | 'pairing';
}

export class ConsentApiError extends Error {
  status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.name = 'ConsentApiError';
    this.status = status;
  }
}

export function legalLanguage(language: string): LegalLanguage {
  return language.toLowerCase().startsWith('zh') ? 'zh-TW' : 'en';
}

async function checkResponse(response: Response): Promise<Response> {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ConsentApiError(response.status, typeof body.detail === 'string' ? body.detail : 'request_failed');
  }
  return response;
}

export async function fetchLegal(kind: LegalKind, lang: string): Promise<LegalResponse> {
  const response = await fetch(`/api/legal/current?kind=${kind}&lang=${legalLanguage(lang)}`, {
    headers: getFaceAuthHeaders(), cache: 'no-store',
  });
  return (await checkResponse(response)).json();
}

export async function fetchConsentStatus(): Promise<ConsentStatus> {
  const response = await fetch('/api/consent/status', { headers: getFaceAuthHeaders(), cache: 'no-store' });
  return (await checkResponse(response)).json();
}

export async function postConsent(input: ConsentInput): Promise<ConsentStatus> {
  const response = await fetch('/api/consent', {
    method: 'POST',
    headers: { ...getFaceAuthHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  return (await checkResponse(response)).json();
}

export async function exportAccount(): Promise<void> {
  const response = await fetch('/api/account/export', { headers: getFaceAuthHeaders(), cache: 'no-store' });
  const blob = await (await checkResponse(response)).blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'medaicareplus-account.zip';
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function deleteAccount(password?: string): Promise<{ deleted: boolean }> {
  const response = await fetch('/api/account/delete', {
    method: 'POST',
    headers: { ...getFaceAuthHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify(password ? { password } : {}),
  });
  return (await checkResponse(response)).json();
}

export function logoutAccount(): void {
  faceLogout();
  for (const key of ['face_auth_session', 'face_auth_token', 'face_auth_user', 'onboarding_complete', 'onboarding_face_done']) {
    localStorage.removeItem(key);
  }
  window.location.href = '/login';
}

// Existing data pages call fetch directly. Observe only our API's consent error,
// leaving the original response body intact for each caller.
export function watchConsentRequired(onRequired: () => void): () => void {
  const originalFetch = window.fetch;
  let active = true;
  const observedFetch: typeof fetch = async (input, init) => {
    const response = await originalFetch.call(window, input, init);
    const url = new URL(input instanceof Request ? input.url : String(input), window.location.href);
    if (active && response.status === 403 && url.origin === window.location.origin && url.pathname.startsWith('/api/')) {
      const body = await response.clone().json().catch(() => ({}));
      if (active && body.detail === 'consent_required') onRequired();
    }
    return response;
  };
  window.fetch = observedFetch;
  return () => {
    active = false;
    if (window.fetch === observedFetch) window.fetch = originalFetch;
  };
}

export function useLegalDocument(language: string) {
  const lang = legalLanguage(language);
  const [revision, setRevision] = useState(0);
  const [result, setResult] = useState<{
    lang: LegalLanguage; revision: number; legal: LegalResponse | null; failed: boolean;
  } | null>(null);

  useEffect(() => {
    let active = true;
    fetchLegal('core', lang).then(
      legal => { if (active) setResult({ lang, revision, legal, failed: false }); },
      () => { if (active) setResult({ lang, revision, legal: null, failed: true }); },
    );
    return () => { active = false; };
  }, [lang, revision]);

  // Never expose the previous language's hash while a new request is in flight.
  const current = result?.lang === lang && result.revision === revision ? result : null;
  return {
    legal: current?.legal ?? null,
    loading: current === null,
    failed: current?.failed ?? false,
    reload: () => setRevision(value => value + 1),
  };
}
