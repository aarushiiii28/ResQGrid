import { expect, test, type Browser, type Page } from '@playwright/test'

const API = 'http://localhost:8011'

async function api(path: string, token?: string, method = 'GET', body?: unknown) {
  const r = await fetch(API + path, {
    method,
    headers: { 'content-type': 'application/json', ...(token ? { authorization: `Bearer ${token}` } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  })
  return r.json()
}
const login = async (role: string) => (await api('/api/auth/demo-login', undefined, 'POST', { role })).token as string

async function open(browser: Browser): Promise<Page> {
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    geolocation: { latitude: 30.901, longitude: 75.8573, accuracy: 12 },
    permissions: ['geolocation'],
  })
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

/** Press and hold the SOS control for longer than the (test-shortened) hold. */
async function holdSos(p: Page) {
  const btn = p.getByRole('button', { name: /SOS/ })
  const box = (await btn.boundingBox())!
  await p.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await p.mouse.down()
  await p.waitForTimeout(800)
  await p.mouse.up()
}

test.beforeEach(async () => {
  await api('/api/admin/reset-demo', await login('admin'), 'POST')
})

test('an accidental press: a quick tap does nothing, a cancelled countdown is kept and checked in on', async ({ browser }) => {
  const caller = await open(browser)
  await enter(caller, 'Caller')
  const dt = await login('dispatcher')
  await caller.getByRole('button', { name: /SOS/ }).click() // a tap, not a hold
  await caller.waitForTimeout(500)
  expect(((await api('/api/incidents', dt)) as unknown[]).length).toBe(0)

  // hold, then cancel during the countdown
  const btn = caller.getByRole('button', { name: /SOS/ })
  const box = (await btn.boundingBox())!
  await caller.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await caller.mouse.down()
  await caller.waitForTimeout(800)
  await caller.mouse.up()
  await expect(caller.getByTestId('countdown-number')).toBeVisible()
  const pending = (await api('/api/incidents', dt)) as { id: string; status: string }[]
  expect(pending.map((i) => i.status)).toEqual(['pending'])         // visible to dispatchers, not dispatched
  await caller.getByRole('button', { name: 'Cancel' }).click()
  await expect(caller.getByText('SOS cancelled')).toBeVisible()
  await expect(caller.getByRole('button', { name: 'I am safe' })).toBeVisible()
  const after = (await api('/api/incidents', dt)) as { id: string; status: string; assigned_vehicle_id: string | null }[]
  expect(after).toHaveLength(1)                                      // nothing was deleted
  expect(after[0]).toMatchObject({ status: 'cancelled_by_caller', assigned_vehicle_id: null })
  await caller.getByRole('button', { name: 'I am safe' }).click()
  await expect(caller.getByText(/You told us you are safe/)).toBeVisible()
})

test('SOS to handover: caller, dispatcher, driver and hospital in a real browser', async ({ browser }) => {
  // ---- caller: one press, then tap three answers ----------------------------------
  const caller = await open(browser)
  await enter(caller, 'Caller')
  await expect(caller.getByText('In a real emergency dial 112.')).toBeVisible()
  await holdSos(caller)
  await expect(caller.getByText('Call in progress')).toBeVisible()
  await expect(caller.getByTestId('headline')).toBeVisible()
  await caller.getByRole('button', { name: 'Chest pain' }).click()
  await caller.getByRole('button', { name: 'Not responding and not breathing' }).click()
  await caller.getByRole('button', { name: 'One person' }).click()
  await expect(caller.getByText(/Stay on the line/).first()).toBeVisible()
  // the ambulance is sent without the caller doing anything else
  await expect(caller.getByTestId('headline')).toContainText(/Ambulance sent|Ambulance accepted/)

  const dt = await login('dispatcher')
  let inc: { id: string; assigned_vehicle_id: string; destination_id: string } | undefined
  await expect
    .poll(async () => {
      inc = (await api('/api/incidents', dt))[0]
      return inc?.assigned_vehicle_id ?? null
    })
    .not.toBeNull()
  const { id, assigned_vehicle_id: unit, destination_id: hospital } = inc!

  // ---- dispatcher: sees it, with the simulated autopilot approval labelled --------------
  const disp = await open(browser)
  await enter(disp, 'Dispatcher')
  await expect(disp.getByRole('complementary', { name: 'Incident queue' }).getByText(id)).toBeVisible()
  await disp.getByRole('tab', { name: /dispatch compare/i }).click()
  await expect(disp.getByText('Ranked unit / destination pairs')).toBeVisible()
  await expect(disp.getByText('demo autopilot (simulated)')).toBeVisible()
  await expect(disp.getByText(unit)).not.toHaveCount(0)

  // ---- driver: accept, en route, drive, on scene, transport, arrive ---------------------------
  const driver = await open(browser)
  await driver.goto('/')
  await driver.getByRole('combobox', { name: 'Choose unit' }).selectOption(`driver-${unit}`)
  await driver.getByRole('button', { name: 'Enter as Driver' }).click()
  await expect(driver.getByText(new RegExp(`New assignment · ${id}`))).toBeVisible()
  await expect(driver.getByText(/Opening this job is not accepting it/)).toBeVisible()
  await driver.getByRole('button', { name: 'Accept job' }).click()
  await expect(driver.getByRole('button', { name: /En route/ })).toBeEnabled()
  await expect(driver.getByRole('button', { name: /Transporting/ })).toBeDisabled()
  await driver.getByRole('button', { name: /En route/ }).click()
  await driver.getByRole('button', { name: /Simulate movement along route/ }).click()
  await expect(driver.getByText('Arriving')).toBeVisible({ timeout: 60_000 })
  await driver.getByRole('button', { name: /On scene/ }).click()
  await driver.getByRole('button', { name: /Transporting/ }).click()
  await expect(driver.getByText(/To hospital/i)).toBeVisible()
  await driver.getByRole('button', { name: /Simulate movement along route/ }).click()
  await expect(driver.getByText('Arriving')).toBeVisible({ timeout: 60_000 })
  await driver.getByRole('button', { name: /Arrived/ }).click()
  await expect(driver.getByText(/Waiting for the hospital to complete handover/)).toBeVisible()

  // ---- caller sees the arrival in plain text --------------------------------------------------
  await expect(caller.getByTestId('headline')).toContainText(/Arrived at/, { timeout: 20_000 })

  // ---- hospital: pre-alert, acknowledge, handover ---------------------------------------------
  const hosp = await open(browser)
  await hosp.goto('/')
  await hosp.getByRole('combobox', { name: 'Choose hospital' }).selectOption(`hosp-${hospital}`)
  await hosp.getByRole('button', { name: 'Enter as Hospital' }).click()
  const list = hosp.getByRole('complementary', { name: 'Incoming pre-alerts' })
  await expect(list.getByText(id)).toBeVisible()
  await expect(hosp.getByRole('button', { name: /Handover started/ })).toBeEnabled() // arrival came from the driver
  await hosp.getByRole('button', { name: /Acknowledge/ }).click()
  await expect(hosp.getByRole('button', { name: /✓ Acknowledge/ })).toBeDisabled()
  await hosp.getByRole('button', { name: /Handover started/ }).click()
  await hosp.getByRole('button', { name: /Handover complete/ }).click()
  await expect(hosp.getByText('handover complete').first()).toBeVisible()

  // ---- everything closed, unit released, caller told ---------------------------------------------
  await expect(caller.getByTestId('headline')).toContainText(/Handover complete/, { timeout: 20_000 })
  const vehicles = (await api('/api/vehicles', dt)) as { id: string; status: string }[]
  expect(vehicles.find((v) => v.id === unit)?.status).toBe('available')

  // ---- the passport shows the whole story with actors --------------------------------------------
  await disp.goto(`/audit/${id}`)
  for (const t of ['Incident · pending created', 'Incident · went live', 'False alarm · assessed', 'Dispatch · approved', 'Assignment · accepted', 'Prealert · handover completed', 'Incident · closed']) {
    await expect(disp.getByText(t).first()).toBeVisible()
  }
  await expect(disp.getByText('demo-autopilot').first()).toBeVisible()
  await expect(disp.getByRole('button', { name: 'Export CSV' })).toBeEnabled()
})

test('a road closure reroutes the driver at once (same destination), tells the dispatcher, and the driver only acknowledges', async ({ browser }) => {
  const dt = await login('dispatcher')
  const created = await api('/api/incidents', dt, 'POST', {
    reported_category: 'cardiac',
    operator_priority: 1,
    location: { lat: 30.901, lon: 75.8573, accuracy_m: 10 },
    text: 'collapsed',
  })
  const rec = await api('/api/dispatch/recommend', dt, 'POST', { incident_id: created.id, autopilot: false })
  await api(`/api/dispatch/${rec.id}/approve`, dt, 'POST', { action: 'approve', reason: 'e2e' })
  const unit = rec.recommendation.vehicle_id as string

  const driver = await open(browser)
  await driver.goto('/')
  await driver.getByRole('combobox', { name: 'Choose unit' }).selectOption(`driver-${unit}`)
  await driver.getByRole('button', { name: 'Enter as Driver' }).click()
  await driver.getByRole('button', { name: 'Accept job' }).click()
  await driver.getByRole('button', { name: /En route/ }).click()
  await expect(driver.getByText('Turn-by-turn').or(driver.getByLabel('Turn by turn'))).toBeVisible()

  const drvToken = (await api('/api/auth/demo-login', undefined, 'POST', { user_id: `driver-${unit}` })).token as string
  const plan = await api(`/api/driver/${unit}/plan`, drvToken)
  const pl = plan.route.polyline as [number, number][]
  const mid = pl[Math.floor(pl.length * 0.6)]
  const before = plan.route.polyline
  await expect(driver.getByText('route locked')).toBeVisible()
  await api('/api/routing/disruptions', dt, 'POST', { kind: 'closure', lat: mid[0], lon: mid[1], radius_m: 250 })

  const banner = driver.getByLabel('Route changed')
  await expect(banner).toBeVisible()
  await expect(banner).toContainText(/Road closed ahead/)
  await expect(banner).toContainText(/Same destination/)
  await expect(driver.getByRole('button', { name: 'Take new route' })).toHaveCount(0)           // not an approval
  const after = (await api(`/api/driver/${unit}/plan`, drvToken)).route.polyline                    // already applied
  expect(JSON.stringify(after)).not.toBe(JSON.stringify(before))
  const alerts = (await api('/api/alerts', dt)) as { kind: string }[]
  expect(alerts.some((a) => a.kind === 'route_changed')).toBe(true)                                 // the dispatcher is told
  await banner.getByRole('button', { name: 'Got it' }).click()
  await expect(driver.getByLabel('Route changed')).toHaveCount(0)
  const inc = (await api(`/api/incidents/${created.id}`, dt)).incident as { assigned_vehicle_id: string }
  expect(inc.assigned_vehicle_id).toBe(unit)                                                         // vehicle unchanged
})
