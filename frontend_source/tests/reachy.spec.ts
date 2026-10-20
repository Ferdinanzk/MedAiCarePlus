import { readFileSync } from 'node:fs';
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

/** The registered name ("user".name) the mocked server answers with when no call name is saved. */
const REGISTERED_NAME = 'Robot Test';
/** The character rules of a call name (memory.valid_name): 1-8 Chinese characters, or letters and single spaces up
 *  to 20. The real server also refuses risk, health and command words; these tests don't need those. */
const validName = (name: string) =>
  /^[㐀-鿿]{1,8}$/.test(name) || (name.length <= 20 && /^[A-Za-z]+(?: [A-Za-z]+)*$/.test(name));

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
  /** The robot's camera from its last heartbeat (robot app 0.7.0+); left out: an older robot, no camera field. */
  camera?: Record<string, unknown> | null;
  online?: boolean;
  /** The call name saved on the card ("user".reachy_call_name); left out: none, so the registered name is used. */
  callName?: string | null;
  /** The server's LIVE_VOICE_TEST, and this robot's live voice switch. */
  liveVoiceTest?: boolean;
  liveVoice?: boolean;
  /** The meal retry switch ("user".reachy_meal_retry, quick fix 5); left out: off, as the server's default. */
  mealRetry?: boolean;
  /** The microphone switch ("user".reachy_mic_muted, quick fix 8); left out: not muted, as the server's default. */
  micMuted?: boolean;
  /** The daemon's microphone volume from the robot's last heartbeat; left out: null (none reported). */
  micVolume?: number | null;
}

/** What POST /api/reachy/demo-dose answers when it succeeds (quick fix 6). */
const demoDoseAnswer = { task_id: 't-demo', status: 'queued', intk_id: 901, med_id: 51, med_name: 'Acetal 500mg' };

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
    camera: options.camera,
    online: options.online ?? true,
    callName: options.callName ?? null,
    liveVoiceTest: options.liveVoiceTest ?? false,
    liveVoice: options.liveVoice ?? false,
    mealRetry: options.mealRetry ?? false,
    micMuted: options.micMuted ?? false,
    micVolume: options.micVolume ?? null,
    /** How many times the card read GET /api/reachy/status. */
    statusReads: 0,
    /** While set, GET /api/reachy/status answers (with the state as it was when it came) only once it resolves. */
    statusGate: null as Promise<void> | null,
    /** While set, PATCH /api/reachy/settings answers only once it resolves; settingsError makes it fail. */
    settingsGate: null as Promise<void> | null,
    settingsError: null as number | null,
    /** POST /api/reachy/demo-dose: how many came; while demoGate is set it answers only once that resolves, and
     *  demoError makes it fail with that status and detail. */
    demoPosts: 0,
    demoGate: null as Promise<void> | null,
    demoError: null as { status: number; detail: string } | null,
  };
  const robotStatus = () => state.paired
    ? { paired: true, device_id: 'd-1', label: 'Reachy Mini', auto_record: state.autoRecord, last_seen_at: new Date().toISOString(),
        online: state.online, robot_reachable: true, landmark_fps: 15.2, last_task: null, alert_contacts: 1,
        call_name: state.callName ?? REGISTERED_NAME, call_name_custom: state.callName !== null,
        live_voice: state.liveVoice, live_voice_test: state.liveVoiceTest, meal_retry: state.mealRetry,
        mic_muted: state.micMuted, mic_volume: state.micVolume,
        ...(state.camera === undefined ? {} : { camera: state.camera }) }
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
    if (path === '/api/reachy/status') {
      state.statusReads += 1;
      const answer = robotStatus();   // as the server has it when the request comes, also when statusGate holds it
      if (state.statusGate) await state.statusGate;
      return route.fulfill({ json: answer });
    }
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
      if (state.settingsGate) await state.settingsGate;
      if (state.settingsError) return route.fulfill({ status: state.settingsError, json: { detail: 'request_failed' } });
      // As the server does: a field left out keeps its value, "" goes back to the registered name, and a name that
      // breaks the rules refuses the whole request with 422 invalid_call_name.
      if ('call_name' in body) {
        const name = String(body.call_name).trim();
        if (name && !validName(name)) return route.fulfill({ status: 422, json: { detail: 'invalid_call_name' } });
        state.callName = name || null;
      }
      if ('auto_record' in body) state.autoRecord = body.auto_record;
      if ('live_voice' in body) state.liveVoice = body.live_voice;
      if ('meal_retry' in body) state.mealRetry = body.meal_retry;
      if ('mic_muted' in body) state.micMuted = body.mic_muted;
      return route.fulfill({ json: robotStatus() });
    }
    if (path === '/api/reachy/demo-dose' && request.method() === 'POST') {
      state.demoPosts += 1;
      if (state.demoGate) await state.demoGate;
      if (state.demoError) return route.fulfill({ status: state.demoError.status, json: { detail: state.demoError.detail } });
      return route.fulfill({ json: demoDoseAnswer });
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

// ── The robot's camera (robot app 0.7.0): a warning while it is not working, its rate while a session runs ──

const reachyCard = (page: Page) => page.locator('section[aria-labelledby="reachy-title"]');
const CAMERA_WARNING = /Robot camera not working|機器人鏡頭無法使用/;
const robotCamera = (state: string, fps: number) =>
  ({ state, fps, since: 1791700000.5, media: { available: true, released: false, no_media: false } });

/** The value next to the card's "Robot camera" row. */
const robotCameraRate = (page: Page) =>
  reachyCard(page).locator('dt', { hasText: /^(Robot camera|機器人鏡頭)$/ }).locator('xpath=following-sibling::dd[1]');

test('the Reachy card warns when the robot camera is stalled, recovering or unavailable', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true, camera: robotCamera('stalled', 4.2) });
  await page.goto('/settings');   // the app starts in Traditional Chinese
  for (const state of ['stalled', 'recovering', 'unavailable']) {
    // a 10 s average that still counts the frames from before the stall: not shown
    api.camera = robotCamera(state, 4.2);
    await page.reload();
    await expect(reachyCard(page).getByRole('alert')).toHaveText('機器人鏡頭無法使用');   // exact: no English fallback
    await expect(robotCameraRate(page)).toHaveText('—');
    await expect(reachyCard(page).getByText('4.2 fps')).toHaveCount(0);
  }
  await page.getByRole('button', { name: '中文' }).first().click();
  await expect(reachyCard(page).getByRole('alert')).toHaveText('Robot camera not working');
  await expect(robotCameraRate(page)).toHaveText('—');
});

