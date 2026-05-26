import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_forecast_repository, get_station_repository
from app.core.config import get_settings
from app.ingestion.repository import ForecastRepository
from app.ingestion.station_repository import StationObservationRepository
from app.schemas.freshness import FreshnessReportStatus
from app.schemas.health import DataQualityBlock, HealthResponse
from app.services.data_quality import DataQualitySnapshot, compute_data_quality

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


def _to_block(snapshot: DataQualitySnapshot) -> DataQualityBlock:
    """Map a data-quality snapshot to the public health block.

    Args:
        snapshot: Computed per-source data-quality snapshot.

    Returns:
        The public data-quality block for the health response.
    """
    return DataQualityBlock(
        gfs_run_age_hours=snapshot.gfs.age_hours,
        ecmwf_run_age_hours=snapshot.ecmwf.age_hours,
        station_observation_age_hours=snapshot.station.age_hours,
        gfs_freshness_status=FreshnessReportStatus(snapshot.gfs.status.value),
        ecmwf_freshness_status=FreshnessReportStatus(snapshot.ecmwf.status.value),
        station_freshness_status=FreshnessReportStatus(snapshot.station.status.value),
    )


@router.get("/health", response_model=HealthResponse, response_model_by_alias=True)
async def read_health(
    forecast_repository: Annotated[ForecastRepository, Depends(get_forecast_repository)],
    station_repository: Annotated[
        StationObservationRepository | None, Depends(get_station_repository)
    ],
) -> HealthResponse:
    """Return backend health status with a per-source data-quality block.

    The endpoint doubles as the Railway liveness probe, so a failure to compute
    the data-quality block degrades to ``data_quality=None`` rather than raising;
    the core ``status`` field always reports ``ok`` when the process is serving.

    Args:
        forecast_repository: Forecast repository injected via dependency.
        station_repository: Station observation repository injected via dependency, or ``None``.

    Returns:
        The backend health status with an optional data-quality block.
    """
    settings = get_settings()
    data_quality: DataQualityBlock | None = None
    try:
        snapshot = await compute_data_quality(
            forecast_repository,
            station_repository,
            settings,
        )
        data_quality = _to_block(snapshot)
    except Exception:
        logger.exception("failed to compute data_quality block for /health")
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        checked_at=datetime.now(UTC),
        data_quality=data_quality,
    )
