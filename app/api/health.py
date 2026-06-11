import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_forecast_repository, get_station_repository
from app.core.config import get_settings
from app.ingestion.repository import ForecastRepository
from app.ingestion.station_repository import StationObservationRepository
from app.schemas.freshness import FreshnessReportStatus
from app.schemas.health import (
    DataQualityBlock,
    HealthResponse,
    PipelineFreshness,
    PipelinesBlock,
)
from app.services.data_quality import (
    DataQualitySnapshot,
    SourceQuality,
    compute_data_quality,
)
from app.services.ops_notifier import PipelineName

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

_BREACHING_STATUSES = {
    FreshnessReportStatus.STALE,
    FreshnessReportStatus.PARTIAL,
    FreshnessReportStatus.FAILED,
}


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


def _to_pipeline_freshness(pipeline: PipelineName, source: SourceQuality) -> PipelineFreshness:
    """Map one source's quality classification to its public pipeline report.

    Args:
        pipeline: Public pipeline identifier for the source.
        source: Computed quality classification for the source.

    Returns:
        The public per-pipeline freshness report.
    """
    status = FreshnessReportStatus(source.status.value)
    return PipelineFreshness(
        pipeline=pipeline.value,
        last_success_at=source.last_success_at,
        age_hours=source.age_hours,
        threshold_hours=source.threshold_hours,
        stale=status in _BREACHING_STATUSES,
        status=status,
        reason=source.reason,
    )


def _to_pipelines_block(snapshot: DataQualitySnapshot) -> PipelinesBlock:
    """Map a data-quality snapshot to the per-pipeline freshness block.

    Args:
        snapshot: Computed per-source data-quality snapshot.

    Returns:
        The public per-pipeline freshness block for the health response.
    """
    return PipelinesBlock(
        gfs=_to_pipeline_freshness(PipelineName.GFS, snapshot.gfs),
        ecmwf=_to_pipeline_freshness(PipelineName.ECMWF, snapshot.ecmwf),
        stations=_to_pipeline_freshness(PipelineName.STATIONS, snapshot.station),
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
    pipelines: PipelinesBlock | None = None
    try:
        snapshot = await compute_data_quality(
            forecast_repository,
            station_repository,
            settings,
        )
        data_quality = _to_block(snapshot)
        pipelines = _to_pipelines_block(snapshot)
    except Exception:
        logger.exception("failed to compute data_quality block for /health")
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        checked_at=datetime.now(UTC),
        data_quality=data_quality,
        pipelines=pipelines,
    )
