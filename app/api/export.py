"""Router for research data exports (CSV / GeoJSON downloads)."""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import JsonValue

from app.api.deps import get_forecast_repository, get_station_repository
from app.data.historical_events import HISTORICAL_EVENTS
from app.ingestion.repository import ForecastRepository
from app.ingestion.station_repository import StationObservationRepository
from app.services.export import (
    ExportDataset,
    ExportFormat,
    ExportRangeError,
    events_to_features,
    events_to_rows,
    export_filename,
    filter_events,
    frames_to_features,
    frames_to_rows,
    observations_to_features,
    observations_to_rows,
    render_csv,
    render_geojson,
    validate_export_range,
)

router = APIRouter(prefix="/export", tags=["export"])

_MEDIA_TYPES: dict[ExportFormat, str] = {
    ExportFormat.CSV: "text/csv; charset=utf-8",
    ExportFormat.GEOJSON: "application/geo+json",
}


async def _stream(document: str) -> AsyncIterator[str]:
    """Yield the rendered export document as a single streamed chunk."""
    yield document


@router.get("")
async def download_export(
    dataset: Annotated[ExportDataset, Query(description="Dataset to export.")],
    export_format: Annotated[
        ExportFormat,
        Query(alias="format", description="File format: csv or geojson."),
    ],
    start: Annotated[
        date, Query(description="First UTC calendar date included (ISO 8601, inclusive).")
    ],
    end: Annotated[
        date, Query(description="Last UTC calendar date included (ISO 8601, inclusive).")
    ],
    forecast_repository: Annotated[ForecastRepository, Depends(get_forecast_repository)],
    station_repository: Annotated[
        StationObservationRepository | None, Depends(get_station_repository)
    ] = None,
) -> StreamingResponse:
    """Download a bounded date range of research data as CSV or GeoJSON.

    Supports three datasets: ``forecast_frames`` (filtered on forecast valid
    time; raw grids summarized to mean/min/max mm), ``risk_history`` (curated
    historical flood events filtered on event date), and
    ``station_observations`` (filtered on observation time). Timestamps are
    UTC ISO 8601; units and column semantics are documented in CSV comment
    rows and GeoJSON top-level metadata. An empty result returns HTTP 200
    with a header-only file rather than an error.

    Args:
        dataset: Dataset to export.
        export_format: File format, ``csv`` or ``geojson``.
        start: First UTC calendar date included in the export (inclusive).
        end: Last UTC calendar date included in the export (inclusive).
        forecast_repository: Forecast repository injected via dependency.
        station_repository: Station repository or ``None`` when persistence is
            unconfigured (exports an empty file). Defaults to ``None``.

    Returns:
        A streamed file download with a ``Content-Disposition`` filename of
        ``hft_<dataset>_<start>_<end>.<csv|geojson>``.

    Raises:
        HTTPException: With status 400 when ``end`` is before ``start`` or
            the inclusive range exceeds the 92-day maximum.
    """
    try:
        range_start, range_end_exclusive = validate_export_range(start, end)
    except ExportRangeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    generated_at = datetime.now(UTC)
    rows: list[dict[str, JsonValue]]
    features: list[dict[str, JsonValue]]

    if dataset is ExportDataset.FORECAST_FRAMES:
        frames = await forecast_repository.list_frames(
            valid_time_from=range_start,
            valid_time_to=range_end_exclusive - timedelta(microseconds=1),
        )
        rows = frames_to_rows(frames)
        features = frames_to_features(frames)
    elif dataset is ExportDataset.RISK_HISTORY:
        events = filter_events(HISTORICAL_EVENTS, start=start, end=end)
        rows = events_to_rows(events)
        features = events_to_features(events)
    else:
        observations = (
            await station_repository.list_between(
                observed_from=range_start,
                observed_to=range_end_exclusive,
            )
            if station_repository is not None
            else []
        )
        rows = observations_to_rows(observations)
        features = observations_to_features(observations)

    if export_format is ExportFormat.CSV:
        document = render_csv(dataset, rows, start=start, end=end, generated_at=generated_at)
    else:
        document = render_geojson(
            dataset, features, start=start, end=end, generated_at=generated_at
        )

    filename = export_filename(dataset, export_format, start, end)
    return StreamingResponse(
        _stream(document),
        media_type=_MEDIA_TYPES[export_format],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
