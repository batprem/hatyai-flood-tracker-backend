import unittest
from datetime import UTC, datetime

from app.ingestion.forecast_cli import run_dry_ingestion
from app.ingestion.models import ForecastProvider, FreshnessStatus
from app.ingestion.normalizer import build_run_record, normalize_frames
from app.ingestion.providers import build_provider_client
from app.ingestion.repository import DryRunForecastRepository


class ForecastIngestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_emits_runs_frames_and_freshness(self) -> None:
        payload = await run_dry_ingestion(
            providers=[ForecastProvider.GFS, ForecastProvider.ECMWF_OPEN_DATA],
            forecast_hours=[6],
            include_mongo_preview=False,
            use_fixtures=True,
        )

        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(len(payload["runs"]), 2)
        self.assertEqual(len(payload["frames"]), 2)
        self.assertIn(
            payload["freshness"]["status"],
            {FreshnessStatus.FRESH, FreshnessStatus.STALE},
        )

    async def test_repository_preserves_native_datetimes_for_mongo_shape(self) -> None:
        retrieved_at = datetime(2026, 5, 1, 4, 30, tzinfo=UTC)
        client = build_provider_client(ForecastProvider.GFS, [6], use_fixtures=True)
        run_ref = client.discover_latest_run(retrieved_at)
        artifacts = client.fetch_run(run_ref)
        run = build_run_record(run_ref, artifacts, retrieved_at)
        frames = normalize_frames(run_ref, artifacts, retrieved_at)

        repository = DryRunForecastRepository()
        await repository.upsert_run(run)
        await repository.upsert_frames(frames)
        mongo_preview = repository.mongo_preview()

        run_doc = mongo_preview["forecast_runs"][0]
        frame_doc = mongo_preview["forecast_frames"][0]
        self.assertIsInstance(run_doc["runTime"], datetime)
        self.assertIsInstance(frame_doc["windowStart"], datetime)
        self.assertEqual(frame_doc["windowEnd"], frame_doc["validTime"])
        self.assertEqual(frame_doc["accumulationHours"], 6)


if __name__ == "__main__":
    unittest.main()
