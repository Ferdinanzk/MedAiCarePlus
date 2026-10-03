import { expect, test, type Locator, type Page } from '@playwright/test';

const robotNotice = {
  kind: 'robot', terms_version: '2026-10', language: 'en', sha256: 'r'.repeat(64), complete: true,
  document: {
    kind: 'robot', version: '2026-10', language: 'en', title: 'Reachy Robot Companion Notice', what_changed: [],
    sections: [{ id: 'robot-service', heading: '1. What Reachy does', blocks: [{ type: 'p', text: 'Reachy **cannot identify** pills.' }] }],
  },
};

const memoryNotice = {
  kind: 'memory', terms_version: '2026-10', language: 'en', sha256: 'm'.repeat(64), complete: true,
  document: {
    kind: 'memory', version: '2026-10', language: 'en', title: 'Reachy Memory Notice', what_changed: [],
    sections: [{ id: 'memory-sharing', heading: '3. Who can see or hear them', blocks: [{ type: 'p', text: 'Family contacts never see your memory notes.' }] }],
  },
};

const CHECKIN_SCOPES = ['robot_microphone', 'cloud_voice', 'conversation_analysis', 'safety_alerts'];

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

interface MockOptions {
  paired?: boolean;
  robotConsent?: boolean;
  busy?: boolean;
  /** Today's doses (default: one Aspirin dose without due_from, which is always due). */
  today?: Record<string, unknown>[];
  /** A 409 body for POST /api/reachy/tasks (the server's dose_not_due_yet). */
  taskRefusal?: Record<string, unknown>;
  /** The four check-in scopes, and memory consent (conversation_memory). */
  checkins?: boolean;
  memory?: boolean;
  /** What GET /api/memory lists. */
  memoryFacts?: Record<string, unknown>[];
}

async function mockApi(page: Page, options: MockOptions = {}) {
  const state = {
    paired: options.paired ?? false,
    scopes: {
      robot_camera: options.robotConsent ?? false,
      ...Object.fromEntries(CHECKIN_SCOPES.map(scope => [scope, options.checkins ?? false])),
      conversation_memory: options.memory ?? false,
    } as Record<string, boolean>,
    autoRecord: false,
    consentPosts: [] as Record<string, unknown>[],
    settingsPatches: [] as Record<string, unknown>[],
    taskPosts: [] as Record<string, unknown>[],
    memoryDeletes: [] as string[],
  };
  const robotStatus = () => state.paired
    ? { paired: true, device_id: 'd-1', label: 'Reachy Mini', auto_record: state.autoRecord, last_seen_at: new Date().toISOString(),
        online: true, robot_reachable: true, landmark_fps: 15.2, last_task: null, alert_contacts: 1 }
    : { paired: false };
  const consentStatus = () => ({
    terms_version: '2026-10', core_current: true, robot_current: false,
    scopes: Object.fromEntries(Object.entries(state.scopes).map(([scope, granted]) => [scope, {
      granted, terms_version: granted ? '2026-10' : null, kind: scope === 'conversation_memory' ? 'memory' : 'robot',
    }])),
  });

  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === '/api/consent/status') return route.fulfill({ json: consentStatus() });
    if (path === '/api/consent') {
      const body = request.postDataJSON();
      state.consentPosts.push(body);
      Object.assign(state.scopes, body.scopes);
      return route.fulfill({ json: consentStatus() });
    }
    if (path === '/api/legal/current') {
      const language = url.searchParams.get('lang') === 'zh-TW' ? 'zh-TW' : 'en';
      const notice = url.searchParams.get('kind') === 'memory' ? memoryNotice : robotNotice;
      return route.fulfill({ json: { ...notice, language, document: { ...notice.document, language } } });
    }
    if (path === '/api/memory' && request.method() === 'GET') {
      return route.fulfill({ json: { enabled: state.scopes.conversation_memory, items: options.memoryFacts ?? [] } });
    }
    if (path.startsWith('/api/memory') && request.method() === 'DELETE') {
      state.memoryDeletes.push(path + url.search);
      return route.fulfill({ json: { deleted: 1 } });
    }
    // The conversations page reads the list in its real (paged) shape.
    if (path === '/api/conversations') return route.fulfill({ json: { items: [], total: 0, has_more: false } });
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
      if (options.taskRefusal) return route.fulfill({ status: 409, json: options.taskRefusal });
      return route.fulfill({ json: { task_id: 't-1', status: 'queued', slot_time: dose.scheduled_time, intk_ids: [41] } });
    }
    if (path === '/api/intake/monitor/start' && options.busy) {
      return route.fulfill({ status: 409, json: { detail: 'busy_other_client' } });
    }
    if (path === '/api/medications/today') return route.fulfill({ json: options.today ?? [dose] });
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

