from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import (
    get_forecast_repository,
    get_station_observation_client,
    get_station_repository,
    get_threshold_database,
)
from app.core.config import Settings, get_settings
from app.geo.basin import BASIN_GEOMETRY_REF
from app.ingestion.models import ForecastProvider
from app.ingestion.repository import ForecastRepository
from app.ingestion.station_repository import StationObservationRepository
from app.ingestion.station_thresholds import get_station_thresholds
from app.ingestion.thaiwater_client import StationObservationClient
from app.schemas.risk import CurrentRiskResponse
from app.services.forecast_frames import DEFAULT_AREA_NAME
from app.services.risk_rules import (
    WaterLevelRiskInput,
    combine_ensemble_risk,
)
from app.services.water_level_contribution import build_water_level_contributions
from app.services.water_levels import get_water_levels

router = APIRouter(prefix="/risk", tags=["risk"])

# Providers combined into the public ensemble risk, in display order.
ENSEMBLE_PROVIDERS: tuple[str, ...] = (
    ForecastProvider.GFS.value,
    ForecastProvider.ECMWF_OPEN_DATA.value,
)


@router.get("/current", response_model=CurrentRiskResponse)
async def read_current_risk(
    settings: Annotated[Settings, Depends(get_settings)],
    forecast_repository: Annotated[ForecastRepository, Depends(get_forecast_repository)],
    client: Annotated[StationObservationClient, Depends(get_station_observation_client)],
    station_repository: Annotated[
        StationObservationRepository | None, Depends(get_station_repository)
    ] = None,
    threshold_database: Annotated[
        AsyncIOMotorDatabase | None, Depends(get_threshold_database)
    ] = None,
) -> CurrentRiskResponse:
    """Return the current ensemble rule-based flood risk summary.

    Rainfall risk is computed per provider (GFS and ECMWF Open Data) from each
    provider's latest stored run, then combined: when both providers are fresh
    the public level is the maximum of the two; when only one is fresh the
    fresh provider drives the level and ``single_provider_warning`` is set; when
    neither is fresh the ensemble is reported as unavailable. Water-level inputs
    use real ThaiWater/HAII observations and are shared across providers. When
    configured, per-station RID watch/warning/danger thresholds are read from
    the ``station_thresholds`` collection to attach a ``water_level_contributions``
    block; ``degraded_inputs`` is set when no fresh station observation is available.

    Args:
        settings: Application settings injected via dependency.
        forecast_repository: Forecast repository injected via dependency.
        client: ThaiWater client injected via dependency.
        station_repository: Station repository or None. Defaults to ``None``.
        threshold_database: Mongo database holding station thresholds, or
            None when unconfigured. Defaults to ``None``.

    Returns:
        A rule-based ensemble flood risk summary with per-provider contributions.
    """
    generated_at = datetime.now(UTC)
    rule_settings = settings.risk_rule_settings()
    frames_by_provider = await forecast_repository.get_latest_frames_per_provider(
        area_name=DEFAULT_AREA_NAME,
        providers=list(ENSEMBLE_PROVIDERS),
    )
    water_levels = await get_water_levels(
        client=client,
        repository=station_repository,
        max_age=timedelta(hours=settings.thaiwater_max_age_hours),
    )
    stations = [
        WaterLevelRiskInput(
            station_id=station.station_id,
            station_name=station.station_name.en,
            water_level_m=station.water_level_m,
            warning_level_m=station.warning_level_m,
            critical_level_m=station.critical_level_m,
            observed_at=station.observed_at,
            source=water_levels.freshness.source,
            is_mock=water_levels.freshness.is_mock,
        )
        for station in water_levels.stations
    ]

    response = combine_ensemble_risk(
        frames_by_provider=frames_by_provider,
        providers=ENSEMBLE_PROVIDERS,
        water_levels=stations,
        settings=rule_settings,
        generated_at=generated_at,
    )

    thresholds = (
        await get_station_thresholds(
            threshold_database,
            station_ids=[station.station_id for station in stations],
        )
        if threshold_database is not None and stations
        else {}
    )
    response.water_level_contributions = build_water_level_contributions(
        stations=stations,
        thresholds=thresholds,
    )
    response.degraded_inputs = not stations
    response.basin_geometry_ref = BASIN_GEOMETRY_REF
    return response
