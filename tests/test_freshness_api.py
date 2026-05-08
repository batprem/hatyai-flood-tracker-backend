import unittest
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.ingestion.models import ForecastProvider, ForecastRunStatus
from app.ingestion.normalizer import build_run_record, normalize_frames
from app.ingestion.providers import build_provider_client
from app.ingestion.repository import DryRunForecastRepository
from app.main import create_app


class FreshnessApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_freshness_reports_latest_run_status(self) -> None:
        repository = DryRunForecastRepository()
        retrieved_at = datetime(2026, 5, 1, 4, 30, tzinfo=UTC)
        provider_client = build_provider_client(ForecastProvider.GFS, [6], use_fixtures=True)
        run_ref = provider_client.discover_latest_run(retrieved_at)
        artifacts = provider_client.fetch_run(run_ref)
        run = build_run_record(run_ref, artifacts, retrieved_at).model_copy(
            update={"status": ForecastRunStatus.STORED}
        )
        frames = normalize_frames(run_ref, artifacts, retrieved_at)
        await repository.upsert_run(run)
        await repository.upsert_frames(frames)

        app = create_app(forecast_repository=repository)
        with TestClient(app) as http:
            response = http.get("/api/freshness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn(payload["status"], {"fresh", "delayed", "stale", "partial", "failed"})
        self.assertEqual(payload["provider"], "gfs")
        self.assertEqual(payload["model"], "gfs")
        self.assertEqual(payload["frameCount"], 1)
        self.assertEqual(payload["thresholdHours"], 7)
        self.assertIsNotNone(payload["runTime"])
        self.assertIsNotNone(payload["retrievedAt"])

    async def test_get_freshness_reports_failed_when_no_runs_stored(self) -> None:
        repository = DryRunForecastRepository()
        app = create_app(forecast_repository=repository)
        with TestClient(app) as http:
            response = http.get("/api/freshness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["frameCount"], 0)
        self.assertEqual(payload["reason"], "no forecast runs stored")
        self.assertIsNone(payload["provider"])

    async def test_get_freshness_filters_by_provider(self) -> None:
        repository = DryRunForecastRepository()
        retrieved_at = datetime(2026, 5, 1, 4, 30, tzinfo=UTC)
        for provider in (ForecastProvider.GFS, ForecastProvider.ECMWF_OPEN_DATA):
            provider_client = build_provider_client(provider, [6], use_fixtures=True)
            run_ref = provider_client.discover_latest_run(retrieved_at)
            artifacts = provider_client.fetch_run(run_ref)
            run = build_run_record(run_ref, artifacts, retrieved_at).model_copy(
                update={"status": ForecastRunStatus.STORED}
            )
            frames = normalize_frames(run_ref, artifacts, retrieved_at)
            await repository.upsert_run(run)
            await repository.upsert_frames(frames)

        app = create_app(forecast_repository=repository)
        with TestClient(app) as http:
            response = http.get("/api/freshness", params={"provider": "ecmwf_open_data"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["provider"], "ecmwf_open_data")
        self.assertEqual(payload["model"], "ifs")
        self.assertEqual(payload["thresholdHours"], 13)


if __name__ == "__main__":
    unittest.main()
