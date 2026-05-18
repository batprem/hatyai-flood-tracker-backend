"""Aggregate normalized ThaiWater station observations into the public response.

The route handler stays thin: it asks this service to produce a
:class:`WaterLevelResponse`. The service:

1. Calls the injected :class:`StationObservationClient` to pull fresh
   normalized records (or falls back to the persisted "latest per station"
   when the live call fails).
2. Optionally writes-through new records to the
   :class:`StationObservationRepository` for history and future analytics.
3. Computes the public per-station risk level using the same
   warning/critical threshold semantics that the risk engine uses.
4. Reports freshness honestly: when no fresh records are available the
   response carries empty stations and a ``source`` suffix of ``:stale`` so
   the risk engine and frontend can degrade gracefully.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from app.ingestion.station_repository import StationObservationRepository
from app.ingestion.thaiwater_client import (
    THAIWATER_PROVIDER_NAME,
    StationObservation,
    StationObservationClient,
    StationVariable,
    ThaiwaterIngestionError,
)
from app.schemas.common import Coordinates, DataFreshness, LocalizedName, RiskLevel
from app.schemas.stations import WaterLevelResponse, WaterLevelTrend, WaterStationLevel

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _classify(observation: StationObservation) -> RiskLevel:
    """Classify a single station observation into a public risk level.

    Uses the same critical/warning/watch ladder as
    :func:`app.services.risk_rules.score_water_level` (watch ratio fixed at
    0.8 — the project default in :mod:`app.core.config`). When thresholds
    are missing the function falls back to green so a missing threshold
    cannot silently raise public risk.
    """
    warning = observation.warning_level_m
    critical = observation.critical_level_m
    if warning is None or critical is None:
        return RiskLevel.GREEN
    if observation.value >= critical:
        return RiskLevel.RED
    if observation.value >= warning:
        return RiskLevel.ORANGE
    if observation.value >= warning * 0.8:
        return RiskLevel.YELLOW
    return RiskLevel.GREEN


def _to_water_station_level(observation: StationObservation) -> WaterStationLevel:
    """Map a station observation to the public water-station response model.

    Args:
        observation: Station observation to map.

    Returns:
        WaterStationLevel with classified risk level.
    """
    return WaterStationLevel(
        station_id=observation.station_id,
        station_name=LocalizedName(
            th=observation.station_name_th,
            en=observation.station_name_en,
        ),
        canal_or_lake=LocalizedName(
            th=observation.canal_or_lake_th,
            en=observation.canal_or_lake_en,
        ),
        location=Coordinates(
            latitude=observation.location.coordinates[1],
            longitude=observation.location.coordinates[0],
        ),
        observed_at=observation.observed_at,
        water_level_m=max(0.0, observation.value),
        warning_level_m=observation.warning_level_m or 0.001,
        critical_level_m=observation.critical_level_m or 0.001,
        # Phase 1: we do not yet compute a real trend from history. The
        # public schema requires a value, so we surface "steady" until the
        # repository-backed trend analysis lands in a follow-up ticket.
        trend=WaterLevelTrend.STEADY,
        risk_level=_classify(observation),
    )


async def get_water_levels(
    *,
    client: StationObservationClient,
    repository: StationObservationRepository | None = None,
    max_age: timedelta = timedelta(hours=3),
    now: datetime | None = None,
) -> WaterLevelResponse:
    """Produce the public ``WaterLevelResponse`` from real provider data.

    Args:
        client: Provider client that returns normalized observations.
        repository: Optional time-series repository. When supplied, fresh
            observations are written through and the repository is queried
            as a fallback when the provider call fails.
        max_age: Maximum age of an observation considered "fresh" for
            public display. Stale records are dropped.
        now: Optional override for the current UTC time; injected by tests.

    Returns:
        A :class:`WaterLevelResponse` whose ``freshness.is_mock`` is
        ``False`` for real fresh records and whose ``source`` is suffixed
        with ``:stale`` or ``:unavailable`` when the provider returns no
        usable data.
    """
    resolved_now = now or _utc_now()
    observations: list[StationObservation] = []
    last_retrieved_at: datetime | None = None
    state: str = "fresh"

    try:
        observations = await client.fetch_latest_water_levels()
    except ThaiwaterIngestionError as exc:
        logger.warning("ThaiWater fetch failed: %s; falling back to repository", exc)
        state = "unavailable"

    if observations and repository is not None:
        try:
            await repository.upsert_many(observations)
        except Exception as exc:  # noqa: BLE001
            # Persistence is best-effort. Public availability must not depend
            # on a healthy Mongo write path, but we surface the error so
            # operators can see the regression in logs.
            logger.warning("Failed to persist station observations: %s", exc)

    fresh_observations = [
        obs for obs in observations if obs.is_fresh(now=resolved_now, max_age=max_age)
    ]

    # If the live call returned no fresh data but persistence is configured,
    # attempt to recover the most recent persisted reading per seed station.
    # Persisted readings are still subject to the freshness gate; a stale
    # cache must not silently keep the public level green.
    if not fresh_observations and repository is not None:
        try:
            cached = await repository.latest_per_station()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to read latest observations from cache: %s", exc)
            cached = []
        fresh_observations = [
            obs for obs in cached if obs.is_fresh(now=resolved_now, max_age=max_age)
        ]
        if fresh_observations:
            state = "fresh"

    if fresh_observations:
        last_retrieved_at = max(obs.retrieved_at for obs in fresh_observations)
        valid_at = max(obs.observed_at for obs in fresh_observations)
        stations = [_to_water_station_level(obs) for obs in fresh_observations]
    else:
        valid_at = None
        stations = []
        if state == "fresh":
            # Provider returned data but every record was stale.
            state = "stale"

    source = THAIWATER_PROVIDER_NAME if state == "fresh" else f"{THAIWATER_PROVIDER_NAME}:{state}"

    return WaterLevelResponse(
        freshness=DataFreshness(
            generated_at=last_retrieved_at or resolved_now,
            valid_at=valid_at,
            source=source,
            is_mock=False,
        ),
        stations=stations,
    )


__all__ = [
    "StationVariable",
    "get_water_levels",
]
