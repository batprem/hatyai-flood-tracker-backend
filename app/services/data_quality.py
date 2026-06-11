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
from app.services.ops_notifier import (
    LoggingOpsNotifier,
    OpsEvent,
    OpsEventKind,
    OpsNotifier,
    PipelineName,
)

logger = logging.getLogger(__name__)

GFS_PROVIDER = ForecastProvider.GFS.value
ECMWF_PROVIDER = ForecastProvider.ECMWF_OPEN_DATA.value

STALE_DATA_ALERT_EVENT = "data_quality_stale_alert"

_SOURCE_TO_PIPELINE: dict[str, PipelineName] = {
    GFS_PROVIDER: PipelineName.GFS,
    ECMWF_PROVIDER: PipelineName.ECMWF,
    "station": PipelineName.STATIONS,
}

_STATUS_TO_EVENT_KIND: dict[FreshnessStatus, OpsEventKind] = {
    FreshnessStatus.FAILED: OpsEventKind.INGESTION_FAILURE,
    FreshnessStatus.PARTIAL: OpsEventKind.INGESTION_PARTIAL,
    FreshnessStatus.STALE: OpsEventKind.STALENESS_BREACH,
}


@dataclass(frozen=True, slots=True)
class SourceQuality:
    """Hold the age and freshness classification for one monitored source."""

    source: str
    age_hours: float | None
    threshold_hours: float
    status: FreshnessStatus
    reason: str | None
    last_success_at: datetime | None = None


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
            last_success_at=None,
        )
    success_time = run_time if isinstance(run_time, datetime) else None
    if run_status_value == ForecastRunStatus.PARTIAL.value:
        return SourceQuality(
            source=source,
            age_hours=age,
            threshold_hours=threshold_hours,
            status=FreshnessStatus.PARTIAL,
            reason=reason_text or "latest run produced partial coverage",
            last_success_at=success_time,
        )
    if age > threshold_hours:
        return SourceQuality(
            source=source,
            age_hours=age,
            threshold_hours=threshold_hours,
            status=FreshnessStatus.STALE,
            reason=f"latest run age {age:.1f}h exceeds {threshold_hours:.1f}h threshold",
            last_success_at=success_time,
        )
    return SourceQuality(
        source=source,
        age_hours=age,
        threshold_hours=threshold_hours,
        status=FreshnessStatus.FRESH,
        reason=None,
        last_success_at=success_time,
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
            last_success_at=newest,
        )
    return SourceQuality(
        source="station",
        age_hours=age,
        threshold_hours=threshold_hours,
        status=FreshnessStatus.FRESH,
        reason=None,
        last_success_at=newest,
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

    Phase 2 legacy path: each source whose status is ``stale``, ``partial``,
    or ``failed`` produces a single ``logging.error`` line whose message is a
    JSON object so it is queryable in the Railway log stream (for example
    ``event:"data_quality_stale_alert"``). Since Phase 4 (HFT-75) the
    scheduler's :func:`evaluate_and_alert` dispatches through the
    :class:`~app.services.ops_notifier.OpsNotifier` interface instead
    (``event:"ops_pipeline_alert"``); this function remains for callers that
    still want the legacy event name.

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


def ops_events_from_snapshot(snapshot: DataQualitySnapshot) -> list[OpsEvent]:
    """Map a data-quality snapshot's breaching sources to typed ops events.

    Each source whose freshness status breaches its threshold (``failed``,
    ``partial``, or ``stale``) produces one :class:`OpsEvent` whose ``kind``
    distinguishes an ingestion failure from a staleness breach so downstream
    delivery channels (HFT-81 LINE ops channel) can route or rate-limit by
    condition.

    Args:
        snapshot: The computed data-quality snapshot to evaluate.

    Returns:
        One ops event per breaching source, in GFS, ECMWF, station order; empty
        when every source is fresh.
    """
    events: list[OpsEvent] = []
    for source in snapshot.stale_or_failed():
        kind = _STATUS_TO_EVENT_KIND.get(source.status)
        pipeline = _SOURCE_TO_PIPELINE.get(source.source)
        if kind is None or pipeline is None:  # pragma: no cover - defensive
            continue
        events.append(
            OpsEvent(
                kind=kind,
                pipeline=pipeline,
                status=source.status.value,
                age_hours=source.age_hours,
                threshold_hours=source.threshold_hours,
                reason=source.reason,
                detected_at=snapshot.evaluated_at,
            )
        )
    return events


async def dispatch_ops_alerts(
    snapshot: DataQualitySnapshot,
    notifier: OpsNotifier,
) -> list[OpsEvent]:
    """Dispatch every breaching source in the snapshot to the ops notifier.

    Args:
        snapshot: The computed data-quality snapshot to evaluate.
        notifier: Ops notifier that delivers each detected event.

    Returns:
        The ops events that were dispatched; empty when every source is fresh.
    """
    events = ops_events_from_snapshot(snapshot)
    for event in events:
        await notifier.notify(event)
    return events


async def evaluate_and_alert(
    forecast_repository: ForecastRepository,
    station_repository: StationObservationRepository | None,
    settings: Settings,
    *,
    now: datetime | None = None,
    notifier: OpsNotifier | None = None,
) -> DataQualitySnapshot:
    """Compute data quality and dispatch ops alerts in one scheduler-friendly call.

    Breaching sources are handed to the ops notifier interface; with the
    default :class:`LoggingOpsNotifier` each event becomes one structured
    ERROR log line (``event:"ops_pipeline_alert"``) queryable in the Railway
    log stream. HFT-81 swaps in a LINE-backed notifier without changing this
    detection path.

    Args:
        forecast_repository: Repository providing forecast-run freshness summaries.
        station_repository: Station observation repository, or ``None`` when unconfigured.
        settings: Application settings carrying the per-source freshness thresholds.
        now: Reference time for age computation. Defaults to ``datetime.now(UTC)``.
        notifier: Ops notifier receiving detected events. Defaults to ``None``,
            which uses :class:`LoggingOpsNotifier`.

    Returns:
        The computed data-quality snapshot, after any breaching sources have
        been dispatched to the notifier.
    """
    snapshot = await compute_data_quality(
        forecast_repository,
        station_repository,
        settings,
        now=now,
    )
    await dispatch_ops_alerts(snapshot, notifier if notifier is not None else LoggingOpsNotifier())
    return snapshot
