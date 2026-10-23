import { expect, test, type Browser, type Page } from '@playwright/test'

const API = 'http://localhost:8011'
async function api(path: string, token?: string, method = 'GET', body?: unknown) {
  const r = await fetch(API + path, { method, headers: { 'content-type': 'application/json', ...(token ? { authorization: `Bearer ${token}` } : {}) }, body: body ? JSON.stringify(body) : undefined })
  return r.json()
}
const login = async (role: string, user_id?: string) => (await api('/api/auth/demo-login', undefined, 'POST', { role, user_id })).token as string

async function open(browser: Browser): Promise<Page> {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, geolocation: { latitude: 30.901, longitude: 75.8573, accuracy: 12 }, permissions: ['geolocation'] })
  const page = await ctx.newPage()
  page.on('pageerror', (e) => { throw new Error(`page error: ${e.message}`) })
  return page
}
const enter = async (p: Page, name: string) => { await p.goto('/'); await p.getByRole('button', { name: `Enter as ${name}` }).click() }

test.beforeEach(async () => { await api('/api/admin/reset-demo', await login('admin'), 'POST') })

test('risk and recovery (human-approved mode): a closure raises one risk, keep-current is offered, and the dispatcher approves a reroute', async ({ browser }) => {
  const dt = await login('dispatcher')
  await api('/api/routing/policy', await login('admin'), 'POST', { enabled: false })   // the stable policy would reroute by itself; this test is for the human-approved mode
  const inc = await api('/api/incidents', dt, 'POST', { reported_category: 'cardiac', operator_priority: 1, location: { lat: 30.901, lon: 75.8573, accuracy_m: 10 }, text: 'collapsed' })
  const rec = await api('/api/dispatch/recommend', dt, 'POST', { incident_id: inc.id, autopilot: false })
  await api(`/api/dispatch/${rec.id}/approve`, dt, 'POST', { action: 'approve', reason: 'e2e' })
  const unit = rec.recommendation.vehicle_id as string
  const drv = await login('driver', `driver-${unit}`)
  const asg = (await api('/api/assignments', dt))[0]
  await api(`/api/assignments/${asg.id}/accept`, drv, 'POST')
  await api(`/api/vehicles/${unit}/state`, drv, 'PATCH', { update_id: 'e2e-en', occurred_at: new Date().toISOString(), status: 'en_route', source: 'reported' })
  const plan = await api(`/api/driver/${unit}/plan`, drv)
  const pl = plan.route.polyline as [number, number][]
  const mid = pl[Math.floor(pl.length * 0.6)]
  await api('/api/routing/disruptions', dt, 'POST', { kind: 'closure', lat: mid[0], lon: mid[1], radius_m: 250 })

  const disp = await open(browser)
  await enter(disp, 'Dispatcher')
  await disp.getByRole('link', { name: /Risks/ }).click()
  await disp.getByRole('button', { name: 'Evaluate now' }).click()
  await expect(disp.getByRole('heading', { name: 'route closure' })).toBeVisible()
  await expect(disp.getByText(/the active route to the target is open/).first()).toBeVisible()
  await expect(disp.getByRole('radio', { name: 'Keep current plan' })).toBeVisible()
  await expect(disp.getByRole('radio', { name: 'Reroute' })).toBeChecked()
  await expect(disp.getByRole('button', { name: /Approve: reroute/ })).toBeDisabled()
  await disp.getByLabel('Reason (required)').fill('road closed ahead')
  await disp.getByRole('button', { name: /Approve: reroute/ }).click()
  await expect(disp.getByText(/Approved\. Waiting for the driver/)).toBeVisible()
  // the driver still has to tap
  const after = await api(`/api/driver/${unit}/plan`, drv)
  expect(after.reroute.status).toBe('pending')
  // evaluating again does not raise the same risk twice
  await disp.getByRole('button', { name: 'Evaluate now' }).click()
  await expect(disp.getByText(/No new risks/)).toBeVisible()
})

