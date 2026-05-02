from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.schemas.risk import CurrentRiskResponse
from app.services.mock_data import get_current_risk

router = APIRouter(prefix="/risk", tags=["risk"])


@router.get("/current", response_model=CurrentRiskResponse)
async def read_current_risk(
    settings: Annotated[Settings, Depends(get_settings)],
) -> CurrentRiskResponse:
    """Return the current rule-based flood risk summary."""
    return get_current_risk(settings.risk_rule_settings())
