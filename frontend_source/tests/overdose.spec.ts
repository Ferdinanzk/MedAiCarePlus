import { expect, test, type Page } from '@playwright/test';

// Overdose protection in the web app (API fully mocked): the Settings switch, the server's refusal sentence, doses
// labelled before they are tried, and a medicine's safety limits.

async function seedSession(page: Page) {
  await page.addInitScript(() => {
    for (const key of ['face_auth_session', 'onboarding_complete', 'onboarding_face_done']) localStorage.setItem(key, 'true');
    localStorage.setItem('face_auth_user', JSON.stringify({ name: 'Dose Test', u_id: 7, loginAt: new Date().toISOString() }));
    localStorage.setItem('face_auth_token', 'test-face-token');
  });
}

interface MockOptions {
  protection?: boolean;
  today?: Record<string, unknown>[];
  /** A 409 body for POST /api/intake/monitor/start. */
  startRefusal?: Record<string, unknown>;
}

async function mockApi(page: Page, options: MockOptions = {}) {
  const state = {
    protection: options.protection ?? true,
    settingsPosts: [] as Record<string, unknown>[],
    medicationPosts: [] as Record<string, unknown>[],
  };
  const settings = () => ({
    remind_before_minutes: 5, remind_after_minutes: 10, remind_after_retries: 3, notify_family_on_missed: true,
    notify_family_on_bad_mood: true, notify_family_on_taken: true, overdose_protection: state.protection,
  });
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/consent/status') {
      return route.fulfill({ json: { terms_version: '2026-10', core_current: true, robot_current: false, scopes: {} } });
    }
    if (path === '/api/reachy/status') return route.fulfill({ json: { paired: false } });
    if (path === '/api/conversations') return route.fulfill({ json: { items: [], total: 0, has_more: false } });
    if (path === '/api/notify/settings') {
      if (request.method() === 'POST') {
        const body = request.postDataJSON();
        state.settingsPosts.push(body);
        state.protection = body.overdose_protection;
        return route.fulfill({ json: { success: true } });
      }
      return route.fulfill({ json: settings() });
    }
    if (path === '/api/medications/today') return route.fulfill({ json: options.today ?? [] });
    if (path === '/api/intake/monitor/start' && options.startRefusal) {
      return route.fulfill({ status: 409, json: options.startRefusal });
    }
    if (path === '/api/medications' && request.method() === 'POST') {
      state.medicationPosts.push(request.postDataJSON());
      return route.fulfill({ json: { id: 99 } });
    }
    return route.fulfill({ json: [] });
  });
  return state;
}

const minutesFromNow = (minutes: number) => new Date(Date.now() + minutes * 60_000).toISOString();

test('overdose protection is on by default; turning it off is confirmed and family is told', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page);
  await page.goto('/settings');
  const toggle = page.getByRole('checkbox', { name: /Overdose protection|防止重複服藥/ });
  const control = page.locator('label', { has: toggle });
  await expect(toggle).toBeChecked();
  await expect(page.getByText(/too soon after the last one|離上次太近/)).toBeVisible();

  // Cancelled: still on, nothing saved.
  let asked = '';
  page.once('dialog', dialog => { asked = dialog.message(); void dialog.dismiss(); });
  await control.click();
  await expect.poll(() => asked).toMatch(/LINE/);
  await expect(toggle).toBeChecked();
  expect(api.settingsPosts).toEqual([]);

  // Confirmed: saved at once with the stored settings, and the page says it is off.
  page.once('dialog', dialog => void dialog.accept());
  await control.click();
  await expect.poll(() => api.settingsPosts.length).toBe(1);
  expect(api.settingsPosts[0]).toMatchObject({ overdose_protection: false, remind_before_minutes: 5, notify_family_on_taken: true });
  await expect(toggle).not.toBeChecked();
  await expect(page.getByText(/Protection is off|保護已關閉/)).toBeVisible();

  // Back on: no question asked.
  await control.click();
  await expect.poll(() => api.settingsPosts.length).toBe(2);
  expect(api.settingsPosts[1]).toMatchObject({ overdose_protection: true });
  await expect(toggle).toBeChecked();
});

test("a refused dose shows the server's sentence, and the dose then says when it can be taken", async ({ page }) => {
  await seedSession(page);
  const reply = '這個藥您凌晨12點05分已經吃過了，請先不要再吃。';
  await mockApi(page, {
    today: [{ id: 51, med_id: 7, name: 'Allegra', dosage: null, status: 'pending', pills_remaining: 20,
              scheduled_time: minutesFromNow(0), due_from: minutesFromNow(-120) }],
    startRefusal: { detail: 'dose_too_soon', intk_id: 51, med_name: 'Allegra', scheduled_time: minutesFromNow(0),
                    last_taken_at: minutesFromNow(-30), next_allowed_at: minutesFromNow(30), reply,
                    speech_text: '这个药您凌晨12点05分已经吃过了，请先不要再吃。' },
  });
  await page.goto('/intake');
  await page.getByRole('button', { name: 'Start camera' }).click();
  await expect(page.getByRole('alert')).toContainText(reply);
  await expect(page.getByRole('button', { name: 'Start camera' })).toHaveCount(0);
  await expect(page.getByText(/Just taken — next from|剛吃過，/)).toBeVisible();
});

