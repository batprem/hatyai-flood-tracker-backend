from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ForecastProvider(StrEnum):
    """Identify a normalized forecast data provider."""

    GFS = "gfs"
    ECMWF_OPEN_DATA = "ecmwf_open_data"


class ForecastVariable(StrEnum):
    """Model supported forecast variables."""

    PRECIPITATION = "precipitation"


class ForecastStatistic(StrEnum):
    """Describe how forecast values are aggregated over time."""

    ACCUMULATION = "accumulation"


class ForecastRunStatus(StrEnum):
    """Track ingestion progress for a provider model run."""

    DISCOVERED = "discovered"
    FETCHED = "fetched"
    NORMALIZED = "normalized"
    STORED = "stored"
    PARTIAL = "partial"
    FAILED = "failed"


class FreshnessStatus(StrEnum):
    """Model consistent data freshness states."""

    FRESH = "fresh"
    DELAYED = "delayed"
    STALE = "stale"
    PARTIAL = "partial"
    FAILED = "failed"


class Phase1Area(BaseModel):
    """Describe the Phase 1 forecast processing area."""

    name: str = "hatyai_utapao_songkhla_phase1"
    bbox: tuple[float, float, float, float] = Field(
        default=(100.15, 6.55, 100.95, 7.35),
        description="Bounding box in west, south, east, north order.",
    )
    crs: str = "EPSG:4326"
    validation_note: str = (
        "Conservative Phase 1 bounding box pending authoritative basin GIS validation."
    )


class ForecastGrid(BaseModel):
    """Describe a clipped forecast grid."""

    type: str = "regular_lat_lon"
    resolution_degrees: float = Field(gt=0, serialization_alias="resolutionDegrees")
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    model_config = ConfigDict(populate_by_name=True)


class ForecastSource(BaseModel):
    """Preserve provider provenance and license review state.

    Per ``docs/data-sources.md`` ("License Notes" production gate), a provider
    record must carry attribution text, license/terms URL, redistribution and
    caching decision, and source-owner name. ``license`` stays the short SPDX
    identifier (or ``"review-required"`` until cleared) while ``licenseUrl``
    and ``redistributionNote`` cover the production-gate detail. Both detail
    fields are optional so existing dry-run fixtures keep working, but the
    real provider clients populate them.
    """

    url: str
    product: str
    license: str
    license_url: str | None = Field(default=None, serialization_alias="licenseUrl")
    redistribution_note: str | None = Field(default=None, serialization_alias="redistributionNote")
    attribution: str
    raw_artifact_ref: str = Field(serialization_alias="rawArtifactRef")

    model_config = ConfigDict(populate_by_name=True)


class ForecastQuality(BaseModel):
    """Summarize data quality checks for a normalized frame."""

    status: ForecastRunStatus
    missing_value_count: int = Field(ge=0, serialization_alias="missingValueCount")
    minimum_mm: float = Field(ge=0, serialization_alias="min")
    maximum_mm: float = Field(ge=0, serialization_alias="max")

    model_config = ConfigDict(populate_by_name=True)


class ForecastRun(BaseModel):
    """Model one provider model cycle as a normalized run record."""

    run_id: str = Field(serialization_alias="runId")
    provider: ForecastProvider
    model: str
    product: str
    run_time: datetime = Field(serialization_alias="runTime")
    retrieved_at: datetime = Field(serialization_alias="retrievedAt")
    processed_at: datetime | None = Field(default=None, serialization_alias="processedAt")
    expected_forecast_hours: list[int] = Field(serialization_alias="expectedForecastHours")
    source_urls: list[str] = Field(serialization_alias="sourceUrls")
    status: ForecastRunStatus
    freshness_status: FreshnessStatus = Field(serialization_alias="freshnessStatus")
    freshness_threshold_hours: int = Field(
        gt=0,
        serialization_alias="freshnessThresholdHours",
    )
    license: str
    license_url: str | None = Field(default=None, serialization_alias="licenseUrl")
    redistribution_note: str | None = Field(default=None, serialization_alias="redistributionNote")
    attribution: str
    error_reason: str | None = Field(default=None, serialization_alias="errorReason")

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("run_time", "retrieved_at", "processed_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        """Require timezone-aware datetimes for storage adapters."""
        if value is None:
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "forecast timestamps must be timezone-aware"
            raise ValueError(msg)
        return value.astimezone(UTC)


class ForecastFrame(BaseModel):
    """Model one normalized rainfall forecast frame."""

    frame_id: str = Field(serialization_alias="frameId")
    run_id: str = Field(serialization_alias="runId")
    provider: ForecastProvider
    model: str
    variable: ForecastVariable
    statistic: ForecastStatistic
    unit: str = "mm"
    run_time: datetime = Field(serialization_alias="runTime")
    valid_time: datetime = Field(serialization_alias="validTime")
    window_start: datetime = Field(serialization_alias="windowStart")
    window_end: datetime = Field(serialization_alias="windowEnd")
    accumulation_hours: int = Field(gt=0, serialization_alias="accumulationHours")
    provider_accumulation_semantics: str = Field(
        serialization_alias="providerAccumulationSemantics"
    )
    forecast_hour: int = Field(ge=0, serialization_alias="forecastHour")
    retrieved_at: datetime = Field(serialization_alias="retrievedAt")
    processed_at: datetime = Field(serialization_alias="processedAt")
    area: Phase1Area
    grid: ForecastGrid
    values_mm: list[float] = Field(serialization_alias="valuesMm")
    source: ForecastSource
    quality: ForecastQuality

    model_config = ConfigDict(populate_by_name=True)

    @field_validator(
        "run_time",
        "valid_time",
        "window_start",
        "window_end",
        "retrieved_at",
        "processed_at",
    )
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        """Require timezone-aware datetimes for storage adapters."""
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "forecast timestamps must be timezone-aware"
            raise ValueError(msg)
        return value.astimezone(UTC)

    @field_validator("values_mm")
    @classmethod
    def require_non_negative_values(cls, value: list[float]) -> list[float]:
        """Reject negative rainfall values after unit normalization."""
        if not value:
            msg = "forecast frame values cannot be empty"
            raise ValueError(msg)
        if any(rainfall < 0 for rainfall in value):
            msg = "forecast rainfall values must be non-negative"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def validate_time_window(self) -> "ForecastFrame":
        """Validate explicit accumulation window semantics."""
        if self.window_end != self.valid_time:
            msg = "window_end must match valid_time for accumulation frames"
            raise ValueError(msg)
        if self.window_start >= self.window_end:
            msg = "window_start must be before window_end"
            raise ValueError(msg)
        expected_hours = int((self.window_end - self.window_start).total_seconds() // 3600)
        if expected_hours != self.accumulation_hours:
            msg = "accumulation_hours must match window_start/window_end"
            raise ValueError(msg)
        return self
