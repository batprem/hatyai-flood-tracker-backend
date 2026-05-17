"""Public schema for the ingestion freshness endpoint."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class FreshnessReportStatus(StrEnum):
    """Public freshness status mirroring :class:`FreshnessStatus`."""

    FRESH = "fresh"
    DELAYED = "delayed"
    STALE = "stale"
    PARTIAL = "partial"
    FAILED = "failed"


class FreshnessReport(BaseModel):
    """Latest forecast-run freshness exposed for operators and the frontend.

    The endpoint reads the same ``freshness_summary`` document used by the
    forecast frames endpoint so dashboards and the public alert view can
    surface ingestion health (last run, retrieval time, threshold, frame
    count) without re-fetching the full frame payload.
    """

    status: FreshnessReportStatus
    provider: str | None = None
    model: str | None = None
    run_time: datetime | None = Field(default=None, serialization_alias="runTime")
    retrieved_at: datetime | None = Field(default=None, serialization_alias="retrievedAt")
    threshold_hours: int | None = Field(
        default=None,
        gt=0,
        serialization_alias="thresholdHours",
    )
    frame_count: int = Field(default=0, ge=0, serialization_alias="frameCount")
    reason: str | None = None

    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={
            "example": {
                "status": "fresh",
                "provider": "gfs",
                "model": "gfs",
                "runTime": "2026-05-08T00:00:00Z",
                "retrievedAt": "2026-05-08T00:05:30Z",
                "thresholdHours": 7,
                "frameCount": 4,
                "reason": None,
            }
        },
    )
