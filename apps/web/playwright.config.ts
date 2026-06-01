import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './src/e2e',
  webServer: {
    command: 'npm run dev -- --port 5173',
    url: 'http://127.0.0.1:5173',
    reuseExistingServer: true
  },
  use: {
    baseURL: 'http://127.0.0.1:5173'
  }
});