// ── The Reachy card's test alert uses a real dose: only one that is due now (3 Oct 2026) ──

const inMinutes = (minutes: number) => new Date(Date.now() + minutes * 60_000).toISOString();
const todayDose = (id: number, name: string, minutes: number, dueFromMinutes: number) => ({
  id, med_id: id, name, dosage: null, status: 'pending', pills_remaining: 20,
  scheduled_time: inMinutes(minutes), due_from: inMinutes(dueFromMinutes),
});

async function sendTestAlert(page: Page) {
  await page.goto('/settings');
  const button = page.getByRole('button', { name: /Send test alert|發送測試提醒/ });
  await button.evaluate(element => element.scrollIntoView({ block: 'center' }));
  await button.click();
}

test('test alert never uses a dose that is not due and names the next one', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, {
    paired: true, robotConsent: true, today: [todayDose(5, 'Allegra', 8 * 60, 6 * 60)],
  });
  await sendTestAlert(page);
  await expect(page.getByText(/No dose is due right now\. The next one is Allegra|現在沒有到時間的藥可用來測試/)).toBeVisible();
  expect(api.taskPosts).toEqual([]);
});

test('test alert uses the due dose nearest to its time and says it is the real dose', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, {
    paired: true, robotConsent: true,
    today: [todayDose(40, 'Metformin', -3 * 60, -5 * 60), todayDose(41, 'Aspirin', 10, -110),
            todayDose(42, 'Zinc', 8 * 60, 6 * 60)],
  });
  await sendTestAlert(page);
  await expect.poll(() => api.taskPosts).toEqual([{ intk_id: 41 }]);
  await expect(page.getByText(/This is that real dose|這是真正的一次服藥/)).toBeVisible();
});

test('a server refusal (dose_not_due_yet) is shown with the time, not as an error', async ({ page }) => {
  await seedSession(page);
  await mockApi(page, {
    paired: true, robotConsent: true,
    taskRefusal: { detail: 'dose_not_due_yet', intk_id: 41, scheduled_time: inMinutes(8 * 60), due_from: inMinutes(6 * 60) },
  });
  await sendTestAlert(page);
  await expect(page.getByText(/a test alert can use it from|起才能用它發送測試提醒/)).toBeVisible();
  await expect(page.locator('section[aria-labelledby="reachy-title"]').getByRole('alert')).toHaveCount(0);
});

test("a dose refused as too soon shows the server's own sentence, not an error", async ({ page }) => {
  await seedSession(page);
  const reply = 'You already took Aspirin at 08:05. Please don\'t take another one yet.';
  await mockApi(page, {
    paired: true, robotConsent: true,
    taskRefusal: { detail: 'dose_too_soon', intk_id: 41, med_name: 'Aspirin', scheduled_time: inMinutes(0),
                   last_taken_at: inMinutes(-30), next_allowed_at: inMinutes(30), reply, speech_text: reply },
  });
  await sendTestAlert(page);
  await expect(page.getByText(reply)).toBeVisible();
  await expect(page.locator('section[aria-labelledby="reachy-title"]').getByRole('alert')).toHaveCount(0);
});

