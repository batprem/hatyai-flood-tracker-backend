"""Tests for ops-notifier dispatch and the per-pipeline health block (HFT-75)."""

import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.ingestion.models import ForecastProvider, ForecastRunStatus
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
    compute_data_quality,
    dispatch_ops_alerts,
    evaluate_and_alert,
    ops_events_from_snapshot,
)
from app.services.ops_notifier import (
    OPS_ALERT_EVENT,
    LineOpsNotifier,
    LoggingOpsNotifier,
    OpsEvent,
    OpsEventKind,
    PipelineName,
)


class RecordingOpsNotifier:
    """Capture dispatched ops events for assertions."""

    def __init__(self) -> None:
        self.events: list[OpsEvent] = []

    async def notify(self, event: OpsEvent) -> None:
        """Record one dispatched event.

        Args:
            event: The dispatched ops event to record.
        """
        self.events.append(event)


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


class OpsNotifierDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_simulated_ingestion_failure_dispatches_to_notifier(self) -> None:
        """A FAILED ingestion run must reach the ops notifier as ingestion_failure."""
        now = datetime(2026, 5, 1, 6, 0, tzinfo=UTC)
        forecast = DryRunForecastRepository()
        await _store_run(
            forecast,
            ForecastProvider.GFS,
            now - timedelta(hours=1),
            status=ForecastRunStatus.FAILED,
        )
        await _store_run(forecast, ForecastProvider.ECMWF_OPEN_DATA, now - timedelta(hours=2))
        stations = DryRunStationRepository()
        await stations.upsert_many([_observation(now - timedelta(minutes=30))])
        notifier = RecordingOpsNotifier()

        await evaluate_and_alert(forecast, stations, Settings(), now=now, notifier=notifier)

        self.assertEqual(len(notifier.events), 1)
        event = notifier.events[0]
        self.assertEqual(event.kind, OpsEventKind.INGESTION_FAILURE)
        self.assertEqual(event.pipeline, PipelineName.GFS)
        self.assertEqual(event.status, "failed")
        self.assertIsNotNone(event.reason)
        self.assertEqual(event.detected_at, now)

    async def test_staleness_breach_dispatches_to_notifier(self) -> None:
        now = datetime(2026, 5, 1, 18, 0, tzinfo=UTC)
        forecast = DryRunForecastRepository()
        # GFS run is 9h old against the 6h default threshold.
        await _store_run(forecast, ForecastProvider.GFS, now - timedelta(hours=9))
        stations = DryRunStationRepository()
        notifier = RecordingOpsNotifier()

        snapshot = await compute_data_quality(forecast, stations, Settings(), now=now)
        events = await dispatch_ops_alerts(snapshot, notifier)

        self.assertEqual(events, notifier.events)
        by_pipeline = {event.pipeline: event for event in notifier.events}
        self.assertEqual(by_pipeline[PipelineName.GFS].kind, OpsEventKind.STALENESS_BREACH)
        # No ECMWF run stored and no station observations -> ingestion failures.
        self.assertEqual(by_pipeline[PipelineName.ECMWF].kind, OpsEventKind.INGESTION_FAILURE)
        self.assertEqual(by_pipeline[PipelineName.STATIONS].kind, OpsEventKind.INGESTION_FAILURE)

    async def test_fresh_pipelines_dispatch_nothing(self) -> None:
        now = datetime(2026, 5, 1, 6, 0, tzinfo=UTC)
        forecast = DryRunForecastRepository()
        await _store_run(forecast, ForecastProvider.GFS, now - timedelta(hours=2))
        await _store_run(forecast, ForecastProvider.ECMWF_OPEN_DATA, now - timedelta(hours=2))
        stations = DryRunStationRepository()
        await stations.upsert_many([_observation(now - timedelta(minutes=10))])
        notifier = RecordingOpsNotifier()

        snapshot = await evaluate_and_alert(
            forecast, stations, Settings(), now=now, notifier=notifier
        )

        self.assertEqual(notifier.events, [])
        self.assertEqual(ops_events_from_snapshot(snapshot), [])


class LoggingOpsNotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_logs_structured_json_error_line(self) -> None:
        notifier = LoggingOpsNotifier()
        event = OpsEvent(
            kind=OpsEventKind.STALENESS_BREACH,
            pipeline=PipelineName.GFS,
            status="stale",
            age_hours=9.016,
            threshold_hours=6.0,
            reason="latest run age 9.0h exceeds 6.0h threshold",
            detected_at=datetime(2026, 5, 1, 18, 0, tzinfo=UTC),
        )

        with self.assertLogs("app.services.ops_notifier", level="ERROR") as captured:
            await notifier.notify(event)

        self.assertEqual(len(captured.records), 1)
        payload = json.loads(captured.records[0].message)
        self.assertEqual(payload["event"], OPS_ALERT_EVENT)
        self.assertEqual(payload["kind"], "staleness_breach")
        self.assertEqual(payload["pipeline"], "gfs")
        self.assertEqual(payload["status"], "stale")
        self.assertEqual(payload["ageHours"], 9.02)
        self.assertEqual(payload["thresholdHours"], 6.0)
        self.assertEqual(payload["detectedAt"], "2026-05-01T18:00:00+00:00")

    async def test_evaluate_and_alert_defaults_to_logging_notifier(self) -> None:
        now = datetime(2026, 5, 1, 18, 0, tzinfo=UTC)
        forecast = DryRunForecastRepository()
        await _store_run(forecast, ForecastProvider.GFS, now - timedelta(hours=9))
        stations = DryRunStationRepository()

        with self.assertLogs("app.services.ops_notifier", level="ERROR") as captured:
            await evaluate_and_alert(forecast, stations, Settings(), now=now)

        events = [json.loads(record.message) for record in captured.records]
        self.assertTrue(all(item["event"] == OPS_ALERT_EVENT for item in events))
        pipelines = {item["pipeline"] for item in events}
        self.assertEqual(pipelines, {"gfs", "ecmwf", "stations"})


