from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_forecast_repository
from app.core.config import Settings, get_settings
from app.ingestion.repository import ForecastRepository
from app.schemas.risk import CurrentRiskResponse
from app.services.forecast_frames import DEFAULT_AREA_NAME
from app.services.mock_data import get_water_levels
from app.services.risk_rules import (
    WaterLevelRiskInput,
    build_rainfall_inputs_from_frames,
    calculate_current_risk,
)

router = APIRouter(prefix="/risk", tags=["risk"])


def _water_level_inputs() -> list[WaterLevelRiskInput]:
    """Build water-level risk inputs from the Phase 1 mock until station ingestion exists."""
    water_levels = get_water_levels()
    return [
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


@router.get("/current", response_model=CurrentRiskResponse)
async def read_current_risk(
    settings: Annotated[Settings, Depends(get_settings)],
    repository: Annotated[ForecastRepository, Depends(get_forecast_repository)],
) -> CurrentRiskResponse:
    """Return the current rule-based flood risk summary from stored forecast frames.

    Rainfall drivers are derived from the latest persisted GFS/ECMWF frames in
    MongoDB via :meth:`ForecastRepository.list_frames`. Water-level inputs
    continue to use the Phase 1 mock until station ingestion lands. When no
    frames are stored, ``calculate_current_risk`` returns the documented
    ``availability: "unavailable"`` response with a non-green display level
    (see ``docs/risk-layer-design.md`` line 117).
    """
    generated_at = datetime.now(UTC)
    frames = await repository.list_frames(area_name=DEFAULT_AREA_NAME)
    rainfall_inputs = build_rainfall_inputs_from_frames(frames)
    water_inputs = _water_level_inputs()
    return calculate_current_risk(
        forecasts=rainfall_inputs,
        water_levels=water_inputs,
        settings=settings.risk_rule_settings(),
        generated_at=generated_at,
    )
