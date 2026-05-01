from fastapi import APIRouter

from app.schemas.risk import CurrentRiskResponse
from app.services.mock_data import get_current_risk

router = APIRouter(prefix="/risk", tags=["risk"])


@router.get("/current", response_model=CurrentRiskResponse)
async def read_current_risk() -> CurrentRiskResponse:
    """Return the current rule-based mock flood risk summary."""
    return get_current_risk()
