from fastapi import APIRouter

from app.api import (
    alerts,
    events,
    forecast,
    freshness,
    map_layers,
    reports,
    risk,
    shelters,
    stations,
)

api_router = APIRouter()
api_router.include_router(forecast.router)
api_router.include_router(stations.router)
api_router.include_router(risk.router)
api_router.include_router(map_layers.router)
api_router.include_router(freshness.router)
api_router.include_router(alerts.router)
api_router.include_router(events.router)
api_router.include_router(shelters.router)
api_router.include_router(reports.router)
