"""Tests for the research export endpoint (HFT-78)."""

import csv
import unittest
from datetime import UTC, datetime
from io import StringIO

import httpx
from fastapi.testclient import TestClient

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
from app.ingestion.repository import DryRunForecastRepository
from app.ingestion.station_repository import DryRunStationRepository
from app.ingestion.thaiwater_client import (
    StationGeoPoint,
    StationObservation,
    StationVariable,
)
from app.main import create_app


def _build_frame(valid_time: datetime, forecast_hour: int) -> ForecastFrame:
    run_time = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    return ForecastFrame(
        frame_id=f"gfs-2026050100-f{forecast_hour:03d}",
        run_id="gfs-2026050100",
        provider=ForecastProvider.GFS,
        model="gfs.0p25",
        variable=ForecastVariable.PRECIPITATION,
        statistic=ForecastStatistic.ACCUMULATION,
        run_time=run_time,
        valid_time=valid_time,
        window_start=run_time,
        window_end=valid_time,
        accumulation_hours=forecast_hour,
        provider_accumulation_semantics="apcp_surface_accumulated_from_run_start",
        forecast_hour=forecast_hour,
        retrieved_at=datetime(2026, 5, 1, 4, 30, tzinfo=UTC),
        processed_at=datetime(2026, 5, 1, 4, 45, tzinfo=UTC),
        area=Phase1Area(),
        grid=ForecastGrid(resolution_degrees=0.25, width=2, height=2),
        values_mm=[1.0, 2.0, 3.0, 4.0],
        source=ForecastSource(
            url="https://example.test/gfs",
            product="gfs.0p25",
            license="review-required",
            attribution="NOAA GFS",
            raw_artifact_ref="fixtures/gfs",
        ),
        quality=ForecastQuality(
            status=ForecastRunStatus.STORED,
            missing_value_count=0,
            minimum_mm=1.0,
            maximum_mm=4.0,
        ),
    )


def _build_observation(observed_at: datetime, station_id: str = "X.173A") -> StationObservation:
    return StationObservation(
        provider="thaiwater-haii",
        source_system="api",
        station_id=station_id,
        station_name_th="คลองอู่ตะเภา",
        station_name_en="U-Tapao Canal",
        canal_or_lake_th="คลองอู่ตะเภา",
        canal_or_lake_en="U-Tapao Canal",
        location=StationGeoPoint(coordinates=(100.474, 7.008)),
        variable=StationVariable.WATER_LEVEL,
        value=3.42,
        unit="m",
        observed_at=observed_at,
        retrieved_at=observed_at,
        warning_level_m=5.0,
        critical_level_m=6.0,
        provenance_url="https://example.test/thaiwater",
    )


def _parse_csv(body: str) -> tuple[list[str], list[str], list[dict[str, str]]]:
    """Split a CSV export into comment lines, header columns, and data rows."""
    lines = body.splitlines()
    comments = [line for line in lines if line.startswith("#")]
    data_lines = [line for line in lines if not line.startswith("#")]
    reader = csv.DictReader(StringIO("\n".join(data_lines)))
    rows = list(reader)
    return comments, list(reader.fieldnames or []), rows


class ExportApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.forecast_repository = DryRunForecastRepository()
        self.station_repository = DryRunStationRepository()
        await self.forecast_repository.upsert_frames(
            [
                _build_frame(datetime(2026, 5, 1, 6, 0, tzinfo=UTC), 6),
                _build_frame(datetime(2026, 5, 1, 12, 0, tzinfo=UTC), 12),
            ]
        )
        await self.station_repository.upsert_many(
            [
                _build_observation(datetime(2026, 5, 1, 7, 0, tzinfo=UTC)),
                _build_observation(datetime(2026, 5, 2, 7, 0, tzinfo=UTC)),
                _build_observation(datetime(2026, 6, 15, 7, 0, tzinfo=UTC)),
            ]
        )
        self.app = create_app(
            forecast_repository=self.forecast_repository,
            station_repository=self.station_repository,
        )

    def _get(self, **params: str) -> httpx.Response:
        with TestClient(self.app) as http:
            return http.get("/api/export", params=params)

    # -- happy paths ---------------------------------------------------

    async def test_forecast_frames_csv(self) -> None:
        response = self._get(
            dataset="forecast_frames", format="csv", start="2026-05-01", end="2026-05-02"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.headers["content-type"])
        self.assertEqual(
            response.headers["content-disposition"],
            'attachment; filename="hft_forecast_frames_2026-05-01_2026-05-02.csv"',
        )
        comments, fieldnames, rows = _parse_csv(response.text)
        self.assertTrue(any("dataset: forecast_frames" in line for line in comments))
        self.assertTrue(any("UTC" in line for line in comments))
        self.assertTrue(any("units:" in line for line in comments))
        self.assertIn("mean_rainfall_mm", fieldnames)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["frame_id"], "gfs-2026050100-f006")
        self.assertEqual(rows[0]["mean_rainfall_mm"], "2.5")
        self.assertEqual(rows[0]["valid_time_utc"], "2026-05-01T06:00:00+00:00")

    async def test_forecast_frames_geojson(self) -> None:
        response = self._get(
            dataset="forecast_frames", format="geojson", start="2026-05-01", end="2026-05-02"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/geo+json", response.headers["content-type"])
        payload = response.json()
        self.assertEqual(payload["type"], "FeatureCollection")
        self.assertEqual(payload["dataset"], "forecast_frames")
        self.assertEqual(payload["record_count"], 2)
        self.assertIn("UTC", payload["timezone"])
        feature = payload["features"][0]
        self.assertEqual(feature["geometry"]["type"], "Polygon")
        ring = feature["geometry"]["coordinates"][0]
        self.assertEqual(len(ring), 5)
        self.assertEqual(ring[0], ring[-1])
        self.assertEqual(feature["properties"]["unit"], "mm")

    async def test_risk_history_csv(self) -> None:
        response = self._get(
            dataset="risk_history", format="csv", start="2010-10-01", end="2010-12-31"
        )
        self.assertEqual(response.status_code, 200)
        comments, fieldnames, rows = _parse_csv(response.text)
        self.assertTrue(any("dataset: risk_history" in line for line in comments))
        self.assertIn("rule_output", fieldnames)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_id"], "hatyai-flood-2010")
        self.assertEqual(rows[0]["rule_output"], "red")

    async def test_risk_history_geojson_has_null_geometry(self) -> None:
        response = self._get(
            dataset="risk_history", format="geojson", start="2010-10-01", end="2010-12-31"
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["record_count"], 1)
        feature = payload["features"][0]
        self.assertIsNone(feature["geometry"])
        self.assertEqual(feature["properties"]["event_id"], "hatyai-flood-2010")

    async def test_station_observations_csv(self) -> None:
        response = self._get(
            dataset="station_observations", format="csv", start="2026-05-01", end="2026-05-31"
        )
        self.assertEqual(response.status_code, 200)
        comments, fieldnames, rows = _parse_csv(response.text)
        self.assertTrue(any("metres" in line for line in comments))
        self.assertIn("observed_at_utc", fieldnames)
        # The 2026-06-15 record is outside the requested range.
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["station_id"], "X.173A")
        self.assertEqual(rows[0]["unit"], "m")

    async def test_station_observations_geojson(self) -> None:
        response = self._get(
            dataset="station_observations",
            format="geojson",
            start="2026-05-01",
            end="2026-05-31",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["record_count"], 2)
        feature = payload["features"][0]
        self.assertEqual(feature["geometry"]["type"], "Point")
        self.assertEqual(feature["geometry"]["coordinates"], [100.474, 7.008])
        self.assertNotIn("longitude", feature["properties"])

    # -- validation ----------------------------------------------------

    async def test_oversized_range_rejected(self) -> None:
        response = self._get(
            dataset="forecast_frames", format="csv", start="2026-01-01", end="2026-06-01"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("maximum is 92 days", response.json()["detail"])

    async def test_end_before_start_rejected(self) -> None:
        response = self._get(
            dataset="forecast_frames", format="csv", start="2026-05-02", end="2026-05-01"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("must not be before", response.json()["detail"])

    async def test_unknown_dataset_rejected(self) -> None:
        response = self._get(
            dataset="secret_data", format="csv", start="2026-05-01", end="2026-05-02"
        )
        self.assertEqual(response.status_code, 422)

    # -- empty results -------------------------------------------------

    async def test_empty_range_returns_header_only_csv(self) -> None:
        response = self._get(
            dataset="station_observations", format="csv", start="2001-01-01", end="2001-01-31"
        )
        self.assertEqual(response.status_code, 200)
        comments, fieldnames, rows = _parse_csv(response.text)
        self.assertTrue(any("record_count: 0" in line for line in comments))
        self.assertIn("station_id", fieldnames)
        self.assertEqual(rows, [])

    async def test_empty_range_returns_empty_geojson(self) -> None:
        response = self._get(
            dataset="forecast_frames", format="geojson", start="2001-01-01", end="2001-01-31"
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["features"], [])
        self.assertEqual(payload["record_count"], 0)


if __name__ == "__main__":
    unittest.main()
