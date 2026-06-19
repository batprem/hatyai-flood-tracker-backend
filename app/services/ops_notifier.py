"""Define the operator-notification dispatch interface for pipeline alerts.

The data-quality monitor (``app.services.data_quality``) detects ingestion
failures and staleness breaches across the GFS, ECMWF, and station pipelines.
Detected conditions are handed to an :class:`OpsNotifier` — a small dispatch
interface that decouples *detection* from *delivery*:

- Phase 4 ships :class:`LoggingOpsNotifier`, which emits one structured JSON
  ``ERROR`` log line per event so alerts are queryable in the Railway log
  stream (``event:"ops_pipeline_alert"``).
- HFT-81 adds :class:`LineOpsNotifier`, which wraps :class:`LoggingOpsNotifier`
  and also broadcasts to the LINE ops channel via the Messaging API.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from app.services.line_notify import send_line_notify

logger = logging.getLogger(__name__)

OPS_ALERT_EVENT = "ops_pipeline_alert"


class PipelineName(StrEnum):
    """Identify a monitored ingestion pipeline."""

    GFS = "gfs"
    ECMWF = "ecmwf"
    STATIONS = "stations"


class OpsEventKind(StrEnum):
    """Classify the operator-facing condition detected on a pipeline."""

    INGESTION_FAILURE = "ingestion_failure"
    INGESTION_PARTIAL = "ingestion_partial"
    STALENESS_BREACH = "staleness_breach"


@dataclass(frozen=True, slots=True)
class OpsEvent:
    """Describe one detected pipeline condition handed to the ops notifier."""

    kind: OpsEventKind
    pipeline: PipelineName
    status: str
    age_hours: float | None
    threshold_hours: float
    reason: str | None
    detected_at: datetime


class OpsNotifier(Protocol):
    """Dispatch detected pipeline conditions to an operator channel."""

    async def notify(self, event: OpsEvent) -> None:
        """Deliver one ops event to the underlying channel.

        Args:
            event: The detected pipeline condition to deliver.
        """
        ...


class LoggingOpsNotifier:
    """Deliver ops events as structured JSON ``ERROR`` log lines.

    This is the Phase 4 default delivery channel: each event produces a single
    ``logging.error`` line whose message is a JSON object so it is queryable in
    the Railway log stream (for example ``event:"ops_pipeline_alert"``). Real
    operator delivery (LINE ops channel) is owned by HFT-81 and will implement
    the same :class:`OpsNotifier` protocol.
    """

    async def notify(self, event: OpsEvent) -> None:
        """Log one ops event as a structured JSON ERROR line.

        Args:
            event: The detected pipeline condition to log.
        """
        logger.error(
            json.dumps(
                {
                    "event": OPS_ALERT_EVENT,
                    "kind": event.kind.value,
                    "pipeline": event.pipeline.value,
                    "status": event.status,
                    "ageHours": round(event.age_hours, 2) if event.age_hours is not None else None,
                    "thresholdHours": event.threshold_hours,
                    "reason": event.reason,
                    "detectedAt": event.detected_at.astimezone(UTC).isoformat(),
                },
                sort_keys=True,
            )
        )


def _format_ops_message(event: OpsEvent) -> str:
    lines = [
        f"[HFT OPS] {event.kind.value} on {event.pipeline.value}",
        f"Status: {event.status} | Age: {round(event.age_hours, 1)}h"
        f" (threshold: {event.threshold_hours}h)"
        if event.age_hours is not None
        else f"Status: {event.status} | Threshold: {event.threshold_hours}h",
    ]
    if event.reason is not None:
        lines.append(f"Reason: {event.reason}")
    return "\n".join(lines)


class LineOpsNotifier:
    """Deliver ops events to a LINE ops channel AND log them as structured JSON.

    Wraps :class:`LoggingOpsNotifier` so structured logs are always emitted.
    When ``token`` is non-empty the event is also broadcast to the LINE ops
    channel via the Messaging API. When ``token`` is empty LINE delivery is
    skipped and a warning is logged — matching the behaviour of the public
    alert channel.
    """

    def __init__(self, token: str) -> None:
        self._token = token
        self._logging_notifier = LoggingOpsNotifier()

    async def notify(self, event: OpsEvent) -> None:
        """Log the ops event as structured JSON and optionally broadcast to LINE.

        Args:
            event: The detected pipeline condition to deliver.
        """
        await self._logging_notifier.notify(event)
        if not self._token:
            logger.warning(
                "LINE ops token is empty; skipping LINE delivery for %s on %s",
                event.kind.value,
                event.pipeline.value,
            )
            return
        await send_line_notify(self._token, _format_ops_message(event))
