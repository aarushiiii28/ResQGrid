import L from 'leaflet'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { MapContainer, TileLayer, useMap, useMapEvents } from 'react-leaflet'

export const CITY: [number, number] = [30.901, 75.8573]
export const OSM_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'

function ClickCatcher({ onClick }: { onClick?: (p: [number, number]) => void }) {
  useMapEvents({ click: (e) => onClick?.([e.latlng.lat, e.latlng.lng]) })
  return null
}

function Fit({ points, padding = 60, max = 16, key2 }: { points?: [number, number][]; padding?: number; max?: number; key2?: string }) {
  const map = useMap()
  const last = useRef<string>('')
  useEffect(() => {
    if (!points || points.length === 0) return
    const sig = key2 ?? JSON.stringify(points.map((p) => [Math.round(p[0] * 1000), Math.round(p[1] * 1000)]))
    if (sig === last.current) return
    last.current = sig
    if (points.length === 1) map.setView(points[0], Math.max(map.getZoom(), 15), { animate: false })
    else map.fitBounds(L.latLngBounds(points), { padding: [padding, padding], maxZoom: max, animate: false })
  }, [points, map, padding, max, key2])
  return null
}

function Resizer() {
  const map = useMap()
  useEffect(() => {
    const el = map.getContainer()
    const ro = new ResizeObserver(() => map.invalidateSize())
    ro.observe(el)
    return () => ro.disconnect()
  }, [map])
  return null
}

function TileGuard({ onOffline }: { onOffline: (v: boolean) => void }) {
  const errors = useRef(0)
  return (
    <TileLayer
      url="https://tile.openstreetmap.org/{z}/{x}/{y}.png"
      attribution={OSM_ATTRIBUTION}
      maxZoom={19}
      eventHandlers={{
        tileerror: () => {
          errors.current += 1
          if (errors.current >= 4) onOffline(true)
        },
        tileload: () => {
          if (errors.current > 0) {
            errors.current = 0
            onOffline(false)
          }
        },
      }}
    />
  )
}

interface Props {
  center?: [number, number]
  zoom?: number
  className?: string
  fit?: [number, number][]
  fitKey?: string
  onClick?: (p: [number, number]) => void
  children?: ReactNode
  warm?: boolean
  label?: string
}

/** Leaflet map with OSM tiles. Tiles get a desaturated warm filter; if tiles cannot load (offline)
 * the map says so and the overlays still draw on a plain surface. */
export default function MapView({ center = CITY, zoom = 13, className = '', fit, fitKey, onClick, children, warm = true, label = 'Map' }: Props) {
  const [offline, setOffline] = useState(false)
  return (
    <div className={`${/\b(absolute|fixed)\b/.test(className) ? '' : 'relative'} ${warm ? 'warm-tiles' : ''} ${className}`} role="region" aria-label={label}>
      <MapContainer center={center} zoom={zoom} zoomControl scrollWheelZoom className="h-full w-full">
        <TileGuard onOffline={setOffline} />
        <Resizer />
        <ClickCatcher onClick={onClick} />
        {fit && <Fit points={fit} key2={fitKey} />}
        {children}
      </MapContainer>
      {offline && (
        <div role="status" className="absolute left-1/2 top-3 z-[1000] -translate-x-1/2 border border-amber bg-ink px-3 py-1 font-mono text-[11px] uppercase text-amber">
          Map tiles offline. Overlays are still live.
        </div>
      )}
    </div>
  )
}
