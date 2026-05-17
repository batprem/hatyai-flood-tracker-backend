from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import (
    get_station_observation_client,
    get_station_repository,
)
from app.core.config import Settings, get_settings
from app.ingestion.station_repository import StationObservationRepository
from app.ingestion.thaiwater_client import StationObservationClient
from app.schemas.stations import WaterLevelResponse
from app.services.water_levels import get_water_levels

router = APIRouter(prefix="/stations", tags=["stations"])


@router.get("/water-level", response_model=WaterLevelResponse)
async def read_water_levels(
    settings: Annotated[Settings, Depends(get_settings)],
    client: Annotated[StationObservationClient, Depends(get_station_observation_client)],
    repository: Annotated[
        StationObservationRepository | None, Depends(get_station_repository)
    ] = None,
) -> WaterLevelResponse:
    """Return normalized real-time water-level observations from ThaiWater / HAII.

    The response carries ``freshness.is_mock=False`` and
    ``freshness.source='thaiwater-haii'`` when fresh observations are
    available. When the provider call fails or every record is older than
    ``HFT_THAIWATER_MAX_AGE_HOURS``, the response carries an empty
    ``stations`` list and a ``source`` suffix (``:stale`` or
    ``:unavailable``) so the risk engine and frontend can degrade
    gracefully without showing a stale all-clear.
    """
    return await get_water_levels(
        client=client,
        repository=repository,
        max_age=timedelta(hours=settings.thaiwater_max_age_hours),
    )
