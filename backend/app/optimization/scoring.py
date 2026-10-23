"""score = wT*time + wF*freshness + wV*coverage_loss + wU*uncertainty + wS*switch
Configurable, all terms in minute-equivalents. Prototype model, not clinically validated."""

from dataclasses import dataclass

from app.core.config import Settings
from app.schemas.dispatch import ScoreComponents, Weights, WeightsOverride


def weights_from(settings: Settings, override: WeightsOverride | None = None) -> Weights:
    w = Weights(
        w_time=settings.w_time, w_fresh=settings.w_fresh, w_coverage=settings.w_coverage,
        w_uncertainty=settings.w_uncertainty, w_switch=settings.w_switch,
        transport_weight=settings.transport_weight,
    )
    if override:
        w = w.model_copy(update=override.model_dump(exclude_none=True))
    return w


@dataclass
class PairInputs:
    scene_mid_s: float
    hospital_mid_s: float
    total_low_s: float
    total_high_s: float
    vehicle_freshness: float  # 0 fresh .. 1 at/over stale threshold
    hospital_freshness: float  # 0 fresh .. 1 stale/unknown
    coverage_loss: float  # fraction 0..1
    entrance_uncertain: bool = False
    degraded_routing: bool = False
    approximate_location: bool = False
    switch: float = 0.0  # 1 for vehicle change, +0.5 for destination change


def score_pair(i: PairInputs, w: Weights) -> ScoreComponents:
    time_min = i.scene_mid_s / 60 + w.transport_weight * i.hospital_mid_s / 60
    fresh = i.vehicle_freshness + i.hospital_freshness
    unc = (i.total_high_s - i.total_low_s) / 120 + 2.0 * i.entrance_uncertain \
        + 1.0 * i.degraded_routing + 1.0 * i.approximate_location
    weighted = {
        "time": w.w_time * time_min,
        "freshness": w.w_fresh * fresh,
        "coverage_loss": w.w_coverage * i.coverage_loss,
        "uncertainty": w.w_uncertainty * unc,
        "switch": w.w_switch * i.switch,
    }
    weighted = {k: round(v, 4) for k, v in weighted.items()}
    return ScoreComponents(
        time=round(time_min, 4), freshness=round(fresh, 4), coverage_loss=round(i.coverage_loss, 4),
        uncertainty=round(unc, 4), switch=i.switch, weighted=weighted,
        total=round(sum(weighted.values()), 4),
    )
