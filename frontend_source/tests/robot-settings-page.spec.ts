import { readFileSync } from 'node:fs';
import { expect, test, type Page, type Route } from '@playwright/test';

// The robot app's own settings page (reachy_app/medcare_reachy/static, served on port 8042 of the robot), loaded
// from disk under a made-up address with its API mocked. Two restarts at once used to leave two bridges driving the
// same robot, so the page sends one request at a time.
const STATIC = new URL('../../reachy_app/medcare_reachy/static/', import.meta.url);
const ROBOT = 'http://robot.test';
const TYPES: Record<string, string> = { html: 'text/html', js: 'text/javascript', css: 'text/css' };

const config = {
  app_url: 'http://192.168.1.20:8001', language: 'zh-TW', capture_fps: 15, vision_on_server: true,
  checkin_ack: true, checkin_gestures: true, device_token_set: true,
};
const running = { state: 'running', detail: 'Waiting for a medication reminder from the server.', since: 0 };
const stopping = { state: 'stopping', detail: 'The previous connection is still closing. Press Restart in a moment.', since: 0 };

function held() {
  let release!: () => void;
  const until = new Promise<void>(resolve => { release = resolve; });
  return { until, release };
}

async function openSettingsPage(page: Page, onPost: (path: string, route: Route) => Promise<void>,
                                status: () => object = () => running) {
  await page.route(`${ROBOT}/**`, async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'POST') return onPost(path, route);
    if (path === '/api/config') return route.fulfill({ json: config });
    if (path === '/api/status') return route.fulfill({ json: status() });
    const name = path === '/' ? 'index.html' : path.replace('/static/', '');
    return route.fulfill({ body: readFileSync(new URL(name, STATIC)), contentType: TYPES[name.split('.').pop() ?? ''] });
  });
  await page.goto(`${ROBOT}/`);
  await expect(page.locator('input[name="app_url"]')).toHaveValue(config.app_url);
  return {
    save: page.getByRole('button', { name: 'Save and connect' }),
    restart: page.getByRole('button', { name: 'Restart' }),
  };
}

test('a double click on Restart sends one request, and both buttons wait for its answer', async ({ page }) => {
  const answer = held();
  let restarts = 0;
  let current: object = running;
  const { save, restart } = await openSettingsPage(page, async (path, route) => {
    if (path === '/api/restart') restarts += 1;
    await answer.until;
    current = stopping;
    await route.fulfill({ json: stopping });
  }, () => current);
  await restart.dblclick();
  await expect(restart).toBeDisabled();
  await expect(save).toBeDisabled();
  answer.release();
  await expect(restart).toBeEnabled();
  await expect(save).toBeEnabled();
  await expect(page.locator('#state')).toHaveText('Stopping…');
  expect(restarts).toBe(1);
});

test('Save sends one request, even with Enter pressed meanwhile, and both buttons wait for its answer', async ({ page }) => {
  const answer = held();
  let saves = 0;
  const { save, restart } = await openSettingsPage(page, async (path, route) => {
    if (path === '/api/config') saves += 1;
    await answer.until;
    await route.fulfill({ json: { config, status: running } });
  });
  await save.click();
  await expect(save).toBeDisabled();
  await expect(restart).toBeDisabled();
  await page.locator('input[name="app_url"]').press('Enter');
  answer.release();
  await expect(save).toBeEnabled();
  await expect(restart).toBeEnabled();
  expect(saves).toBe(1);
});

test('a request that fails gives both buttons back', async ({ page }) => {
  let restarts = 0;
  const { save, restart } = await openSettingsPage(page, async (_path, route) => {
    restarts += 1;
    await route.abort();
  });
  await restart.click();
  await expect.poll(() => restarts).toBe(1);
  await expect(restart).toBeEnabled();
  await expect(save).toBeEnabled();
  await restart.click();
  await expect.poll(() => restarts).toBe(2);
});
