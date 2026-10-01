import { readFileSync } from 'node:fs';
import { expect, test, type Page } from '@playwright/test';

const authKeys = ['face_auth_session', 'face_auth_token', 'face_auth_user', 'onboarding_complete', 'onboarding_face_done'];
const gateTitle = /Please review the updated terms|請閱讀更新後的條款/;
const acceptLabel = /^(Accept|同意)$/;
const exportLabel = /Export my data|匯出我的資料/;
const deleteLabel = /Delete my account|刪除我的帳戶/;

function legalDocument(language = 'zh-TW', revision = 1) {
  return {
    kind: 'core', terms_version: '2026-10', language,
    sha256: (language === 'en' ? 'a' : 'b').repeat(63) + revision,
    complete: true,
    document: {
      kind: 'core', version: '2026-10', language,
      title: `${language === 'en' ? 'Terms and privacy notice' : '服務條款與隱私告知'} ${revision}`,
      what_changed: ['Consent is **recorded**.'],
      sections: [
        { id: 'operator', heading: 'Operator', blocks: [
          { type: 'p', text: 'Operated by **Example Care**. <img src=x onerror=alert(1)>' },
          { type: 'p', text: 'Read how we handle your records. '.repeat(25) },
        ] },
        { id: 'data', heading: 'Data we collect', blocks: [
          { type: 'list', items: ['**Face photos**', 'Medication records'] },
          { type: 'table', header: ['Data', 'Processor', 'Location', 'Retention'], rows: [
            ['Face photos', '**Example Care**', 'Taiwan', 'Until account deletion'],
          ] },
        ] },
        { id: 'rights', heading: 'Your rights', blocks: [
          { type: 'p', text: 'You may export or delete your data. '.repeat(30) },
        ] },
      ],
    },
  };
}

async function seedSession(page: Page, onboarding = true) {
  await page.addInitScript(({ keys, onboarding }) => {
    if (sessionStorage.getItem('consent-test-seeded')) return;
    sessionStorage.setItem('consent-test-seeded', '1');
    for (const key of keys) localStorage.setItem(key, 'true');
    localStorage.setItem('face_auth_user', JSON.stringify({ name: 'Consent Test', u_id: 7, loginAt: new Date().toISOString() }));
    localStorage.setItem('face_auth_token', 'test-face-token');
    if (!onboarding) localStorage.removeItem('onboarding_complete');
  }, { keys: authKeys, onboarding });
}

async function mockApi(page: Page, options: {
  current?: boolean;
  conflict?: 'stale_terms_version' | 'document_hash_mismatch';
  statusFailure?: boolean;
  legalFailure?: boolean;
  denyData?: boolean;
  reauth?: boolean;
} = {}) {
  const state = {
    current: options.current ?? false,
    revision: 1,
    legalRequests: [] as string[],
    dataRequests: [] as string[],
    consentPosts: [] as Record<string, unknown>[],
    registerPosts: [] as Record<string, unknown>[],
    deletePosts: [] as Record<string, unknown>[],
    statusRequests: 0,
  };
  const status = () => ({ terms_version: '2026-10', core_current: state.current, robot_current: false, scopes: {} });

  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === '/api/legal/current') {
      const language = url.searchParams.get('lang') || 'en';
      state.legalRequests.push(language);
      await route.fulfill(options.legalFailure
        ? { status: 503, json: { detail: 'document_not_configured' } }
        : { json: legalDocument(language, state.revision) });
    } else if (url.pathname === '/api/consent/status') {
      state.statusRequests++;
      expect(request.headers().authorization).toBe('Bearer test-face-token');
      await route.fulfill(options.statusFailure ? { status: 503, json: { detail: 'unavailable' } } : { json: status() });
    } else if (url.pathname === '/api/consent') {
      expect(request.headers().authorization).toBe('Bearer test-face-token');
      state.consentPosts.push(request.postDataJSON());
      if (options.conflict && state.consentPosts.length === 1) {
        state.revision++;
        await route.fulfill({ status: 409, json: { detail: options.conflict } });
      } else {
        state.current = true;
        await route.fulfill({ json: status() });
      }
    } else if (url.pathname === '/api/account/export') {
      expect(request.headers().authorization).toBe('Bearer test-face-token');
      await route.fulfill({ contentType: 'application/zip', body: 'mock-zip', headers: { 'Content-Disposition': 'attachment; filename="account.zip"' } });
    } else if (url.pathname === '/api/account/delete') {
      expect(request.headers().authorization).toBe('Bearer test-face-token');
      state.deletePosts.push(request.postDataJSON());
      await route.fulfill(options.reauth && state.deletePosts.length === 1
        ? { status: 401, json: { detail: 'reauth_required' } }
        : { json: { deleted: true } });
    } else if (url.pathname === '/api/auth/register') {
      state.registerPosts.push(request.postDataJSON());
      await route.fulfill({ json: { success: true, u_id: 7, token: 'test-face-token' } });
    } else {
      state.dataRequests.push(url.pathname);
      await route.fulfill(options.denyData
        ? { status: 403, json: { detail: 'consent_required' } }
        : { json: [] });
    }
  });
  return state;
}

