import { expect, test, type Page } from '@playwright/test';

// The Scan page with every medicine of a paper (API fully mocked; the camera is a canvas stream, because Chrome's
// fake capture device on Windows sometimes answers NotFoundError between tests). The OCR answer is shaped like the
// server's answer for a three-row pharmacy receipt; every name and number in it is made up.
// Set SCAN_SCREENSHOTS to a folder to save screenshots of the result.

test.use({ locale: 'zh-TW' });

async function fakeCamera(page: Page) {
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = async () => {
      const canvas = document.createElement('canvas');
      canvas.width = 640;
      canvas.height = 480;
      const ctx = canvas.getContext('2d')!;
      const draw = () => {
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(0, 0, 640, 480);
        ctx.fillStyle = '#000000';
        ctx.fillText(`Rx ${Date.now()}`, 40, 60);
      };
      draw();
      setInterval(draw, 100);
      return canvas.captureStream(10);
    };
  });
}

const SIX = (on: string[]) => Object.fromEntries(
  ['morning', 'noon', 'night', 'bedtime', 'before_meals', 'after_meals'].map((slot) => [slot, on.includes(slot)]));

const medicine = (changes: Record<string, unknown>) => ({
  med_name: 'VITACOBAL CAPSULES 0', dosage: 'N/A', quantity: '15 錠', pill_count: 15, unit: '錠',
  frequency_text: '一天3次', instructions: '一天3次', times_per_day: 3, interval_hours: null, days: 5,
  amount_each_intake: 'N/A', units_per_dose: 1, form: 'capsule', dose_form: 'solid_oral', as_needed: false,
  schedule_time: SIX(['morning', 'noon', 'night']), schedule_source: 'text', stock: 15,
  total_intake: '15錠 (3次/天 × 1錠/次 × 5天)', intake_time_label: '一天3次 [早上 | 中午 | 晚上]', warning: '◎請按時服藥!!',
  pill_description: 'N/A', clinical_uses: 'N/A', manufacturer: 'N/A', ...changes,
});

function receiptScan(dispensed: string) {
  const meds = [
    medicine({}),
    medicine({ med_name: 'GASTRO F.C. TABLETS', quantity: '15 TAB', unit: 'TAB', form: 'tablet' }),
    medicine({
      med_name: 'OTOMYX OTIC DROPS.', quantity: '1 瓶', pill_count: 1, unit: '瓶', frequency_text: '一天2次',
      instructions: '一天2次', times_per_day: 2, units_per_dose: null, form: 'ear_drops', dose_form: 'other',
      schedule_time: SIX(['morning', 'night']), stock: 10, total_intake: 'N/A (partial: 2次/天 × 5天)',
    }),
  ];
  return {
    ...meds[0], hospital: 'N/A', pharmacy: '安心藥局', prescription_no: 'N/A', use_before: 'N/A', physician: 'N/A',
    pharmacist: '林小青', patient_name: '王小華', date_dispensed: dispensed, visit_date: dispensed,
    document_type: 'pharmacy_receipt', medications: meds,
  };
}

async function seedSession(page: Page) {
  await fakeCamera(page);
  await page.addInitScript(() => {
    for (const key of ['face_auth_session', 'onboarding_complete', 'onboarding_face_done']) localStorage.setItem(key, 'true');
    localStorage.setItem('face_auth_user', JSON.stringify({ name: 'Scan Test', u_id: 7, loginAt: new Date().toISOString() }));
    localStorage.setItem('face_auth_token', 'test-face-token');
  });
}

async function mockApi(page: Page, scan: Record<string, unknown>, existing: Record<string, unknown>[] = []) {
  const state = { medicationPosts: [] as Record<string, unknown>[] };
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/consent/status') {
      return route.fulfill({ json: { terms_version: '2026-10', core_current: true, robot_current: false, scopes: {} } });
    }
    if (path === '/api/reachy/status') return route.fulfill({ json: { paired: false } });
    if (path === '/api/ocr/parse') return route.fulfill({ json: scan });
    if (path === '/api/medications' && request.method() === 'POST') {
      state.medicationPosts.push(request.postDataJSON());
      return route.fulfill({ json: { id: 90 + state.medicationPosts.length } });
    }
    if (path === '/api/medications' && request.method() === 'GET') return route.fulfill({ json: existing });
    return route.fulfill({ json: [] });
  });
  return state;
}

