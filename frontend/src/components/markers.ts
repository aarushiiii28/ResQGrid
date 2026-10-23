import L from 'leaflet'

const mk = (html: string, size: [number, number], anchor?: [number, number]) =>
  L.divIcon({ html, className: 'rg-marker', iconSize: size, iconAnchor: anchor ?? [size[0] / 2, size[1] / 2] })

const STATUS_COLOR: Record<string, string> = {
  available: '#3F7A3A',
  assigned: '#E9A91A',
  accepted: '#E9A91A',
  en_route: '#E4570F',
  on_scene: '#C0182B',
  transporting: '#E4570F',
  unavailable: '#BDB5A3',
}

/** Ambulance marker. A stale/unknown position is drawn hollow and dashed with a "?" so it can never
 * be mistaken for a live pin. */
export function vehicleIcon(o: { id: string; status: string; stale: boolean; selected?: boolean; simulated?: boolean }) {
  const c = o.stale ? '#BDB5A3' : (STATUS_COLOR[o.status] ?? '#ECE6D8')
  const fill = o.stale ? 'transparent' : '#15120E'
  const dash = o.stale ? 'stroke-dasharray="4 3"' : ''
  const sel = o.selected ? `<rect x="-3" y="-3" width="36" height="36" fill="none" stroke="#E4570F" stroke-width="2"/>` : ''
  const glyph = o.stale
    ? `<text x="15" y="21" text-anchor="middle" font-family="IBM Plex Mono" font-size="16" fill="#BDB5A3">?</text>`
    : `<path d="M13 8h4v5h5v4h-5v5h-4v-5H8v-4h5z" fill="#ECE6D8"/>`
  const label = `<div style="position:absolute;left:34px;top:5px;white-space:nowrap;font:11px 'IBM Plex Mono';color:#15120E;background:#ECE6D8;padding:0 3px;border:1px solid #15120E">${o.id.replace('AMB-', 'A')}${o.simulated ? ' sim' : ''}</div>`
  return mk(
    `<div style="position:relative;width:30px;height:30px"><svg width="30" height="30" viewBox="0 0 30 30" overflow="visible">${sel}<rect x="1" y="1" width="28" height="28" fill="${fill}" stroke="${c}" stroke-width="3" ${dash}/>${glyph}</svg>${label}</div>`,
    [30, 30],
  )
}

export function hospitalIcon(o: { stale: boolean; selected?: boolean }) {
  const c = o.stale ? '#E9A91A' : '#ECE6D8'
  const badge = o.stale ? `<rect x="14" y="-6" width="14" height="12" fill="#E9A91A"/><text x="21" y="4" text-anchor="middle" font-family="IBM Plex Mono" font-size="10" font-weight="700" fill="#15120E">!</text>` : ''
  return mk(
    `<svg width="28" height="28" viewBox="0 0 28 28" overflow="visible"><rect x="1" y="1" width="26" height="26" fill="#15120E" stroke="${c}" stroke-width="2.5"/><path d="M11 6h6v5h5v6h-5v5h-6v-5H6v-6h5z" fill="#C0182B"/>${badge}</svg>`,
    [28, 28],
  )
}

export function incidentIcon(o: { priority: number | null; selected?: boolean; n?: string }) {
  const color = ({ 1: '#C0182B', 2: '#E4570F', 3: '#E9A91A', 4: '#3F7A3A' } as Record<number, string>)[o.priority ?? 3] ?? '#E9A91A'
  const ring = o.selected ? `<circle cx="18" cy="18" r="17" fill="none" stroke="#ECE6D8" stroke-width="2" stroke-dasharray="5 4"/>` : ''
  return mk(
    `<svg width="36" height="36" viewBox="0 0 36 36">${ring}<path d="M18 4 32 18 18 32 4 18z" fill="${color}" stroke="#15120E" stroke-width="2.5"/><text x="18" y="22" text-anchor="middle" font-family="Barlow Condensed" font-weight="700" font-size="14" fill="#15120E">${o.n ?? '!'}</text></svg>`,
    [36, 36],
  )
}

export function sceneIcon() {
  return mk(
    `<div style="position:relative;width:36px;height:36px"><div class="ring-pulse" style="position:absolute;inset:0;border:3px solid #E4570F;border-radius:0"></div><svg width="36" height="36" viewBox="0 0 36 36"><path d="M18 4 32 18 18 32 4 18z" fill="#E4570F" stroke="#15120E" stroke-width="2.5"/><circle cx="18" cy="18" r="4" fill="#15120E"/></svg></div>`,
    [36, 36],
  )
}

export function aedIcon(nearest = false) {
  const c = nearest ? '#3F7A3A' : '#15120E'
  return mk(
    `<svg width="26" height="26" viewBox="0 0 26 26"><rect x="1.5" y="1.5" width="23" height="23" fill="${c}" stroke="#ECE6D8" stroke-width="2"/><path d="M14 4 7 15h5l-1 7 8-11h-5z" fill="#ECE6D8"/></svg>`,
    [26, 26],
  )
}

export function volunteerIcon() {
  return mk(
    `<svg width="22" height="22" viewBox="0 0 22 22"><path d="M11 1 21 11 11 21 1 11z" fill="#E9A91A" stroke="#15120E" stroke-width="2"/><circle cx="11" cy="9" r="2.5" fill="#15120E"/><path d="M6 16c1-3 3-4 5-4s4 1 5 4" fill="#15120E"/></svg>`,
    [22, 22],
  )
}

export function pinIcon(approximate = false) {
  return mk(
    `<div style="position:relative;width:44px;height:56px"><svg width="44" height="56" viewBox="0 0 44 56"><path d="M22 54C10 38 4 30 4 20a18 18 0 0 1 36 0c0 10-6 18-18 34z" fill="#E4570F" stroke="#15120E" stroke-width="3"/><circle cx="22" cy="20" r="7" fill="#15120E"/></svg>${approximate ? `<div style="position:absolute;left:46px;top:6px;white-space:nowrap;font:11px 'IBM Plex Mono';background:#15120E;color:#E9A91A;padding:2px 5px;border:1px solid #E9A91A">APPROXIMATE LOCATION</div>` : ''}</div>`,
    [44, 56],
    [22, 54],
  )
}

export function youIcon() {
  return mk(
    `<div style="position:relative;width:22px;height:22px"><div class="ring-pulse" style="position:absolute;inset:0;border:2px solid #15120E"></div><svg width="22" height="22" viewBox="0 0 22 22"><rect x="4" y="4" width="14" height="14" fill="#15120E" stroke="#F6F2E8" stroke-width="2"/></svg></div>`,
    [22, 22],
  )
}

export function signalIcon(green: boolean) {
  const c = green ? '#3F7A3A' : '#C0182B'
  return mk(`<svg width="14" height="14" viewBox="0 0 14 14"><rect x="1" y="1" width="12" height="12" fill="${c}" stroke="#15120E" stroke-width="2"/></svg>`, [14, 14])
}

export function standbyIcon(label: string) {
  return mk(
    `<svg width="30" height="30" viewBox="0 0 30 30"><rect x="2" y="2" width="26" height="26" fill="#ECE6D8" stroke="#15120E" stroke-width="3" stroke-dasharray="5 3"/><text x="15" y="20" text-anchor="middle" font-family="Barlow Condensed" font-weight="700" font-size="14" fill="#15120E">${label}</text></svg>`,
    [30, 30],
  )
}
