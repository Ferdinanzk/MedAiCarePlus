import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: '../playwright_test',
  timeout: 30_000,
  expect: {
    timeout: 10_000,
  },
  use: {
    baseURL: 'http://localhost:8000',
    ...devices['Pixel 5'],
  },
  reporter: 'list',
  workers: 1,
});