test('a dose that becomes due while the page is open is offered without a reload', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-10-03T05:59:00+08:00') });
  await seedSession(page);
  await mockApi(page, {
    paired: true, robotConsent: true,
    today: [{ id: 5, med_id: 5, name: 'Allegra', dosage: null, status: 'pending', pills_remaining: 20,
              scheduled_time: '2026-10-03T08:00:00+08:00', due_from: '2026-10-03T06:00:00+08:00' }],
  });
  await page.goto('/intake');
  await expect(page.getByText(/Can be recorded from|起可記錄/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Start camera' })).toHaveCount(0);
  await page.clock.fastForward('01:30');                                  // 06:00:30, no reload
  await expect(page.getByRole('button', { name: 'Start camera' })).toBeVisible();
  await expect(page.getByText(/Can be recorded from|起可記錄/)).toHaveCount(0);
});

// ── Check-in memory (Oct 2026): its own notice and consent kind; switching it off offers to delete the notes ──

const memorySwitch = (page: Page) => page.getByRole('checkbox', { name: /Remember our chats|記住聊天內容/ });
const checkinSwitch = (page: Page) => page.getByRole('checkbox', { name: /Daily check-ins|每日關心/ });
const DELETE_ALL = /Also delete everything Reachy remembers|也要刪除 Reachy 記得的所有事/;

async function clickCentred(locator: Locator) {
  // Playwright scrolls minimally; centre it so the fixed mobile nav doesn't cover it.
  await locator.evaluate(element => element.scrollIntoView({ block: 'center' }));
  await locator.click();
}

const kindsAndScopes = (posts: Record<string, unknown>[]) => posts.map(post => [post.kind, post.scopes]);

test('memory shows its own notice first and is granted as its own consent kind', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true });
  await page.goto('/settings');
  await expect(memorySwitch(page)).toHaveCount(0);             // memory comes with check-ins
  await clickCentred(checkinSwitch(page));
  const memory = memorySwitch(page);
  await expect(memory).not.toBeChecked();
  await clickCentred(memory);
  await expect(page.getByText('Family contacts never see your memory notes.')).toBeVisible();
  // Only the check-in scopes so far: nothing for memory before its notice is accepted.
  expect(kindsAndScopes(api.consentPosts)).toEqual([['robot', Object.fromEntries(CHECKIN_SCOPES.map(scope => [scope, true]))]]);
  await clickCentred(page.getByRole('button', { name: /Accept and turn on memory|同意並開啟記憶功能/ }));
  await expect.poll(() => api.consentPosts.length).toBe(2);
  expect(api.consentPosts[1]).toMatchObject({
    kind: 'memory', terms_version: '2026-10', document_sha256: 'm'.repeat(64),
    scopes: { conversation_memory: true }, source: 'settings',
  });
  await expect(memory).toBeChecked();
});

test('switching memory off without deleting keeps the notes', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true, checkins: true, memory: true });
  const dialogs: string[] = [];
  page.on('dialog', dialog => { dialogs.push(dialog.message()); void dialog.dismiss(); });
  await page.goto('/settings');
  await expect(memorySwitch(page)).toBeChecked();
  await clickCentred(memorySwitch(page));
  await expect.poll(() => kindsAndScopes(api.consentPosts)).toEqual([['memory', { conversation_memory: false }]]);
  await expect(memorySwitch(page)).not.toBeChecked();
  expect(dialogs).toEqual([expect.stringMatching(DELETE_ALL)]);
  expect(api.memoryDeletes).toEqual([]);
});

