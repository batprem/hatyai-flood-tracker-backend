from datetime import UTC, datetime, timedelta

from app.ingestion.models import (
    ForecastFrame,
    ForecastGrid,
    ForecastQuality,
    ForecastRun,
    ForecastRunStatus,
    ForecastSource,
    ForecastStatistic,
    ForecastVariable,
    FreshnessStatus,
    Phase1Area,
)
from app.ingestion.providers import ProviderFrameArtifact, ProviderRunRef


def build_run_record(
    run_ref: ProviderRunRef,
    artifacts: list[ProviderFrameArtifact],
    retrieved_at: datetime,
) -> ForecastRun:
    """Normalize provider run metadata into a storage-ready run record.

    Args:
        run_ref (ProviderRunRef): Reference to the forecast run.
        artifacts (list[ProviderFrameArtifact]): List of frame artifacts from the provider.
        retrieved_at (datetime): Timestamp when the run was retrieved.
    """
    expected_hours = [artifact.forecast_hour for artifact in artifacts]
    freshness_status = _freshness_status(
        run_time=run_ref.run_time,
        retrieved_at=retrieved_at,
        threshold_hours=run_ref.freshness_threshold_hours,
    )
    return ForecastRun(
        run_id=_run_id(run_ref),
        provider=run_ref.provider,
        model=run_ref.model,
        product=run_ref.product,
        run_time=run_ref.run_time,
        retrieved_at=retrieved_at,
        processed_at=datetime.now(UTC),
        expected_forecast_hours=expected_hours,
        source_urls=[artifact.source_url for artifact in artifacts],
        status=ForecastRunStatus.NORMALIZED,
        freshness_status=freshness_status,
        freshness_threshold_hours=run_ref.freshness_threshold_hours,
        license=run_ref.license,
        license_url=run_ref.license_url,
        redistribution_note=run_ref.redistribution_note,
        attribution=run_ref.attribution,
    )


def normalize_frames(
    run_ref: ProviderRunRef,
    artifacts: list[ProviderFrameArtifact],
    retrieved_at: datetime,
    area: Phase1Area | None = None,
) -> list[ForecastFrame]:
    """Normalize fetched provider artifacts into forecast frame records.

    Args:
        run_ref (ProviderRunRef): Reference to the forecast run.
        artifacts (list[ProviderFrameArtifact]): List of frame artifacts from the provider.
        retrieved_at (datetime): Timestamp when the run was retrieved.
        area (Phase1Area | None): Area configuration. Defaults to ``None``.
    """
    resolved_area = area or Phase1Area()
    processed_at = datetime.now(UTC)
    frames: list[ForecastFrame] = []
    for artifact in artifacts:
        valid_time = run_ref.run_time + timedelta(hours=artifact.forecast_hour)
        window_start = valid_time - timedelta(hours=artifact.accumulation_hours)
        values = list(artifact.values_mm)
        frame_id = _frame_id(run_ref, artifact)
        frames.append(
            ForecastFrame(
                frame_id=frame_id,
                run_id=_run_id(run_ref),
                provider=run_ref.provider,
                model=run_ref.model,
                variable=ForecastVariable.PRECIPITATION,
                statistic=ForecastStatistic.ACCUMULATION,
                unit="mm",
                run_time=run_ref.run_time,
                valid_time=valid_time,
                window_start=window_start,
                window_end=valid_time,
                accumulation_hours=artifact.accumulation_hours,
                provider_accumulation_semantics=artifact.provider_accumulation_semantics,
                forecast_hour=artifact.forecast_hour,
                retrieved_at=retrieved_at,
                processed_at=processed_at,
                area=resolved_area,
                grid=ForecastGrid(
                    resolution_degrees=artifact.grid_resolution_degrees,
                    width=artifact.grid_width,
                    height=artifact.grid_height,
                ),
                values_mm=values,
                source=ForecastSource(
                    url=artifact.source_url,
                    product=run_ref.product,
                    license=run_ref.license,
                    license_url=run_ref.license_url,
                    redistribution_note=run_ref.redistribution_note,
                    attribution=run_ref.attribution,
                    raw_artifact_ref=artifact.raw_artifact_ref,
                ),
                quality=ForecastQuality(
                    status=ForecastRunStatus.NORMALIZED,
                    missing_value_count=0,
                    minimum_mm=min(values),
                    maximum_mm=max(values),
                ),
            )
        )
    return frames


def _freshness_status(
    run_time: datetime,
    retrieved_at: datetime,
    threshold_hours: int,
) -> FreshnessStatus:
    age_hours = (retrieved_at.astimezone(UTC) - run_time.astimezone(UTC)).total_seconds() / 3600
    if age_hours <= threshold_hours:
        return FreshnessStatus.FRESH
    return FreshnessStatus.STALE


def _run_id(run_ref: ProviderRunRef) -> str:
    cycle = run_ref.run_time.strftime("%Y%m%d%H")
    return f"{run_ref.provider}:{run_ref.model}:{cycle}"


def _frame_id(run_ref: ProviderRunRef, artifact: ProviderFrameArtifact) -> str:
    return f"{_run_id(run_ref)}:precipitation:f{artifact.forecast_hour:03d}"
