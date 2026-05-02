import unittest
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.ingestion.forecast_cli import run_dry_ingestion
from app.ingestion.models import ForecastProvider, ForecastRunStatus
from app.ingestion.normalizer import build_run_record, normalize_frames
from app.ingestion.providers import build_provider_client
from app.ingestion.repository import DryRunForecastRepository
from app.main import create_app


class ForecastFramesApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_forecast_frames_returns_documented_shape(self) -> None:
        repository = DryRunForecastRepository()
        retrieved_at = datetime(2026, 5, 1, 4, 30, tzinfo=UTC)
        client_provider = build_provider_client(ForecastProvider.GFS, [6, 12], use_fixtures=True)
        run_ref = client_provider.discover_latest_run(retrieved_at)
        artifacts = client_provider.fetch_run(run_ref)
        run = build_run_record(run_ref, artifacts, retrieved_at).model_copy(
            update={"status": ForecastRunStatus.STORED}
        )
        frames = normalize_frames(run_ref, artifacts, retrieved_at)
        await repository.upsert_run(run)
        await repository.upsert_frames(frames)

        app = create_app(forecast_repository=repository)
        with TestClient(app) as http:
            response = http.get("/api/forecast/frames", params={"provider": "gfs"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertIn("freshness", payload)
        self.assertIn("query", payload)
        self.assertIn("frames", payload)

        freshness = payload["freshness"]
        self.assertIn(freshness["status"], {"fresh", "delayed", "stale", "partial", "failed"})
        self.assertIsNotNone(freshness["retrievedAt"])
        self.assertEqual(freshness["thresholdHours"], 7)
        self.assertEqual(freshness["provider"], "gfs")
        self.assertEqual(freshness["frameCount"], 2)

        query = payload["query"]
        self.assertEqual(query["provider"], "gfs")
        self.assertEqual(query["area"], "hatyai_utapao_songkhla_phase1")

        self.assertEqual(len(payload["frames"]), 2)
        sample = payload["frames"][0]
        for key in (
            "frameId",
            "runId",
            "provider",
            "model",
            "variable",
            "statistic",
            "unit",
            "runTime",
            "validTime",
            "windowStart",
            "windowEnd",
            "accumulationHours",
            "providerAccumulationSemantics",
            "forecastHour",
            "retrievedAt",
            "processedAt",
            "area",
            "grid",
            "valuesMm",
            "source",
            "quality",
        ):
            self.assertIn(key, sample, f"missing public field {key}")

        self.assertEqual(sample["provider"], "gfs")
        self.assertEqual(sample["unit"], "mm")
        self.assertEqual(sample["windowEnd"], sample["validTime"])
        self.assertEqual(sample["area"]["name"], "hatyai_utapao_songkhla_phase1")
        self.assertEqual(len(sample["area"]["bbox"]), 4)
        self.assertEqual(sample["area"]["crs"], "EPSG:4326")
        self.assertGreater(sample["grid"]["resolutionDegrees"], 0)

        source = sample["source"]
        for key in ("url", "product", "license", "attribution", "rawArtifactRef"):
            self.assertIn(key, source)
        self.assertTrue(source["attribution"], "attribution must be non-empty")

        quality = sample["quality"]
        self.assertEqual(quality["missingValueCount"], 0)
        self.assertGreaterEqual(quality["max"], quality["min"])

    async def test_dry_run_ingestion_then_api_round_trips_through_repository(self) -> None:
        repository = DryRunForecastRepository()

        await run_dry_ingestion(
            providers=[ForecastProvider.GFS, ForecastProvider.ECMWF_OPEN_DATA],
            forecast_hours=[6],
            include_mongo_preview=False,
            repository=repository,
            use_fixtures=True,
        )

        app = create_app(forecast_repository=repository)
        with TestClient(app) as http:
            all_response = http.get("/api/forecast/frames")
            ecmwf_response = http.get(
                "/api/forecast/frames", params={"provider": "ecmwf_open_data"}
            )

        self.assertEqual(all_response.status_code, 200)
        all_payload = all_response.json()
        providers = {frame["provider"] for frame in all_payload["frames"]}
        self.assertEqual(providers, {"gfs", "ecmwf_open_data"})

        self.assertEqual(ecmwf_response.status_code, 200)
        ecmwf_payload = ecmwf_response.json()
        self.assertEqual(len(ecmwf_payload["frames"]), 1)
        self.assertEqual(ecmwf_payload["frames"][0]["provider"], "ecmwf_open_data")
        self.assertEqual(ecmwf_payload["freshness"]["thresholdHours"], 13)


if __name__ == "__main__":
    unittest.main()
