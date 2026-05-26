"""Compute data-quality and stale-data signals for forecast and station sources.

This module centralizes the age + freshness classification used by both the
``/health`` ``data_quality`` block and the ingestion scheduler's stale-data
alert so the operator-facing view and the log-based alert never drift. Rules
are deliberately simple and auditable per the Phase 1 risk-logic conventions:

- A source whose latest record's age is at or below its threshold is ``fresh``.
- A source that has a record but exceeds its threshold is ``stale``.
- A source whose latest forecast run ended with ingestion ``status=FAILED``
  (or has no records at all) is ``failed``.
- A source whose latest forecast run ended with ingestion ``status=PARTIAL``
  is ``partial``.

The ``failed``/``partial`` precedence over age means an actively broken run is
never masked by a recent ``retrievedAt`` timestamp.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.config import Settings
from app.ingestion.models import ForecastProvider, ForecastRunStatus, FreshnessStatus
from app.ingestion.repository import ForecastRepository, MongoDocument
from app.ingestion.station_repository import StationObservationRepository

logger = logging.getLogger(__name__)

GFS_PROVIDER = ForecastProvider.GFS.value
ECMWF_PROVIDER = ForecastProvider.ECMWF_OPEN_DATA.value

STALE_DATA_ALERT_EVENT = "data_quality_stale_alert"


@dataclass(frozen=True, slots=True)
class SourceQuality:
    """Hold the age and freshness classification for one monitored source."""

    source: str
    age_hours: float | None
    threshold_hours: float
    status: FreshnessStatus
    reason: str | None


@dataclass(frozen=True, slots=True)
class DataQualitySnapshot:
    """Bundle per-source data-quality results for one evaluation moment."""

    evaluated_at: datetime
    gfs: SourceQuality
    ecmwf: SourceQuality
    station: SourceQuality

    def stale_or_failed(self) -> list[SourceQuality]:
        """Return the sources whose status exceeds their freshness threshold.

        Returns:
            The sources whose status is ``stale``, ``partial``, or ``failed``,
            in GFS, ECMWF, station order.
        """
        breaching = {FreshnessStatus.STALE, FreshnessStatus.PARTIAL, FreshnessStatus.FAILED}
        return [
            source for source in (self.gfs, self.ecmwf, self.station) if source.status in breaching
        ]


def _age_hours(reference: datetime, observed: datetime) -> float:
    """Return the non-negative age in hours between ``observed`` and ``reference``."""
    delta = reference.astimezone(UTC) - observed.astimezone(UTC)
    return max(delta.total_seconds() / 3600, 0.0)


def _classify_forecast_source(
    source: str,
    summary: MongoDocument,
    threshold_hours: float,
    *,
    now: datetime,
) -> SourceQuality:
    """Classify a forecast source from its latest-run freshness summary.

    Args:
        source: Source label, e.g. ``'gfs'``.
        summary: Repository freshness summary for the latest run of the source.
        threshold_hours: Maximum run age before the source is flagged stale.
        now: Current reference time.

    Returns:
        The source's age and freshness classification.
    """
    raw_status = summary.get("status")
    status_value = raw_status.value if isinstance(raw_status, FreshnessStatus) else raw_status
    run_status = summary.get("runStatus")
    run_status_value = run_status.value if isinstance(run_status, ForecastRunStatus) else run_status
    reason = summary.get("reason")
    reason_text = reason if isinstance(reason, str) else None

    run_time = summary.get("runTime")
    age = _age_hours(now, run_time) if isinstance(run_time, datetime) else None

    if status_value == FreshnessStatus.FAILED.value or age is None:
        return SourceQuality(
            source=source,
            age_hours=age,
            threshold_hours=threshold_hours,
            status=FreshnessStatus.FAILED,
            reason=reason_text or "no successful run stored",
        )
    if run_status_value == ForecastRunStatus.PARTIAL.value:
        return SourceQuality(
            source=source,
            age_hours=age,
            threshold_hours=threshold_hours,
            status=FreshnessStatus.PARTIAL,
            reason=reason_text or "latest run produced partial coverage",
        )
    if age > threshold_hours:
        return SourceQuality(
            source=source,
            age_hours=age,
            threshold_hours=threshold_hours,
            status=FreshnessStatus.STALE,
            reason=f"latest run age {age:.1f}h exceeds {threshold_hours:.1f}h threshold",
        )
    return SourceQuality(
        source=source,
        age_hours=age,
        threshold_hours=threshold_hours,
        status=FreshnessStatus.FRESH,
        reason=None,
    )


async def _classify_station_source(
    station_repository: StationObservationRepository | None,
    threshold_hours: float,
    *,
    now: datetime,
) -> SourceQuality:
    """Classify the station-observation source from its newest record.

    Args:
        station_repository: Station observation repository, or ``None`` when unconfigured.
        threshold_hours: Maximum observation age before the source is flagged stale.
        now: Current reference time.

    Returns:
        The station source's age and freshness classification.
    """
    if station_repository is None:
        return SourceQuality(
            source="station",
            age_hours=None,
            threshold_hours=threshold_hours,
            status=FreshnessStatus.FAILED,
            reason="station repository not configured",
        )
    observations = await station_repository.latest_per_station()
    if not observations:
        return SourceQuality(
            source="station",
            age_hours=None,
            threshold_hours=threshold_hours,
            status=FreshnessStatus.FAILED,
            reason="no station observations stored",
        )
    newest = max(observation.observed_at for observation in observations)
    age = _age_hours(now, newest)
    if age > threshold_hours:
        return SourceQuality(
            source="station",
            age_hours=age,
            threshold_hours=threshold_hours,
            status=FreshnessStatus.STALE,
            reason=f"newest observation age {age:.1f}h exceeds {threshold_hours:.1f}h threshold",
        )
    return SourceQuality(
        source="station",
        age_hours=age,
        threshold_hours=threshold_hours,
        status=FreshnessStatus.FRESH,
        reason=None,
    )


async def compute_data_quality(
    forecast_repository: ForecastRepository,
    station_repository: StationObservationRepository | None,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> DataQualitySnapshot:
    """Compute per-source data-quality classifications for GFS, ECMWF, and stations.

    The latest run for each forecast provider is read through the repository's
    freshness summary, and the newest station observation is read through the
    station repository. Each source is classified independently against its
    configured freshness threshold so both the ``/health`` endpoint and the
    ingestion scheduler can act on the same auditable result.

    Args:
        forecast_repository: Repository providing forecast-run freshness summaries.
        station_repository: Station observation repository, or ``None`` when unconfigured.
        settings: Application settings carrying the per-source freshness thresholds.
        now: Reference time for age computation. Defaults to ``datetime.now(UTC)``.

    Returns:
        A snapshot bundling the GFS, ECMWF, and station classifications.
    """
    reference = now or datetime.now(UTC)
    gfs_summary = await forecast_repository.freshness_summary(provider=GFS_PROVIDER)
    ecmwf_summary = await forecast_repository.freshness_summary(provider=ECMWF_PROVIDER)
    gfs = _classify_forecast_source(
        GFS_PROVIDER,
        gfs_summary,
        settings.data_quality_gfs_max_age_hours,
        now=reference,
    )
    ecmwf = _classify_forecast_source(
        ECMWF_PROVIDER,
        ecmwf_summary,
        settings.data_quality_ecmwf_max_age_hours,
        now=reference,
    )
    station = await _classify_station_source(
        station_repository,
        settings.data_quality_station_max_age_hours,
        now=reference,
    )
    return DataQualitySnapshot(
        evaluated_at=reference,
        gfs=gfs,
        ecmwf=ecmwf,
        station=station,
    )


def emit_stale_data_alert(snapshot: DataQualitySnapshot) -> bool:
    """Emit one structured ERROR log line per breaching source for log-based alerting.

    Phase 2 alerting is log-based only (no PagerDuty/email/LINE): each source
    whose status is ``stale``, ``partial``, or ``failed`` produces a single
    ``logging.error`` line whose message is a JSON object so it is queryable in
    the Railway log stream (for example ``event:"data_quality_stale_alert"``).

    Args:
        snapshot: The computed data-quality snapshot to evaluate.

    Returns:
        True when at least one breaching source was logged, False otherwise.
    """
    breaching = snapshot.stale_or_failed()
    for source in breaching:
        logger.error(
            json.dumps(
                {
                    "event": STALE_DATA_ALERT_EVENT,
                    "source": source.source,
                    "status": source.status.value,
                    "ageHours": round(source.age_hours, 2)
                    if source.age_hours is not None
                    else None,
                    "thresholdHours": source.threshold_hours,
                    "reason": source.reason,
                    "evaluatedAt": snapshot.evaluated_at.astimezone(UTC).isoformat(),
                },
                sort_keys=True,
            )
        )
    return bool(breaching)


async def evaluate_and_alert(
    forecast_repository: ForecastRepository,
    station_repository: StationObservationRepository | None,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> DataQualitySnapshot:
    """Compute data quality and emit stale-data alerts in one scheduler-friendly call.

    Args:
        forecast_repository: Repository providing forecast-run freshness summaries.
        station_repository: Station observation repository, or ``None`` when unconfigured.
        settings: Application settings carrying the per-source freshness thresholds.
        now: Reference time for age computation. Defaults to ``datetime.now(UTC)``.

    Returns:
        The computed data-quality snapshot, after any breaching sources have been logged.
    """
    snapshot = await compute_data_quality(
        forecast_repository,
        station_repository,
        settings,
        now=now,
    )
    emit_stale_data_alert(snapshot)
    return snapshot
