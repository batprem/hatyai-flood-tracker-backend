from fastapi import APIRouter

from app.schemas.stations import WaterLevelResponse
from app.services.mock_data import get_water_levels

router = APIRouter(prefix="/stations", tags=["stations"])


@router.get("/water-level", response_model=WaterLevelResponse)
async def read_water_levels() -> WaterLevelResponse:
    """Return normalized mock water-level observations."""
    return get_water_levels()