test('the Reachy card shows no camera warning when the camera works, the robot is offline or older', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true, camera: robotCamera('ok', 9.8) });
  await page.goto('/settings');
  await expect(robotCameraRate(page)).toHaveText('9.8 fps');               // the camera's rate while a session runs
  await expect(reachyCard(page).getByText(CAMERA_WARNING)).toHaveCount(0);

  api.camera = robotCamera('idle', 0);                                       // no session: no rate, no warning
  await page.reload();
  await expect(reachyCard(page).getByText(/Online|在線/)).toBeVisible();      // the card has its status first, or a count of 0 proves nothing
  await expect(reachyCard(page).getByText(/Robot camera|機器人鏡頭/)).toHaveCount(0);
  await expect(reachyCard(page).getByRole('alert').filter({ hasText: CAMERA_WARNING })).toHaveCount(0);

  api.camera = robotCamera('unavailable', 0);                               // last report of a robot now offline
  api.online = false;
  await page.reload();
  await expect(reachyCard(page).getByText(/^(Offline|離線)$/)).toBeVisible();   // the status (the demo note says it too)
  await expect(reachyCard(page).getByText(CAMERA_WARNING)).toHaveCount(0);

  api.camera = undefined;                                                    // a robot older than 0.7.0
  api.online = true;
  await page.reload();
  await expect(reachyCard(page).getByText(/Online|在線/)).toBeVisible();
  await expect(reachyCard(page).getByText(/Robot camera|機器人鏡頭/)).toHaveCount(0);
  await expect(reachyCard(page).getByRole('alert')).toHaveCount(0);
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

test('the check-ins help names the AI connections card, not a company', async ({ page }) => {
  await seedSession(page);
  await mockApi(page, { paired: true, robotConsent: true });
  await page.goto('/settings');   // the app starts in Traditional Chinese
  const help = page.locator('label').filter({ has: checkinSwitch(page) });
  await expect(help).toContainText('只有文字會傳送到「AI 連線」卡片上選定的 AI 服務');
  await expect(help).not.toContainText('OpenRouter');
  await page.getByRole('button', { name: '中文' }).first().click();
  await expect(help).toContainText('only the text is sent to the AI service chosen on the AI connections card');
  await expect(help).not.toContainText('OpenRouter');
});

// ── Live voice 2a (Oct 2026): what Reachy calls the patient, and the live voice test switch ──
// Set REACHY_SCREENSHOTS to a folder to save the card at 390 and 1280 px.

const nameField = (page: Page) =>
  reachyCard(page).getByRole('textbox', { name: /What should Reachy call you\?|Reachy 要怎麼稱呼您？/ });
const saveName = (page: Page) => reachyCard(page).getByRole('button', { name: /^(Save|儲存)$/ });
const resetName = (page: Page) => reachyCard(page).getByRole('button', { name: /Use my registered name|改用註冊時的姓名/ });
const liveSwitch = (page: Page) => page.getByRole('checkbox', { name: /Live voice \(test\)|即時語音（測試）/ });

