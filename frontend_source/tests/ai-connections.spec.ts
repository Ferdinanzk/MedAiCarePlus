import { expect, test, type Page } from '@playwright/test';

const OPENAI_KEY = 'sk-proj-PLAYWRIGHT-secret-9876';
const openrouterProfile = { token_param: 'max_tokens', temperature: true, reasoning_off: { reasoning: { enabled: false } } };

function startingSettings() {
  const copied = (model_ref: number, model_id: string) => ({
    model_ref, connection_id: 1, model_id, company: 'Google', region: 'global', retention: 'not kept',
    profile: openrouterProfile, tested: true, copied_from_env: true, tested_at: null, test_ms: null, last_error: null,
  });
  return {
    connections: [{ connection_id: 1, name: 'OpenRouter', kind: 'openrouter', base_url: 'https://openrouter.ai/api/v1',
      key_last4: 'ab12', key_readable: true, has_key: true, service_name: 'OpenRouter' }],
    models: [copied(11, 'google/gemini-2.5-flash-lite'), copied(12, 'google/gemini-2.5-flash')],
    jobs: { reply: { primary_ref: 11, fallback_ref: 12 }, safety: { primary_ref: 11, fallback_ref: 12 },
      after_chat: { primary_ref: 11, fallback_ref: 12 } },
    jobs_usable: { reply: true, safety: true, after_chat: true },
    seeded_at: '2026-10-10T12:00:00+00:00', weak_secret: true, no_key_in_use: false,
  };
}

/** Flaw F12 (11 Oct 2026): the replies run on an OpenAI model with a key, but the safety check and the after-chat
 *  work run on an OpenRouter connection whose key was never filled in. A key is in use, so no_key_in_use is false. */
function keylessOpenRouterSettings() {
  const settings: any = startingSettings();
  settings.connections = [
    { ...settings.connections[0], key_last4: null, has_key: false },
    { connection_id: 2, name: 'OpenAI', kind: 'openai', base_url: 'https://api.openai.com/v1', key_last4: '9876',
      key_readable: true, has_key: true, service_name: 'OpenAI' },
  ];
  settings.models.push({ model_ref: 21, connection_id: 2, model_id: 'gpt-5.4-mini', company: 'OpenAI', region: 'USA',
    retention: '30 days', profile: { token_param: 'max_completion_tokens', temperature: true, reasoning_off: { reasoning_effort: 'none' } },
    tested: true, copied_from_env: false, tested_at: '2026-10-10T13:00:00+00:00', test_ms: 812, last_error: null });
  settings.jobs.reply = { primary_ref: 21, fallback_ref: null };
  settings.jobs_usable = { reply: true, safety: false, after_chat: false };
  settings.weak_secret = false;
  return settings;
}

async function seedSession(page: Page) {
  await page.addInitScript(() => {
    for (const key of ['face_auth_session', 'onboarding_complete', 'onboarding_face_done']) localStorage.setItem(key, 'true');
    localStorage.setItem('face_auth_user', JSON.stringify({ name: 'AI Test', u_id: 7, loginAt: new Date().toISOString() }));
    localStorage.setItem('face_auth_token', 'test-face-token');
  });
}

