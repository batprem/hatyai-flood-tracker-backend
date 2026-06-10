"""Load and cache the committed U-Tapao basin polygon for geographic clipping.

The basin boundary is the HydroSHEDS HydroBASINS Level 7 polygon for the
U-Tapao canal basin (HYBAS_ID 4070019470), committed as a GeoJSON file under
``backend/data``. Risk aggregation clips forecast grid cells to this polygon so
the public flood level reflects rainfall over the actual drainage basin rather
than the rectangular GRIB download bounding box.
"""

import json
from functools import lru_cache
from pathlib import Path

from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

# Public reference returned in API responses so clients and auditors can tie a
# risk aggregation back to the exact committed boundary file that produced it.
BASIN_GEOMETRY_REF = "basin_utapao.geojson"

_BASIN_FILE = Path(__file__).parent.parent.parent / "data" / BASIN_GEOMETRY_REF


@lru_cache(maxsize=1)
def get_basin_polygon() -> BaseGeometry:
    """Load and return the U-Tapao basin polygon from the committed GeoJSON.

    The result is cached for the process lifetime because the boundary is a
    static committed asset. ``shapely.geometry.shape`` builds a ``Polygon`` or
    ``MultiPolygon`` transparently, so ``contains`` checks work for either.

    Returns:
        The basin boundary geometry in WGS84 (EPSG:4326) lon/lat coordinates.
    """
    with _BASIN_FILE.open() as basin_file:
        data = json.load(basin_file)
    return shape(data["features"][0]["geometry"])
