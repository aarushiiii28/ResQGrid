import { expect, test, type Browser, type Page } from '@playwright/test'

const API = 'http://localhost:8011'

async function api(path: string, token?: string, method = 'GET', body?: unknown) {
  const r = await fetch(API + path, { method, headers: { 'content-type': 'application/json', ...(token ? { authorization: `Bearer ${token}` } : {}) }, body: body ? JSON.stringify(body) : undefined })
  return r.json()
}
const login = async (role: string, userId?: string) => (await api('/api/auth/demo-login', undefined, 'POST', userId ? { user_id: userId } : { role })).token as string

async function open(browser: Browser): Promise<Page> {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, geolocation: { latitude: 30.901, longitude: 75.8573, accuracy: 12 }, permissions: ['geolocation'] })
  const page = await ctx.newPage()
  page.on('pageerror', (e) => {
    throw new Error(`page error: ${e.message}`)
  })
  return page
}
const enter = async (p: Page, name: string) => {
  await p.goto('/')
  await p.getByRole('button', { name: `Enter as ${name}` }).click()
}

test.beforeEach(async () => {
  await api('/api/admin/reset-demo', await login('admin'), 'POST')
})

test('signed SOS, receipt check, Verify Integrity passes, and the tamper demo is detected on a copy', async ({ browser }) => {
  const dt = await login('dispatcher')
  const caller = await open(browser)
  await enter(caller, 'Caller')
  const ct = await login('caller')
  await expect.poll(async () => ((await api('/api/keys/me', ct)) as unknown[]).length, { message: 'the device key is registered after login' }).toBeGreaterThan(0)

  // press and hold, countdown, live
  const btn = caller.getByRole('button', { name: /SOS/ })
  const box = (await btn.boundingBox())!
  await caller.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await caller.mouse.down()
  await caller.waitForTimeout(800)
  await caller.mouse.up()
  await expect(caller.getByTestId('countdown-number')).toBeVisible()
  await expect(caller.getByText('Call in progress')).toBeVisible({ timeout: 15_000 })

  // the caller's actions are signed on the device and the server verified them
  const incs = (await api('/api/incidents', dt)) as { id: string }[]
  const id = incs[0].id
  const events = (await api(`/api/incidents/${id}/audit`, dt)) as { event_type: string; ledger: { client_signed: boolean; ledger_status: string } }[]
  expect(events.find((e) => e.event_type === 'incident.pending_created')?.ledger.client_signed).toBe(true)
  expect(events.find((e) => e.event_type === 'incident.went_live')?.ledger.client_signed).toBe(true)

  // the receipt kept on the phone verifies offline
  await caller.getByRole('link', { name: 'Receipts' }).click()
  await expect(caller.getByText(/\d+ saved/)).toBeVisible()
  await caller.getByRole('button', { name: 'Fetch from server' }).click()
  await caller.getByRole('button', { name: 'Verify offline' }).click()
  await expect(caller.getByTestId('receipt-result')).toContainText('SIGNATURE VALID')

  // anchor, then dispatcher verifies the whole ledger
  await api('/api/ledger/anchor', await login('admin'), 'POST')
  const disp = await open(browser)
  await enter(disp, 'Dispatcher')
  await disp.getByRole('link', { name: 'Integrity' }).click()
  await expect(disp.getByText('local anchor, not a public blockchain').first()).toBeVisible()
  await disp.getByRole('button', { name: 'Verify integrity' }).click()
  await expect(disp.getByTestId('verify-result')).toContainText('VERIFIED', { timeout: 20_000 })
  await expect(disp.getByTestId('verify-result')).toContainText(/signed on devices/)

  // tamper demo: runs on a copy, detected, real ledger unchanged
  await disp.getByRole('button', { name: 'Delete an entry' }).click()
  const out = disp.getByTestId('tamper-result')
  await expect(out).toContainText('demonstration on a copy')
  await expect(out).toContainText('detected')
  await expect(out).toContainText('real ledger unchanged')
  await disp.getByRole('button', { name: 'Verify integrity' }).click()
  await expect(disp.getByTestId('verify-result')).toContainText('VERIFIED', { timeout: 20_000 })

  // the passport shows SIGNED or ANCHORED stamps per event
  await disp.goto(`/audit/${id}`)
  await expect(disp.getByText('ANCHORED').first()).toBeVisible()
})

test('wrong turn: the stable-route policy rejoins on its own and the driver only acknowledges', async ({ browser }) => {
  const dt = await login('dispatcher')
  const created = await api('/api/incidents', dt, 'POST', { reported_category: 'cardiac', operator_priority: 1, location: { lat: 30.901, lon: 75.8573, accuracy_m: 10 }, text: 'collapsed' })
  const rec = await api('/api/dispatch/recommend', dt, 'POST', { incident_id: created.id, autopilot: false })
  await api(`/api/dispatch/${rec.id}/approve`, dt, 'POST', { action: 'approve', reason: 'e2e' })
  const unit = rec.recommendation.vehicle_id as string
  const drvToken = await login('driver', `driver-${unit}`)

  const driver = await open(browser)
  await driver.goto('/')
  await driver.getByRole('combobox', { name: 'Choose unit' }).selectOption(`driver-${unit}`)
  await driver.getByRole('button', { name: 'Enter as Driver' }).click()
  await driver.getByRole('button', { name: 'Accept job' }).click()
  await driver.getByRole('button', { name: /En route/ }).click()
  await expect(driver.getByText('route locked')).toBeVisible()

  // three real-device style fixes, 220 m beside the route, with good accuracy
  const plan = await api(`/api/driver/${unit}/plan`, drvToken)
  const pl = plan.route.polyline as [number, number][]
  const base = pl[Math.floor(pl.length * 0.3)]
  for (let i = 0; i < 3; i++) {
    await api(`/api/vehicles/${unit}/state`, drvToken, 'PATCH', {
      update_id: `wt-${i}-${Date.now()}`,
      occurred_at: new Date().toISOString(),
      location: { lat: base[0] + 0.0025, lon: base[1] + 0.0025, accuracy_m: 8 },
      source: 'measured',
    })
  }
  const banner = driver.getByLabel('Route changed')
  await expect(banner).toBeVisible({ timeout: 20_000 })
  await expect(banner).toContainText(/Off route/)
  await expect(driver.getByRole('button', { name: 'Take new route' })).toHaveCount(0)
  const after = await api(`/api/driver/${unit}/plan`, drvToken)
  expect(after.route_lock.reroutes).toBe(1)
  await banner.getByRole('button', { name: 'Got it' }).click()
  await expect(driver.getByLabel('Route changed')).toHaveCount(0)
})
