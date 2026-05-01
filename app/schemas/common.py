from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RiskLevel(StrEnum):
    """Represent the Phase 1 rule-based flood risk levels."""

    GREEN = "green"
    YELLOW = "yellow"
    ORANGE = "orange"
    RED = "red"


class Coordinates(BaseModel):
    """Represent a geographic point in WGS84 coordinates."""

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class LocalizedName(BaseModel):
    """Represent a Thai and English display name."""

    th: str
    en: str


class DataFreshness(BaseModel):
    """Describe freshness and provenance for normalized public data."""

    generated_at: datetime
    valid_at: datetime | None = None
    source: str
    is_mock: bool = True

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "generated_at": "2026-05-01T17:30:00Z",
                "valid_at": "2026-05-01T18:00:00Z",
                "source": "phase-1-normalized-mock",
                "is_mock": True,
            }
        }
    )
