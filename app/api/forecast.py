from fastapi import APIRouter

from app.schemas.forecast import RainfallForecastResponse
from app.services.mock_data import get_rainfall_forecast

router = APIRouter(prefix="/forecast", tags=["forecast"])


@router.get("/rainfall", response_model=RainfallForecastResponse)
async def read_rainfall_forecast() -> RainfallForecastResponse:
    """Return normalized mock rainfall forecasts."""
    return get_rainfall_forecast()
