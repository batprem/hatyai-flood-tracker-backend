"""Integration tests for :class:`MongoForecastRepository`.

These tests run against ``mongomock-motor`` so they execute without a live
MongoDB instance. They cover idempotent upsert semantics on ``runId`` and
``frameId`` (mirroring ``DryRunForecastRepository`` at ``repository.py:51``
and ``repository.py:56``), repository selection from configuration, and
verify that the indexes documented for Phase 1 are created.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from mongomock_motor import AsyncMongoMockClient

from app.core.config import ForecastRepositoryBackend, Settings
from app.ingestion.forecast_cli import _ingest_into_repository
from app.ingestion.models import (
    ForecastProvider,
    ForecastRunStatus,
    FreshnessStatus,
)
from app.ingestion.mongo_repository import (
    FORECAST_FRAMES_COLLECTION,
    FORECAST_RUNS_COLLECTION,
    MongoForecastRepository,
    build_mongo_repository,
)
from app.ingestion.normalizer import build_run_record, normalize_frames
from app.ingestion.providers import build_provider_client


def _build_repository() -> MongoForecastRepository:
    client = AsyncMongoMockClient()
    return build_mongo_repository(client, "hatyai_flood_warning_test")


async def _seed_gfs_run(
    repository: MongoForecastRepository,
    *,
    forecast_hours: list[int],
    retrieved_at: datetime,
) -> tuple[int, int]:
    client = build_provider_client(ForecastProvider.GFS, forecast_hours, use_fixtures=True)
    run_ref = client.discover_latest_run(retrieved_at)
    artifacts = client.fetch_run(run_ref)
    run = build_run_record(run_ref, artifacts, retrieved_at).model_copy(
        update={"status": ForecastRunStatus.STORED}
    )
    frames = normalize_frames(run_ref, artifacts, retrieved_at)
    await repository.upsert_run(run)
    await repository.upsert_frames(frames)
    return 1, len(frames)


class MongoForecastRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_indexes_creates_documented_indexes(self) -> None:
        repository = _build_repository()
        await repository.ensure_indexes()

        runs_indexes = {
            index["name"]
            async for index in repository.database[FORECAST_RUNS_COLLECTION].list_indexes()
        }
        frames_indexes = {
            index["name"]
            async for index in repository.database[FORECAST_FRAMES_COLLECTION].list_indexes()
        }

        self.assertIn("runId_unique", runs_indexes)
        self.assertIn("provider_model_runTime_unique", runs_indexes)
        # MongoDB time-series collections do not support unique indexes, so
        # the frames collection carries a non-unique ``frameId`` lookup index
        # (see commit 567a4c9 "fix: drop unique constraint on frames
        # time-series index"). Idempotency is enforced by ``upsert_frames``
        # via delete-by-frameId-then-insert rather than a Mongo constraint.
        self.assertIn("frameId", frames_indexes)
        self.assertIn("provider_model_area_validTime", frames_indexes)

    async def test_upsert_run_is_idempotent_on_run_id(self) -> None:
        repository = _build_repository()
        await repository.ensure_indexes()

        retrieved_at = datetime(2026, 5, 1, 4, 30, tzinfo=UTC)
        await _seed_gfs_run(repository, forecast_hours=[6], retrieved_at=retrieved_at)
        await _seed_gfs_run(
            repository,
            forecast_hours=[6],
            retrieved_at=retrieved_at.replace(hour=5),
        )

        run_count = await repository.database[FORECAST_RUNS_COLLECTION].count_documents({})
        self.assertEqual(run_count, 1)

    async def test_upsert_frames_is_idempotent_on_frame_id(self) -> None:
        repository = _build_repository()
        await repository.ensure_indexes()

        retrieved_at = datetime(2026, 5, 1, 4, 30, tzinfo=UTC)
        _, first_frame_count = await _seed_gfs_run(
            repository, forecast_hours=[6, 12], retrieved_at=retrieved_at
        )
        await _seed_gfs_run(
            repository,
            forecast_hours=[6, 12],
            retrieved_at=retrieved_at.replace(minute=45),
        )

        frame_count = await repository.database[FORECAST_FRAMES_COLLECTION].count_documents({})
        self.assertEqual(frame_count, first_frame_count)

    async def test_list_frames_round_trips_through_pydantic(self) -> None:
        repository = _build_repository()
        await repository.ensure_indexes()

        retrieved_at = datetime(2026, 5, 1, 4, 30, tzinfo=UTC)
        await _seed_gfs_run(repository, forecast_hours=[6, 12], retrieved_at=retrieved_at)

        frames = await repository.list_frames(provider=ForecastProvider.GFS.value)
        self.assertEqual(len(frames), 2)
        self.assertTrue(all(frame.provider is ForecastProvider.GFS for frame in frames))
        self.assertTrue(all(frame.area.name == "hatyai_utapao_songkhla_phase1" for frame in frames))
        valid_times = [frame.valid_time for frame in frames]
        self.assertEqual(valid_times, sorted(valid_times))

    async def test_list_frames_filters_by_valid_time_window(self) -> None:
        repository = _build_repository()
        await repository.ensure_indexes()

        retrieved_at = datetime(2026, 5, 1, 4, 30, tzinfo=UTC)
        await _seed_gfs_run(repository, forecast_hours=[6, 12], retrieved_at=retrieved_at)

        all_frames = await repository.list_frames(provider=ForecastProvider.GFS.value)
        self.assertEqual(len(all_frames), 2)

        cutoff = all_frames[0].valid_time
        narrowed = await repository.list_frames(
            provider=ForecastProvider.GFS.value,
            valid_time_to=cutoff,
        )
        self.assertEqual(len(narrowed), 1)
        self.assertEqual(narrowed[0].valid_time, cutoff)

    async def test_freshness_summary_after_ingestion_is_fresh_or_stale(self) -> None:
        repository = _build_repository()
        await repository.ensure_indexes()

        retrieved_at = datetime(2026, 5, 1, 4, 30, tzinfo=UTC)
        await _seed_gfs_run(repository, forecast_hours=[6], retrieved_at=retrieved_at)

        summary = await repository.freshness_summary(provider=ForecastProvider.GFS.value)
        self.assertEqual(summary["provider"], ForecastProvider.GFS.value)
        self.assertEqual(summary["frameCount"], 1)
        self.assertIn(
            summary["status"],
            {
                FreshnessStatus.FRESH.value,
                FreshnessStatus.DELAYED.value,
                FreshnessStatus.STALE.value,
                FreshnessStatus.PARTIAL.value,
            },
        )

    async def test_freshness_summary_without_runs_reports_failed(self) -> None:
        repository = _build_repository()
        await repository.ensure_indexes()

        summary = await repository.freshness_summary()
        self.assertEqual(summary["status"], FreshnessStatus.FAILED)
        self.assertIn("reason", summary)

    async def test_cli_ingestion_helper_persists_through_repository(self) -> None:
        repository = _build_repository()
        await repository.ensure_indexes()

        runs, frames, failures = await _ingest_into_repository(
            repository,
            providers=[ForecastProvider.GFS],
            forecast_hours=[6],
            use_fixtures=True,
        )
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(frames), 1)
        self.assertEqual(failures, [])

        stored_runs = await repository.database[FORECAST_RUNS_COLLECTION].count_documents({})
        stored_frames = await repository.database[FORECAST_FRAMES_COLLECTION].count_documents({})
        self.assertEqual(stored_runs, 1)
        self.assertEqual(stored_frames, 1)


class ForecastRepositorySelectionTests(unittest.IsolatedAsyncioTestCase):
    def test_default_settings_select_dry_run_backend(self) -> None:
        settings = Settings(_env_file=None)
        self.assertIs(
            settings.forecast_repository_backend,
            ForecastRepositoryBackend.DRY_RUN,
        )

    def test_settings_accept_mongo_backend(self) -> None:
        settings = Settings(
            _env_file=None,
            forecast_repository_backend=ForecastRepositoryBackend.MONGO,
            mongodb_uri="mongodb://example:27017",
            mongodb_database="hft_test",
        )
        self.assertIs(
            settings.forecast_repository_backend,
            ForecastRepositoryBackend.MONGO,
        )
        self.assertEqual(settings.mongodb_uri, "mongodb://example:27017")
        self.assertEqual(settings.mongodb_database, "hft_test")


if __name__ == "__main__":
    unittest.main()