async function mockApi(page: Page, options: { aiDown?: boolean; aiBody?: unknown; jobsRefused?: boolean; testError?: string } = {}) {
  const mock = { settings: startingSettings(), requests: [] as { method: string; path: string; body: any }[] };
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    const body = request.postData() ? request.postDataJSON() : null;
    if (path.startsWith('/api/ai/')) mock.requests.push({ method, path, body });
    if (path === '/api/consent/status') {
      return route.fulfill({ json: { terms_version: '2026-10', core_current: true, robot_current: false,
        scopes: { core: { granted: true, terms_version: '2026-10', kind: 'core' } } } });
    }
    if (path === '/api/reachy/status') return route.fulfill({ json: { paired: false } });
    if (path === '/api/notify/settings') return route.fulfill({ json: {} });
    if (path === '/api/ai/settings') {
      return options.aiDown ? route.fulfill({ status: 500, json: { detail: 'boom' } }) : route.fulfill({ json: options.aiBody ?? mock.settings });
    }
    if (path === '/api/ai/connections' && method === 'POST') {
      mock.settings.connections.push({ connection_id: 2, name: body.name, kind: body.kind, base_url: 'https://api.openai.com/v1',
        key_last4: body.api_key.slice(-4), key_readable: true, has_key: true, service_name: body.service_name });
      return route.fulfill({ json: mock.settings });
    }
    if (path === '/api/ai/connections/1' && method === 'DELETE') return route.fulfill({ status: 409, json: { detail: 'in_use' } });
    if (path === '/api/ai/models' && method === 'POST') {
      mock.settings.models.push({ model_ref: 21, connection_id: body.connection_id, model_id: body.model_id, company: body.company,
        region: body.region, retention: body.retention, profile: null as any, tested: false, copied_from_env: false,
        tested_at: null, test_ms: null, last_error: null });
      return route.fulfill({ json: mock.settings });
    }
    if (path === '/api/ai/models/21/test') {
      const model: any = mock.settings.models.find(m => m.model_ref === 21);
      if (options.testError) {
        model.last_error = options.testError;   // as ai_store.save_test: the reason is saved, the model stays untested
        return route.fulfill({ json: { ok: false, ms: 412, sample: null, profile: null, error: options.testError, settings: mock.settings } });
      }
      Object.assign(model, { profile: { token_param: 'max_completion_tokens', temperature: true, reasoning_off: { reasoning_effort: 'none' } },
        tested: true, tested_at: '2026-10-10T13:00:00+00:00', test_ms: 812 });
      return route.fulfill({ json: { ok: true, ms: 812, sample: '您好！很高興見到您。', profile: model.profile, error: null, settings: mock.settings } });
    }
    if (path === '/api/ai/jobs' && method === 'PUT') {
      if (options.jobsRefused) return route.fulfill({ status: 400, json: { detail: 'notice_incomplete' } });
      mock.settings.jobs = body;
      return route.fulfill({ json: mock.settings });
    }
    return route.fulfill({ json: [] });
  });
  return mock;
}

// The app starts in Traditional Chinese (i18n.ts sets lng), so switch to English before checking the card's words.
async function openSettings(page: Page) {
  await page.goto('/settings');
  await page.getByRole('button', { name: '中文' }).first().click();
}

const card = (page: Page) => page.locator('section[aria-labelledby="ai-title"]');

async function addOpenAi(page: Page) {
  await card(page).getByRole('button', { name: 'Add connection' }).click();
  await card(page).getByLabel('API key').fill(OPENAI_KEY);
  await card(page).getByRole('button', { name: 'Save', exact: true }).click();
  // exact: the model rows' names ("OpenAI · …") contain the connection's name
  await expect(card(page).getByRole('group', { name: 'OpenAI', exact: true })).toContainText('Key ••••9876');
}

test('keys show only their last 4 characters, with the default-secret warning', async ({ page }) => {
  await seedSession(page);
  await mockApi(page);
  await openSettings(page);
  await expect(card(page).getByRole('group', { name: 'OpenRouter', exact: true })).toContainText('Key ••••ab12');
  await expect(card(page).getByText(/SECRET_KEY/)).toBeVisible();
  await expect(card(page).getByRole('group', { name: 'OpenRouter · google/gemini-2.5-flash-lite', exact: true }))
    .toContainText('Copied from .env');
});

test('a new OpenAI key is sent once and never shown again', async ({ page }) => {
  await seedSession(page);
  const mock = await mockApi(page);
  await openSettings(page);
  await addOpenAi(page);
  expect(mock.requests.find(r => r.path === '/api/ai/connections')?.body).toMatchObject(
    { kind: 'openai', name: 'OpenAI', api_key: OPENAI_KEY, service_name: 'OpenAI' });
  await expect(page.locator('body')).not.toContainText('PLAYWRIGHT-secret');
  await expect(card(page).getByLabel('API key')).toHaveCount(0);
});

