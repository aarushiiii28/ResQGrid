// Dev helper: drives the real UI through the whole flow and saves screenshots.  node scripts/flow-shots.mjs <outdir>
import { chromium } from '@playwright/test'
import { mkdirSync } from 'node:fs'
const out = process.argv[2] ?? 'shots'
mkdirSync(out, { recursive: true })
const base = process.env.BASE ?? 'http://localhost:5180'
const api = async (path, token, method = 'GET', body) => {
  const r = await fetch(base + path, { method, headers: { 'content-type': 'application/json', ...(token ? { authorization: `Bearer ${token}` } : {}) }, body: body ? JSON.stringify(body) : undefined })
  return r.json()
}
const adminTok = (await api('/api/auth/demo-login', null, 'POST', { role: 'admin' })).token
await api('/api/admin/reset-demo', adminTok, 'POST')
const b = await chromium.launch()
const mk = async (extra = {}) => {
  const c = await b.newContext({ viewport: { width: 1440, height: 900 }, geolocation: { latitude: 30.901, longitude: 75.8573, accuracy: 12 }, permissions: ['geolocation'], ...extra })
  const p = await c.newPage()
  p.on('pageerror', (e) => console.log('PAGEERROR', e.message))
  p.on('console', (m) => { if (m.type() === 'error' && !/tile|ERR_|Failed to load resource/.test(m.text())) console.log('CONSOLE', m.text()) })
  return p
}
const enter = async (p, name) => { await p.goto(base); await p.getByRole('button', { name: `Enter as ${name}` }).click() }

const caller = await mk()
await enter(caller, 'Caller')
await caller.getByRole('button', { name: /SOS/ }).click()
await caller.getByTestId('headline').waitFor()
await caller.waitForTimeout(2500)
await caller.screenshot({ path: `${out}/10-caller-sent.png`, fullPage: true })

const disp = await mk()
await enter(disp, 'Dispatcher')
await disp.waitForURL('**/command')
await disp.waitForTimeout(3500)
await disp.screenshot({ path: `${out}/20-command.png` })
await disp.getByRole('tab', { name: /dispatch compare/i }).click()
await disp.waitForTimeout(800)
await disp.screenshot({ path: `${out}/21-command-compare.png` })
await disp.getByRole('tab', { name: /timeline/i }).click()
await disp.waitForTimeout(800)
await disp.screenshot({ path: `${out}/22-command-timeline.png` })

const dt = (await api('/api/auth/demo-login', null, 'POST', { role: 'dispatcher' })).token
const inc = (await api('/api/incidents', dt))[0]
console.log('incident', inc.id, inc.assigned_vehicle_id, inc.destination_id)

const drv = await mk()
await drv.goto(base)
await drv.getByRole('combobox', { name: 'Choose unit' }).selectOption(`driver-${inc.assigned_vehicle_id}`)
await drv.getByRole('button', { name: 'Enter as Driver' }).click()
await drv.waitForURL('**/driver')
await drv.getByRole('button', { name: 'Accept job' }).waitFor()
await drv.screenshot({ path: `${out}/30-driver-assignment.png`, fullPage: true })
await drv.getByRole('button', { name: 'Accept job' }).click()
await drv.getByRole('button', { name: /^En route/ }).click()
await drv.getByRole('button', { name: /Simulate movement/ }).click()
await drv.waitForTimeout(6000)
await drv.screenshot({ path: `${out}/31-driver-navigating.png`, fullPage: true })

await disp.getByRole('tab', { name: /^incident/i }).click()
await disp.waitForTimeout(500)
await disp.screenshot({ path: `${out}/23-command-moving.png` })

const hos = await mk()
await hos.goto(base)
await hos.getByRole('combobox', { name: 'Choose hospital' }).selectOption(`hosp-${inc.destination_id}`)
await hos.getByRole('button', { name: 'Enter as Hospital' }).click()
await hos.waitForURL('**/hospital')
await hos.waitForTimeout(2500)
await hos.screenshot({ path: `${out}/40-hospital.png`, fullPage: true })

await disp.goto(`${base}/audit/${inc.id}`)
await disp.waitForTimeout(1500)
await disp.screenshot({ path: `${out}/50-audit.png`, fullPage: true })
await b.close()