for (const viewport of [{ name: 'desktop', width: 1280, height: 900 }, { name: 'mobile', width: 390, height: 844 }]) {
  test.describe(viewport.name, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height }, isMobile: false });

    test('existing user sees the gate before data loads and acceptance reloads the dashboard', async ({ page }) => {
      await seedSession(page);
      const api = await mockApi(page);
      await page.goto('/medications');
      await expect(page.getByRole('heading', { name: gateTitle })).toBeVisible();
      await expect(page.getByText(/What changed|本次更新重點/)).toBeVisible();
      expect(api.dataRequests).toEqual([]);
      const displayedLanguage = await page.locator('article').getAttribute('lang');
      const navigation = page.waitForEvent('request', { predicate: request => request.isNavigationRequest() && new URL(request.url()).pathname === '/dashboard' });
      await page.getByRole('button', { name: acceptLabel }).click();
      await navigation;
      await expect(page).toHaveURL(/\/dashboard$/);
      await expect(page.getByRole('heading', { name: /Today's Medications|今日用藥/ }).first()).toBeVisible();
      await expect(page.getByRole('heading', { name: gateTitle })).toHaveCount(0);
      expect(api.consentPosts).toEqual([{
        kind: 'core', terms_version: '2026-10', language: displayedLanguage,
        document_sha256: legalDocument(displayedLanguage!).sha256,
        scopes: { core: true }, source: 'reconsent',
      }]);
      expect(api.statusRequests).toBe(2);
    });

    test('Not now opens limited mode with export, deletion, logout and no horizontal overflow', async ({ page }) => {
      await seedSession(page, false);
      const api = await mockApi(page);
      await page.goto('/onboarding');
      await expect(page.getByRole('heading', { name: gateTitle })).toBeVisible();
      await page.getByRole('link', { name: /Not now|暫不同意/ }).click();
      await expect(page).toHaveURL(/\/privacy-settings$/);
      await expect(page.getByRole('status')).toContainText(/You have not accepted|您尚未同意/);
      await expect(page.getByRole('button', { name: exportLabel })).toBeVisible();
      await expect(page.getByRole('button', { name: deleteLabel })).toBeVisible();
      await expect(page.getByRole('button', { name: /Logout|登出/ })).toBeVisible();
      expect(api.dataRequests).toEqual([]);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      const download = page.waitForEvent('download');
      await page.getByRole('button', { name: exportLabel }).click();
      expect((await download).suggestedFilename()).toBe('medaicareplus-account.zip');
      await page.getByRole('button', { name: /Review and accept terms|閱讀並同意條款/ }).click();
      await expect(page.locator('article')).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      await page.getByRole('button', { name: acceptLabel }).click();
      await expect.poll(() => api.consentPosts.length).toBe(1);
      expect(api.consentPosts[0].source).toBe('settings');
    });
  });
}

