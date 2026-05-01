from datetime import UTC, datetime

from fastapi import APIRouter

from app.core.config import get_settings
from app.schemas.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def read_health() -> HealthResponse:
    """Return backend health status."""
    settings = get_settings()
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        checked_at=datetime.now(UTC),
    )
