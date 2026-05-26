import json
import logging
import unittest
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.ingestion.models import (
    ForecastProvider,
    ForecastRunStatus,
    FreshnessStatus,
)
from app.ingestion.normalizer import build_run_record, normalize_frames
from app.ingestion.providers import build_provider_client
from app.ingestion.repository import DryRunForecastRepository
from app.ingestion.station_repository import DryRunStationRepository
from app.ingestion.thaiwater_client import (
    StationGeoPoint,
    StationObservation,
    StationVariable,
)
from app.main import create_app
from app.services.data_quality import (
    STALE_DATA_ALERT_EVENT,
    compute_data_quality,
    emit_stale_data_alert,
    evaluate_and_alert,
)


async def _store_run(
    repository: DryRunForecastRepository,
    provider: ForecastProvider,
    retrieved_at: datetime,
    *,
    status: ForecastRunStatus = ForecastRunStatus.STORED,
) -> None:
    client_provider = build_provider_client(provider, [6], use_fixtures=True)
    run_ref = client_provider.discover_latest_run(retrieved_at)
    artifacts = client_provider.fetch_run(run_ref)
    run = build_run_record(run_ref, artifacts, retrieved_at).model_copy(update={"status": status})
    frames = normalize_frames(run_ref, artifacts, retrieved_at)
    await repository.upsert_run(run)
    await repository.upsert_frames(frames)


def _observation(observed_at: datetime) -> StationObservation:
    return StationObservation(
        provider="thaiwater-haii",
        source_system="api",
        station_id="X.173A",
        station_name_th="สถานี",
        station_name_en="Station",
        canal_or_lake_th="คลอง",
        canal_or_lake_en="Canal",
        location=StationGeoPoint(type="Point", coordinates=(100.5, 7.0)),
        variable=StationVariable.WATER_LEVEL,
        value=1.2,
        unit="m",
        observed_at=observed_at,
        retrieved_at=observed_at,
        provenance_url="https://example.org/obs",
    )


class ComputeDataQualityTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_sources_classify_fresh(self) -> None:
        now = datetime(2026, 5, 1, 6, 0, tzinfo=UTC)
        forecast = DryRunForecastRepository()
        await _store_run(forecast, ForecastProvider.GFS, now - timedelta(hours=2))
        await _store_run(forecast, ForecastProvider.ECMWF_OPEN_DATA, now - timedelta(hours=2))
        stations = DryRunStationRepository()
        await stations.upsert_many([_observation(now - timedelta(minutes=30))])

        snapshot = await compute_data_quality(forecast, stations, Settings(), now=now)

        self.assertEqual(snapshot.gfs.status, FreshnessStatus.FRESH)
        self.assertEqual(snapshot.ecmwf.status, FreshnessStatus.FRESH)
        self.assertEqual(snapshot.station.status, FreshnessStatus.FRESH)
        self.assertEqual(snapshot.stale_or_failed(), [])

    async def test_aged_run_classifies_stale(self) -> None:
        now = datetime(2026, 5, 1, 18, 0, tzinfo=UTC)
        forecast = DryRunForecastRepository()
        await _store_run(forecast, ForecastProvider.GFS, now - timedelta(hours=9))
        stations = DryRunStationRepository()

        snapshot = await compute_data_quality(forecast, stations, Settings(), now=now)

        # GFS run is 9h old against the 6h default threshold.
        self.assertEqual(snapshot.gfs.status, FreshnessStatus.STALE)
        self.assertIsNotNone(snapshot.gfs.age_hours)
        # No ECMWF run stored and no station observations.
        self.assertEqual(snapshot.ecmwf.status, FreshnessStatus.FAILED)
        self.assertEqual(snapshot.station.status, FreshnessStatus.FAILED)

    async def test_failed_run_classifies_failed_with_reason(self) -> None:
        now = datetime(2026, 5, 1, 6, 0, tzinfo=UTC)
        forecast = DryRunForecastRepository()
        await _store_run(
            forecast,
            ForecastProvider.GFS,
            now - timedelta(hours=1),
            status=ForecastRunStatus.FAILED,
        )
        stations = DryRunStationRepository()

        snapshot = await compute_data_quality(forecast, stations, Settings(), now=now)

        self.assertEqual(snapshot.gfs.status, FreshnessStatus.FAILED)
        self.assertIsNotNone(snapshot.gfs.reason)