test('a tested model can be given a job, and the card sends the chosen jobs', async ({ page }) => {
  await seedSession(page);
  const mock = await mockApi(page);
  await openSettings(page);
  await addOpenAi(page);
  await card(page).getByRole('button', { name: 'Add model' }).click();
  await card(page).getByLabel('Connection', { exact: true }).selectOption({ label: 'OpenAI' });
  await card(page).getByLabel('Model name').fill('gpt-5.4-mini');
  await card(page).getByLabel('Company behind the model').fill('OpenAI');
  await card(page).getByLabel('Region').fill('USA');
  await card(page).getByLabel('How long it keeps data').fill('30 days');
  await card(page).getByRole('button', { name: 'Add', exact: true }).click();
  const row = card(page).getByRole('group', { name: 'OpenAI · gpt-5.4-mini' });
  await expect(row).toContainText('Not tested');
  const main = card(page).getByLabel("Reachy's replies: Main model");
  await expect(main.locator('option', { hasText: 'gpt-5.4-mini' })).toHaveCount(0);
  await row.getByRole('button', { name: 'Test' }).click();
  await expect(row).toContainText('0.8 s');
  // each job's two lists say which is the main model and which the backup
  await expect(card(page).locator('label', { hasText: 'Main model' })).toHaveCount(3);
  await expect(card(page).locator('label', { hasText: 'Backup model' })).toHaveCount(3);
  await main.selectOption({ label: 'OpenAI · gpt-5.4-mini' });
  await card(page).getByRole('button', { name: 'Save jobs' }).click();
  await expect.poll(() => mock.requests.find(r => r.path === '/api/ai/jobs')?.body?.reply)
    .toEqual({ primary_ref: 21, fallback_ref: 12 });
  await expect(card(page).getByRole('status')).toHaveText('Saved.');
  await main.selectOption({ label: 'OpenRouter · google/gemini-2.5-flash' });   // a new change clears it
  await expect(card(page).getByRole('status')).toHaveText('');
});

test("a refused change shows the card's own words next to what caused it, after a confirmation", async ({ page }) => {
  await seedSession(page);
  const mock = await mockApi(page);
  await openSettings(page);
  const remove = card(page).getByRole('button', { name: 'Remove OpenRouter' });
  page.once('dialog', dialog => void dialog.dismiss());
  await remove.click();
  expect(mock.requests.filter(r => r.method === 'DELETE')).toEqual([]);   // dismissed: nothing is removed
  let question = '';
  page.once('dialog', dialog => { question = dialog.message(); void dialog.accept(); });
  await remove.click();
  expect(question).toContain('Remove OpenRouter?');
  const refusal = card(page).getByRole('group', { name: 'OpenRouter', exact: true }).getByRole('alert');
  await expect(refusal).toContainText('A job still uses this');
  await expect(refusal).toBeInViewport();
});

test('a refused Save jobs shows why where it was pressed, and never says saved', async ({ page }) => {
  await seedSession(page);
  await mockApi(page, { jobsRefused: true });
  await openSettings(page);
  await card(page).getByRole('button', { name: 'Save jobs' }).click();
  // the default-secret warning is an alert too, so pick the refusal by its words
  const refusal = card(page).getByRole('alert').filter({ hasText: 'Fill in company, region and data retention' });
  await expect(refusal).toBeInViewport();
  await expect(card(page).getByRole('status')).toHaveText('');
});

test('the rest of Settings keeps working when the AI settings cannot load', async ({ page }) => {
  await seedSession(page);
  await mockApi(page, { aiDown: true });
  await openSettings(page);
  await expect(card(page)).toContainText('Could not load the AI settings.');
  await expect(page.locator('section[aria-labelledby="reachy-title"]')).toBeVisible();
});

test('an incomplete AI settings answer is a load failure, not a crash', async ({ page }) => {
  await seedSession(page);
  await mockApi(page, { aiBody: { connections: [], models: [], jobs: {}, seeded_at: null, weak_secret: false, no_key_in_use: false } });
  await openSettings(page);
  await expect(card(page)).toContainText('Could not load the AI settings.');
  await expect(page.locator('section[aria-labelledby="reachy-title"]')).toBeVisible();
});

test('a job choice not yet saved survives adding another model', async ({ page }) => {
  await seedSession(page);
  await mockApi(page);
  await openSettings(page);
  const main = card(page).getByLabel("Reachy's replies: Main model");
  await main.selectOption({ label: 'OpenRouter · google/gemini-2.5-flash' });
  await addOpenAi(page);
  await card(page).getByRole('button', { name: 'Add model' }).click();
  await card(page).getByLabel('Connection', { exact: true }).selectOption({ label: 'OpenAI' });
  await card(page).getByLabel('Model name').fill('gpt-5.4-mini');
  await card(page).getByRole('button', { name: 'Add', exact: true }).click();
  await expect(card(page).getByRole('group', { name: 'OpenAI · gpt-5.4-mini' })).toBeVisible();
  await expect(main).toHaveValue('12');
});

// ── Spec 2026-10-11 C1 and C3: a warning under each job that can't run, and the voice-only Test message ──

