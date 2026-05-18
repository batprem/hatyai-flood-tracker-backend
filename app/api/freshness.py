"""Freshness endpoint exposing the latest forecast-run summary."""

from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_forecast_repository
from app.ingestion.repository import ForecastRepository, MongoDocument
from app.schemas.freshness import FreshnessReport, FreshnessReportStatus

router = APIRouter(prefix="/freshness", tags=["freshness"])


@router.get(
    "",
    response_model=FreshnessReport,
    response_model_by_alias=True,
)
async def read_freshness(
    repository: Annotated[ForecastRepository, Depends(get_forecast_repository)],
    provider: Annotated[
        str | None,
        Query(description="Optionally scope freshness to a normalized provider, e.g. 'gfs'."),
    ] = None,
    model: Annotated[
        str | None,
        Query(description="Optionally scope freshness to a normalized model name."),
    ] = None,
) -> FreshnessReport:
    """Return the latest stored forecast-run freshness snapshot.

    The response mirrors the freshness block used by the forecast frames
    endpoint so the frontend can read either source consistently. When no
    runs have been ingested yet the endpoint returns ``status='failed'`` with
    a ``reason`` payload rather than 404 so the public UI keeps a stable
    contract while ingestion warms up.

    Args:
        repository: Forecast repository injected via dependency.
        provider: Optionally scope freshness to a provider name. Defaults to ``None``.
        model: Optionally scope freshness to a model name. Defaults to ``None``.

    Returns:
        The latest freshness snapshot, or a ``failed`` placeholder if none exist yet.
    """
    summary = await repository.freshness_summary(provider=provider, model=model)
    return _build_report(summary)


def _build_report(summary: MongoDocument) -> FreshnessReport:
    """Translate the repository freshness document into a typed report."""
    status = _resolve_status(summary.get("status"))
    return FreshnessReport(
        status=status,
        provider=_optional_str(summary.get("provider")),
        model=_optional_str(summary.get("model")),
        run_time=_optional_datetime(summary.get("runTime")),
        retrieved_at=_optional_datetime(summary.get("retrievedAt")),
        threshold_hours=_optional_int(summary.get("thresholdHours")),
        frame_count=_count(summary.get("frameCount")),
        reason=_optional_str(summary.get("reason")),
    )


def _resolve_status(value: object) -> FreshnessReportStatus:
    if isinstance(value, FreshnessReportStatus):
        return value
    if hasattr(value, "value"):
        attr = getattr(value, "value", None)
        if isinstance(attr, str):
            try:
                return FreshnessReportStatus(attr)
            except ValueError:
                return FreshnessReportStatus.FAILED
    if isinstance(value, str):
        try:
            return FreshnessReportStatus(value)
        except ValueError:
            return FreshnessReportStatus.FAILED
    return FreshnessReportStatus.FAILED


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


def _count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return cast(int, value)
    return 0
