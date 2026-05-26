from fastapi import Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import Settings, get_settings
from app.ingestion.repository import ForecastRepository
from app.ingestion.station_repository import StationObservationRepository
from app.ingestion.thaiwater_client import StationObservationClient


def get_app_settings(request: Request) -> Settings:
    """Return the application settings configured on ``app.state``.

    Prefers the lifespan/test-injected settings on ``app.state`` so endpoints
    honor settings passed to :func:`create_app`, falling back to the cached
    global settings when none were attached.

    Args:
        request: The FastAPI request object providing access to app state.

    Returns:
        The active application settings.
    """
    settings = getattr(request.app.state, "settings", None)
    if isinstance(settings, Settings):
        return settings
    return get_settings()


def get_forecast_repository(request: Request) -> ForecastRepository:
    """Return the lifespan-managed forecast repository for request handlers.

    Args:
        request: The FastAPI request object providing access to app state.

    Returns:
        The forecast repository configured during application startup.

    Raises:
        RuntimeError: If the forecast repository is missing on ``app.state``.
    """
    repository = getattr(request.app.state, "forecast_repository", None)
    if repository is None:
        msg = "forecast repository is not configured on app.state"
        raise RuntimeError(msg)
    return repository


def get_station_observation_client(request: Request) -> StationObservationClient:
    """Return the lifespan-managed ThaiWater client for request handlers.

    Args:
        request: The FastAPI request object providing access to app state.

    Returns:
        The station observation client configured during application startup.

    Raises:
        RuntimeError: If the station observation client is missing on ``app.state``.
    """
    client = getattr(request.app.state, "thaiwater_client", None)
    if client is None:
        msg = "station observation client is not configured on app.state"
        raise RuntimeError(msg)
    return client


def get_station_repository(request: Request) -> StationObservationRepository | None:
    """Return the lifespan-managed station repository, or None when unconfigured.

    Args:
        request: The FastAPI request object providing access to app state.

    Returns:
        The station observation repository, or ``None`` when not configured.
    """
    return getattr(request.app.state, "station_repository", None)


def get_threshold_database(request: Request) -> AsyncIOMotorDatabase | None:
    """Return the Mongo database holding station thresholds, or None.

    The ``station_thresholds`` collection only exists when the Mongo backend
    is active. In dry-run mode no database is configured, so the risk endpoint
    degrades to an empty contribution block.

    Args:
        request: The FastAPI request object providing access to app state.

    Returns:
        The threshold-backing database handle, or ``None`` when not configured.
    """
    return getattr(request.app.state, "threshold_database", None)
