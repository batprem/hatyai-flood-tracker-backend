from datetime import UTC, datetime, timedelta
from unittest import TestCase

from shapely.geometry import Point

from app.geo.basin import BASIN_GEOMETRY_REF, get_basin_polygon
from app.ingestion.models import (
    ForecastFrame,
    ForecastGrid,
    ForecastProvider,
    ForecastQuality,
    ForecastRunStatus,
    ForecastSource,
    ForecastStatistic,
    ForecastVariable,
    Phase1Area,
)
from app.services.risk_rules import build_rainfall_inputs_from_frames

# Points clearly inside the U-Tapao basin, around Hat Yai and the canal corridor.
INSIDE_POINTS: tuple[tuple[float, float], ...] = (
    (100.47, 7.01),
    (100.50, 6.95),
    (100.42, 6.85),
)

# Points clearly outside the basin: the sea south of Ko Yo, the mountains east,
# and a point west of the watershed divide.
OUTSIDE_POINTS: tuple[tuple[float, float], ...] = (
    (100.30, 6.30),
    (101.00, 7.00),
    (100.10, 7.20),
)


def _grid_frame(
    *, bbox: tuple[float, float, float, float], values_mm: list[float]
) -> ForecastFrame:
    """Build a 3x3 forecast frame with an explicit bounding box for clip tests.

    Args:
        bbox: West, south, east, north box whose north/west edges anchor the
            reconstructed cell lon/lat in GRIB scan order.
        values_mm: Nine rainfall values in row-major north-to-south, west-to-east
            order.

    Returns:
        A normalized 3x3 forecast frame at 0.25-degree resolution.
    """
    run_time = datetime(2026, 5, 1, 0, tzinfo=UTC)
    valid_time = run_time + timedelta(hours=24)
    return ForecastFrame(
        frame_id="gfs:gfs:2026050100:precipitation:f024",
        run_id="gfs:gfs:2026050100",
        provider=ForecastProvider.GFS,
        model="gfs",
        variable=ForecastVariable.PRECIPITATION,
        statistic=ForecastStatistic.ACCUMULATION,
        unit="mm",
        run_time=run_time,
        valid_time=valid_time,
        window_start=run_time,
        window_end=valid_time,
        accumulation_hours=24,
        provider_accumulation_semantics="window_accumulation_mm",
        forecast_hour=24,
        retrieved_at=run_time + timedelta(minutes=30),
        processed_at=run_time + timedelta(minutes=30),
        area=Phase1Area(bbox=bbox),
        grid=ForecastGrid(resolution_degrees=0.25, width=3, height=3),
        values_mm=values_mm,
        source=ForecastSource(
            url="https://example.test/fixture",
            product="gfs.fixture",
            license="public-domain",
            attribution="gfs fixture",
            raw_artifact_ref="fixture://gfs",
        ),
        quality=ForecastQuality(
            status=ForecastRunStatus.NORMALIZED,
            missing_value_count=0,
            minimum_mm=min(values_mm),
            maximum_mm=max(values_mm),
        ),
    )


class BasinPolygonMembershipTest(TestCase):
    def test_inside_points_are_contained(self) -> None:
        basin = get_basin_polygon()
        for lon, lat in INSIDE_POINTS:
            self.assertTrue(
                basin.contains(Point(lon, lat)),
                msg=f"expected ({lon}, {lat}) inside the basin",
            )

    def test_outside_points_are_not_contained(self) -> None:
        basin = get_basin_polygon()
        for lon, lat in OUTSIDE_POINTS:
            self.assertFalse(
                basin.contains(Point(lon, lat)),
                msg=f"expected ({lon}, {lat}) outside the basin",
            )

    def test_only_inside_points_pass_the_filter(self) -> None:
        basin = get_basin_polygon()
        passing = [
            (lon, lat)
            for lon, lat in (*INSIDE_POINTS, *OUTSIDE_POINTS)
            if basin.contains(Point(lon, lat))
        ]
        self.assertEqual(set(passing), set(INSIDE_POINTS))

    def test_geometry_ref_matches_committed_filename(self) -> None:
        self.assertEqual(BASIN_GEOMETRY_REF, "basin_utapao.geojson")


class BasinClippedAggregationTest(TestCase):
    def test_outside_grid_cells_are_excluded_from_the_max(self) -> None:
        # bbox north=7.25, west=100.25 places the 3x3 cell centres so that the
        # northern row and eastern column fall outside the basin (verified
        # against the committed polygon), while the lower-left 2x2 block is in.
        bbox = (100.25, 6.75, 100.75, 7.25)
        # Row-major north-to-south, west-to-east. Outside cells (idx 0,1,2,5,8)
        # carry a huge decoy value; inside cells (idx 3,4,6,7) cap at 120 mm.
        values_mm = [
            999.0, 999.0, 999.0,  # row 0 (lat 7.25): all outside
            80.0, 120.0, 999.0,   # row 1 (lat 7.00): idx 3,4 inside; idx 5 outside
            60.0, 100.0, 999.0,   # row 2 (lat 6.75): idx 6,7 inside; idx 8 outside
        ]
        frame = _grid_frame(bbox=bbox, values_mm=values_mm)

        inputs = build_rainfall_inputs_from_frames([frame])

        self.assertEqual(len(inputs), 1)
        # The basin-clipped max must be the inside maximum (120), never the 999
        # decoys that sit outside the basin polygon.
        self.assertEqual(inputs[0].rainfall_mm, 120.0)

    def test_frame_with_no_inside_cell_is_skipped(self) -> None:
        # Shift the box far east so every cell centre lands outside the basin.
        bbox = (101.00, 7.00, 101.50, 7.50)
        values_mm = [50.0] * 9
        frame = _grid_frame(bbox=bbox, values_mm=values_mm)

        inputs = build_rainfall_inputs_from_frames([frame])

        self.assertEqual(inputs, [])
