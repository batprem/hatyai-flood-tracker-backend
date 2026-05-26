from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.freshness import FreshnessReportStatus


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
            }
        },
    )
