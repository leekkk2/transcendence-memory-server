import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '../deploy',
  testMatch: 'smoke-test-e2e.spec.js',
  use: {
    headless: true,
    screenshot: 'only-on-failure',
  },
  reporter: 'list',
});
