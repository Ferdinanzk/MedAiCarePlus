import { getFaceToken } from './face-auth';

interface Landmark {
  x: number;
  y: number;
  z: number;
}

// When served by FastAPI (same origin), use relative paths.
// When on Vercel, set VITE_AI_API_URL to the backend URL.
const AI_BASE_URL = import.meta.env.VITE_AI_API_URL || '';

function getAuthHeaders(): Record<string, string> {
  const token = getFaceToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export function aiFetch(path: string, options?: RequestInit): Promise<Response> {
  const headers = getAuthHeaders();
  return fetch(`${AI_BASE_URL}${path}`, {
    ...options,
    headers: {
      ...headers,
      ...(options?.headers || {}),
    },
  });
}

export const aiApi = {
  async faceLogin(imageFile: File): Promise<{
    identified: boolean;
    name?: string;
    token?: string;
    u_id?: number;
    distance?: number;
    face_count?: number;
    error?: string;
  }> {
    const form = new FormData();
    form.append('file', imageFile);
    const resp = await fetch('/api/face/login', { method: 'POST', body: form });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      return { identified: false, error: err.detail || `HTTP ${resp.status}` };
    }
    return resp.json();
  },

  async identifyFace(imageFile: File): Promise<{
    identified: boolean;
    name?: string;
    distance?: number;
    face_count?: number;
    error?: string;
  }> {
    const form = new FormData();
    form.append('file', imageFile);
    const resp = await aiFetch('/api/face/identify', {
      method: 'POST',
      body: form,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      return { identified: false, error: err.detail || `HTTP ${resp.status}` };
    }
    return resp.json();
  },

  async enrollFace(faceLabel: string, photos: File[]): Promise<{
    saved?: string[];
    face_label?: string;
    error?: string;
  }> {
    const form = new FormData();
    form.append('face_label', faceLabel);
    photos.forEach((p) => form.append('photos', p));
    const resp = await aiFetch('/api/face/enroll', {
      method: 'POST',
      body: form,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      return { error: err.detail || `HTTP ${resp.status}` };
    }
    return resp.json();
  },

  async parsePrescription(imageFile: File): Promise<{
    med_name?: string;
    quantity?: string;
    amount_each_intake?: string;
    total_intake?: string;
    schedule_time?: Record<string, boolean>;
    warning?: string;
    intake_time_label?: string;
    error?: string;
  }> {
    const form = new FormData();
    form.append('file', imageFile);
    const resp = await aiFetch('/api/ocr/parse', {
      method: 'POST',
      body: form,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      return { error: err.detail || `HTTP ${resp.status}` };
    }
    return resp.json();
  },

  async analyzeEmotion(imageFile: File): Promise<{
    detected: boolean;
    emotion_type?: string;
    emotion_score?: number;
    probabilities?: Record<string, number>;
    error?: string;
  }> {
    const form = new FormData();
    form.append('file', imageFile);
    const resp = await aiFetch('/api/emotion/analyze', {
      method: 'POST',
      body: form,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      return { detected: false, error: err.detail || `HTTP ${resp.status}` };
    }
    return resp.json();
  },

  async analyzeEmotionBatch(frames: Blob[], context = ''): Promise<{
    detected: boolean;
    emotion_type?: string;
    emotion_score?: number;
    frame_count: number;
    aggregated?: Record<string, { count: number; avg: number; max: number }>;
    error?: string;
  }> {
    const form = new FormData();
    form.append('context', context);
    frames.forEach((blob, idx) => form.append('frames', blob, `frame_${idx}.jpg`));
    const resp = await aiFetch('/api/emotion/analyze-batch', {
      method: 'POST',
      body: form,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      return { detected: false, frame_count: frames.length, error: err.detail || `HTTP ${resp.status}` };
    }
    return resp.json();
  },

  async detectIntake(payload: {
    session_id: string;
    frame_seq: number;
    width: number;
    height: number;
    face_landmarks: Landmark[];
    hand_landmarks: Landmark[][];
    timestamp: number;
  }, signal?: AbortSignal): Promise<{
    confidence?: number;
    mouth_open?: boolean;
    hand_near_mouth?: boolean;
    status?: string;
    stage?: string;
    missing_observation?: string | null;
    accepted?: boolean;
    frame_seq?: number;
    candidate_id?: number;
    waiting_reason?: string;
    transition_reason?: string;
    approach_velocity?: number;
    withdrawal_velocity?: number;
    occlusion_duration?: number;
    contact_distance?: number | null;
    entry_distance?: number;
    exit_distance?: number;
    event_detected?: boolean;
    decision?: 'confirmed' | 'uncertain' | 'none';
    decision_reason?: string;
    frame_confidence?: number;
    event_confidence?: number;
    peak_confidence?: number;
    debug?: Record<string, unknown>;
    error?: string;
  }> {
    const resp = await aiFetch('/api/intake/detect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      return { error: err.detail || `HTTP ${resp.status}` };
    }
    return resp.json();
  },

  async recordIntake(payload: {
    intk_id: number;
    detection_confidence?: number;
    detection_method?: string;
  }): Promise<{
    success?: boolean;
    error?: string;
  }> {
    const resp = await aiFetch('/api/intake/record', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      return { error: err.detail || `HTTP ${resp.status}` };
    }
    return resp.json();
  },

  async endIntakeSession(sessionId: string): Promise<{
    success?: boolean;
    error?: string;
  }> {
    const resp = await aiFetch('/api/intake/end', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      return { error: err.detail || `HTTP ${resp.status}` };
    }
    return resp.json();
  },
};
