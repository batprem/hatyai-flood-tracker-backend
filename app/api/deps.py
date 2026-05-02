from fastapi import Request

from app.ingestion.repository import ForecastRepository


def get_forecast_repository(request: Request) -> ForecastRepository:
    """Return the lifespan-managed forecast repository for request handlers."""
    repository = getattr(request.app.state, "forecast_repository", None)
    if repository is None:
        msg = "forecast repository is not configured on app.state"
        raise RuntimeError(msg)
    return repository
