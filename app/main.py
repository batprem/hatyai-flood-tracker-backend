from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.health import router as health_router
from app.api.routes import api_router
from app.core.config import ForecastRepositoryBackend, Settings, get_settings
from app.ingestion.mongo_repository import MongoForecastRepository, build_mongo_repository
from app.ingestion.repository import DryRunForecastRepository, ForecastRepository


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Construct lifespan-managed resources, including the forecast repository."""
    if not hasattr(app.state, "settings"):
        app.state.settings = get_settings()
    settings: Settings = app.state.settings

    repository_already_set = hasattr(app.state, "forecast_repository")
    mongo_client: AsyncIOMotorClient | None = getattr(app.state, "mongo_client", None)

    if not repository_already_set:
        if settings.forecast_repository_backend is ForecastRepositoryBackend.MONGO:
            mongo_client = AsyncIOMotorClient(settings.mongodb_uri)
            repository = build_mongo_repository(mongo_client, settings.mongodb_database)
            await repository.ensure_indexes()
            app.state.mongo_client = mongo_client
            app.state.forecast_repository = repository
        else:
            app.state.forecast_repository = DryRunForecastRepository()

    try:
        yield
    finally:
        client_to_close: AsyncIOMotorClient | None = getattr(app.state, "mongo_client", None)
        if client_to_close is not None:
            client_to_close.close()
            app.state.mongo_client = None


def create_app(
    settings: Settings | None = None,
    forecast_repository: ForecastRepository | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""
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
