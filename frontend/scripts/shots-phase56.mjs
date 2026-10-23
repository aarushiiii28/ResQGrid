import { chromium } from '@playwright/test'
import { mkdirSync } from 'node:fs'
const out = process.argv[2] ?? 'shots'
mkdirSync(out, { recursive: true })
const base = 'http://localhost:5180'
const api = async (path, token, method = 'GET', body) => (await fetch(base + path, { method, headers: { 'content-type': 'application/json', ...(token ? { authorization: `Bearer ${token}` } : {}) }, body: body ? JSON.stringify(body) : undefined })).json()
const login = async (role, user_id) => (await api('/api/auth/demo-login', null, 'POST', { role, user_id })).token
await api('/api/admin/reset-demo', await login('admin'), 'POST')
const dt = await login('dispatcher')
const inc = await api('/api/incidents', dt, 'POST', { reported_category: 'cardiac', operator_priority: 1, location: { lat: 30.901, lon: 75.8573, accuracy_m: 10 }, text: 'collapsed' })
const rec = await api('/api/dispatch/recommend', dt, 'POST', { incident_id: inc.id, autopilot: false })
await api(`/api/dispatch/${rec.id}/approve`, dt, 'POST', { action: 'approve', reason: 'shots' })
const unit = rec.recommendation.vehicle_id
const drv = await login('driver', `driver-${unit}`)
const asg = (await api('/api/assignments', dt))[0]
await api(`/api/assignments/${asg.id}/accept`, drv, 'POST')
await api(`/api/vehicles/${unit}/state`, drv, 'PATCH', { update_id: 'x1', occurred_at: new Date().toISOString(), status: 'en_route', source: 'reported' })
const plan = await api(`/api/driver/${unit}/plan`, drv)
const pl = plan.route.polyline
const mid = pl[Math.floor(pl.length * 0.6)]
await api('/api/routing/disruptions', dt, 'POST', { kind: 'closure', lat: mid[0], lon: mid[1], radius_m: 250 })
await api('/api/incidents', dt, 'POST', { reported_category: 'trauma', operator_priority: 1, location: { lat: 30.905, lon: 75.86, accuracy_m: 10 }, text: 'bus collision' })
const b = await chromium.launch()
const ctx = await b.newContext({ viewport: { width: 1440, height: 900 } })
const p = await ctx.newPage()
p.on('pageerror', (e) => console.log('PAGEERROR', e.message))
await p.goto(base); await p.getByRole('button', { name: 'Enter as Dispatcher' }).click()
await p.getByRole('button', { name: 'Demand', exact: true }).click(); await p.waitForTimeout(4000)
await p.screenshot({ path: `${out}/60-command-demand.png` })
await p.getByRole('link', { name: /Risks/ }).click(); await p.getByRole('button', { name: 'Evaluate now' }).click(); await p.waitForTimeout(1500)
await p.screenshot({ path: `${out}/61-risks.png`, fullPage: true })
await p.getByRole('link', { name: /Mass casualty/ }).click(); await p.getByLabel('Patients').fill('8'); await p.getByRole('button', { name: 'Open mass-casualty record' }).click()
for (const [i, c] of ['red','red','red','amber','amber','amber','green','black'].entries()) await p.getByRole('button', { name: `P-0${i+1} ${c}` }).click()
await p.getByRole('button', { name: 'Plan split and units' }).click(); await p.waitForTimeout(2000)
await p.screenshot({ path: `${out}/62-mci.png`, fullPage: true })
await p.getByRole('link', { name: /Replay lab/ }).click(); await p.getByRole('button', { name: 'Run simulation' }).click(); await p.waitForTimeout(2500)
await p.getByLabel('Timeline scrubber').fill('2400'); await p.waitForTimeout(2500)
await p.screenshot({ path: `${out}/63-replay.png`, fullPage: true })
await p.getByLabel('Benchmark seeds').fill('12'); await p.getByRole('button', { name: 'Run benchmark' }).click(); await p.waitForTimeout(9000)
await p.screenshot({ path: `${out}/64-replay-bench.png`, fullPage: true })
await b.close()