test('the name field starts with the registered name and saves only the new name', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true });
  await page.goto('/settings');   // the app starts in Traditional Chinese
  await expect(nameField(page)).toHaveValue(REGISTERED_NAME);
  await expect(nameField(page)).toHaveAttribute('placeholder', '例如：林奶奶');
  await expect(resetName(page)).toHaveCount(0);                 // the registered name is already in use
  await expect(saveName(page)).toBeDisabled();                  // nothing typed yet
  await nameField(page).evaluate(element => element.scrollIntoView({ block: 'center' }));
  await nameField(page).fill('  阿嬤  ');
  await clickCentred(saveName(page));
  await expect.poll(() => api.settingsPatches).toEqual([{ call_name: '阿嬤' }]);   // trimmed, and nothing else
  await expect(reachyCard(page).getByRole('status')).toHaveText('已儲存。');
  await expect(nameField(page)).toHaveValue('阿嬤');
  await expect(saveName(page)).toBeDisabled();
  await expect(resetName(page)).toBeVisible();
  await page.reload();
  await expect(nameField(page)).toHaveValue('阿嬤');
});

test('a name of their own can go back to the registered name', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true, callName: 'Grandma Lin' });
  await page.goto('/settings');
  await expect(nameField(page)).toHaveValue('Grandma Lin');
  await clickCentred(resetName(page));
  await expect.poll(() => api.settingsPatches).toEqual([{ call_name: '' }]);
  await expect(nameField(page)).toHaveValue(REGISTERED_NAME);
  await expect(resetName(page)).toHaveCount(0);
});

test('a name the server refuses is explained next to the field, in both languages', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true });
  await page.goto('/settings');   // the app starts in Traditional Chinese
  await nameField(page).evaluate(element => element.scrollIntoView({ block: 'center' }));
  await nameField(page).fill('Dr. Lin 2');
  await clickCentred(saveName(page));
  await expect(reachyCard(page).getByRole('alert')).toHaveText(
    'Reachy 無法使用這個名字。請用最多 8 個中文字，或最多 20 個英文字母和空格，不能有數字、符號，或和健康、藥物有關的字。');
  expect(api.settingsPatches).toEqual([{ call_name: 'Dr. Lin 2' }]);
  await expect(nameField(page)).toHaveValue('Dr. Lin 2');          // kept, so it can be fixed
  await page.getByRole('button', { name: '中文' }).first().click();
  await expect(reachyCard(page).getByRole('alert')).toHaveText(
    "Reachy can't use this name. Use up to 8 Chinese characters, or up to 20 English letters and spaces, with no numbers, symbols, or words about health or medicine.");
  await nameField(page).fill('Grandma Lin');
  await expect(reachyCard(page).getByRole('alert')).toHaveCount(0);   // typing clears it
  await clickCentred(saveName(page));
  await expect(reachyCard(page).getByRole('status')).toHaveText('Saved.');
  expect(api.settingsPatches).toEqual([{ call_name: 'Dr. Lin 2' }, { call_name: 'Grandma Lin' }]);
});

test('the live voice switch shows only when the server allows the test, and saves only live_voice', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true });
  await page.goto('/settings');
  await expect(nameField(page)).toBeVisible();     // the card has loaded, or a count of 0 proves nothing
  await expect(liveSwitch(page)).toHaveCount(0);

  api.liveVoiceTest = true;                        // LIVE_VOICE_TEST=1 in the server's .env
  await page.reload();
  await expect(liveSwitch(page)).not.toBeChecked();
  await clickCentred(liveSwitch(page));
  await expect.poll(() => api.settingsPatches).toEqual([{ live_voice: true }]);
  await expect(liveSwitch(page)).toBeChecked();      // mirrors the server's answer
  await clickCentred(liveSwitch(page));
  await expect.poll(() => api.settingsPatches).toEqual([{ live_voice: true }, { live_voice: false }]);
  await expect(liveSwitch(page)).not.toBeChecked();

  // The help says plainly that the voice itself goes to OpenAI, unlike today's text-only check-ins.
  const help = page.locator('label').filter({ has: liveSwitch(page) });
  await expect(help).toContainText('您的聲音會傳送到 OpenAI 的即時語音模型');
  await page.getByRole('button', { name: '中文' }).first().click();
  await expect(help).toContainText("your voice is sent to OpenAI's live voice model");
});

