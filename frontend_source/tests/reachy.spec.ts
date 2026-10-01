import { expect, test, type Page } from '@playwright/test';

const robotNotice = {
  kind: 'robot', terms_version: '2026-10', language: 'en', sha256: 'r'.repeat(64), complete: true,
  document: {
    kind: 'robot', version: '2026-10', language: 'en', title: 'Reachy Robot Companion Notice', what_changed: [],
    sections: [{ id: 'robot-service', heading: '1. What Reachy does', blocks: [{ type: 'p', text: 'Reachy **cannot identify** pills.' }] }],
  },
};

const dose = {
  id: 41, med_id: 5, name: 'Aspirin', dosage: '100mg', status: 'pending', pills_remaining: 20,
  scheduled_time: new Date().toISOString(),
};

async function seedSession(page: Page) {
  await page.addInitScript(() => {
    for (const key of ['face_auth_session', 'onboarding_complete', 'onboarding_face_done']) localStorage.setItem(key, 'true');
    localStorage.setItem('face_auth_user', JSON.stringify({ name: 'Robot Test', u_id: 7, loginAt: new Date().toISOString() }));
    localStorage.setItem('face_auth_token', 'test-face-token');
  });
}

async function mockApi(page: Page, options: { paired?: boolean; robotConsent?: boolean; busy?: boolean } = {}) {
  const state = {
    paired: options.paired ?? false,
    robotConsent: options.robotConsent ?? false,
    autoRecord: false,
    consentPosts: [] as Record<string, unknown>[],
    settingsPatches: [] as Record<string, unknown>[],
    taskPosts: [] as Record<string, unknown>[],
  };
  const robotStatus = () => state.paired
    ? { paired: true, device_id: 'd-1', label: 'Reachy Mini', auto_record: state.autoRecord, last_seen_at: new Date().toISOString(),
        online: true, robot_reachable: true, landmark_fps: 15.2, last_task: null }
    : { paired: false };
  const consentStatus = () => ({
    terms_version: '2026-10', core_current: true, robot_current: false,
    scopes: { robot_camera: { granted: state.robotConsent, terms_version: state.robotConsent ? '2026-10' : null, kind: 'robot' } },
  });

  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === '/api/consent/status') return route.fulfill({ json: consentStatus() });
    if (path === '/api/consent') {
      const body = request.postDataJSON();
      state.consentPosts.push(body);
      state.robotConsent = body.scopes.robot_camera === true;
      return route.fulfill({ json: consentStatus() });
    }
    if (path === '/api/legal/current') {
      const language = url.searchParams.get('lang') === 'zh-TW' ? 'zh-TW' : 'en';
      return route.fulfill({ json: { ...robotNotice, language, document: { ...robotNotice.document, language } } });
    }
    if (path === '/api/reachy/status') return route.fulfill({ json: robotStatus() });
    if (path === '/api/reachy/pairing' && request.method() === 'POST') {
      state.paired = true;
      return route.fulfill({ json: { device_id: 'd-1', token: 'rdv1.secret-token' } });
    }
    if (path === '/api/reachy/pairing' && request.method() === 'DELETE') {
      state.paired = false;
      return route.fulfill({ json: { paired: false } });
    }
    if (path === '/api/reachy/settings') {
      const body = request.postDataJSON();
      state.settingsPatches.push(body);
      state.autoRecord = body.auto_record;
      return route.fulfill({ json: robotStatus() });
    }
    if (path === '/api/reachy/tasks') {
      state.taskPosts.push(request.postDataJSON());
      return route.fulfill({ json: { task_id: 't-1', status: 'queued', slot_time: dose.scheduled_time, intk_ids: [41] } });
    }
    if (path === '/api/intake/monitor/start' && options.busy) {
      return route.fulfill({ status: 409, json: { detail: 'busy_other_client' } });
    }
    if (path === '/api/medications/today') return route.fulfill({ json: [dose] });
    if (path === '/api/notify/settings') return route.fulfill({ json: {} });
    return route.fulfill({ json: [] });
  });
  return state;
}

test('robot notice consent, pairing shows the key once, and auto-record defaults off', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page);
  await page.goto('/settings');
  await page.getByRole('button', { name: /Read the Reachy notice|閱讀 Reachy 聲明/ }).click();
  await expect(page.getByText('cannot identify')).toBeVisible();
  await page.getByRole('button', { name: /Allow the Reachy camera|允許 Reachy 於服藥時間開啟鏡頭/ }).click();
  await expect.poll(() => api.consentPosts.length).toBe(1);
  expect(api.consentPosts[0]).toMatchObject({
    kind: 'robot', terms_version: '2026-10', document_sha256: 'r'.repeat(64),
    scopes: { robot_camera: true }, source: 'pairing',
  });
  await page.getByRole('button', { name: /Pair a Reachy robot|配對 Reachy 機器人/ }).click();
  await expect(page.getByText('rdv1.secret-token')).toBeVisible();
  const autoRecord = page.getByRole('checkbox', { name: /record doses by itself|讓 Reachy 自行記錄服藥/ });
  await expect(autoRecord).not.toBeChecked();
  // Playwright scrolls minimally; centre it so the fixed mobile nav doesn't cover it.
  await autoRecord.evaluate(element => element.scrollIntoView({ block: 'center' }));
  // The checkbox mirrors the server's value, so it flips only after the PATCH succeeds.
  await autoRecord.click();
  await expect.poll(() => api.settingsPatches).toEqual([{ auto_record: true }]);
  await expect(autoRecord).toBeChecked();
  await page.reload();
  await expect(page.getByText('rdv1.secret-token')).toHaveCount(0);
});

test('intake offers Reachy only when paired and queues the selected dose', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true });
  await page.goto('/intake');
  await page.getByRole('button', { name: /Use Reachy|使用 Reachy/ }).click();
  await expect.poll(() => api.taskPosts).toEqual([{ intk_id: 41 }]);
  await expect(page.getByText(/Reachy will come and help you take Aspirin|Reachy 會來協助您服用 Aspirin/)).toBeVisible();
});

test('intake hides Reachy when not paired', async ({ page }) => {
  await seedSession(page);
  await mockApi(page);
  await page.goto('/intake');
  await expect(page.getByText('Aspirin')).toBeVisible();
  await expect(page.getByRole('button', { name: /Use Reachy|使用 Reachy/ })).toHaveCount(0);
});
