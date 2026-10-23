"""M13 demand heatmap and pre-positioning service. Prototype approximation on synthetic history."""

from datetime import datetime

from app.core.geo import haversine_m
from app.optimization import demand as dm
from app.optimization.ortools_solver import ortools_assign


class DemandService:
    def __init__(self, ctx):
        self.ctx = ctx
        self._hist: dict[int, list[dm.Hist]] = {}

    def history(self) -> list[dm.Hist]:
        s = self.ctx.settings
        if s.seed not in self._hist:
            self._hist[s.seed] = dm.generate_history((s.city_lat, s.city_lng), s.seed)
        return self._hist[s.seed]

    @staticmethod
    def pick(now: datetime, bucket: int | None, daytype: str | None) -> tuple[int, bool]:
        b = dm.bucket_index(now.hour) if bucket is None else bucket
        weekend = (now.weekday() >= 5) if daytype in (None, "auto") else daytype == "weekend"
        return b, weekend

    async def forecast(self, bucket: int | None = None, daytype: str | None = None, standby: int | None = None) -> dict:
        ctx, s = self.ctx, self.ctx.settings
        center = (s.city_lat, s.city_lng)
        b, weekend = self.pick(ctx.now(), bucket, daytype)
        hist = self.history()
        cells = dm.grid_cells(center)
        per_hour, info = dm.forecast_cells(hist, center, b, weekend)
        top = sorted(range(len(cells)), key=lambda i: -per_hour[i])[:5]
        peak = max(per_hour) or 1.0
        heat = [[round(cells[i][0], 6), round(cells[i][1], 6), round(per_hour[i] / peak, 4)] for i in range(len(cells)) if per_hour[i] / peak >= 0.04]
        # units that could be moved: available, fresh
        avail = [(v.id, (v.location.lat, v.location.lon)) for v in await ctx.vehicles.list_views()
                 if v.availability == "available" and v.location is not None]
        k = standby if standby is not None else min(max(len(avail), 1), 6)
        picks = dm.suggest_standby(cells, per_hour, k, s.coverage_radius_m)
        before = dm.coverage([p for _, p in avail], cells, per_hour, s.coverage_radius_m)
        # assign nearest units to the suggested points (min total distance via OR-Tools) and re-measure
        cost = {(f"S{i + 1}", vid): round(haversine_m(pt[0], pt[1], *pos) / 1000, 3) for i, (pt, _) in enumerate(picks) for vid, pos in avail}
        sol = ortools_assign([f"S{i + 1}" for i in range(len(picks))], cost) if picks and avail else None
        moves, after_pts = [], dict(avail)
        if sol:
            for sid, vid in sol.assignment.items():
                i = int(sid[1:]) - 1
                moves.append({"standby": sid, "vehicle_id": vid, "lat": picks[i][0][0], "lon": picks[i][0][1],
                              "distance_km": cost[(sid, vid)], "covers_share": round(picks[i][1], 4)})
                after_pts[vid] = picks[i][0]
        after = dm.coverage(list(after_pts.values()), cells, per_hour, s.coverage_radius_m)
        return {
            "label": "Prototype approximation on synthetic history",
            "approximation": ("Seeded synthetic incidents from six hotspots; six 4-hour buckets by weekday or weekend; counts on a ~700 m grid "
                              "divided by days observed, Gaussian-smoothed with a tiny prior. Standby points maximise demand-weighted coverage "
                              "(greedy) within a straight-line response radius. Not validated against real demand."),
            "bucket": info["bucket"], "bucket_index": b, "day_type": info["day_type"], "history_count": len(hist),
            "history_used": info["history_used"], "days_of_this_type": info["days_of_this_type"],
            "expected_per_hour_total": round(sum(per_hour), 4),
            "explain": {"formula": "expected_per_hour(cell) = (smoothed_count(cell, bucket, day type) / days + prior) / bucket_hours",
                        "top_cells": [{"lat": round(cells[i][0], 5), "lon": round(cells[i][1], 5), "expected_per_hour": round(per_hour[i], 5)} for i in top],
                        "hotspots": [h[0] for h in dm.HOTSPOTS]},
            "heat": heat,
            "standby": [{"id": f"S{i + 1}", "lat": p[0][0], "lon": p[0][1], "covers_share": round(p[1], 4)} for i, p in enumerate(picks)],
            "moves": moves,
            "coverage_before": round(before, 4), "coverage_after": round(after, 4), "radius_m": s.coverage_radius_m,
            "units_considered": len(avail),
        }
