import contextlib
import io
import json
import logging
import sys
import unittest
from datetime import UTC, datetime
from unittest import mock

from app.ingestion import forecast_cli
from app.ingestion.forecast_cli import run_dry_ingestion
from app.ingestion.models import (
    ForecastProvider,
    ForecastRunStatus,
    FreshnessStatus,
)
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
        self.assertEqual(payload["failures"], [])
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

    async def test_provider_failure_records_failed_run_and_returns_failures(self) -> None:
        repository = DryRunForecastRepository()

        def boom(
            provider: ForecastProvider,
            forecast_hours: list[int],
            *,
            use_fixtures: bool,
        ) -> object:
            raise RuntimeError("provider unavailable")

        with (
            mock.patch.object(forecast_cli, "build_provider_client", side_effect=boom),
            self.assertLogs(forecast_cli.logger, level=logging.ERROR),
        ):
            payload = await run_dry_ingestion(
                providers=[ForecastProvider.GFS],
                forecast_hours=[6],
                include_mongo_preview=False,
                repository=repository,
                use_fixtures=True,
            )

        self.assertEqual(payload["runs"], [])
        self.assertEqual(payload["frames"], [])
        self.assertEqual(len(payload["failures"]), 1)
        failure = payload["failures"][0]
        self.assertEqual(failure["provider"], "gfs")
        self.assertIn("provider unavailable", failure["errorReason"])

        self.assertEqual(len(repository.runs), 1)
        failure_run = repository.runs[0]
        self.assertEqual(failure_run.status, ForecastRunStatus.FAILED)
        self.assertEqual(failure_run.freshness_status, FreshnessStatus.FAILED)
        self.assertIsNotNone(failure_run.error_reason)

        summary = await repository.freshness_summary()
        self.assertEqual(summary["status"], FreshnessStatus.FAILED)


class ForecastCliExitCodeTests(unittest.TestCase):
    def test_main_exits_non_zero_when_provider_fails(self) -> None:
        argv = [
            "forecast_cli",
            "--provider",
            "gfs",
            "--forecast-hours",
            "6",
            "--use-fixtures",
        ]

        def boom(
            provider: ForecastProvider,
            forecast_hours: list[int],
            *,
            use_fixtures: bool,
        ) -> object:
            raise RuntimeError("provider unavailable")

        captured = io.StringIO()
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(forecast_cli, "build_provider_client", side_effect=boom),
            contextlib.redirect_stdout(captured),
            self.assertLogs(forecast_cli.logger, level=logging.ERROR),
            self.assertRaises(SystemExit) as exit_ctx,
        ):
            forecast_cli.main()

        self.assertEqual(exit_ctx.exception.code, 1)
        payload = json.loads(captured.getvalue())
        self.assertEqual(payload["runs"], [])
        self.assertEqual(len(payload["failures"]), 1)
        self.assertEqual(payload["failures"][0]["provider"], "gfs")

    def test_main_exits_zero_on_success(self) -> None:
        argv = [
            "forecast_cli",
            "--provider",
            "gfs",
            "--forecast-hours",
            "6",
            "--use-fixtures",
        ]
        captured = io.StringIO()
        with (
            mock.patch.object(sys, "argv", argv),
            contextlib.redirect_stdout(captured),
        ):
            # main() returns normally on success; assert no SystemExit raised.
            forecast_cli.main()

        payload = json.loads(captured.getvalue())
        self.assertEqual(payload["failures"], [])
        self.assertGreaterEqual(len(payload["runs"]), 1)


if __name__ == "__main__":
    unittest.main()
