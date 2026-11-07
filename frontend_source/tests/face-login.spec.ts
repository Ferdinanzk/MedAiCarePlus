import { expect, test, type Page } from '@playwright/test';

const COPY: Record<string, { en: string; zh: string }> = {
  come_closer: { en: 'Please come closer to the camera.', zh: '請靠近鏡頭一點。' },
  no_face: { en: 'Please come closer to the camera.', zh: '請靠近鏡頭一點。' },
  two_faces: { en: 'Two people are close to the camera. Please let one person come closer.', zh: '有兩個人靠近鏡頭，請讓一個人靠近一點。' },
  not_live: { en: 'Please look at the camera yourself.', zh: '請本人看著鏡頭。' },
  unknown: { en: 'Face not recognised. Try email login below.', zh: '無法辨識臉部，請改用下方的電子郵件登入。' },
};

interface Mock { bodies: Buffer[]; release: () => void }

async function mockLogin(page: Page, answer: object | { status: number }, hold = false): Promise<Mock> {
  const mock: Mock = { bodies: [], release: () => undefined };
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/face/login') {
      mock.bodies.push(route.request().postDataBuffer() ?? Buffer.alloc(0));
      if (hold) await new Promise<void>(resolve => { mock.release = resolve; });
      if ('status' in answer) return route.fulfill({ status: answer.status, json: {} });
      return route.fulfill({ json: answer });
    }
    if (path === '/api/consent/status') {
      return route.fulfill({ json: { terms_version: '2026-10', core_current: true, robot_current: false,
        scopes: { core: { granted: true, terms_version: '2026-10', kind: 'core' } } } });
    }
    if (path === '/api/reachy/status') return route.fulfill({ json: { paired: false } });
    return route.fulfill({ json: [] });
  });
  return mock;
}

async function openFaceLogin(page: Page, lang: 'en' | 'zh') {
  await page.goto('/login');
  // The app starts in Traditional Chinese, so switch to English before checking English words.
  if (lang === 'en') {
    await page.getByRole('button', { name: '中文' }).first().click({ timeout: 3000 }).catch(() => undefined);
    await expect(page.getByRole('button', { name: 'EN' }).first()).toBeVisible();
  }
  await page.getByRole('button', { name: /Face|臉/ }).first().click();
  const button = page.locator('button.touch-target-large');
  await expect(button).toBeEnabled({ timeout: 15000 });
  return button;
}

const files = (body: Buffer) => (body.toString('latin1').match(/name="files"/g) ?? []).length;

test('one press sends exactly 5 files, and the button is disabled and says Scanning meanwhile', async ({ page }) => {
  const mock = await mockLogin(page, { identified: false, reason: 'unknown', frames: [] }, true);
  const button = await openFaceLogin(page, 'en');
  const height = (await button.boundingBox())!.height;
  await button.click();
  await expect(button).toBeDisabled();
  await expect(button).toContainText('Scanning…');
  await expect.poll(() => mock.bodies.length).toBe(1);
  expect(files(mock.bodies[0])).toBe(5);
  expect((await button.boundingBox())!.height).toBeGreaterThanOrEqual(height);
  mock.release();
  await expect(page.getByText(COPY.unknown.en)).toBeVisible();
  await expect(button).toBeEnabled();
});

for (const reason of Object.keys(COPY)) {
  test(`reason ${reason} shows its copy in English and in Chinese`, async ({ page }) => {
    await mockLogin(page, { identified: false, reason, frames: [] });
    for (const lang of ['en', 'zh'] as const) {
      const button = await openFaceLogin(page, lang);
      await button.click();
      await expect(page.getByText(COPY[reason][lang])).toBeVisible();
    }
  });
}

test('match with a token goes to the dashboard', async ({ page }) => {
  await mockLogin(page, { identified: true, name: 'Amy', distance: 0.1, reason: 'match', token: 'tok', u_id: 3, frames: [] });
  const button = await openFaceLogin(page, 'en');
  await button.click();
  await page.waitForURL('**/dashboard');
  expect(await page.evaluate(() => localStorage.getItem('face_auth_token'))).toBe('tok');
});

test('a server error keeps the unavailable message', async ({ page }) => {
  await mockLogin(page, { status: 500 });
  const button = await openFaceLogin(page, 'en');
  await button.click();
  await expect(page.getByText('Face recognition server unavailable. Please try again later.')).toBeVisible();
});
