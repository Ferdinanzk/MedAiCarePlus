import { expect, test, type Page } from '@playwright/test';

type Admin = { paused: boolean; since: string | null; changed_by: string | null;
  quota: { limit: number | null; used: number | null; error: string | null } };

const SINCE = '2026-11-02T10:30:00+00:00';
const on = (quota?: Admin['quota']): Admin => ({ paused: false, since: null, changed_by: null,
  quota: quota ?? { limit: 200, used: 150, error: null } });
const paused = (quota?: Admin['quota']): Admin => ({ ...on(quota), paused: true, since: SINCE, changed_by: 'Admin Amy' });

async function seedSession(page: Page) {
  await page.addInitScript(() => {
    for (const key of ['face_auth_session', 'onboarding_complete', 'onboarding_face_done']) localStorage.setItem(key, 'true');
    localStorage.setItem('face_auth_user', JSON.stringify({ name: 'Line Test', u_id: 7, loginAt: new Date().toISOString() }));
    localStorage.setItem('face_auth_token', 'test-face-token');
  });
}

interface Options { admin?: Admin | 'forbidden'; putStatus?: number; holdPut?: boolean }

async function mockApi(page: Page, options: Options = {}) {
  const state = { admin: options.admin ?? on(), puts: [] as any[], releasePut: null as null | (() => void) };
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    if (path === '/api/consent/status') {
      return route.fulfill({ json: { terms_version: '2026-10', core_current: true, robot_current: false,
        scopes: { core: { granted: true, terms_version: '2026-10', kind: 'core' } } } });
    }
    if (path === '/api/reachy/status') return route.fulfill({ json: { paired: false } });
    if (path === '/api/notify/settings') return route.fulfill({ json: {} });
    if (path === '/api/conversations') return route.fulfill({ json: { items: [], total: 0, has_more: false } });
    if (path === '/api/line/reminders') {
      const a = state.admin === 'forbidden' ? on() : state.admin;
      return route.fulfill({ json: { paused: a.paused, since: a.since } });
    }
    if (path === '/api/admin/line' && method === 'GET') {
      return state.admin === 'forbidden'
        ? route.fulfill({ status: 403, json: { detail: 'admin_only' } }) : route.fulfill({ json: state.admin });
    }
    if (path === '/api/admin/line/reminders' && method === 'PUT') {
      const body = request.postDataJSON();
      state.puts.push(body);
      if (options.holdPut) await new Promise<void>(resolve => { state.releasePut = resolve; });
      if (options.putStatus) return route.fulfill({ status: options.putStatus, json: { detail: 'boom' } });
      const current = state.admin as Admin;
      state.admin = body.paused ? { ...current, paused: true, since: SINCE, changed_by: 'Admin Amy' }
        : { ...current, paused: false, since: null, changed_by: null };
      return route.fulfill({ json: state.admin });
    }
    return route.fulfill({ json: [] });
  });
  return state;
}

const card = (page: Page) => page.locator('section[aria-labelledby="line-admin-title"]');
const banner = (page: Page) => page.getByRole('status').filter({ hasText: 'paused by the admin' });

// The app starts in Traditional Chinese, so switch to English before checking words.
async function english(page: Page) {
  // The click can report "detached" because the switch re-renders the header; what matters is that it now reads EN.
  await page.getByRole('button', { name: '中文' }).first().click({ timeout: 3000 }).catch(() => undefined);
  await expect(page.getByRole('button', { name: 'EN' }).first()).toBeVisible();
}

test('banner shows while paused and hides when on', async ({ page }) => {
  await seedSession(page);
  await mockApi(page, { admin: paused() });
  await page.goto('/dashboard');
  await english(page);
  await expect(banner(page)).toBeVisible();
  await expect(banner(page)).toContainText('LINE medication reminders paused by the admin since');
  await expect(banner(page)).toContainText('Reachy still reminds on the robot, and other LINE messages still go out.');

  const other = await page.context().newPage();
  await seedSession(other);
  const state = await mockApi(other, { admin: on() });
  await other.goto('/dashboard');
  await english(other);
  await expect(other.getByText('Reachy still reminds on the robot')).toHaveCount(0);
  expect(state.puts).toEqual([]);
});

test('card hidden for a non-admin (403)', async ({ page }) => {
  await seedSession(page);
  await mockApi(page, { admin: 'forbidden' });
  await page.goto('/settings');
  await english(page);
  await expect(page.getByRole('heading', { name: /AI connections/i }).first()).toBeVisible();
  await expect(card(page)).toHaveCount(0);
  await expect(page.getByText('LINE medication reminders')).toHaveCount(0);
});

test('admin pauses: state flips after the PUT succeeds and the button re-enables', async ({ page }) => {
  await seedSession(page);
  const state = await mockApi(page, { holdPut: true });
  await page.goto('/settings');
  await english(page);
  await expect(card(page)).toContainText('Now: on');
  const button = card(page).getByRole('button', { name: 'Pause medication reminders' });
  expect((await button.boundingBox())!.height).toBeGreaterThanOrEqual(48);
  await button.click();
  // while the request runs: still the old state, button disabled
  await expect(card(page).getByRole('button')).toBeDisabled();
  await expect(card(page)).toContainText('Now: on');
  expect(state.puts).toEqual([{ paused: true }]);
  state.releasePut!();
  await expect(card(page)).toContainText('Now: paused (since');
  await expect(card(page)).toContainText('by Admin Amy');
  await expect(card(page).getByRole('button', { name: 'Resume medication reminders' })).toBeEnabled();
});

test('failed PUT keeps the old state', async ({ page }) => {
  await seedSession(page);
  await mockApi(page, { putStatus: 500 });
  await page.goto('/settings');
  await english(page);
  await card(page).getByRole('button', { name: 'Pause medication reminders' }).click();
  await expect(card(page).getByRole('alert')).toBeVisible();
  await expect(card(page)).toContainText('Now: on');
  await expect(card(page).getByRole('button', { name: 'Pause medication reminders' })).toBeEnabled();
});

test('usage line: limited, unlimited, unavailable', async ({ page }) => {
  const cases = [
    [{ limit: 200, used: 150, error: null }, 'This month: 150 of 200 messages'],
    [{ limit: null, used: 42, error: null }, 'This month: 42 messages (no limit)'],
    [{ limit: null, used: null, error: 'line_unavailable' }, 'Usage unavailable'],
  ] as const;
  for (const [quota, text] of cases) {
    const tab = await page.context().newPage();
    await seedSession(tab);
    await mockApi(tab, { admin: on(quota) });
    await tab.goto('/settings');
    await english(tab);
    await expect(card(tab)).toContainText(text);
    await expect(card(tab).getByRole('button', { name: 'Pause medication reminders' })).toBeEnabled();
    await tab.close();
  }
});

test('no horizontal scroll at 390 px', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await seedSession(page);
  await mockApi(page, { admin: paused() });
  for (const path of ['/dashboard', '/settings']) {
    await page.goto(path);
    await english(page);
    await expect(banner(page)).toBeVisible();
    if (path === '/settings') await expect(card(page)).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  }
});