test('the name field and the live switch fit a phone and a desktop screen', async ({ page }) => {
  await seedSession(page);
  await mockApi(page, { paired: true, robotConsent: true, checkins: true, liveVoiceTest: true, callName: '阿嬤' });
  for (const width of [390, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/settings');
    await expect(nameField(page)).toHaveValue('阿嬤');
    await expect(resetName(page)).toBeVisible();
    await expect(liveSwitch(page)).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    expect((await saveName(page).boundingBox())?.height).toBeGreaterThanOrEqual(48);   // min-h-12: easy to press
    if (process.env.REACHY_SCREENSHOTS) {
      // 'instant', because index.css scrolls smoothly and the picture would be taken mid-scroll.
      await nameField(page).evaluate(element => element.scrollIntoView({ block: 'center', behavior: 'instant' }));
      await page.screenshot({ path: `${process.env.REACHY_SCREENSHOTS}/reachy-card-${width}.png` });
    }
  }
});

// ── Quick fix 5 (20 Oct 2026): Reachy comes back 20 minutes after "not eaten yet" only while this switch is on ──

const mealRetrySwitch = (page: Page) =>
  page.getByRole('checkbox', { name: /Come back 20 minutes after 'not eaten yet'|還沒吃飯時，20 分鐘後再來提醒/ });

test("the meal retry switch defaults off, saves only meal_retry and flips only on the server's answer", async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true });
  await page.goto('/settings');   // the app starts in Traditional Chinese
  await expect(mealRetrySwitch(page)).not.toBeChecked();
  const help = reachyCard(page).locator('label').filter({ has: mealRetrySwitch(page) });
  await expect(help).toContainText('還沒吃飯時，20 分鐘後再來提醒');
  await expect(help).toContainText(
    '長輩說還沒吃飯時，Reachy 會在 20 分鐘後回來再提醒一次（最多 3 次）。關閉時只提醒要飯後吃藥並結束這次服藥。');

  // The switch mirrors the server's value: while the PATCH waits for its answer it stays off.
  let answer = () => {};
  api.settingsGate = new Promise<void>(resolve => { answer = resolve; });
  await clickCentred(mealRetrySwitch(page));
  await expect.poll(() => api.settingsPatches).toEqual([{ meal_retry: true }]);
  await expect(mealRetrySwitch(page)).not.toBeChecked();
  answer();
  await expect(mealRetrySwitch(page)).toBeChecked();
  api.settingsGate = null;

  // A PATCH that fails leaves it as the server has it, and says so.
  api.settingsError = 500;
  await clickCentred(mealRetrySwitch(page));
  await expect(reachyCard(page).getByRole('alert')).toHaveText('操作未成功，請再試一次。');
  expect(api.settingsPatches).toEqual([{ meal_retry: true }, { meal_retry: false }]);
  await expect(mealRetrySwitch(page)).toBeChecked();
  api.settingsError = null;
  await clickCentred(mealRetrySwitch(page));
  await expect.poll(() => api.settingsPatches).toEqual([{ meal_retry: true }, { meal_retry: false }, { meal_retry: false }]);
  await expect(mealRetrySwitch(page)).not.toBeChecked();
});

test('the meal retry switch shows the value the server has, in English too', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true, mealRetry: true });
  await page.goto('/settings');
  await expect(mealRetrySwitch(page)).toBeChecked();
  await page.getByRole('button', { name: '中文' }).first().click();
  const help = reachyCard(page).locator('label').filter({ has: mealRetrySwitch(page) });
  await expect(help).toContainText("Come back 20 minutes after 'not eaten yet'");
  await expect(help).toContainText("When the patient hasn't eaten yet, Reachy comes back 20 minutes later to remind them " +
    'again (at most 3 times). When off, Reachy only reminds them to take it after a meal and ends this intake.');
  expect(api.settingsPatches).toEqual([]);   // showing the card sends nothing
  const source = readFileSync(new URL('../src/i18n.ts', import.meta.url), 'utf8');
  expect(source).not.toContain('??');        // the Chinese words were saved as UTF-8, not as question marks
});

test('the Reachy card translations have the same keys in English and Traditional Chinese', () => {
  const source = readFileSync(new URL('../src/i18n.ts', import.meta.url), 'utf8');
  const blocks = [...source.matchAll(/^ {6}reachy: \{\r?\n([\s\S]*?)^ {6}\},/gm)];
  expect(blocks).toHaveLength(2);
  const keys = blocks.map(block => [...block[1].matchAll(/^ {8}(\w+):/gm)].map(match => match[1]).sort());
  expect(keys[0]).toEqual(keys[1]);
  expect(keys[0]).toEqual(expect.arrayContaining([
    'callName', 'callNameHelp', 'callNamePlaceholder', 'callNameReset', 'callNameSaved', 'callNameInvalid',
    'liveVoice', 'liveVoiceHelp', 'mealRetry', 'mealRetryHelp',
    'micSwitch', 'micSwitchHelp', 'micVolume', 'micApplying', 'micAtZero',
    'demoDose', 'demoDoseHelp', 'demoDoseSent', 'demoDoseNotPaired', 'demoDoseSlotBusy', 'demoDoseOffline',
  ]));
});

// ── Quick fix 6 (20 Oct 2026): a demo dose of Acetal 500mg, for testing the intake flow ──

