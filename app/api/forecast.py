from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_forecast_repository
from app.ingestion.repository import ForecastRepository
from app.schemas.forecast import RainfallForecastResponse
from app.schemas.forecast_frames import ForecastFramesResponse
from app.services.forecast_frames import DEFAULT_AREA_NAME, list_forecast_frames
from app.services.mock_data import get_rainfall_forecast

router = APIRouter(prefix="/forecast", tags=["forecast"])


@router.get("/rainfall", response_model=RainfallForecastResponse)
async def read_rainfall_forecast() -> RainfallForecastResponse:
    """Return normalized mock rainfall forecasts."""
    return get_rainfall_forecast()


@router.get(
    "/frames",
    response_model=ForecastFramesResponse,
    response_model_by_alias=True,
)
async def read_forecast_frames(
    repository: Annotated[ForecastRepository, Depends(get_forecast_repository)],
    provider: Annotated[
        str | None,
        Query(description="Filter frames by normalized provider, e.g. 'gfs'."),
    ] = None,
    model: Annotated[
        str | None,
        Query(description="Filter frames by normalized model name, e.g. 'gfs' or 'ifs'."),
    ] = None,
    valid_time_from: Annotated[
        datetime | None,
        Query(
            alias="validTimeFrom",
            description="Inclusive lower bound on frame validTime (ISO 8601 UTC).",
        ),
    ] = None,
    valid_time_to: Annotated[
        datetime | None,
        Query(
            alias="validTimeTo",
            description="Inclusive upper bound on frame validTime (ISO 8601 UTC).",
        ),
    ] = None,
    area: Annotated[
        str | None,
        Query(
            description=(
                "Configured area name; defaults to the Phase 1 Hat Yai/U-Tapao/Songkhla bbox."
            ),
        ),
    ] = DEFAULT_AREA_NAME,
) -> ForecastFramesResponse:
    """Return normalized forecast frames with a top-level freshness block."""
    return await list_forecast_frames(
        repository,
        provider=provider,
        model=model,
        area=area,
        valid_time_from=valid_time_from,
        valid_time_to=valid_time_to,
    )
