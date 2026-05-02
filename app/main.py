from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.routes import api_router
from app.core.config import Settings, get_settings
from app.ingestion.repository import DryRunForecastRepository, ForecastRepository


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Prepare async application resources for future integrations."""
    if not hasattr(app.state, "settings"):
        app.state.settings = get_settings()
    if not hasattr(app.state, "forecast_repository"):
        app.state.forecast_repository = DryRunForecastRepository()
    yield


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
    app.state.forecast_repository = forecast_repository or DryRunForecastRepository()

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