const demoButton = (page: Page) => page.getByRole('button', { name: /Demo dose: Acetal 500mg|示範藥物：Acetal 500mg/ });
const DEMO_HELP_ZH = '建立一份 Acetal 500mg（飯後）的示範劑量，Reachy 會馬上來提醒服藥，家人也會像真的服藥一樣收到 LINE 確認。之前的示範劑量會先清除。';
const DEMO_HELP_EN = 'Creates a demo dose of Acetal 500mg (after meals) now. Reachy comes right away, and family get the LINE ' +
  'confirmation as for a real dose. Earlier demo doses are cleared first.';

test('the demo dose button shows only while a robot is paired', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { robotConsent: true });
  await page.goto('/settings');
  const pair = page.getByRole('button', { name: /Pair a Reachy robot|配對 Reachy 機器人/ });
  await expect(pair).toBeVisible();
  await expect(demoButton(page)).toHaveCount(0);
  await clickCentred(pair);
  await expect(demoButton(page)).toBeVisible();   // for every account, not only in test mode
  expect(api.demoPosts).toBe(0);
});

test('a demo dose press posts once, waits disabled and says Reachy is on the way', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true });
  await page.goto('/settings');   // the app starts in Traditional Chinese
  await expect(demoButton(page)).toBeVisible();
  await expect(reachyCard(page)).toContainText(DEMO_HELP_ZH);
  let answer = () => {};
  api.demoGate = new Promise<void>(resolve => { answer = resolve; });
  await clickCentred(demoButton(page));
  await expect.poll(() => api.demoPosts).toBe(1);
  await expect(demoButton(page)).toBeDisabled();   // while its request runs
  await demoButton(page).click({ force: true });   // a second press meanwhile sends nothing
  answer();
  await expect(reachyCard(page).getByRole('status').filter({ hasText: '已建立示範劑量，Reachy 正在過來。' })).toBeVisible();
  await expect(demoButton(page)).toBeEnabled();
  expect(api.demoPosts).toBe(1);
  await expect(reachyCard(page).getByRole('alert')).toHaveCount(0);
});

test("a refused demo dose is explained in the card's own words", async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true });
  await page.goto('/settings');
  const cases: [number, string, string][] = [
    [409, 'robot_not_paired', '機器人已取消配對，請先重新配對。'],
    [403, 'robot_consent_required', '請先同意 Reachy 聲明。'],
    // Not "try again": the press keeps failing until that 5-minute slot is over (review of quick fix 6).
    [409, 'slot_busy', 'Reachy 這幾分鐘已有其他服藥提醒，請 5 分鐘後再試。'],
    [500, 'request_failed', '操作未成功，請再試一次。'],
  ];
  for (const [status, detail, words] of cases) {
    api.demoError = { status, detail };
    await clickCentred(demoButton(page));
    await expect(reachyCard(page).getByRole('alert')).toHaveText(words);
    await expect(reachyCard(page).getByText('已建立示範劑量，Reachy 正在過來。')).toHaveCount(0);
  }
  expect(api.demoPosts).toBe(cases.length);
  api.demoError = null;
  await clickCentred(demoButton(page));
  await expect(reachyCard(page).getByText('已建立示範劑量，Reachy 正在過來。')).toBeVisible();
  await expect(reachyCard(page).getByRole('alert')).toHaveCount(0);   // a press that works clears the error
});

test('the demo dose button in English, on a phone and a desktop screen', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true });
  await page.goto('/settings');
  await page.getByRole('button', { name: '中文' }).first().click();
  await expect(reachyCard(page)).toContainText(DEMO_HELP_EN);
  api.demoError = { status: 409, detail: 'robot_not_paired' };
  await clickCentred(demoButton(page));
  await expect(reachyCard(page).getByRole('alert')).toHaveText('The robot is no longer paired. Pair it again first.');
  api.demoError = { status: 409, detail: 'slot_busy' };
  await clickCentred(demoButton(page));
  await expect(reachyCard(page).getByRole('alert'))
    .toHaveText('Reachy already has another dose in these few minutes. Try again in 5 minutes.');
  api.demoError = null;
  await clickCentred(demoButton(page));
  await expect(reachyCard(page).getByText('Demo dose created. Reachy is on the way.')).toBeVisible();
  for (const width of [390, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(demoButton(page)).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    expect((await demoButton(page).boundingBox())?.height).toBeGreaterThanOrEqual(48);   // min-h-12: easy to press
    if (process.env.REACHY_SCREENSHOTS) {
      // 'instant', because index.css scrolls smoothly and the picture would be taken mid-scroll.
      await demoButton(page).evaluate(element => element.scrollIntoView({ block: 'center', behavior: 'instant' }));
      await page.screenshot({ path: `${process.env.REACHY_SCREENSHOTS}/reachy-demo-dose-${width}.png` });
    }
  }
});

