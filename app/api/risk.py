from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_station_observation_client, get_station_repository
from app.core.config import Settings, get_settings
from app.ingestion.station_repository import StationObservationRepository
from app.ingestion.thaiwater_client import StationObservationClient
from app.schemas.risk import CurrentRiskResponse
from app.services.mock_data import get_rainfall_forecast
from app.services.risk_rules import (
    RainfallRiskInput,
    WaterLevelRiskInput,
    calculate_current_risk,
)
from app.services.water_levels import get_water_levels

router = APIRouter(prefix="/risk", tags=["risk"])


@router.get("/current", response_model=CurrentRiskResponse)
async def read_current_risk(
    settings: Annotated[Settings, Depends(get_settings)],
    client: Annotated[StationObservationClient, Depends(get_station_observation_client)],
    repository: Annotated[
        StationObservationRepository | None, Depends(get_station_repository)
    ] = None,
) -> CurrentRiskResponse:
    """Return the current rule-based flood risk summary.

    Combines:
    - Mock rainfall forecasts (until ECMWF/GFS lands in the risk pipeline).
    - Real ThaiWater water-level observations from the lifespan-managed
      client. Real fresh records carry ``is_mock=False`` and are therefore
      allowed to raise public risk per the engine's mock gating.
    """
    rainfall = get_rainfall_forecast()
    water_levels = await get_water_levels(
        client=client,
        repository=repository,
        max_age=timedelta(hours=settings.thaiwater_max_age_hours),
    )
    forecasts = [
        RainfallRiskInput(
            area_id=forecast.basin_id,
            area_name=forecast.basin_name.en,
            rainfall_mm=forecast.rainfall_mm,
            accumulation_hours=forecast.accumulation_hours,
            source=rainfall.freshness.source,
            model_run_time=rainfall.freshness.generated_at,
            valid_time=forecast.forecast_time,
            retrieved_at=rainfall.freshness.generated_at,
            is_mock=rainfall.freshness.is_mock,
        )
        for forecast in rainfall.forecasts
    ]
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
    generated_at = max(
        rainfall.freshness.generated_at,
        water_levels.freshness.generated_at,
    )
    return calculate_current_risk(
        forecasts=forecasts,
        water_levels=stations,
        settings=settings.risk_rule_settings(),
        generated_at=generated_at,
    )