test('public terms render every block safely without login', async ({ page }) => {
  const api = await mockApi(page);
  await page.goto('/terms');
  await expect(page.locator('article')).toBeVisible();
  await expect(page.locator('article strong').first()).toHaveText('Example Care');
  await expect(page.locator('article img')).toHaveCount(0);
  await expect(page.locator('article')).toContainText('<img src=x onerror=alert(1)>');
  await expect(page.getByRole('listitem').filter({ hasText: 'Face photos' })).toBeVisible();
  await expect(page.getByRole('table')).toBeVisible();
  await expect(page.getByText('Consent is recorded.')).toHaveCount(0);
  expect(api.statusRequests).toBe(0);
  expect(api.dataRequests).toEqual([]);
});

test('public privacy scrolls to the data section without login', async ({ page }) => {
  await mockApi(page);
  await page.goto('/privacy');
  await expect(page.locator('#data')).toBeInViewport();
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
});

for (const conflict of ['stale_terms_version', 'document_hash_mismatch'] as const) {
  test(`${conflict} reloads the legal document before accepting again`, async ({ page }) => {
    await seedSession(page);
    const api = await mockApi(page, { conflict });
    await page.goto('/dashboard');
    await page.getByRole('button', { name: acceptLabel }).click();
    await expect(page.getByRole('alert')).toContainText(/terms have changed|條款已有變更/);
    await expect(page.locator('article h2')).toContainText('2');
    expect(api.consentPosts).toHaveLength(1);
    await page.getByRole('button', { name: acceptLabel }).click();
    await expect.poll(() => api.consentPosts.length).toBe(2);
    expect(api.consentPosts[1].document_sha256).not.toBe(api.consentPosts[0].document_sha256);
    await expect(page.getByRole('heading', { name: /Today's Medications|今日用藥/ }).first()).toBeVisible();
    await expect(page.getByRole('heading', { name: gateTitle })).toHaveCount(0);
  });
}

test('changing language disables acceptance until the matching document arrives', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page);
  await page.goto('/dashboard');
  await expect(page.locator('article')).toHaveAttribute('lang', 'zh-TW');
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/legal/current?kind=core&lang=en', async route => {
    await pending;
    await route.fulfill({ json: legalDocument('en') });
  });
  await page.getByRole('button', { name: '中文', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Accept', exact: true })).toBeDisabled();
  await expect(page.locator('article')).toHaveCount(0);
  release();
  await expect(page.locator('article')).toHaveAttribute('lang', 'en');
  await page.getByRole('button', { name: 'Accept', exact: true }).click();
  await expect.poll(() => api.consentPosts.length).toBe(1);
  expect(api.consentPosts[0]).toMatchObject({ language: 'en', document_sha256: legalDocument('en').sha256 });
});

test('a data-route consent_required response restores the gate', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { current: true, denyData: true });
  await page.goto('/dashboard');
  await expect(page.getByRole('heading', { name: gateTitle })).toBeVisible();
  expect(api.statusRequests).toBe(1);
  expect(api.dataRequests).toContain('/api/medications/today');
});

test('status failure blocks data pages while privacy controls remain available', async ({ page }) => {
  await seedSession(page);
  const api = await mockApi(page, { statusFailure: true });
  await page.goto('/dashboard');
  await expect(page.getByRole('alert')).toContainText(/could not load|無法載入/);
  expect(api.dataRequests).toEqual([]);
  await page.getByRole('link', { name: /Privacy & data|隱私與資料/ }).click();
  await expect(page.getByRole('button', { name: exportLabel })).toBeVisible();
  await expect(page.getByRole('button', { name: deleteLabel })).toBeVisible();
});

test('legal load failure keeps acceptance disabled and offers retry', async ({ page }) => {
  await seedSession(page);
  await mockApi(page, { legalFailure: true });
  await page.goto('/dashboard');
  await expect(page.getByRole('button', { name: acceptLabel })).toBeDisabled();
  await expect(page.getByRole('button', { name: /Try again|再試一次/ })).toBeVisible();
});