const dashboardDay = () => [
  // Missed, and past halfway to the same medicine's next dose: not made up.
  { id: 61, med_id: 7, name: 'Allegra', status: 'missed', pills_remaining: 20, units_per_dose: 1,
    scheduled_time: minutesFromNow(-180), due_from: minutesFromNow(-300) },
  { id: 62, med_id: 7, name: 'Allegra', status: 'pending', pills_remaining: 20, units_per_dose: 1,
    scheduled_time: minutesFromNow(60), due_from: minutesFromNow(-60) },
  { id: 63, med_id: 8, name: 'Zinc', status: 'pending', pills_remaining: 20, units_per_dose: 1,
    scheduled_time: minutesFromNow(300), due_from: minutesFromNow(180) },
];

test('the dashboard does not offer a missed dose or one that is not due yet', async ({ page }) => {
  await seedSession(page);
  await mockApi(page, { today: dashboardDay() });
  await page.goto('/dashboard');
  await expect(page.getByText(/Missed — don't make it up|已錯過，請勿補吃/)).toBeVisible();
  await expect(page.getByRole('button', { name: /^(Take Now|立即服用)$/ })).toHaveCount(1);
  await expect(page.getByRole('button', { name: /Can be recorded from|起可記錄/ })).toBeDisabled();
  await expect(page.getByText(/Overdose protection is off|已關閉，記錄服藥時/)).toHaveCount(0);
});

test('with protection off every open dose can be started, and the dashboard says it is off', async ({ page }) => {
  await seedSession(page);
  await mockApi(page, { protection: false, today: dashboardDay() });
  await page.goto('/dashboard');
  await expect(page.getByText(/Overdose protection is off|已關閉，記錄服藥時/)).toBeVisible();
  await expect(page.getByRole('button', { name: /^(Take Now|立即服用)$/ })).toHaveCount(3);
  await expect(page.getByText(/Missed — don't make it up|已錯過，請勿補吃/)).toHaveCount(0);
});

test('a medicine saves its safety limits; blank means the default', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page);
  await page.goto('/medications');
  await page.getByRole('button', { name: /Add Medication|新增藥物/ }).last().click();
  await page.getByPlaceholder('e.g. Allegra (Fexofenadine) 60mg/tab').fill('Panadol');
  // No dose times: the default is 4 hours and no daily limit, and a pharmacist should set them.
  await expect(page.getByText(/at least 4 h apart, no daily limit|至少間隔 4 小時，每天次數不限/)).toBeVisible();
  await expect(page.getByText(/ask a pharmacist|請向藥師確認/)).toBeVisible();
  await page.getByLabel(/Minimum hours between doses|兩次服藥至少間隔幾小時/).fill('6');
  await page.getByRole('button', { name: /^(Save|儲存)$/ }).click();
  await expect.poll(() => api.medicationPosts.length).toBe(1);
  expect(api.medicationPosts[0]).toMatchObject({ name: 'Panadol', min_interval_minutes: 360, max_daily_doses: null });
});

test('limits that contradict the schedule are warned about, not blocked', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page);
  await page.goto('/medications');
  await page.getByRole('button', { name: /Add Medication|新增藥物/ }).last().click();
  await page.getByPlaceholder('e.g. Allegra (Fexofenadine) 60mg/tab').fill('Allegra');
  for (const slot of ['早上', '中午', '晚上', '睡前']) await page.getByRole('button', { name: slot, exact: true }).click();
  const warnings = page.getByRole('status');
  await expect(warnings).toHaveCount(0);
  // Allegra at 08/12/20/22: two a day refuses two scheduled doses; 3 h apart is more than 20:00 -> 22:00.
  await page.getByLabel(/Maximum doses per day|每天最多吃幾次/).fill('2');
  await expect(warnings.filter({ hasText: /4 dose times: 2 scheduled|排定的 4 個時間少：每天會有 2 次/ })).toBeVisible();
  await page.getByLabel(/Minimum hours between doses|兩次服藥至少間隔幾小時/).fill('3');
  await expect(warnings.filter({ hasText: /the 2 h between|最短的 2 小時/ })).toBeVisible();
  await page.getByRole('button', { name: /^(Save|儲存)$/ }).click();
  await expect.poll(() => api.medicationPosts.length).toBe(1);
  expect(api.medicationPosts[0]).toMatchObject({ min_interval_minutes: 180, max_daily_doses: 2 });
});
