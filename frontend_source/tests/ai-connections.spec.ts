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
    seeded_at: '2026-10-10T12:00:00+00:00', weak_secret: true, no_key_in_use: false,
  };
}

async function seedSession(page: Page) {
  await page.addInitScript(() => {
    for (const key of ['face_auth_session', 'onboarding_complete', 'onboarding_face_done']) localStorage.setItem(key, 'true');
    localStorage.setItem('face_auth_user', JSON.stringify({ name: 'AI Test', u_id: 7, loginAt: new Date().toISOString() }));
    localStorage.setItem('face_auth_token', 'test-face-token');
  });
}

async function mockApi(page: Page, options: { aiDown?: boolean; aiBody?: unknown } = {}) {
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
      Object.assign(model, { profile: { token_param: 'max_completion_tokens', temperature: true, reasoning_off: { reasoning_effort: 'none' } },
        tested: true, tested_at: '2026-10-10T13:00:00+00:00', test_ms: 812 });
      return route.fulfill({ json: { ok: true, ms: 812, sample: '您好！很高興見到您。', profile: model.profile, error: null, settings: mock.settings } });
    }
    if (path === '/api/ai/jobs' && method === 'PUT') {
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
  await main.selectOption({ label: 'OpenAI · gpt-5.4-mini' });
  await card(page).getByRole('button', { name: 'Save jobs' }).click();
  await expect.poll(() => mock.requests.find(r => r.path === '/api/ai/jobs')?.body?.reply)
    .toEqual({ primary_ref: 21, fallback_ref: 12 });
});

test("a refused change shows the card's own words", async ({ page }) => {
  await seedSession(page);
  await mockApi(page);
  await openSettings(page);
  await card(page).getByRole('button', { name: 'Remove OpenRouter' }).click();
  // the default-secret warning is an alert too, so pick the refusal by its words
  await expect(card(page).getByRole('alert').filter({ hasText: 'A job still uses this' })).toBeVisible();
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
