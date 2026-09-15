import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests', testMatch: 'full-admin.spec.js', workers: 1, timeout: 60_000,
  use: { headless: true, viewport: { width: 1440, height: 1000 }, screenshot: 'only-on-failure' },
  reporter: [['list'], ['json', { outputFile: '.local/full-admin-browser.json' }]],
});
