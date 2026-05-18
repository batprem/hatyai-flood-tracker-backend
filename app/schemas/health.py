from datetime import datetime

from pydantic import BaseModel, ConfigDict


class HealthResponse(BaseModel):
    """Model backend health status."""

    status: str
    service: str
    checked_at: datetime

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "ok",
                "service": "Hat Yai Flood Warning API",
                "checked_at": "2026-05-01T17:30:00Z",
            }
        }
    )
