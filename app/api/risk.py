from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import (
    get_forecast_repository,
    get_station_observation_client,
    get_station_repository,
)
from app.core.config import Settings, get_settings
from app.ingestion.repository import ForecastRepository
from app.ingestion.station_repository import StationObservationRepository
from app.ingestion.thaiwater_client import StationObservationClient
from app.schemas.risk import CurrentRiskResponse
from app.services.forecast_frames import DEFAULT_AREA_NAME
from app.services.risk_rules import (
    WaterLevelRiskInput,
    build_rainfall_inputs_from_frames,
    calculate_current_risk,
)
from app.services.water_levels import get_water_levels

router = APIRouter(prefix="/risk", tags=["risk"])


@router.get("/current", response_model=CurrentRiskResponse)
async def read_current_risk(
    settings: Annotated[Settings, Depends(get_settings)],
    forecast_repository: Annotated[ForecastRepository, Depends(get_forecast_repository)],
    client: Annotated[StationObservationClient, Depends(get_station_observation_client)],
    station_repository: Annotated[
        StationObservationRepository | None, Depends(get_station_repository)
    ] = None,
) -> CurrentRiskResponse:
    """Return the current rule-based flood risk summary.

    Rainfall drivers are derived from the latest persisted GFS/ECMWF frames via
    ForecastRepository.list_frames. Water-level inputs use real ThaiWater/HAII
    observations; is_mock=False so the risk engine can raise public risk on
    real fresh station data.

    Args:
        settings: Application settings injected via dependency.
        forecast_repository: Forecast repository injected via dependency.
        client: ThaiWater client injected via dependency.
        station_repository: Station repository or None. Defaults to ``None``.

    Returns:
        A rule-based flood risk summary with rainfall and station drivers.
    """
    frames = await forecast_repository.list_frames(area_name=DEFAULT_AREA_NAME)
    rainfall_inputs = build_rainfall_inputs_from_frames(frames)
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
    return calculate_current_risk(
        forecasts=rainfall_inputs,
        water_levels=stations,
        settings=settings.risk_rule_settings(),
        generated_at=datetime.now(UTC),
    )