test('deletion requires confirmation, handles reauthentication, and clears the session on success', async ({ page }) => {
  await seedSession(page, false);
  const api = await mockApi(page, { reauth: true });
  await page.goto('/privacy-settings');
  await page.getByRole('button', { name: deleteLabel }).click();
  await expect(page.getByText(/Permanently delete|確定要永久刪除/)).toBeVisible();
  expect(api.deletePosts).toEqual([]);
  await page.getByRole('button', { name: deleteLabel }).click();
  await expect(page.getByRole('alert')).toContainText(/could not confirm your identity|無法確認您的身分/);
  expect(api.deletePosts).toEqual([{}]);
  await page.getByLabel(/Enter your password|若帳戶設有密碼/).fill('confirmed-password');
  await page.getByRole('button', { name: deleteLabel }).click();
  await expect(page).toHaveURL(/\/login$/);
  expect(api.deletePosts[1]).toEqual({ password: 'confirmed-password' });
  expect(await page.evaluate(keys => keys.map(key => localStorage.getItem(key)), authKeys)).toEqual(authKeys.map(() => null));
});

test('limited-mode logout clears every authentication and onboarding key', async ({ page }) => {
  await seedSession(page);
  await mockApi(page);
  await page.goto('/privacy-settings');
  await page.getByRole('button', { name: /Logout|登出/ }).click();
  await expect(page).toHaveURL(/\/login$/);
  expect(await page.evaluate(keys => keys.map(key => localStorage.getItem(key)), authKeys)).toEqual(authKeys.map(() => null));
});

test('registration renews agreement after a language switch and posts the displayed hash', async ({ page }) => {
  const api = await mockApi(page);
  await page.goto('/register');
  await page.getByPlaceholder(/Your full name|您的全名/).fill('Consent Test');
  await page.getByPlaceholder('your@email.com').fill('consent@example.test');
  await page.locator('input[type="password"]').nth(0).fill('example-password');
  await page.locator('input[type="password"]').nth(1).fill('example-password');
  await page.getByRole('button', { name: /^(Terms & Conditions|服務條款)$/ }).click();
  await expect(page.getByRole('dialog').locator('article')).toHaveAttribute('lang', 'zh-TW');
  await page.getByRole('button', { name: /^(I Agree|我同意)$/ }).click();
  await expect(page.locator('#agree-terms')).toBeChecked();
  await page.getByRole('button', { name: '中文', exact: true }).click();
  await expect(page.locator('#agree-terms')).not.toBeChecked();
  await expect(page.getByRole('button', { name: 'Next', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Terms & Conditions', exact: true }).click();
  await expect(page.getByRole('dialog').locator('article')).toHaveAttribute('lang', 'en');
  await page.getByRole('button', { name: 'I Agree', exact: true }).click();
  await page.getByRole('button', { name: 'Next', exact: true }).click();
  await expect.poll(() => api.registerPosts.length).toBe(1);
  expect(api.registerPosts[0].consent).toEqual({
    terms_version: '2026-10', language: 'en', document_sha256: legalDocument('en').sha256, scopes: { core: true },
  });
});

test('legal UI translations have matching English and Traditional Chinese keys', () => {
  const source = readFileSync(new URL('../src/i18n.ts', import.meta.url), 'utf8');
  const blocks = [...source.matchAll(/^      legal: \{\r?\n([\s\S]*?)^      \},/gm)];
  expect(blocks).toHaveLength(2);
  const keys = blocks.map(block => [...block[1].matchAll(/^        (\w+):/gm)].map(match => match[1]).sort());
  expect(keys[0]).toEqual(keys[1]);
  expect(keys[0]).toEqual(expect.arrayContaining(['gateTitle', 'whatChanged', 'accept', 'notNow', 'limitedBanner', 'export', 'delete', 'deleteConfirm', 'passwordPrompt', 'reauthRequired', 'loadFailed', 'privacyCardTitle', 'privacyCardDesc']));
  expect(source).not.toMatch(/^          (tosTitle|tosContent|privacyTitle|privacyContent|dataTitle|dataItems|rightsTitle|rightsContent):/m);
});
