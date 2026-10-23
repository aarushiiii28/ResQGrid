"""Duplicate suggestion: time window + distance + text similarity. Suggests only; merging is
always a human action."""

import re
from datetime import datetime

from app.core.config import Settings
from app.core.geo import haversine_m
from app.schemas.common import GeoPoint
from app.schemas.incident import DuplicateSuggestion, Incident

_STOP = {"the", "a", "an", "and", "is", "are", "at", "on", "in", "of", "to", "near", "there", "has", "have", "was", "with", "for"}


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2 and t not in _STOP}


def text_similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def suggest_duplicates(
    *,
    created_at: datetime,
    location: GeoPoint | None,
    text: str,
    others: list[tuple[Incident, str]],
    settings: Settings,
) -> list[DuplicateSuggestion]:
    out: list[DuplicateSuggestion] = []
    for other, other_text in others:
        if other.status in ("closed", "merged", "cancelled_by_caller", "pending"):
            continue
        dt = abs((created_at - other.created_at).total_seconds())
        if dt > settings.duplicate_window_s:
            continue
        dist = None
        if location is not None and other.location_candidates:
            o = other.location_candidates[0]
            dist = haversine_m(location.lat, location.lon, o.lat, o.lon)
            # accuracy radii widen the allowed distance
            slack = (location.accuracy_m or 0) + (o.accuracy_m or 0)
            if dist > settings.duplicate_distance_m + slack:
                continue
        sim = text_similarity(text, other_text)
        parts = [(0.2, 1 - dt / settings.duplicate_window_s), (0.4, sim)]
        if dist is not None:
            parts.append((0.4, max(0.0, 1 - dist / settings.duplicate_distance_m)))
        score = sum(w * v for w, v in parts) / sum(w for w, _ in parts)
        if score < settings.duplicate_min_score:
            continue
        reasons = [f"reported {dt / 60:.1f} min apart"]
        if dist is not None:
            reasons.append(f"{dist:.0f} m apart")
        if sim > 0:
            reasons.append(f"text similarity {sim:.2f}")
        out.append(
            DuplicateSuggestion(
                incident_id=other.id,
                score=round(score, 3),
                distance_m=None if dist is None else round(dist, 1),
                minutes_apart=round(dt / 60, 2),
                text_similarity=round(sim, 3),
                reasons=reasons,
            )
        )
    return sorted(out, key=lambda s: (-s.score, s.incident_id))