test('turning check-ins off withdraws memory too and deletes the notes when confirmed', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true, checkins: true, memory: true });
  const dialogs: string[] = [];
  page.on('dialog', dialog => { dialogs.push(dialog.message()); void dialog.accept(); });
  await page.goto('/settings');
  await expect(memorySwitch(page)).toBeChecked();
  await clickCentred(checkinSwitch(page));
  await expect.poll(() => api.memoryDeletes).toEqual(['/api/memory?confirm=all']);
  expect(kindsAndScopes(api.consentPosts)).toEqual([
    ['robot', { cloud_voice: false, conversation_analysis: false, safety_alerts: false }],
    ['memory', { conversation_memory: false }],
  ]);
  expect(dialogs).toEqual([expect.stringMatching(DELETE_ALL)]);
  await expect(checkinSwitch(page)).not.toBeChecked();
  await expect(memorySwitch(page)).toHaveCount(0);
});

test('turning the Reachy camera off withdraws memory too', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true, checkins: true, memory: true });
  const dialogs: string[] = [];
  page.on('dialog', dialog => { dialogs.push(dialog.message()); void dialog.accept(); });
  await page.goto('/settings');
  await expect(memorySwitch(page)).toBeChecked();
  await clickCentred(page.getByRole('button', { name: /Turn off the Reachy camera|關閉 Reachy 鏡頭/ }));
  await expect.poll(() => api.memoryDeletes).toEqual(['/api/memory?confirm=all']);
  expect(kindsAndScopes(api.consentPosts)).toEqual([['robot', { robot_camera: false }], ['memory', { conversation_memory: false }]]);
  expect(dialogs).toEqual([expect.stringMatching(/Turn off the Reachy camera\?|確定要關閉 Reachy 鏡頭嗎/),
                           expect.stringMatching(DELETE_ALL)]);
});

test('the conversations page lists what Reachy remembers and deletes one note', async ({ page }) => {
  await seedSession(page);
  const learned = new Date().toISOString();
  const api = await mockApi(page, {
    paired: true, robotConsent: true, checkins: true, memory: true,
    memoryFacts: [
      { kind: 'name', subject: 'preferred_name', text: 'Grandma Lin', event_date: null, source: 'patient',
        learned_at: learned, conversation_ids: [] },
      { kind: 'like', subject: 'garden', text: 'Likes growing flowers on the balcony', event_date: null,
        source: 'chat', learned_at: learned, conversation_ids: ['c-1', 'c-2'] },
      { kind: 'event', subject: 'amy_visit', text: 'Granddaughter Amy visits', event_date: '2026-10-11',
        source: 'chat', learned_at: learned, conversation_ids: ['c-1'] },
    ],
  });
  const dialogs: string[] = [];
  page.on('dialog', dialog => { dialogs.push(dialog.message()); void dialog.accept(); });
  await page.goto('/conversations');
  const panel = page.locator('section[aria-labelledby="memory-title"]');
  await expect(panel.getByText('Likes growing flowers on the balcony')).toBeVisible();
  await expect(panel.getByText('Grandma Lin')).toBeVisible();
  await expect(panel.getByText(/You added this|您自己新增/)).toBeVisible();
  await expect(panel.getByText(/From 2 chats|來自 2 次聊天/)).toBeVisible();      // the plural key
  await expect(panel.getByText(/2026-10-11\s*(From 1 chat|來自 1 次聊天)$/)).toBeVisible();   // the singular one
  const row = panel.getByRole('listitem').filter({ hasText: 'balcony' });
  await clickCentred(row.getByRole('button', { name: /^(Delete|刪除)$/ }));
  await expect.poll(() => api.memoryDeletes).toEqual(['/api/memory/fact?kind=like&subject=garden']);
  expect(dialogs).toEqual([expect.stringMatching(/Delete this note\?|刪除這則筆記/)]);
});

test('when every dose today is closed the test alert says so', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, {
    paired: true, robotConsent: true, today: [{ ...todayDose(5, 'Allegra', -60, -180), status: 'taken' }],
  });
  await sendTestAlert(page);
  await expect(page.getByText(/Every dose today is already taken|今天的藥都已服用/)).toBeVisible();
  expect(api.taskPosts).toEqual([]);
});
