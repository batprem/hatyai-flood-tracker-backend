from fastapi import APIRouter

from app.schemas.map_layers import MapLayersResponse
from app.services.mock_data import get_map_layers

router = APIRouter(prefix="/map", tags=["map"])


@router.get("/layers", response_model=MapLayersResponse)
async def read_map_layers() -> MapLayersResponse:
    """Return available public map layer descriptors."""
    return get_map_layers()
