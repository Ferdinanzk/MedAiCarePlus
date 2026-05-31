const FACE_USER_KEY = 'face_auth_user';
const FACE_TOKEN_KEY = 'face_auth_token';

export interface FaceAuthUser {
  name: string;
  loginAt: string;
  u_id?: number;
}

export function setFaceSession(user: FaceAuthUser, token?: string): void {
  localStorage.setItem(FACE_USER_KEY, JSON.stringify(user));
  if (token) localStorage.setItem(FACE_TOKEN_KEY, token);
}

export function getFaceSession(): FaceAuthUser | null {
  const raw = localStorage.getItem(FACE_USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as FaceAuthUser;
  } catch {
    localStorage.removeItem(FACE_USER_KEY);
    return null;
  }
}

export function getFaceToken(): string | null {
  return localStorage.getItem(FACE_TOKEN_KEY);
}

export function getFaceAuthHeaders(): Record<string, string> {
  const token = getFaceToken();
  if (!token) return {};
  return { Authorization: `Bearer ${token}` };
}

export function faceLogout(): void {
  localStorage.removeItem(FACE_USER_KEY);
  localStorage.removeItem(FACE_TOKEN_KEY);
}