class EmitStaleDataAlertTests(unittest.IsolatedAsyncioTestCase):
    async def test_emits_structured_error_log_per_breaching_source(self) -> None:
        now = datetime(2026, 5, 1, 18, 0, tzinfo=UTC)
        forecast = DryRunForecastRepository()
        await _store_run(forecast, ForecastProvider.GFS, now - timedelta(hours=9))
        stations = DryRunStationRepository()

        snapshot = await compute_data_quality(forecast, stations, Settings(), now=now)
        with self.assertLogs("app.services.data_quality", level="ERROR") as captured:
            had_breach = emit_stale_data_alert(snapshot)

        self.assertTrue(had_breach)
        events = [json.loads(record.message) for record in captured.records]
        self.assertTrue(all(event["event"] == STALE_DATA_ALERT_EVENT for event in events))
        sources = {event["source"] for event in events}
        self.assertIn("gfs", sources)
        self.assertIn("ecmwf_open_data", sources)
        self.assertIn("station", sources)

    async def test_no_log_when_all_fresh(self) -> None:
        now = datetime(2026, 5, 1, 6, 0, tzinfo=UTC)
        forecast = DryRunForecastRepository()
        await _store_run(forecast, ForecastProvider.GFS, now - timedelta(hours=2))
        await _store_run(forecast, ForecastProvider.ECMWF_OPEN_DATA, now - timedelta(hours=2))
        stations = DryRunStationRepository()
        await stations.upsert_many([_observation(now - timedelta(minutes=10))])

        snapshot = await evaluate_and_alert(forecast, stations, Settings(), now=now)
        logger = logging.getLogger("app.services.data_quality")
        with self.assertNoLogs(logger, level="ERROR"):
            had_breach = emit_stale_data_alert(snapshot)
        self.assertFalse(had_breach)


class HealthDataQualityApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_includes_data_quality_block(self) -> None:
        forecast = DryRunForecastRepository()
        retrieved_at = datetime.now(UTC) - timedelta(hours=1)
        await _store_run(forecast, ForecastProvider.GFS, retrieved_at)
        stations = DryRunStationRepository()

        app = create_app(forecast_repository=forecast, station_repository=stations)
        with TestClient(app) as http:
            response = http.get("/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        block = payload["dataQuality"]
        self.assertIn(
            block["gfsFreshnessStatus"],
            {"fresh", "delayed", "stale", "partial", "failed"},
        )
        self.assertEqual(block["ecmwfFreshnessStatus"], "failed")
        self.assertEqual(block["stationFreshnessStatus"], "failed")
        self.assertIsNotNone(block["gfsRunAgeHours"])


class ForecastFramesFailedReasonTests(unittest.IsolatedAsyncioTestCase):
    async def test_frames_returns_failed_status_and_reason_on_failed_run(self) -> None:
        forecast = DryRunForecastRepository()
        retrieved_at = datetime(2026, 5, 1, 4, 30, tzinfo=UTC)
        await _store_run(
            forecast,
            ForecastProvider.GFS,
            retrieved_at,
            status=ForecastRunStatus.FAILED,
        )

        app = create_app(forecast_repository=forecast)
        with TestClient(app) as http:
            response = http.get("/api/forecast/frames", params={"provider": "gfs"})

        self.assertEqual(response.status_code, 200)
        freshness = response.json()["freshness"]
        self.assertEqual(freshness["status"], "failed")
        self.assertIsNotNone(freshness["reason"])


if __name__ == "__main__":
    unittest.main()
