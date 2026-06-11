from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.freshness import FreshnessReportStatus


class PipelineFreshness(BaseModel):
    """Report one ingestion pipeline's freshness for operator monitoring."""

    pipeline: str = Field(
        description="Pipeline identifier: 'gfs', 'ecmwf', or 'stations'.",
    )
    last_success_at: datetime | None = Field(
        default=None,
        serialization_alias="lastSuccessAt",
        description=(
            "Timestamp of the pipeline's newest successfully ingested record "
            "(forecast run time or station observation time), or null when none stored."
        ),
    )
    age_hours: float | None = Field(
        default=None,
        ge=0,
        serialization_alias="ageHours",
        description="Age in hours of the newest record, or null when none stored.",
    )
    threshold_hours: float = Field(
        gt=0,
        serialization_alias="thresholdHours",
        description="Maximum age in hours before the pipeline is flagged stale.",
    )
    stale: bool = Field(
        description=(
            "True when the pipeline breaches its threshold (status stale, partial, or failed)."
        ),
    )
    status: FreshnessReportStatus = Field(
        description="Freshness classification for the pipeline.",
    )
    reason: str | None = Field(
        default=None,
        description="Operator-readable reason when the pipeline is not fresh.",
    )

    model_config = ConfigDict(populate_by_name=True)


class PipelinesBlock(BaseModel):
    """Group per-pipeline freshness reports for the health response."""

    gfs: PipelineFreshness = Field(description="GFS rainfall-forecast ingestion pipeline.")
    ecmwf: PipelineFreshness = Field(
        description="ECMWF Open Data rainfall-forecast ingestion pipeline."
    )
    stations: PipelineFreshness = Field(
        description="ThaiWater station-observation ingestion pipeline."
    )

    model_config = ConfigDict(populate_by_name=True)


class DataQualityBlock(BaseModel):
    """Summarize per-source data freshness for operator health monitoring."""

    gfs_run_age_hours: float | None = Field(
        default=None,
        ge=0,
        serialization_alias="gfsRunAgeHours",
        description="Age in hours of the latest successful GFS run, or null when none stored.",
    )
    ecmwf_run_age_hours: float | None = Field(
        default=None,
        ge=0,
        serialization_alias="ecmwfRunAgeHours",
        description="Age in hours of the latest successful ECMWF run, or null when none stored.",
    )
    station_observation_age_hours: float | None = Field(
        default=None,
        ge=0,
        serialization_alias="stationObservationAgeHours",
        description="Age in hours of the newest station observation, or null when none stored.",
    )
    gfs_freshness_status: FreshnessReportStatus = Field(
        serialization_alias="gfsFreshnessStatus",
        description="Freshness classification for the GFS source.",
    )
    ecmwf_freshness_status: FreshnessReportStatus = Field(
        serialization_alias="ecmwfFreshnessStatus",
        description="Freshness classification for the ECMWF source.",
    )
    station_freshness_status: FreshnessReportStatus = Field(
        serialization_alias="stationFreshnessStatus",
        description="Freshness classification for the station-observation source.",
    )

    model_config = ConfigDict(populate_by_name=True)


class HealthResponse(BaseModel):
    """Model backend health status."""

    status: str
    service: str
    checked_at: datetime
    data_quality: DataQualityBlock | None = Field(
        default=None,
        serialization_alias="dataQuality",
        description="Per-source data-quality block; null when quality cannot be computed.",
    )
    pipelines: PipelinesBlock | None = Field(
        default=None,
        description=(
            "Per-pipeline freshness block (gfs, ecmwf, stations); "
            "null when freshness cannot be computed."
        ),
    )

    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={
            "example": {
                "status": "ok",
                "service": "Hat Yai Flood Warning API",
                "checked_at": "2026-05-01T17:30:00Z",
                "dataQuality": {
                    "gfsRunAgeHours": 4.2,
                    "ecmwfRunAgeHours": 8.1,
                    "stationObservationAgeHours": 0.5,
                    "gfsFreshnessStatus": "fresh",
                    "ecmwfFreshnessStatus": "fresh",
                    "stationFreshnessStatus": "fresh",
                },
                "pipelines": {
                    "gfs": {
                        "pipeline": "gfs",
                        "lastSuccessAt": "2026-05-01T12:00:00Z",
                        "ageHours": 4.2,
                        "thresholdHours": 6.0,
                        "stale": False,
                        "status": "fresh",
                        "reason": None,
                    },
                    "ecmwf": {
                        "pipeline": "ecmwf",
                        "lastSuccessAt": "2026-05-01T06:00:00Z",
                        "ageHours": 8.1,
                        "thresholdHours": 12.0,
                        "stale": False,
                        "status": "fresh",
                        "reason": None,
                    },
                    "stations": {
                        "pipeline": "stations",
                        "lastSuccessAt": "2026-05-01T17:00:00Z",
                        "ageHours": 0.5,
                        "thresholdHours": 3.0,
                        "stale": False,
                        "status": "fresh",
                        "reason": None,
                    },
                },
            }
        },
    )
