"""Pydantic schemas for the events API."""

from pydantic import BaseModel, Field

from app.data.historical_events import HistoricalEvent


class HistoricalEventsResponse(BaseModel):
    """Model the response for the GET /events/historical endpoint."""

    events: list[HistoricalEvent] = Field(description="List of historical flood event fixtures.")
    data_note: str = Field(description="Provenance and confidence note for the fixture data set.")
    event_count: int = Field(description="Total number of events returned.")