const job = (page: Page, name: string) => card(page).getByRole('group', { name, exact: true });

test('each job without a usable model says so and what stops with it, in both languages', async ({ page }) => {
  await seedSession(page);
  await mockApi(page, { aiBody: keylessOpenRouterSettings() });
  await page.goto('/settings');   // the app starts in Traditional Chinese
  await expect(job(page, '安全檢查')).toContainText('沒有可用的模型：這項工作無法執行。');
  await expect(job(page, '安全檢查')).toContainText('Reachy 的模型安全檢查已停止。');
  await expect(job(page, '聊天後的摘要與記憶')).toContainText('沒有可用的模型：這項工作無法執行。');
  await expect(job(page, '聊天後的摘要與記憶')).toContainText('摘要與摘要安全檢查已停止。');
  await page.getByRole('button', { name: '中文' }).first().click();
  const safety = job(page, 'Safety check');
  await expect(safety.getByRole('alert')).toContainText("No usable model: this job can't run.");
  await expect(safety.getByRole('alert')).toContainText("Reachy's model safety check is off.");
  const afterChat = job(page, 'After-chat summary and memory');
  await expect(afterChat.getByRole('alert')).toContainText("No usable model: this job can't run.");
  await expect(afterChat.getByRole('alert')).toContainText('Summaries and the summary safety check are off.');
  await expect(afterChat).not.toContainText("Reachy's model safety check is off.");
  // the replies have a model with a key: no warning there, and the card-wide "no key" warning stays silent
  await expect(job(page, "Reachy's replies").getByRole('alert')).toHaveCount(0);
  await expect(card(page).getByText('No model in use has an API key')).toHaveCount(0);
});

for (const [what, jobsUsable] of [['missing', undefined], ['incomplete', { reply: true, safety: 'no' }]] as const) {
  test(`an AI settings answer with jobs_usable ${what} is a load failure, not a crash`, async ({ page }) => {
    await seedSession(page);
    await mockApi(page, { aiBody: { ...startingSettings(), jobs_usable: jobsUsable } });
    await openSettings(page);
    await expect(card(page)).toContainText('Could not load the AI settings.');
    await expect(page.locator('section[aria-labelledby="reachy-title"]')).toBeVisible();
  });
}

test("Test on a voice-only model shows the card's own words, not the code", async ({ page }) => {
  await seedSession(page);
  await mockApi(page, { testError: 'not_chat_model' });
  await openSettings(page);
  await addOpenAi(page);
  await card(page).getByRole('button', { name: 'Add model' }).click();
  await card(page).getByLabel('Connection', { exact: true }).selectOption({ label: 'OpenAI' });
  await card(page).getByLabel('Model name').fill('gpt-live-1');
  await card(page).getByRole('button', { name: 'Add', exact: true }).click();
  const row = card(page).getByRole('group', { name: 'OpenAI · gpt-live-1' });
  await row.getByRole('button', { name: 'Test' }).click();
  await expect(row).toContainText("Voice-only model (like GPT-Live or a TTS model): it can't write Reachy's text replies.");
  await expect(row).not.toContainText('not_chat_model');
  // still untested, so no job can be given to it
  await expect(card(page).getByLabel("Reachy's replies: Main model").locator('option', { hasText: 'gpt-live-1' })).toHaveCount(0);
});

test('a saved voice-only failure shows the same words as the last call, in both languages', async ({ page }) => {
  await seedSession(page);
  const settings: any = startingSettings();
  settings.models.push({ model_ref: 22, connection_id: 1, model_id: 'openai/gpt-live-1', company: 'OpenAI', region: 'USA',
    retention: '30 days', profile: null, tested: false, copied_from_env: false, tested_at: null, test_ms: null,
    last_error: 'not_chat_model' });
  await mockApi(page, { aiBody: settings });
  await page.goto('/settings');   // the app starts in Traditional Chinese
  const row = card(page).getByRole('group', { name: 'OpenRouter · openai/gpt-live-1', exact: true });
  await expect(row).toContainText('上次呼叫失敗：語音專用模型（例如 GPT-Live 或 TTS）：無法撰寫 Reachy 的文字回覆。');
  await page.getByRole('button', { name: '中文' }).first().click();
  await expect(row).toContainText("Last call failed: Voice-only model (like GPT-Live or a TTS model): it can't write Reachy's text replies.");
  await expect(row).not.toContainText('not_chat_model');
});
