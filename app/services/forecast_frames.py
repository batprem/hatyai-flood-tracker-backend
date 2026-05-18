from datetime import datetime
from typing import cast

from app.ingestion.models import ForecastFrame, FreshnessStatus
from app.ingestion.repository import ForecastRepository, MongoDocument
from app.schemas.forecast_frames import (
    ForecastFrameArea,
    ForecastFrameFreshness,
    ForecastFrameFreshnessStatus,
    ForecastFrameGrid,
    ForecastFramePublic,
    ForecastFrameQuality,
    ForecastFrameSource,
    ForecastFramesQuery,
    ForecastFramesResponse,
)

DEFAULT_AREA_NAME = "hatyai_utapao_songkhla_phase1"


def to_public_frame(frame: ForecastFrame) -> ForecastFramePublic:
    """Map an internal normalized frame to its public response model.

    Args:
        frame (ForecastFrame): Internal normalized forecast frame.
    """
    return ForecastFramePublic(
        frame_id=frame.frame_id,
        run_id=frame.run_id,
        provider=frame.provider.value,
        model=frame.model,
        variable=frame.variable.value,
        statistic=frame.statistic.value,
        unit=frame.unit,
        run_time=frame.run_time,
        valid_time=frame.valid_time,
        window_start=frame.window_start,
        window_end=frame.window_end,
        accumulation_hours=frame.accumulation_hours,
        provider_accumulation_semantics=frame.provider_accumulation_semantics,
        forecast_hour=frame.forecast_hour,
        retrieved_at=frame.retrieved_at,
        processed_at=frame.processed_at,
        area=ForecastFrameArea(
            name=frame.area.name,
            bbox=frame.area.bbox,
            crs=frame.area.crs,
        ),
        grid=ForecastFrameGrid(
            type=frame.grid.type,
            resolution_degrees=frame.grid.resolution_degrees,
            width=frame.grid.width,
            height=frame.grid.height,
        ),
        values_mm=list(frame.values_mm),
        source=ForecastFrameSource(
            url=frame.source.url,
            product=frame.source.product,
            license=frame.source.license,
            attribution=frame.source.attribution,
            raw_artifact_ref=frame.source.raw_artifact_ref,
        ),
        quality=ForecastFrameQuality(
            status=frame.quality.status.value,
            missing_value_count=frame.quality.missing_value_count,
            minimum_mm=frame.quality.minimum_mm,
            maximum_mm=frame.quality.maximum_mm,
        ),
    )


def build_freshness_block(
    summary: MongoDocument,
    frame_count: int,
) -> ForecastFrameFreshness:
    """Translate the repository freshness document into the public freshness block.

    Args:
        summary (MongoDocument): Repository freshness summary.
        frame_count (int): Number of frames matching the query.
    """
    raw_status = summary.get("status")
    status = _resolve_freshness_status(raw_status)
    provider = _optional_str(summary.get("provider"))
    model = _optional_str(summary.get("model"))
    run_time = _optional_datetime(summary.get("runTime"))
    retrieved_at = _optional_datetime(summary.get("retrievedAt"))
    threshold_hours = _optional_int(summary.get("thresholdHours"))
    reason = _optional_str(summary.get("reason"))
    return ForecastFrameFreshness(
        status=status,
        retrieved_at=retrieved_at,
        threshold_hours=threshold_hours,
        provider=provider,
        model=model,
        run_time=run_time,
        frame_count=frame_count,
        reason=reason,
    )


async def list_forecast_frames(
    repository: ForecastRepository,
    *,
    provider: str | None,
    model: str | None,
    area: str | None,
    valid_time_from: datetime | None,
    valid_time_to: datetime | None,
) -> ForecastFramesResponse:
    """Read frames through the repository and return the public response.

    Args:
        repository (ForecastRepository): Forecast repository instance.
        provider (str | None): Filter by normalized provider.
        model (str | None): Filter by normalized model name.
        area (str | None): Configured area name.
        valid_time_from (datetime | None): Lower bound on validTime.
        valid_time_to (datetime | None): Upper bound on validTime.
    """
    resolved_area = area or DEFAULT_AREA_NAME
    frames = await repository.list_frames(
        provider=provider,
        model=model,
        area_name=resolved_area,
        valid_time_from=valid_time_from,
        valid_time_to=valid_time_to,
    )
    summary = await repository.freshness_summary(provider=provider, model=model)
    public_frames = [to_public_frame(frame) for frame in frames]
    freshness = build_freshness_block(summary, frame_count=len(public_frames))
    query = ForecastFramesQuery(
        provider=provider,
        model=model,
        area=resolved_area,
        valid_time_from=valid_time_from,
        valid_time_to=valid_time_to,
    )
    return ForecastFramesResponse(
        freshness=freshness,
        query=query,
        frames=public_frames,
    )


def _resolve_freshness_status(value: object) -> ForecastFrameFreshnessStatus:
    if isinstance(value, FreshnessStatus):
        return ForecastFrameFreshnessStatus(value.value)
    if isinstance(value, str):
        try:
            return ForecastFrameFreshnessStatus(value)
        except ValueError:
            return ForecastFrameFreshnessStatus.FAILED
    return ForecastFrameFreshnessStatus.FAILED


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if hasattr(value, "value"):
        attr = getattr(value, "value", None)
        if isinstance(attr, str):
            return attr
    return str(value)


def _optional_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    return None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return cast(int, value)
    return None
