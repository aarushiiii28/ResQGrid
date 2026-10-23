import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { defineConfig } from '@playwright/test'

// Real browser, real API, throw-away test database. Simulated crew is OFF so the test taps every
// driver and hospital button itself; ambulance movement is sped up.
function testDb(): string {
  try {
    const env = readFileSync(resolve(import.meta.dirname, '../.env'), 'utf8')
    const m = env.match(/^TEST_DATABASE_URL=(.*)$/m)
    if (m) return m[1].trim()
  } catch {
    /* no .env: fall back to the default below */
  }
  return process.env.TEST_DATABASE_URL ?? 'postgresql://postgres:postgres@localhost:5432/resqgrid_test'
}

const API_PORT = 8011
const WEB_PORT = 5181

export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  expect: { timeout: 15_000 },
  workers: 1,
  fullyParallel: false,
  reporter: [['list']],
  use: {
    baseURL: `http://localhost:${WEB_PORT}`,
    viewport: { width: 1440, height: 900 },
    geolocation: { latitude: 30.901, longitude: 75.8573, accuracy: 12 },
    permissions: ['geolocation'],
    trace: 'retain-on-failure',
  },
  webServer: [
    {
      command: `"${resolve(import.meta.dirname, '../venv/Scripts/python.exe')}" "${resolve(import.meta.dirname, '../scripts/start_backend.py')}" ${API_PORT}`,
      url: `http://localhost:${API_PORT}/health`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        DATABASE_URL: testDb(),
        SIM_DRIVER: 'false',
        SIM_VOLUNTEERS: 'false',
        SIM_SPEED_FACTOR: '300',
        SIM_TICK_S: '0.25',
        DEMO_AUTOPILOT_DELAY_S: '0.5',
        INTAKE_WAIT_S: '8', // wait for the caller's description before choosing an ambulance (25 s by default)
        FA_HOLD_MS: '400', // the real hold is 2 s and the countdown 10 s; shortened so the browser test is quick
        FA_COUNTDOWN_S: '1.5',
        SIMULATED_HEARTBEAT: 'true',
      },
    },
    {
      command: `npx vite --port ${WEB_PORT} --strictPort`,
      url: `http://localhost:${WEB_PORT}`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: { RESQGRID_API: `http://localhost:${API_PORT}` },
    },
  ],
})
