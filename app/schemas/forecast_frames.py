from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ForecastFrameFreshnessStatus(StrEnum):
    """Public freshness status for forecast frames."""

    FRESH = "fresh"
    DELAYED = "delayed"
    STALE = "stale"
    PARTIAL = "partial"
    FAILED = "failed"


class ForecastFrameArea(BaseModel):
    """Describe the geographic area the frame covers."""

    name: str
    bbox: tuple[float, float, float, float] = Field(
        description="Bounding box in west, south, east, north order.",
    )
    crs: str

    model_config = ConfigDict(populate_by_name=True)


class ForecastFrameGrid(BaseModel):
    """Describe the clipped forecast grid shape."""

    type: str
    resolution_degrees: float = Field(gt=0, serialization_alias="resolutionDegrees")
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    model_config = ConfigDict(populate_by_name=True)


class ForecastFrameSource(BaseModel):
    """Preserve provider provenance for the public contract."""

    url: str
    product: str
    license: str
    attribution: str
    raw_artifact_ref: str = Field(serialization_alias="rawArtifactRef")

    model_config = ConfigDict(populate_by_name=True)


class ForecastFrameQuality(BaseModel):
    """Public quality summary for a single frame."""

    status: str
    missing_value_count: int = Field(ge=0, serialization_alias="missingValueCount")
    minimum_mm: float = Field(ge=0, serialization_alias="min")
    maximum_mm: float = Field(ge=0, serialization_alias="max")

    model_config = ConfigDict(populate_by_name=True)


class ForecastFramePublic(BaseModel):
    """Public, provider-agnostic representation of a normalized forecast frame."""

    frame_id: str = Field(serialization_alias="frameId")
    run_id: str = Field(serialization_alias="runId")
    provider: str
    model: str
    variable: str
    statistic: str
    unit: str
    run_time: datetime = Field(serialization_alias="runTime")
    valid_time: datetime = Field(serialization_alias="validTime")
    window_start: datetime = Field(serialization_alias="windowStart")
    window_end: datetime = Field(serialization_alias="windowEnd")
    accumulation_hours: int = Field(gt=0, serialization_alias="accumulationHours")
    provider_accumulation_semantics: str = Field(
        serialization_alias="providerAccumulationSemantics",
    )
    forecast_hour: int = Field(ge=0, serialization_alias="forecastHour")
    retrieved_at: datetime = Field(serialization_alias="retrievedAt")
    processed_at: datetime = Field(serialization_alias="processedAt")
    area: ForecastFrameArea
    grid: ForecastFrameGrid
    values_mm: list[float] = Field(serialization_alias="valuesMm")
    source: ForecastFrameSource
    quality: ForecastFrameQuality

    model_config = ConfigDict(populate_by_name=True)


class ForecastFrameFreshness(BaseModel):
    """Top-level freshness block for the forecast frames endpoint."""

    status: ForecastFrameFreshnessStatus
    retrieved_at: datetime | None = Field(
        default=None,
        serialization_alias="retrievedAt",
    )
    threshold_hours: int | None = Field(
        default=None,
        gt=0,
        serialization_alias="thresholdHours",
    )
    provider: str | None = None
    model: str | None = None
    run_time: datetime | None = Field(default=None, serialization_alias="runTime")
    frame_count: int = Field(default=0, ge=0, serialization_alias="frameCount")
    reason: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class ForecastFramesQuery(BaseModel):
    """Echo the resolved query parameters back to the caller for transparency."""

    provider: str | None = None
    model: str | None = None
    area: str
    valid_time_from: datetime | None = Field(default=None, serialization_alias="validTimeFrom")
    valid_time_to: datetime | None = Field(default=None, serialization_alias="validTimeTo")

    model_config = ConfigDict(populate_by_name=True)


class ForecastFramesResponse(BaseModel):
    """Public response for the forecast frames endpoint."""

    freshness: ForecastFrameFreshness
    query: ForecastFramesQuery
    frames: list[ForecastFramePublic]

    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={
            "example": {
                "freshness": {
                    "status": "fresh",
                    "retrievedAt": "2026-05-01T04:18:30Z",
                    "thresholdHours": 7,
                    "provider": "gfs",
                    "model": "gfs",
                    "runTime": "2026-05-01T00:00:00Z",
                    "frameCount": 2,
                },
                "query": {
                    "provider": "gfs",
                    "model": "gfs",
                    "area": "hatyai_utapao_songkhla_phase1",
                    "validTimeFrom": None,
                    "validTimeTo": None,
                },
                "frames": [
                    {
                        "frameId": "gfs:gfs:2026050100:precipitation:f006",
                        "runId": "gfs:gfs:2026050100",
                        "provider": "gfs",
                        "model": "gfs",
                        "variable": "precipitation",
                        "statistic": "accumulation",
                        "unit": "mm",
                        "runTime": "2026-05-01T00:00:00Z",
                        "validTime": "2026-05-01T06:00:00Z",
                        "windowStart": "2026-05-01T00:00:00Z",
                        "windowEnd": "2026-05-01T06:00:00Z",
                        "accumulationHours": 6,
                        "providerAccumulationSemantics": (
                            "POC treats APCP as an accumulation over the previous "
                            "forecast interval; real client must preserve GRIB "
                            "step range semantics."
                        ),
                        "forecastHour": 6,
                        "retrievedAt": "2026-05-01T04:18:30Z",
                        "processedAt": "2026-05-01T04:18:30Z",
                        "area": {
                            "name": "hatyai_utapao_songkhla_phase1",
                            "bbox": [100.15, 6.55, 100.95, 7.35],
                            "crs": "EPSG:4326",
                        },
                        "grid": {
                            "type": "regular_lat_lon",
                            "resolutionDegrees": 0.25,
                            "width": 2,
                            "height": 2,
                        },
                        "valuesMm": [4.0, 7.2, 9.6, 5.1],
                        "source": {
                            "url": (
                                "https://nomads.ncep.noaa.gov/pub/data/nccf/com/"
                                "gfs/prod/2026050100/pgrb2.0p25.apcp/f006"
                            ),
                            "product": "pgrb2.0p25.apcp",
                            "license": (
                                "NOAA public data; attribution and redistribution review "
                                "required before production"
                            ),
                            "attribution": "NOAA/NCEP Global Forecast System (GFS)",
                            "rawArtifactRef": ("gfs/gfs/20260501/00/pgrb2.0p25.apcp/f006"),
                        },
                        "quality": {
                            "status": "normalized",
                            "missingValueCount": 0,
                            "min": 4.0,
                            "max": 9.6,
                        },
                    }
                ],
            }
        },
    )
