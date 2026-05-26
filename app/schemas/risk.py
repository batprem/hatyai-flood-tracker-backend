from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import DataFreshness, RiskLevel


class RiskAvailability(StrEnum):
    """Model whether current risk data is usable for public display."""

    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class RiskFreshnessStatus(StrEnum):
    """Model freshness status for current risk inputs."""

    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class RiskUncertaintyLevel(StrEnum):
    """Model qualitative uncertainty in a risk decision."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ThresholdApplied(StrEnum):
    """Model which station alert threshold an observed level reached."""

    DANGER = "danger"
    WARNING = "warning"
    WATCH = "watch"
    NONE = "none"


class WaterLevelContribution(BaseModel):
    """Model one station's threshold-based contribution to current risk."""

    station_id: str = Field(description="Provider-stable station code, e.g. 'X.44'.")
    station_name: str = Field(description="Human-readable station name for display.")
    observed_level_m: float = Field(description="Observed water level in metres.")
    watch_level_m: float | None = Field(
        default=None,
        description="Configured watch alert level in metres, or null when unknown.",
    )
    warning_level_m: float | None = Field(
        default=None,
        description="Configured warning alert level in metres, or null when unknown.",
    )
    danger_level_m: float | None = Field(
        default=None,
        description="Configured danger alert level in metres, or null when unknown.",
    )
    threshold_applied: ThresholdApplied = Field(
        description="Highest configured threshold the observed level reached."
    )
    risk_contribution: RiskLevel = Field(
        description="Per-station risk level implied by the applied threshold."
    )


class RiskSignal(BaseModel):
    """Model one inspectable input to the current risk decision."""

    name: str
    value: str
    level: RiskLevel
    detail: str
    source: str | None = None
    observed_at: datetime | None = None
    valid_at: datetime | None = None
    window_hours: int | None = Field(default=None, gt=0)


class RiskCoverage(BaseModel):
    """Describe basin input coverage used by the current risk decision."""

    forecast_cells_expected: int = Field(ge=0)
    forecast_cells_available: int = Field(ge=0)
    basin_coverage_ratio: float = Field(ge=0, le=1)
    water_stations_expected: int = Field(ge=0)
    water_stations_available: int = Field(ge=0)
    elevated_signal_count: int = Field(ge=0)


class RiskUncertainty(BaseModel):
    """Describe uncertainty drivers for public risk interpretation."""

    level: RiskUncertaintyLevel
    reasons: list[str]


class RiskMapProperties(BaseModel):
    """Model GeoJSON-compatible risk properties for map layers."""

    area_id: str
    level: RiskLevel
    score: int = Field(ge=0, le=3)
    primary_driver: str | None = None
    generated_at: datetime
    valid_at: datetime | None = None
    availability: RiskAvailability
    freshness_status: RiskFreshnessStatus
    uncertainty_level: RiskUncertaintyLevel
    source: str
    model_run_time: datetime | None = None
    latest_source_retrieved_at: datetime | None = None
    is_official_warning: bool = False


class CurrentRiskResponse(BaseModel):
    """Model the current public flood risk summary."""

    freshness: DataFreshness
    availability: RiskAvailability = RiskAvailability.AVAILABLE
    freshness_status: RiskFreshnessStatus = RiskFreshnessStatus.FRESH
    level: RiskLevel
    score: int = Field(ge=0, le=3)
    computed_level: RiskLevel | None = None
    computed_score: int | None = Field(default=None, ge=0, le=3)
    headline: str
    summary: str
    recommended_action: str
    confidence: float = Field(ge=0, le=1)
    signals: list[RiskSignal]
    coverage: RiskCoverage
    uncertainty: RiskUncertainty
    map_properties: RiskMapProperties
    water_level_contributions: list[WaterLevelContribution] = Field(default_factory=list)
    degraded_inputs: bool = False
    is_official_warning: bool = False

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "freshness": {
                    "generated_at": "2026-05-01T17:30:00Z",
                    "valid_at": "2026-05-01T17:30:00Z",
                    "source": "phase-1-normalized-mock",
                    "is_mock": True,
                },
                "availability": "available",
                "freshness_status": "fresh",
                "level": "yellow",
                "score": 1,
                "computed_level": "yellow",
                "computed_score": 1,
                "headline": "Monitor rain and canal levels",
                "summary": "Moderate rainfall is forecast while U-Tapao Canal is rising.",
                "recommended_action": "Follow local updates and avoid low-lying shortcuts.",
                "confidence": 0.7,
                "signals": [
                    {
                        "name": "6-hour rainfall",
                        "value": "34.5 mm",
                        "level": "yellow",
                        "detail": "Rainfall exceeds the Phase 1 watch threshold.",
                        "source": "phase-1-normalized-mock",
                        "valid_at": "2026-05-01T18:00:00Z",
                        "window_hours": 6,
                    }
                ],
                "coverage": {
                    "forecast_cells_expected": 2,
                    "forecast_cells_available": 2,
                    "basin_coverage_ratio": 1,
                    "water_stations_expected": 2,
                    "water_stations_available": 2,
                    "elevated_signal_count": 1,
                },
                "uncertainty": {
                    "level": "medium",
                    "reasons": ["Mock forecast data is suitable for prototype validation only."],
                },
                "map_properties": {
                    "area_id": "hatyai-basin",
                    "level": "yellow",
                    "score": 1,
                    "primary_driver": "6-hour rainfall",
                    "generated_at": "2026-05-01T17:30:00Z",
                    "valid_at": "2026-05-01T18:00:00Z",
                    "availability": "available",
                    "freshness_status": "fresh",
                    "uncertainty_level": "medium",
                    "source": "phase-1-normalized-mock",
                    "model_run_time": "2026-05-01T17:30:00Z",
                    "latest_source_retrieved_at": "2026-05-01T17:30:00Z",
                    "is_official_warning": False,
                },
                "water_level_contributions": [
                    {
                        "station_id": "X.44",
                        "station_name": "U-Tapao Canal Upstream (Rattaphum)",
                        "observed_level_m": 3.1,
                        "watch_level_m": 2.0,
                        "warning_level_m": 3.0,
                        "danger_level_m": 4.0,
                        "threshold_applied": "warning",
                        "risk_contribution": "orange",
                    }
                ],
                "degraded_inputs": False,
                "is_official_warning": False,
            }
        }
    )
