import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: 'tests',
  testMatch: '*.spec.ts',
  timeout: 10_000,
  use: {
    baseURL: 'http://localhost:8889',
  },
  webServer: {
    command: 'python3 -m http.server 8889',
    port: 8889,
    reuseExistingServer: true,
  },
});
