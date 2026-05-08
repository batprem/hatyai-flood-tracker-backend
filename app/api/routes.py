from fastapi import APIRouter

from app.api import forecast, freshness, map_layers, risk, stations

api_router = APIRouter()
api_router.include_router(forecast.router)
api_router.include_router(stations.router)
api_router.include_router(risk.router)
api_router.include_router(map_layers.router)
api_router.include_router(freshness.router)