async function scanPaper(page: Page) {
  await page.goto('/scan');
  await page.getByRole('button', { name: /^(拍照|Capture)$/ }).first().click();
  await expect(page.getByText(/請將處方對準框內|Align the prescription/)).toBeVisible();
  await page.locator('video').evaluate((video: HTMLVideoElement) =>
    new Promise<void>((resolve) => (video.readyState >= 2 ? resolve() : video.addEventListener('loadeddata', () => resolve()))));
  await page.getByRole('button', { name: /^(拍照|Capture)$/ }).click();
  await page.getByRole('button', { name: /^(確認|Confirm)$/ }).click();
}

function isoDaysAgo(days: number) {
  const date = new Date();
  date.setDate(date.getDate() - days);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

test('every medicine on the paper is a card, and each kept one is saved', async ({ page }) => {
  await seedSession(page);
  const today = isoDaysAgo(0);
  const state = await mockApi(page, receiptScan(today));
  await scanPaper(page);

  await expect(page.getByText('找到 3 種藥物')).toBeVisible();
  const names = page.getByLabel('藥物名稱');
  await expect(names).toHaveCount(3);
  await expect(names.nth(2)).toHaveValue('OTOMYX OTIC DROPS.');
  // The ear drops are not a tablet: the robot never records them by itself.
  await expect(page.getByLabel('劑型').nth(2)).toHaveValue('other');
  await expect(page.getByText('Reachy 無法自行記錄此藥物，將請家人協助確認。')).toHaveCount(1);
  await expect(page.getByLabel('一天幾次').nth(0)).toHaveValue('3');
  await expect(page.getByText('藥局', { exact: true })).toBeVisible();
  await expect(page.getByText('◎請按時服藥!!')).toHaveCount(1);     // the paper's note, once

  if (process.env.SCAN_SCREENSHOTS) {
    await page.screenshot({ path: `${process.env.SCAN_SCREENSHOTS}/scan-cards.png`, fullPage: true });
  }

  // The user fixes a misread name, adds a bedtime dose to the second one and leaves the drops out.
  await names.nth(0).fill('VITACOBAL CAPSULES 0.5MG');
  await page.getByRole('checkbox', { name: '睡前' }).nth(1).check();
  await expect(page.getByLabel('一天幾次').nth(1)).toHaveValue('4');
  await page.getByRole('checkbox', { name: '加入這個藥物' }).nth(2).uncheck();
  await page.getByRole('button', { name: '加入 2 種藥物' }).click();

  await expect(page.getByText('已將 2 種藥物加入我的藥物。')).toBeVisible();
  expect(state.medicationPosts).toHaveLength(2);
  const [first, second] = state.medicationPosts;
  expect(first).toMatchObject({
    name: 'VITACOBAL CAPSULES 0.5MG', dose_form: 'solid_oral', units_per_dose: 1, pills_remaining: 15, total_pills: 15,
    schedule_time: { morning: true, noon: true, night: true, bedtime: false, custom_times: [] },
  });
  expect(first.use_before).toBe(isoDaysAgo(-4));       // five days from the dispensing date
  expect(second).toMatchObject({ name: 'GASTRO F.C. TABLETS', schedule_time: { bedtime: true } });
  expect((first.prescription_meta as Record<string, unknown>).pharmacy).toBe('安心藥局');

  if (process.env.SCAN_SCREENSHOTS) {
    await page.screenshot({ path: `${process.env.SCAN_SCREENSHOTS}/scan-saved.png`, fullPage: true });
  }
});

test('a course that already ended starts unticked, and is saved only with a new last day', async ({ page }) => {
  await seedSession(page);
  const state = await mockApi(page, receiptScan(isoDaysAgo(6)));
  await scanPaper(page);

  await expect(page.getByText(/這個療程已在 .* 結束/)).toHaveCount(3);
  // Saving an ended course would still remind for the rest of today: none is ticked, nothing can be added.
  const boxes = page.getByRole('checkbox', { name: '加入這個藥物' });
  for (let index = 0; index < 3; index++) await expect(boxes.nth(index)).not.toBeChecked();
  await expect(page.getByRole('button', { name: '加入 0 種藥物' })).toBeDisabled();

  // Ticked anyway, it asks for a new last day first.
  await boxes.nth(2).check();
  await expect(page.getByText('最後服藥日已經過了。請修改或清除日期後再加入。')).toBeVisible();
  await page.getByRole('button', { name: '加入 1 種藥物' }).click();
  await expect(page.getByText('有些藥物需要先修正，請看紅色的說明。')).toBeVisible();
  expect(state.medicationPosts).toHaveLength(0);

  await page.getByLabel('最後服藥日').nth(2).fill(isoDaysAgo(-3));
  await page.getByRole('button', { name: '加入 1 種藥物' }).click();
  await expect(page.getByText('已將 1 種藥物加入我的藥物。')).toBeVisible();
  expect(state.medicationPosts).toEqual([expect.objectContaining({
    name: 'OTOMYX OTIC DROPS.', dose_form: 'other', pills_remaining: 10, use_before: isoDaysAgo(-3),
    schedule_time: expect.objectContaining({ morning: true, night: true, noon: false }),
  })]);
});

test('a failed save is shown on its card in the page language and can be retried', async ({ page }) => {
  await seedSession(page);
  const state = await mockApi(page, receiptScan(isoDaysAgo(0)));
  let failOnce = true;
  await page.route('**/api/medications', async (route) => {
    if (route.request().method() === 'POST' && failOnce) {
      failOnce = false;
      return route.fulfill({ status: 400, json: { detail: 'database unavailable' } });
    }
    return route.fallback();
  });
  await scanPaper(page);
  await page.getByRole('button', { name: '加入 3 種藥物' }).click();
  await expect(page.getByText('這個藥物無法儲存，請檢查內容後再試一次。')).toBeVisible();
  await expect(page.getByText('database unavailable')).toHaveCount(0);   // the server's text stays in the console
  await expect(page.getByText('已加入我的藥物')).toHaveCount(2);
  await page.getByRole('button', { name: '加入 1 種藥物' }).click();
  await expect(page.getByText('已將 3 種藥物加入我的藥物。')).toBeVisible();
  expect(state.medicationPosts).toHaveLength(3);
});

test('a scanned course says when it ends, not that it expires, and an edit keeps what the scan read', async ({ page }) => {
  await seedSession(page);
  const end = isoDaysAgo(-3);
  const meta = { pharmacy: '安心藥局', course_end: end, days: 5 };
  await mockApi(page, receiptScan(isoDaysAgo(0)), [{
    id: 5, name: 'GASTRO F.C. TABLETS', is_active: true, use_before: end, total_pills: 15, pills_remaining: 15,
    dose_form: 'solid_oral', units_per_dose: 1,
    // asyncpg returns JSONB as text.
    schedule_time: JSON.stringify({ morning: true }), prescription_meta: JSON.stringify(meta),
  }]);
  const patches: Record<string, unknown>[] = [];
  await page.route('**/api/medications/5', async (route) => {
    if (route.request().method() !== 'PATCH') return route.fallback();
    patches.push(route.request().postDataJSON());
    return route.fulfill({ json: { id: 5 } });
  });
  await page.goto('/medications');
  await expect(page.getByText(`療程到 ${end} 結束`)).toBeVisible();
  await expect(page.getByText(/天後到期|過期/)).toHaveCount(0);
  await page.getByRole('button', { name: '編輯藥物: GASTRO F.C. TABLETS' }).first().click();
  await page.getByRole('button', { name: /^(Save|儲存)$/ }).click();
  await expect.poll(() => patches.length).toBe(1);
  expect(patches[0].prescription_meta).toEqual(meta);
});

test('a medicine already in the list starts unticked with a note', async ({ page }) => {
  await seedSession(page);
  const state = await mockApi(page, receiptScan(isoDaysAgo(0)),
    [{ id: 5, name: 'Gastro F.C. Tablets', is_active: true }, { id: 6, name: 'OTOMYX OTIC DROPS.', is_active: false }]);
  await scanPaper(page);

  const boxes = page.getByRole('checkbox', { name: '加入這個藥物' });
  await expect(boxes.nth(1)).not.toBeChecked();
  await expect(boxes.nth(2)).toBeChecked();                   // an archived medicine is no duplicate
  await expect(page.getByText(/您的藥物清單中已有同名且使用中的藥物/)).toHaveCount(1);
  await page.getByRole('button', { name: '加入 2 種藥物' }).click();
  await expect(page.getByText('已將 2 種藥物加入我的藥物。')).toBeVisible();
  expect(state.medicationPosts.map((post) => post.name)).toEqual(['VITACOBAL CAPSULES 0', 'OTOMYX OTIC DROPS.']);
});
