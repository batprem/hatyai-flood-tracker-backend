"""Validate that a citizen-report location is inside or near the U-Tapao basin.

Reports must describe flooding within the project's area of responsibility, so
a submitted point is checked against the committed U-Tapao basin polygon
(:func:`app.geo.basin.get_basin_polygon`). A small geographic buffer is allowed
because flood-prone urban edges (roads, markets, canals at the basin rim) sit
just outside the hydrological boundary but are still relevant to Hat Yai
flooding.
"""

from __future__ import annotations

from shapely.geometry import Point

from app.geo.basin import get_basin_polygon

# ~0.05 degrees ≈ 5.5 km at this latitude. Generous enough to capture urban
# edges hugging the basin rim without admitting far-away noise submissions.
BASIN_BUFFER_DEGREES = 0.05


def is_within_basin(
    longitude: float, latitude: float, *, buffer: float = BASIN_BUFFER_DEGREES
) -> bool:
    """Return whether a point lies inside the U-Tapao basin plus a buffer.

    Args:
        longitude: WGS84 longitude of the reported point.
        latitude: WGS84 latitude of the reported point.
        buffer: Degrees of slack added around the basin polygon to admit urban
            edges. Defaults to :data:`BASIN_BUFFER_DEGREES`.

    Returns:
        ``True`` when the point is inside the buffered basin, else ``False``.
    """
    polygon = get_basin_polygon()
    point = Point(longitude, latitude)
    if polygon.contains(point):
        return True
    return polygon.buffer(buffer).contains(point)


__all__ = ["BASIN_BUFFER_DEGREES", "is_within_basin"]
