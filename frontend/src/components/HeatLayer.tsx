import L from 'leaflet'
import 'leaflet.heat'
import { useEffect } from 'react'
import { useMap } from 'react-leaflet'

/** Demand heat layer (leaflet.heat). Ramp stays inside the palette: amber to orange to red. */
export default function HeatLayer({ points, radius = 38, max = 1 }: { points: [number, number, number][]; radius?: number; max?: number }) {
  const map = useMap()
  useEffect(() => {
    const layer = (L as unknown as { heatLayer: (p: [number, number, number][], o: object) => L.Layer }).heatLayer(points, {
      radius,
      blur: 28,
      max,
      minOpacity: 0.25,
      gradient: { 0.2: '#E9A91A', 0.6: '#E4570F', 1.0: '#C0182B' },
    })
    layer.addTo(map)
    return () => {
      map.removeLayer(layer)
    }
  }, [map, points, radius, max])
  return null
}
