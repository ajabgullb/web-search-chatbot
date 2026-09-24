import { defineConfig, devices } from '@playwright/test'

/**
 * End-to-end tests against the real app with scripted LLM and search
 * providers (backend/tests/e2e_server.py). Playwright starts both servers on
 * their own ports, so a running development stack is left alone.
 */
const API_PORT = 8010
const WEB_PORT = 5174
const WEB_ORIGIN = `http://localhost:${WEB_PORT}`
const CI = Boolean(process.env.CI)

export default defineConfig({
  testDir: './tests/e2e',
  // Specs register their own users, so they can run side by side.
  fullyParallel: true,
  forbidOnly: CI,
  retries: CI ? 1 : 0,
  workers: CI ? 2 : undefined,
  reporter: CI ? [['github'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: WEB_ORIGIN,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] }, testIgnore: /mobile\.spec\.ts/ },
    { name: 'mobile', use: { ...devices['Pixel 7'] }, testMatch: /mobile\.spec\.ts/ },
  ],
  webServer: [
    {
      command: `uv run uvicorn tests.e2e_server:app --host 127.0.0.1 --port ${API_PORT}`,
      cwd: '../backend',
      url: `http://127.0.0.1:${API_PORT}/api/v1/health/ready`,
      env: { E2E_FRONTEND_ORIGIN: WEB_ORIGIN },
      reuseExistingServer: !CI,
      timeout: 120_000,
    },
    {
      command: `npx vite --port ${WEB_PORT} --strictPort`,
      url: WEB_ORIGIN,
      env: { VITE_API_PROXY_TARGET: `http://127.0.0.1:${API_PORT}` },
      reuseExistingServer: !CI,
      timeout: 60_000,
    },
  ],
})
