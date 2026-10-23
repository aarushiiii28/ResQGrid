// Dev helper: screenshots of key screens for design review.  node scripts/shots.mjs <outdir>
import { chromium } from '@playwright/test'
import { mkdirSync } from 'node:fs'
const out = process.argv[2] ?? 'shots'
mkdirSync(out, { recursive: true })
const base = process.env.BASE ?? 'http://localhost:5180'
const b = await chromium.launch()
const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, geolocation: { latitude: 30.901, longitude: 75.8573, accuracy: 12 }, permissions: ['geolocation'] })
const p = await ctx.newPage()
p.on('pageerror', (e) => console.log('PAGEERROR', e.message))
p.on('console', (m) => { if (m.type() === 'error') console.log('CONSOLE', m.text()) })
await p.goto(base)
await p.waitForTimeout(1500)
await p.screenshot({ path: `${out}/01-landing.png`, fullPage: false })
await p.screenshot({ path: `${out}/01b-landing-full.png`, fullPage: true })
await p.getByRole('button', { name: 'Enter as Caller' }).click()
await p.waitForURL('**/caller')
await p.waitForTimeout(800)
await p.screenshot({ path: `${out}/02-caller-idle.png` })
await p.getByRole('button', { name: /SOS/ }).click()
await p.waitForTimeout(4000)
await p.screenshot({ path: `${out}/03-caller-live.png`, fullPage: true })
await b.close()
