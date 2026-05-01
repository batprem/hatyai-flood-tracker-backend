from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import DataFreshness, RiskLevel


class RiskSignal(BaseModel):
    """Represent one inspectable input to the current risk decision."""

    name: str
    value: str
    level: RiskLevel
    detail: str


class CurrentRiskResponse(BaseModel):
    """Represent the current public flood risk summary."""

    freshness: DataFreshness
    level: RiskLevel
    headline: str
    summary: str
    recommended_action: str
    confidence: float = Field(ge=0, le=1)
    signals: list[RiskSignal]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "freshness": {
                    "generated_at": "2026-05-01T17:30:00Z",
                    "valid_at": "2026-05-01T17:30:00Z",
                    "source": "phase-1-normalized-mock",
                    "is_mock": True,
                },
                "level": "yellow",
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
                    }
                ],
            }
        }
    )
