from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Coordinates, DataFreshness, LocalizedName, RiskLevel


class WaterLevelTrend(StrEnum):
    """Model the recent water-level trend direction."""

    RISING = "rising"
    STEADY = "steady"
    FALLING = "falling"


class WaterStationLevel(BaseModel):
    """Model a normalized water-level observation at a station."""

    station_id: str
    station_name: LocalizedName
    canal_or_lake: LocalizedName
    location: Coordinates
    observed_at: datetime
    water_level_m: float = Field(ge=0)
    warning_level_m: float = Field(gt=0)
    critical_level_m: float = Field(gt=0)
    trend: WaterLevelTrend
    risk_level: RiskLevel


class WaterLevelResponse(BaseModel):
    """Model current water-level observations for public display."""

    freshness: DataFreshness
    stations: list[WaterStationLevel]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "freshness": {
                    "generated_at": "2026-05-01T17:30:00Z",
                    "valid_at": "2026-05-01T17:25:00Z",
                    "source": "phase-1-normalized-mock",
                    "is_mock": True,
                },
                "stations": [
                    {
                        "station_id": "HY-UTP-001",
                        "station_name": {
                            "th": "สะพานคลองอู่ตะเภา หาดใหญ่",
                            "en": "U-Tapao Canal Bridge, Hat Yai",
                        },
                        "canal_or_lake": {
                            "th": "คลองอู่ตะเภา",
                            "en": "U-Tapao Canal",
                        },
                        "location": {"latitude": 7.0167, "longitude": 100.4708},
                        "observed_at": "2026-05-01T17:25:00Z",
                        "water_level_m": 2.4,
                        "warning_level_m": 2.8,
                        "critical_level_m": 3.2,
                        "trend": "rising",
                        "risk_level": "yellow",
                    }
                ],
            }
        }
    )
