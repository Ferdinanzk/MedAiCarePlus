import { aiFetch } from './ai-api';

// The patient's "overdose protection" switch (notification_settings.overdose_protection, on by default). Pages that
// grey out doses ask for it: with it off the server starts any open dose, so nothing should look blocked.
let protection: { value: boolean; at: number } | null = null;
const PROTECTION_FRESH_MS = 30_000;

export const overdoseProtectionCached = (): boolean | null => protection?.value ?? null;

export function rememberOverdoseProtection(value: boolean): void {
  protection = { value, at: Date.now() };
}

/** Never rejects: when the settings cannot be read, protection counts as on, like the server's default. */
export async function fetchOverdoseProtection(): Promise<boolean> {
  if (protection && Date.now() - protection.at < PROTECTION_FRESH_MS) return protection.value;
  try {
    const resp = await aiFetch('/api/notify/settings', { cache: 'no-store' });
    if (resp.ok) {
      const body = await resp.json() as { overdose_protection?: boolean } | null;
      const value = body?.overdose_protection !== false;
      rememberOverdoseProtection(value);
      return value;
    }
  } catch {
    // offline or not signed in: fall through
  }
  return protection?.value ?? true;
}

export const notifyApi = {
  async generateCode(contactId: number): Promise<{ code?: string; error?: string }> {
    const resp = await aiFetch('/api/notify/generate-code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ contact_id: contactId }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      return { error: err.detail || `HTTP ${resp.status}` };
    }
    return resp.json();
  },

  async checkStatus(): Promise<{ configured: boolean }> {
    const resp = await aiFetch('/api/notify/status');
    if (!resp.ok) return { configured: false };
    return resp.json();
  },

  async sendMissedDose(
    lineId: string,
    patientName: string,
    medicationName: string,
    scheduledTime: string
  ): Promise<{ sent: boolean; error?: string }> {
    const resp = await aiFetch('/api/notify/missed-dose', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        line_id: lineId,
        patient_name: patientName,
        medication_name: medicationName,
        scheduled_time: scheduledTime,
      }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      return { sent: false, error: err.detail || `HTTP ${resp.status}` };
    }
    return resp.json();
  },

  async sendEmotionAlert(
    lineId: string,
    patientName: string,
    emotion: string,
    score: number
  ): Promise<{ sent: boolean; error?: string }> {
    const resp = await aiFetch('/api/notify/emotion-alert', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        line_id: lineId,
        patient_name: patientName,
        emotion,
        score,
      }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      return { sent: false, error: err.detail || `HTTP ${resp.status}` };
    }
    return resp.json();
  },
};
