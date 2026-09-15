import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './browser',
  testMatch: '**/*.pw.ts',
  timeout: 90000,
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:5184',
    channel: 'msedge',
    headless: true,
    viewport: { width: 1440, height: 1000 },
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5184 --strictPort',
    url: 'http://127.0.0.1:5184',
    reuseExistingServer: false,
    timeout: 60000,
  },
});