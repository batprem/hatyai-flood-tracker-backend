from fastapi import Request

from app.ingestion.repository import ForecastRepository
from app.ingestion.station_repository import StationObservationRepository
from app.ingestion.thaiwater_client import StationObservationClient


def get_forecast_repository(request: Request) -> ForecastRepository:
    """Return the lifespan-managed forecast repository for request handlers."""
    repository = getattr(request.app.state, "forecast_repository", None)
    if repository is None:
        msg = "forecast repository is not configured on app.state"
        raise RuntimeError(msg)
    return repository


def get_station_observation_client(request: Request) -> StationObservationClient:
    """Return the lifespan-managed ThaiWater client for request handlers."""
    client = getattr(request.app.state, "thaiwater_client", None)
    if client is None:
        msg = "station observation client is not configured on app.state"
        raise RuntimeError(msg)
    return client


def get_station_repository(request: Request) -> StationObservationRepository | None:
    """Return the lifespan-managed station repository, or None when unconfigured."""
    return getattr(request.app.state, "station_repository", None)
