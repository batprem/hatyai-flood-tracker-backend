from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.health import router as health_router
from app.api.routes import api_router
from app.core.config import ForecastRepositoryBackend, Settings, get_settings
from app.ingestion.mongo_repository import MongoForecastRepository, build_mongo_repository
from app.ingestion.repository import DryRunForecastRepository, ForecastRepository
from app.ingestion.station_repository import (
    DryRunStationRepository,
    StationObservationRepository,
    build_mongo_station_repository,
)
from app.ingestion.thaiwater_client import (
    ThaiwaterStationClient,
    build_thaiwater_client,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Construct lifespan-managed resources, including provider clients and repositories.

    Args:
        app: The FastAPI application to configure.
    """
    if not hasattr(app.state, "settings"):
        app.state.settings = get_settings()
    settings: Settings = app.state.settings

    repository_already_set = hasattr(app.state, "forecast_repository")
    station_repository_already_set = hasattr(app.state, "station_repository")
    thaiwater_client_already_set = hasattr(app.state, "thaiwater_client")
    thaiwater_http_already_set = hasattr(app.state, "thaiwater_http")
    mongo_client: AsyncIOMotorClient | None = getattr(app.state, "mongo_client", None)

    if not repository_already_set:
        if settings.forecast_repository_backend is ForecastRepositoryBackend.MONGO:
            if mongo_client is None:
                mongo_client = AsyncIOMotorClient(settings.mongodb_uri)
                app.state.mongo_client = mongo_client
            repository = build_mongo_repository(mongo_client, settings.mongodb_database)
            await repository.ensure_indexes()
            app.state.forecast_repository = repository
        else:
            app.state.forecast_repository = DryRunForecastRepository()

    if not station_repository_already_set:
        if settings.forecast_repository_backend is ForecastRepositoryBackend.MONGO:
            if mongo_client is None:
                mongo_client = AsyncIOMotorClient(settings.mongodb_uri)
                app.state.mongo_client = mongo_client
            station_repo: StationObservationRepository = build_mongo_station_repository(
                mongo_client, settings.mongodb_database
            )
            await station_repo.ensure_indexes()
            app.state.station_repository = station_repo
        else:
            app.state.station_repository = DryRunStationRepository()

    if not thaiwater_client_already_set:
        if not thaiwater_http_already_set:
            app.state.thaiwater_http = httpx.AsyncClient(
                timeout=settings.thaiwater_timeout_seconds,
                headers={"User-Agent": "hatyai-flood-warning/0.1 (+thaiwater)"},
            )
        app.state.thaiwater_client = build_thaiwater_client(
            http_client=app.state.thaiwater_http,
            base_url=settings.thaiwater_base_url,
            api_key=settings.thaiwater_api_key,
            max_age_hours=settings.thaiwater_max_age_hours,
        )

    try:
        yield
    finally:
        http_to_close: httpx.AsyncClient | None = getattr(app.state, "thaiwater_http", None)
        if http_to_close is not None and not thaiwater_http_already_set:
            await http_to_close.aclose()
            app.state.thaiwater_http = None
        client_to_close: AsyncIOMotorClient | None = getattr(app.state, "mongo_client", None)
        if client_to_close is not None:
            client_to_close.close()
            app.state.mongo_client = None


def create_app(
    settings: Settings | None = None,
    forecast_repository: ForecastRepository | None = None,
    station_repository: StationObservationRepository | None = None,
    thaiwater_client: ThaiwaterStationClient | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        settings: Application settings. Defaults to ``None``.
        forecast_repository: Forecast repository. Defaults to ``None``.
        station_repository: Station repository. Defaults to ``None``.
        thaiwater_client: ThaiWater client. Defaults to ``None``.

    Returns:
        Configured FastAPI application instance.
    """
    resolved_settings = settings or get_settings()
    app = FastAPI(
        title=resolved_settings.app_name,
        description=(
            "Public flood awareness API for Hat Yai, U-Tapao Canal, and Songkhla Lake basin."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    if forecast_repository is not None:
        app.state.forecast_repository = forecast_repository
    elif resolved_settings.forecast_repository_backend is ForecastRepositoryBackend.DRY_RUN:
        # Eagerly create the dry-run repository so synchronous test clients
        # (e.g. `with TestClient(app)` against the in-memory backend) keep
        # working without going through Mongo.
        app.state.forecast_repository = DryRunForecastRepository()
    if station_repository is not None:
        app.state.station_repository = station_repository
    elif resolved_settings.forecast_repository_backend is ForecastRepositoryBackend.DRY_RUN:
        app.state.station_repository = DryRunStationRepository()
    if thaiwater_client is not None:
        app.state.thaiwater_client = thaiwater_client

    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.allowed_cors_origins(),
        allow_origin_regex=resolved_settings.cors_origin_regex,
        allow_credentials=True,
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(api_router, prefix=resolved_settings.api_prefix)
    return app


app = create_app()


__all__ = ["MongoForecastRepository", "app", "create_app"]
