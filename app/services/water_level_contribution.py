"""Build threshold-aware per-station water-level contributions.

Kept separate from :mod:`app.services.risk_rules` so the threshold ladder can
be unit-tested in isolation and so the public ``water_level_contributions``
block stays an additive, descriptive layer over the existing risk engine
output. The risk engine still owns the headline level; this module only
explains how each gauge's observed stage maps onto its published alert
thresholds (RID watch/warning/danger) for the frontend and researchers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.ingestion.station_thresholds import StationThreshold
from app.schemas.common import RiskLevel
from app.schemas.risk import ThresholdApplied, WaterLevelContribution
from app.services.risk_rules import WaterLevelRiskInput

_THRESHOLD_RISK: Mapping[ThresholdApplied, RiskLevel] = {
    ThresholdApplied.DANGER: RiskLevel.RED,
    ThresholdApplied.WARNING: RiskLevel.ORANGE,
    ThresholdApplied.WATCH: RiskLevel.YELLOW,
    ThresholdApplied.NONE: RiskLevel.GREEN,
}


def classify_threshold(
    observed_level_m: float,
    threshold: StationThreshold,
) -> ThresholdApplied:
    """Return the highest alert threshold an observed level has reached.

    Uses inclusive lower bounds: ``danger`` first, then ``warning``, then
    ``watch``, otherwise ``none``.

    Args:
        observed_level_m: Observed water level in metres.
        threshold: Configured watch/warning/danger thresholds for the station.

    Returns:
        The applied threshold tier for the observed level.
    """
    if observed_level_m >= threshold.danger_level_m:
        return ThresholdApplied.DANGER
    if observed_level_m >= threshold.warning_level_m:
        return ThresholdApplied.WARNING
    if observed_level_m >= threshold.watch_level_m:
        return ThresholdApplied.WATCH
    return ThresholdApplied.NONE


def build_water_level_contributions(
    *,
    stations: Sequence[WaterLevelRiskInput],
    thresholds: Mapping[str, StationThreshold],
) -> list[WaterLevelContribution]:
    """Build per-station threshold contributions for the risk response.

    Only stations with a configured threshold record contribute a
    threshold-classified entry. Stations without a matching threshold are
    reported with ``threshold_applied=none`` and a green contribution so a
    missing threshold can never silently raise public risk.

    Args:
        stations: Normalized station inputs already gathered for the risk
            calculation.
        thresholds: Mapping of ``station_id`` to its configured thresholds.

    Returns:
        A list of ``WaterLevelContribution`` sorted by ``station_id``.
    """
    contributions: list[WaterLevelContribution] = []
    for station in stations:
        threshold = thresholds.get(station.station_id)
        if threshold is None:
            contributions.append(
                WaterLevelContribution(
                    station_id=station.station_id,
                    station_name=station.station_name,
                    observed_level_m=station.water_level_m,
                    watch_level_m=None,
                    warning_level_m=None,
                    danger_level_m=None,
                    threshold_applied=ThresholdApplied.NONE,
                    risk_contribution=RiskLevel.GREEN,
                )
            )
            continue
        applied = classify_threshold(station.water_level_m, threshold)
        contributions.append(
            WaterLevelContribution(
                station_id=station.station_id,
                station_name=threshold.station_name_en or station.station_name,
                observed_level_m=station.water_level_m,
                watch_level_m=threshold.watch_level_m,
                warning_level_m=threshold.warning_level_m,
                danger_level_m=threshold.danger_level_m,
                threshold_applied=applied,
                risk_contribution=_THRESHOLD_RISK[applied],
            )
        )
    return sorted(contributions, key=lambda contribution: contribution.station_id)


__all__ = [
    "build_water_level_contributions",
    "classify_threshold",
]