// ── Review of quick fix 6 ──

const DEMO_OFFLINE_ZH = 'Reachy 目前離線：要等它重新上線才會來提醒示範劑量，家人也可能收到「機器人離線」的 LINE 通知。';
const DEMO_OFFLINE_EN = 'Reachy is offline. It comes for a demo dose only once it is back online, and family may get a ' +
  'LINE notice that the robot is offline.';

test('a demo dose refused as not paired shows the card unpaired, with its Pair button', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true });
  await page.goto('/settings');
  await expect(demoButton(page)).toBeVisible();
  // Unpaired meanwhile (another tab): the server refuses the press, and the card must not keep its paired view.
  api.paired = false;
  api.demoError = { status: 409, detail: 'robot_not_paired' };
  await clickCentred(demoButton(page));
  await expect(reachyCard(page).getByRole('alert')).toHaveText('機器人已取消配對，請先重新配對。');
  await expect(page.getByRole('button', { name: /Pair a Reachy robot|配對 Reachy 機器人/ })).toBeVisible();
  await expect(demoButton(page)).toHaveCount(0);
});

test('while the robot is offline the demo dose button says what a press does', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true, online: false });
  await page.goto('/settings');
  await expect(reachyCard(page).getByText(DEMO_OFFLINE_ZH)).toBeVisible();
  await expect(demoButton(page)).toBeEnabled();   // a press still works: its task waits for the robot
  await page.getByRole('button', { name: '中文' }).first().click();
  await expect(reachyCard(page).getByText(DEMO_OFFLINE_EN)).toBeVisible();
  api.demoError = { status: 409, detail: 'slot_busy' };
  await clickCentred(demoButton(page));
  await expect(reachyCard(page).getByRole('alert'))
    .toHaveText('Reachy already has another dose in these few minutes. Try again in 5 minutes.');
  for (const width of [390, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(reachyCard(page).getByText(DEMO_OFFLINE_EN)).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    if (process.env.REACHY_SCREENSHOTS) {
      await demoButton(page).evaluate(element => element.scrollIntoView({ block: 'center', behavior: 'instant' }));
      await page.screenshot({ path: `${process.env.REACHY_SCREENSHOTS}/reachy-demo-dose-offline-${width}.png` });
    }
  }
  api.demoError = null;
  api.online = true;
  await page.reload();
  await expect(demoButton(page)).toBeVisible();
  await expect(reachyCard(page).getByText(DEMO_OFFLINE_EN)).toHaveCount(0);
  await expect(reachyCard(page).getByText(DEMO_OFFLINE_ZH)).toHaveCount(0);
  expect(api.demoPosts).toBe(1);
});

// ── Quick fix 8 (20 Oct 2026): the robot's microphone on and off (the robot app sets the daemon's volume to 0) ──

const micSwitch = (page: Page) => page.getByRole('checkbox', { name: /^(Robot microphone|機器人麥克風)/ });
/** The switch's row: its words, and the volume lines under them. */
const micRow = (page: Page) => micSwitch(page).locator('xpath=..');
const MIC_HELP_ZH = '關閉時會把 Reachy 的麥克風音量設為 0，Reachy 聽不到任何聲音（包括「我吃完了」和聊天）。約 10 秒內生效。';
const MIC_HELP_EN = 'When off, Reachy\'s microphone volume is set to 0, so Reachy hears nothing (including "I finished" and ' +
  'chats). Takes effect within about 10 seconds.';
const MIC_APPLYING = /正在套用到機器人…|Applying on the robot…/;
const MIC_AT_ZERO_ZH = '機器人的麥克風音量是 0，Reachy 聽不到聲音。請在 Reachy Mini Control 把麥克風音量調高。';
const MIC_AT_ZERO_EN = "The robot's microphone volume is 0, so Reachy hears nothing. Raise it in Reachy Mini Control.";
const MIC_REREADS = 20;   // ReachyCard.tsx: the status reads after a microphone change, 3 s apart

test('the microphone switch shows only while a robot is paired', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { robotConsent: true });
  await page.goto('/settings');
  const pair = page.getByRole('button', { name: /Pair a Reachy robot|配對 Reachy 機器人/ });
  await expect(pair).toBeVisible();
  await expect(micSwitch(page)).toHaveCount(0);
  await clickCentred(pair);
  await expect(micSwitch(page)).toBeChecked();   // on: the server's default
  expect(api.settingsPatches).toEqual([]);
});

