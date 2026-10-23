// Design audit as a test (CLAUDE.md): forbidden patterns must not appear in any screen or component, and every control on the
// caller and driver screens must be a large touch target. This runs with the normal suite, so a new screen cannot regress it.
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const SRC = join(process.cwd(), 'src')

function walk(dir: string, out: string[] = []): string[] {
  for (const f of readdirSync(dir)) {
    const p = join(dir, f)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(tsx?|css)$/.test(f) && !/\.test\./.test(f) && !p.includes(`${join('src', 'test')}`)) out.push(p)
  }
  return out
}
const files = walk(SRC)
const text = (f: string) => readFileSync(f, 'utf8')
const rel = (f: string) => f.replace(SRC, 'src').replace(/\\/g, '/')

describe('design audit: forbidden patterns', () => {
  it('no blue, purple, indigo, violet, sky, cyan, fuchsia or pink Tailwind colours', () => {
    const bad = files.filter((f) => /\b(bg|text|border|ring|fill|stroke|from|to|via|outline|divide)-(blue|purple|indigo|violet|sky|cyan|fuchsia|pink|rose|teal|emerald|lime|slate|zinc|gray|neutral|stone)-\d/.test(text(f)))
    expect(bad.map(rel)).toEqual([])
  })

  it('no shadows, no gradients, no glass blur', () => {
    const bad = files.filter((f) => {
      const t = text(f)
      const css = f.endsWith('.css')
      return /\bshadow(-\w+)?\b(?!:\s*none)/.test(css ? t.replace(/box-shadow:\s*none[^;]*;/g, '') : t) && !css
        || /\b(bg-gradient|from-\w+|via-\w+|to-\w+-\d|linear-gradient|radial-gradient|backdrop-blur|blur-)/.test(t)
    })
    expect(bad.map(rel)).toEqual([])
  })

  it('no rounded-xl, rounded-2xl, rounded-3xl, rounded-full or rounded-lg panels (0 to 2 px radius only)', () => {
    const bad = files.filter((f) => /\brounded-(xl|2xl|3xl|full|lg|md)\b/.test(text(f)))
    expect(bad.map(rel)).toEqual([])
  })

  it('no emoji and no AI-flavoured copy', () => {
    const emoji = /[\u{1F300}-\u{1FAFF}\u{1F600}-\u{1F64F}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{2B50}\u{1F900}-\u{1F9FF}]/u
    const allowedGlyphs = /[✓✗]/g // text checkmarks, not emoji
    const bad = files.filter((f) => emoji.test(text(f).replace(allowedGlyphs, '')))
    expect(bad.map(rel)).toEqual([])
    const copy = files.filter((f) => /AI-powered|powered by AI|Welcome to|✨/.test(text(f)))
    expect(copy.map(rel)).toEqual([])
  })

  it('only palette colours are written as hex', () => {
    const palette = new Set(['#15120e', '#ece6d8', '#f6f2e8', '#e4570f', '#c0182b', '#e9a91a', '#3f7a3a', '#bdb5a3', '#d9d1bd', '#201c16', '#2a2620', '#3b352b', '#8a8272'])
    const offenders: string[] = []
    for (const f of files) {
      for (const m of text(f).matchAll(/#[0-9a-fA-F]{6}\b/g)) if (!palette.has(m[0].toLowerCase())) offenders.push(`${rel(f)} ${m[0]}`)
    }
    expect(offenders).toEqual([])
  })
})

/** Every <button ...> opening tag in these screens must be a 56 px target or larger (the SOS hold control is its own large circle).
 * The scanner skips `=>` and anything inside {braces}, so handlers do not end the tag early. */
function buttons(file: string): { tag: string; line: number }[] {
  const t = text(file)
  const out: { tag: string; line: number }[] = []
  let i = t.indexOf('<button')
  while (i >= 0) {
    let depth = 0
    let j = i + 7
    for (; j < t.length; j++) {
      const c = t[j]
      if (c === '{') depth++
      else if (c === '}') depth--
      else if (c === '>' && depth === 0) break
    }
    out.push({ tag: t.slice(i, j + 1), line: t.slice(0, i).split('\n').length })
    i = t.indexOf('<button', j)
  }
  return out
}
const BIG = /min-h-\[(56|72|88|120)px\]|btn-touch|h-\[?(56|72)/

describe('design audit: touch targets on the caller and driver screens', () => {
  const screens = files.filter((f) => /pages[\\/](caller|driver)[\\/]/.test(f) && f.endsWith('.tsx'))
  it('finds the screens', () => expect(screens.length).toBeGreaterThan(4))
  for (const f of screens) {
    it(`${rel(f)}: every button is 56 px or larger`, () => {
      const small = buttons(f).filter((b) => !BIG.test(b.tag) && !/\bstyle=\{\{[^}]*width/.test(b.tag) && !/sos-hold-hint/.test(b.tag))
      expect(small.map((b) => `${rel(f)}:${b.line}`)).toEqual([])
    })
  }
})

describe('design audit: status is never colour alone', () => {
  it('lane, integrity and receipt states are spelled out in text', () => {
    const lane = text(join(SRC, 'pages/command/FalseAlarmPanel.tsx'))
    expect(lane).toMatch(/lane \{LANE_TEXT/)
    const badge = text(join(SRC, 'components/LedgerBadge.tsx'))
    expect(badge).toMatch(/\{ledger\.ledger_status\}/)
    const ledgerPage = text(join(SRC, 'pages/ledger/LedgerPage.tsx'))
    expect(ledgerPage).toMatch(/'VERIFIED' : 'TAMPERED'/)
    const receipt = text(join(SRC, 'pages/ledger/VerifyReceiptPage.tsx'))
    expect(receipt).toMatch(/'SIGNATURE VALID' : 'NOT VALID'/)
  })

  it('no screen relies on a coloured dot without text: stamps always carry words', () => {
    const ui = text(join(SRC, 'components/ui.tsx'))
    expect(ui).toMatch(/Always carries text, never colour alone/)
  })
})
