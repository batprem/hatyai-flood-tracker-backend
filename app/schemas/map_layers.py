from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import DataFreshness


class MapLayerType(StrEnum):
    """Model supported public map layer categories."""

    POINT = "point"
    LINE = "line"
    POLYGON = "polygon"
    RASTER = "raster"


class MapLayer(BaseModel):
    """Model a frontend map layer descriptor."""

    layer_id: str
    title: str
    description: str
    layer_type: MapLayerType
    visible_by_default: bool
    min_zoom: float = Field(ge=0, le=24)
    max_zoom: float = Field(ge=0, le=24)
    source_url: str
    metadata_fields: list[str] = Field(default_factory=list)


class MapLayersResponse(BaseModel):
    """Model available map layers for public flood monitoring."""

    freshness: DataFreshness
    layers: list[MapLayer]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "freshness": {
                    "generated_at": "2026-05-01T17:30:00Z",
                    "valid_at": "2026-05-01T17:30:00Z",
                    "source": "phase-1-normalized-mock",
                    "is_mock": True,
                },
                "layers": [
                    {
                        "layer_id": "water-stations",
                        "title": "Water level stations",
                        "description": "Mock station points for U-Tapao Canal monitoring.",
                        "layer_type": "point",
                        "visible_by_default": True,
                        "min_zoom": 8,
                        "max_zoom": 18,
                        "source_url": "/api/stations/water-level",
                        "metadata_fields": [],
                    }
                ],
            }
        }
    )
