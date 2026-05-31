import { aiFetch } from './ai-api';

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