test("the microphone switch defaults on, saves only mic_muted and flips only on the server's answer", async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true, micVolume: 100 });
  await page.goto('/settings');   // the app starts in Traditional Chinese
  await expect(micSwitch(page)).toBeChecked();
  await expect(micRow(page)).toContainText('機器人麥克風');
  await expect(micRow(page)).toContainText(MIC_HELP_ZH);
  await expect(micRow(page)).toContainText('麥克風音量：100');
  await expect(reachyCard(page).getByText('麥克風音量：100')).toBeVisible();
  await expect(reachyCard(page).getByText(MIC_APPLYING)).toHaveCount(0);   // on, and the robot hears

  // The switch mirrors the server's value: while the PATCH waits for its answer it stays on.
  let answer = () => {};
  api.settingsGate = new Promise<void>(resolve => { answer = resolve; });
  await clickCentred(micSwitch(page));
  await expect.poll(() => api.settingsPatches).toEqual([{ mic_muted: true }]);
  await expect(micSwitch(page)).toBeChecked();
  answer();
  await expect(micSwitch(page)).not.toBeChecked();
  api.settingsGate = null;
  // Off, but the robot still reports its volume: it applies the switch at its next heartbeat.
  await expect(reachyCard(page).getByText('正在套用到機器人…')).toBeVisible();
  await expect(reachyCard(page).getByText('麥克風音量：100')).toBeVisible();

  // The card reads the status again while it waits, and the line goes once the robot reports 0.
  const reads = api.statusReads;
  api.micVolume = 0;
  await expect(reachyCard(page).getByText('麥克風音量：0')).toBeVisible();
  await expect(reachyCard(page).getByText(MIC_APPLYING)).toHaveCount(0);
  expect(api.statusReads).toBeGreaterThan(reads);

  // On again: applying until the robot reports a volume above 0.
  await clickCentred(micSwitch(page));
  await expect.poll(() => api.settingsPatches).toEqual([{ mic_muted: true }, { mic_muted: false }]);
  await expect(micSwitch(page)).toBeChecked();
  await expect(reachyCard(page).getByText('正在套用到機器人…')).toBeVisible();
  api.micVolume = 85;
  await expect(reachyCard(page).getByText('麥克風音量：85')).toBeVisible();
  await expect(reachyCard(page).getByText(MIC_APPLYING)).toHaveCount(0);

  // A PATCH that fails leaves it as the server has it, and says so.
  api.settingsError = 500;
  await clickCentred(micSwitch(page));
  await expect(reachyCard(page).getByRole('alert')).toHaveText('操作未成功，請再試一次。');
  expect(api.settingsPatches).toEqual([{ mic_muted: true }, { mic_muted: false }, { mic_muted: true }]);
  await expect(micSwitch(page)).toBeChecked();
  await expect(reachyCard(page).getByText(MIC_APPLYING)).toHaveCount(0);
});

test('the microphone switch shows the value the server has, in English, and no volume before one is reported', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true, micMuted: true });
  await page.goto('/settings');   // the app starts in Traditional Chinese
  await expect(micSwitch(page)).not.toBeChecked();
  // An older robot (or a daemon that did not answer) reports no volume: no volume line and no applying line.
  await expect(reachyCard(page).getByText('麥克風音量：')).toHaveCount(0);
  await expect(reachyCard(page).getByText(MIC_APPLYING)).toHaveCount(0);

  api.micVolume = 40;   // off, but the robot's microphone is still at 40
  await page.reload();
  await expect(reachyCard(page).getByText('麥克風音量：40')).toBeVisible();
  await expect(reachyCard(page).getByText('正在套用到機器人…')).toBeVisible();
  await page.getByRole('button', { name: '中文' }).first().click();
  await expect(micRow(page)).toContainText('Robot microphone');
  await expect(micRow(page)).toContainText(MIC_HELP_EN);
  await expect(micSwitch(page)).not.toBeChecked();
  await expect(reachyCard(page).getByText('Microphone volume: 40')).toBeVisible();
  await expect(reachyCard(page).getByText('Applying on the robot…')).toBeVisible();
  // The volume lines are not part of the switch: pressing them flips nothing.
  await clickCentred(reachyCard(page).getByText('Microphone volume: 40'));
  await expect(micSwitch(page)).not.toBeChecked();
  for (const width of [390, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(micSwitch(page)).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    if (process.env.REACHY_SCREENSHOTS) {
      // 'instant', because index.css scrolls smoothly and the picture would be taken mid-scroll.
      await micSwitch(page).evaluate(element => element.scrollIntoView({ block: 'center', behavior: 'instant' }));
      await page.screenshot({ path: `${process.env.REACHY_SCREENSHOTS}/reachy-mic-${width}.png` });
    }
  }
  expect(api.settingsPatches).toEqual([]);   // showing the card sends nothing
});

// ── Review of quick fix 8 ──