test('mass casualty: set triage colours, plan, approve', async ({ browser }) => {
  const dt = await login('dispatcher')
  await api('/api/incidents', dt, 'POST', { reported_category: 'trauma', operator_priority: 1, location: { lat: 30.901, lon: 75.8573, accuracy_m: 10 }, text: 'bus collision' })
  const disp = await open(browser)
  await enter(disp, 'Dispatcher')
  await disp.getByRole('link', { name: /Mass casualty/ }).click()
  await disp.getByLabel('Patients').fill('4')
  await disp.getByRole('button', { name: 'Open mass-casualty record' }).click()
  await expect(disp.getByText('P-04')).toBeVisible()
  await expect(disp.getByText('planned as red').first()).toBeVisible() // unknown is not minor
  for (const [p, c] of [['P-01', 'red'], ['P-02', 'amber'], ['P-03', 'green'], ['P-04', 'black']]) await disp.getByRole('button', { name: `${p} ${c}` }).click()
  await expect(disp.getByRole('button', { name: 'P-01 red' })).toHaveAttribute('aria-pressed', 'true')
  await disp.getByRole('button', { name: 'Plan split and units' }).click()
  await expect(disp.getByText('Hospital load')).toBeVisible()
  await expect(disp.getByText('Ambulance loads')).toBeVisible()
  await expect(disp.getByText(/OR-Tools cost/).first()).toBeVisible()
  const approve = disp.getByRole('button', { name: 'Approve plan' })
  await expect(approve).toBeDisabled()
  await disp.getByLabel(/Reason for approving/).fill('reviewed on scene')
  await approve.click()
  await expect(disp.getByText(/Approved by/)).toBeVisible()
  const asg = await api('/api/assignments', dt)
  expect(asg.length).toBeGreaterThanOrEqual(3)
  expect(asg.every((a: { state: string }) => a.state === 'sent')).toBe(true) // sent, not accepted
})

test('command center demand layer shows a labelled forecast and standby moves', async ({ browser }) => {
  const disp = await open(browser)
  await enter(disp, 'Dispatcher')
  await disp.getByRole('button', { name: 'Demand', exact: true }).click()
  const panel = disp.getByLabel('Pre-positioning')
  await expect(panel.getByText('prototype approximation')).toBeVisible()
  await expect(panel.getByText('Coverage now')).toBeVisible()
  await expect(panel.getByText('After standby moves')).toBeVisible()
  await expect(panel.getByText(/synthetic history of 700 incidents/)).toBeVisible()
})

test('replay lab: run a seed, scrub the timeline, compare P0 with P2, export, benchmark', async ({ browser }) => {
  const disp = await open(browser)
  await enter(disp, 'Dispatcher')
  await disp.getByRole('link', { name: /Replay lab/ }).click()
  await expect(disp.getByRole('note').filter({ hasText: 'Results describe' })).toContainText(/simulated/i)
  await disp.getByLabel('Seed', { exact: true }).fill('3')
  await disp.getByRole('button', { name: 'Run simulation' }).click()
  await expect(disp.getByText(/SIM-0001 · seed 3/)).toBeVisible()
  await expect(disp.getByLabel('Left policy')).toHaveValue('P0')
  await expect(disp.getByLabel('Right policy')).toHaveValue('P2')
  await expect(disp.getByRole('region', { name: 'P0 baseline map' })).toBeVisible()
  await expect(disp.getByRole('region', { name: 'P2 ResQGrid map' })).toBeVisible()
  await disp.getByLabel('Timeline scrubber').fill('3000')
  await expect(disp.getByRole('list', { name: 'P0 event log' }).getByRole('listitem').first()).toBeVisible()
  await expect(disp.getByText('Response time, median').first()).toBeVisible()
  const [dl] = await Promise.all([disp.waitForEvent('download'), disp.getByRole('button', { name: 'CSV metrics' }).click()])
  expect(dl.suggestedFilename()).toContain('SIM-0001')
  await disp.getByLabel('Benchmark seeds').fill('3')
  await disp.getByRole('button', { name: 'Run benchmark' }).click()
  await expect(disp.getByText(/Benchmark · 3 seeds/)).toBeVisible({ timeout: 60_000 })
  await expect(disp.getByText('Where ResQGrid (P2) is worse than the baseline')).toBeVisible()
})