def _make_staleness_event(age_hours: float | None = 9.0) -> OpsEvent:
    return OpsEvent(
        kind=OpsEventKind.STALENESS_BREACH,
        pipeline=PipelineName.GFS,
        status="stale",
        age_hours=age_hours,
        threshold_hours=6.0,
        reason="latest run age 9.0h exceeds 6.0h threshold",
        detected_at=datetime(2026, 5, 1, 18, 0, tzinfo=UTC),
    )


class LineOpsNotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_to_line_when_token_set(self) -> None:
        """When token is non-empty, send_line_notify is called once with event details."""
        event = _make_staleness_event()
        notifier = LineOpsNotifier("ops-secret-token")

        with patch(
            "app.services.ops_notifier.send_line_notify", new_callable=AsyncMock
        ) as mock_send:
            with self.assertLogs("app.services.ops_notifier", level="ERROR"):
                await notifier.notify(event)

        mock_send.assert_awaited_once()
        call_args = mock_send.call_args
        token_arg, message_arg = call_args.args
        self.assertEqual(token_arg, "ops-secret-token")
        self.assertIn("staleness_breach", message_arg)
        self.assertIn("gfs", message_arg)

    async def test_skips_line_when_token_empty(self) -> None:
        """When token is empty, send_line_notify is not called and a warning is logged."""
        event = _make_staleness_event()
        notifier = LineOpsNotifier("")

        with patch(
            "app.services.ops_notifier.send_line_notify", new_callable=AsyncMock
        ) as mock_send:
            with self.assertLogs("app.services.ops_notifier", level="WARNING") as captured:
                await notifier.notify(event)

        mock_send.assert_not_awaited()
        warning_messages = [r.message for r in captured.records if r.levelname == "WARNING"]
        self.assertTrue(
            any("empty" in msg.lower() or "skipping" in msg.lower() for msg in warning_messages)
        )

    async def test_always_logs_structured_json(self) -> None:
        """LoggingOpsNotifier still emits a structured JSON ERROR even when LINE is sent."""
        event = _make_staleness_event()
        notifier = LineOpsNotifier("ops-secret-token")

        with patch("app.services.ops_notifier.send_line_notify", new_callable=AsyncMock):
            with self.assertLogs("app.services.ops_notifier", level="ERROR") as captured:
                await notifier.notify(event)

        error_records = [r for r in captured.records if r.levelname == "ERROR"]
        self.assertEqual(len(error_records), 1)
        payload = json.loads(error_records[0].message)
        self.assertEqual(payload["event"], OPS_ALERT_EVENT)
        self.assertEqual(payload["kind"], "staleness_breach")
        self.assertEqual(payload["pipeline"], "gfs")

    async def test_ops_token_independent_of_public_token(self) -> None:
        """Settings keeps ops and public LINE tokens in separate fields."""
        settings = Settings.model_validate(
            {"LINE_OPS_TOKEN": "ops-tok", "LINE_NOTIFY_TOKEN": "pub-tok"}
        )
        self.assertEqual(settings.line_ops_token, "ops-tok")
        self.assertEqual(settings.line_notify_token, "pub-tok")


class HealthPipelinesBlockTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_reports_per_pipeline_freshness(self) -> None:
        forecast = DryRunForecastRepository()
        retrieved_at = datetime.now(UTC) - timedelta(hours=1)
        await _store_run(forecast, ForecastProvider.GFS, retrieved_at)
        stations = DryRunStationRepository()
        await stations.upsert_many([_observation(datetime.now(UTC) - timedelta(minutes=30))])

        app = create_app(forecast_repository=forecast, station_repository=stations)
        with TestClient(app) as http:
            response = http.get("/health")

        self.assertEqual(response.status_code, 200)
        pipelines = response.json()["pipelines"]
        self.assertEqual(set(pipelines), {"gfs", "ecmwf", "stations"})

        # The fixture GFS run time floats with real wall-clock time, so assert
        # the stale flag's consistency with the status instead of exact values.
        gfs = pipelines["gfs"]
        self.assertEqual(gfs["pipeline"], "gfs")
        self.assertIsNotNone(gfs["lastSuccessAt"])
        self.assertIsNotNone(gfs["ageHours"])
        self.assertEqual(gfs["thresholdHours"], 6.0)
        self.assertEqual(gfs["stale"], gfs["status"] in {"stale", "partial", "failed"})

        ecmwf = pipelines["ecmwf"]
        self.assertEqual(ecmwf["pipeline"], "ecmwf")
        self.assertIsNone(ecmwf["lastSuccessAt"])
        self.assertTrue(ecmwf["stale"])
        self.assertEqual(ecmwf["status"], "failed")

        stations_block = pipelines["stations"]
        self.assertEqual(stations_block["pipeline"], "stations")
        self.assertIsNotNone(stations_block["lastSuccessAt"])
        self.assertFalse(stations_block["stale"])
        self.assertEqual(stations_block["status"], "fresh")


if __name__ == "__main__":
    unittest.main()
