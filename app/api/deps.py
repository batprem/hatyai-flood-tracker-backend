from fastapi import Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import Settings, get_settings
from app.ingestion.report_repository import ReportRepository
from app.ingestion.repository import ForecastRepository
from app.ingestion.station_repository import StationObservationRepository
from app.ingestion.subscription_repository import SubscriptionRepository
from app.ingestion.thaiwater_client import StationObservationClient
from app.services.photo_storage import PhotoStorage


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


def get_subscription_repository(request: Request) -> SubscriptionRepository:
    """Return the lifespan-managed Web Push subscription repository.

    Args:
        request: The FastAPI request object providing access to app state.

    Returns:
        The push subscription repository configured during application startup.

    Raises:
        RuntimeError: If the subscription repository is missing on ``app.state``.
    """
    repository = getattr(request.app.state, "subscription_repository", None)
    if repository is None:
        msg = "subscription repository is not configured on app.state"
        raise RuntimeError(msg)
    return repository


def get_report_repository(request: Request) -> ReportRepository:
    """Return the lifespan-managed citizen-report repository.

    Args:
        request: The FastAPI request object providing access to app state.

    Returns:
        The citizen-report repository configured during application startup.

    Raises:
        RuntimeError: If the report repository is missing on ``app.state``.
    """
    repository = getattr(request.app.state, "report_repository", None)
    if repository is None:
        msg = "report repository is not configured on app.state"
        raise RuntimeError(msg)
    return repository


def get_photo_storage(request: Request) -> PhotoStorage:
    """Return the lifespan-managed report photo storage backend.

    Args:
        request: The FastAPI request object providing access to app state.

    Returns:
        The photo storage backend configured during application startup.

    Raises:
        RuntimeError: If the photo storage is missing on ``app.state``.
    """
    storage = getattr(request.app.state, "photo_storage", None)
    if storage is None:
        msg = "photo storage is not configured on app.state"
        raise RuntimeError(msg)
    return storage
