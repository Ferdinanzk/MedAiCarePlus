import { call } from './reachy-api';

/** What any signed-in user may read: whether the admin paused LINE medication reminders (GET /api/line/reminders). */
export interface LineReminders { paused: boolean; since: string | null }

/** The admin's view (GET /api/admin/line, 403 admin_only for anyone else). `quota.limit` null with `error` null means
 *  no monthly limit; `error` set means the usage could not be read (the switch still works). */
export interface LineAdmin {
  paused: boolean; since: string | null; changed_by: string | null;
  quota: { limit: number | null; used: number | null; error: string | null };
}

/** A body that is not the admin view (a proxy's page, an unmocked route) is no admin view: the card stays hidden. */
export const isLineAdmin = (value: unknown): value is LineAdmin => {
  const v = value as LineAdmin | null;
  return !!v && typeof v.paused === 'boolean' && !!v.quota && typeof v.quota === 'object';
};

export const getLineReminders = () => call<LineReminders>('/api/line/reminders', { cache: 'no-store' });

export const getLineAdmin = () => call<LineAdmin>('/api/admin/line', { cache: 'no-store' });

/** No client timeout on purpose: with LINE down the server may take about 20 s, and a PUT given up on here would show a
 *  failure for a switch that succeeded. The caller keeps its button disabled while this runs. */
export const setLineReminders = (paused: boolean) =>
  call<LineAdmin>('/api/admin/line/reminders', { method: 'PUT', body: JSON.stringify({ paused }) });