test('switched on with the volume at 0 and no change waiting, the card says where to raise it', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true, micVolume: 0 });
  await page.goto('/settings');   // the app starts in Traditional Chinese
  await expect(micSwitch(page)).toBeChecked();
  await expect(micRow(page)).toContainText('麥克風音量：0');
  // The robot app raises only a volume it muted itself, so the switch alone may never bring this back.
  await expect(reachyCard(page).getByText(MIC_AT_ZERO_ZH)).toBeVisible();
  await expect(reachyCard(page).getByText(MIC_APPLYING)).toHaveCount(0);
  for (const width of [390, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(reachyCard(page).getByText(MIC_AT_ZERO_ZH)).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    if (process.env.REACHY_SCREENSHOTS) {
      await micSwitch(page).evaluate(element => element.scrollIntoView({ block: 'center', behavior: 'instant' }));
      await page.screenshot({ path: `${process.env.REACHY_SCREENSHOTS}/reachy-mic-zero-${width}.png` });
    }
  }
  await page.getByRole('button', { name: '中文' }).first().click();
  await expect(reachyCard(page).getByText(MIC_AT_ZERO_EN)).toBeVisible();
  api.micVolume = 70;
  await page.reload();   // back in Traditional Chinese
  await expect(reachyCard(page).getByText('麥克風音量：70')).toBeVisible();
  await expect(reachyCard(page).getByText(MIC_AT_ZERO_ZH)).toHaveCount(0);
  expect(api.settingsPatches).toEqual([]);
});

test('switched on, the applying line gives way to the volume line once the re-reads end at 0', async ({ page }) => {
  await page.clock.install();
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true, micMuted: true, micVolume: 0 });
  await page.goto('/settings');
  await expect(micSwitch(page)).not.toBeChecked();
  await expect(reachyCard(page).getByText(MIC_APPLYING)).toHaveCount(0);   // off, and at 0: they agree
  await clickCentred(micSwitch(page));
  await expect(micSwitch(page)).toBeChecked();
  // The robot restores the volume at its next heartbeat when it muted it; meanwhile the card waits.
  await expect(reachyCard(page).getByText('正在套用到機器人…')).toBeVisible();
  await expect(reachyCard(page).getByText(MIC_AT_ZERO_ZH)).toHaveCount(0);
  const reads = api.statusReads;
  await expect.poll(async () => { await page.clock.fastForward(3000); return api.statusReads; },
    { timeout: 20_000 }).toBeGreaterThanOrEqual(reads + MIC_REREADS);
  // Still 0 after every re-read: no endless applying line, but where to raise it.
  await expect(reachyCard(page).getByText(MIC_AT_ZERO_ZH)).toBeVisible();
  await expect(reachyCard(page).getByText(MIC_APPLYING)).toHaveCount(0);
  await page.clock.fastForward(10_000);
  expect(api.statusReads).toBe(reads + MIC_REREADS);   // and the card stops reading
});

test('an offline robot shows no microphone volume and no applying line', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true, online: false, micMuted: true, micVolume: 40 });
  await page.goto('/settings');
  await expect(reachyCard(page).getByText('離線', { exact: true })).toBeVisible();
  await expect(micSwitch(page)).not.toBeChecked();
  // Its last report is old: the robot may never apply the switch while it is away.
  await expect(reachyCard(page).getByText('麥克風音量：')).toHaveCount(0);
  await expect(reachyCard(page).getByText(MIC_APPLYING)).toHaveCount(0);
  api.online = true;
  await page.reload();
  await expect(reachyCard(page).getByText('麥克風音量：40')).toBeVisible();
  await expect(reachyCard(page).getByText('正在套用到機器人…')).toBeVisible();
});

test("a microphone re-read sent before another switch's answer never puts that switch back", async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { paired: true, robotConsent: true, micVolume: 100 });
  await page.goto('/settings');
  await expect(micSwitch(page)).toBeChecked();
  await clickCentred(micSwitch(page));          // off: the card re-reads every 3 s while the robot still reports 100
  await expect(micSwitch(page)).not.toBeChecked();
  let release = () => {};
  api.statusGate = new Promise<void>(resolve => { release = resolve; });
  const reads = api.statusReads;
  await expect.poll(() => api.statusReads).toBe(reads + 1);   // a re-read waits, holding the meal retry still off
  await clickCentred(mealRetrySwitch(page));
  await expect(mealRetrySwitch(page)).toBeChecked();          // that PATCH's answer
  let releaseNext = () => {};
  api.statusGate = new Promise<void>(resolve => { releaseNext = resolve; });
  release();                                                  // the older answer comes now
  await expect.poll(() => api.statusReads).toBe(reads + 2);   // handled: the next re-read went out
  await expect(mealRetrySwitch(page)).toBeChecked();
  releaseNext();
  await expect(micSwitch(page)).not.toBeChecked();
  await expect(mealRetrySwitch(page)).toBeChecked();
  expect(api.settingsPatches).toEqual([{ mic_muted: true }, { meal_retry: true }]);
});
