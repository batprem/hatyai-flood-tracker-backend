from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Coordinates, DataFreshness, LocalizedName, RiskLevel


class RainfallForecastPoint(BaseModel):
    """Model normalized basin rainfall forecast data."""

    basin_id: str
    basin_name: LocalizedName
    centroid: Coordinates
    forecast_time: datetime
    lead_time_hours: int = Field(ge=0)
    rainfall_mm: float = Field(ge=0)
    accumulation_hours: int = Field(gt=0)
    risk_level: RiskLevel


class RainfallForecastResponse(BaseModel):
    """Model rainfall forecasts for Hat Yai flood awareness."""

    freshness: DataFreshness
    forecasts: list[RainfallForecastPoint]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "freshness": {
                    "generated_at": "2026-05-01T17:30:00Z",
                    "valid_at": "2026-05-01T18:00:00Z",
                    "source": "phase-1-normalized-mock",
                    "is_mock": True,
                },
                "forecasts": [
                    {
                        "basin_id": "utapao-canal",
                        "basin_name": {
                            "th": "คลองอู่ตะเภา",
                            "en": "U-Tapao Canal",
                        },
                        "centroid": {"latitude": 7.0084, "longitude": 100.4747},
                        "forecast_time": "2026-05-01T18:00:00Z",
                        "lead_time_hours": 6,
                        "rainfall_mm": 34.5,
                        "accumulation_hours": 6,
                        "risk_level": "yellow",
                    }
                ],
            }
        }
    )
