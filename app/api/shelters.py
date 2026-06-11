"""Router for public flood evacuation shelter endpoints."""

from fastapi import APIRouter

from app.schemas.shelters import SheltersResponse
from app.services.shelters import get_shelters_response

router = APIRouter(prefix="/shelters", tags=["shelters"])


@router.get("", response_model=SheltersResponse)
async def list_shelters() -> SheltersResponse:
    """Return the normalized Hat Yai flood evacuation shelter list.

    The shelters come from the committed, QA-validated dataset of facilities
    designated during the November 2025 (B.E. 2568) Hat Yai flood. The
    response includes dataset-level provenance (license, retrieval date,
    dataset file reference, and coordinate accuracy note) so the frontend can
    attribute sources and flag accuracy caveats.

    Returns:
        The normalized shelter list with dataset-level provenance metadata.
    """
    return get_shelters_response()
