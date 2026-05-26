"""Router for historical flood event endpoints."""

from fastapi import APIRouter

from app.data.historical_events import HISTORICAL_EVENTS, HistoricalEvent
from app.schemas.events import HistoricalEventsResponse

router = APIRouter(prefix="/events", tags=["events"])


@router.get("/historical", response_model=HistoricalEventsResponse)
async def list_historical_events() -> HistoricalEventsResponse:
    """Return the static list of historical Hat Yai flood events.

    Returns:
        A response containing all historical flood event fixtures with a
        data note and event count.
    """
    events: list[HistoricalEvent] = HISTORICAL_EVENTS
    return HistoricalEventsResponse(
        events=events,
        data_note=("Fixture data from public TMD/DDPM/WMO post-event reports. Confidence: medium."),
        event_count=len(events),
    )
