"""Tests for the LINE flood-alert dispatch path and the test endpoint.

The LINE Notify HTTP call is mocked throughout so tests run offline. Coverage
mirrors HFT-40: correct payload on an upward transition, cooldown
deduplication, no alert on a downward transition, a further upward transition
bypassing cooldown, and bearer-token protection on POST /api/alerts/test.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from app.main import create_app
from app.schemas.common import RiskLevel
from app.services.alert_dispatch import (
    AlertState,
    dispatch_risk_alert,
    format_alert_message,
    read_alert_state,
    should_send_alert,
    write_alert_state,
)

DASHBOARD_URL = "https://hatyai-flood-warning.vercel.app"
TOKEN = "test-line-token"


def _database() -> object:
    client = AsyncMongoMockClient()
    return client["hatyai_flood_warning_test"]


class ShouldSendAlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 5, 27, 18, 0, tzinfo=UTC)

    def test_first_orange_alert_fires(self) -> None:
        decision = should_send_alert(
            current_level=RiskLevel.ORANGE,
            state=None,
            now=self.now,
            cooldown_hours=3,
        )
        self.assertTrue(decision.should_send)

    def test_yellow_does_not_alert(self) -> None:
        decision = should_send_alert(
            current_level=RiskLevel.YELLOW,
            state=None,
            now=self.now,
            cooldown_hours=3,
        )
        self.assertFalse(decision.should_send)

    def test_upward_orange_to_red_bypasses_cooldown(self) -> None:
        state = AlertState(
            source="line_notify",
            last_risk_level=RiskLevel.ORANGE,
            alerted_at=self.now - timedelta(minutes=10),
        )
        decision = should_send_alert(
            current_level=RiskLevel.RED,
            state=state,
            now=self.now,
            cooldown_hours=3,
        )
        self.assertTrue(decision.should_send)

    def test_same_level_within_cooldown_suppressed(self) -> None:
        state = AlertState(
            source="line_notify",
            last_risk_level=RiskLevel.ORANGE,
            alerted_at=self.now - timedelta(hours=1),
        )
        decision = should_send_alert(
            current_level=RiskLevel.ORANGE,
            state=state,
            now=self.now,
            cooldown_hours=3,
        )
        self.assertFalse(decision.should_send)

    def test_same_level_after_cooldown_fires(self) -> None:
        state = AlertState(
            source="line_notify",
            last_risk_level=RiskLevel.ORANGE,
            alerted_at=self.now - timedelta(hours=4),
        )
        decision = should_send_alert(
            current_level=RiskLevel.ORANGE,
            state=state,
            now=self.now,
            cooldown_hours=3,
        )
        self.assertTrue(decision.should_send)

    def test_downward_transition_does_not_alert(self) -> None:
        state = AlertState(
            source="line_notify",
            last_risk_level=RiskLevel.RED,
            alerted_at=self.now - timedelta(hours=4),
        )
        decision = should_send_alert(
            current_level=RiskLevel.GREEN,
            state=state,
            now=self.now,
            cooldown_hours=3,
        )
        self.assertFalse(decision.should_send)


class FormatAlertMessageTests(unittest.TestCase):
    def test_bilingual_orange_message(self) -> None:
        message = format_alert_message(
            level=RiskLevel.ORANGE,
            valid_at=datetime(2026, 5, 27, 18, 0, tzinfo=UTC),
            dashboard_url=DASHBOARD_URL,
        )
        self.assertIn("Hat Yai Flood Alert / แจ้งเตือนน้ำท่วมหาดใหญ่", message)
        self.assertIn("Risk level: ORANGE / ระดับ: เฝ้าระวัง", message)
        self.assertIn("Valid: 2026-05-27 18:00 UTC", message)
        self.assertIn(DASHBOARD_URL, message)


class DispatchRiskAlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 5, 27, 18, 0, tzinfo=UTC)

    async def test_sends_payload_on_upward_transition(self) -> None:
        database = _database()
        await write_alert_state(
            database,
            AlertState(
                source="line_notify",
                last_risk_level=RiskLevel.YELLOW,
                alerted_at=self.now - timedelta(hours=1),
            ),
        )
        with patch(
            "app.services.alert_dispatch.send_line_notify", new=AsyncMock(return_value=200)
        ) as mock_send:
            decision = await dispatch_risk_alert(
                database=database,
                current_level=RiskLevel.ORANGE,
                valid_at=self.now,
                token=TOKEN,
                cooldown_hours=3,
                dashboard_url=DASHBOARD_URL,
                now=self.now,
            )

        self.assertTrue(decision.should_send)
        mock_send.assert_awaited_once()
        sent_token, sent_message = mock_send.await_args.args
        self.assertEqual(sent_token, TOKEN)
        self.assertIn("Risk level: ORANGE / ระดับ: เฝ้าระวัง", sent_message)
        stored = await read_alert_state(database)
        assert stored is not None
        self.assertEqual(stored.last_risk_level, RiskLevel.ORANGE)
        self.assertEqual(stored.alerted_at, self.now)

    async def test_no_double_fire_within_cooldown(self) -> None:
        database = _database()
        await write_alert_state(
            database,
            AlertState(
                source="line_notify",
                last_risk_level=RiskLevel.ORANGE,
                alerted_at=self.now - timedelta(hours=1),
            ),
        )
        with patch(
            "app.services.alert_dispatch.send_line_notify", new=AsyncMock(return_value=200)
        ) as mock_send:
            decision = await dispatch_risk_alert(
                database=database,
                current_level=RiskLevel.ORANGE,
                valid_at=self.now,
                token=TOKEN,
                cooldown_hours=3,
                dashboard_url=DASHBOARD_URL,
                now=self.now,
            )

        self.assertFalse(decision.should_send)
        mock_send.assert_not_awaited()

    async def test_no_alert_on_downward_transition(self) -> None:
        database = _database()
        await write_alert_state(
            database,
            AlertState(
                source="line_notify",
                last_risk_level=RiskLevel.RED,
                alerted_at=self.now - timedelta(hours=4),
            ),
        )
        with patch(
            "app.services.alert_dispatch.send_line_notify", new=AsyncMock(return_value=200)
        ) as mock_send:
            decision = await dispatch_risk_alert(
                database=database,
                current_level=RiskLevel.GREEN,
                valid_at=self.now,
                token=TOKEN,
                cooldown_hours=3,
                dashboard_url=DASHBOARD_URL,
                now=self.now,
            )

        self.assertFalse(decision.should_send)
        mock_send.assert_not_awaited()
        stored = await read_alert_state(database)
        assert stored is not None
        self.assertEqual(stored.last_risk_level, RiskLevel.RED)

    async def test_sends_on_increase_orange_to_red_within_cooldown(self) -> None:
        database = _database()
        await write_alert_state(
            database,
            AlertState(
                source="line_notify",
                last_risk_level=RiskLevel.ORANGE,
                alerted_at=self.now - timedelta(minutes=20),
            ),
        )
        with patch(
            "app.services.alert_dispatch.send_line_notify", new=AsyncMock(return_value=200)
        ) as mock_send:
            decision = await dispatch_risk_alert(
                database=database,
                current_level=RiskLevel.RED,
                valid_at=self.now,
                token=TOKEN,
                cooldown_hours=3,
                dashboard_url=DASHBOARD_URL,
                now=self.now,
            )

        self.assertTrue(decision.should_send)
        mock_send.assert_awaited_once()
        _, sent_message = mock_send.await_args.args
        self.assertIn("Risk level: RED / ระดับ: อันตราย", sent_message)
        stored = await read_alert_state(database)
        assert stored is not None
        self.assertEqual(stored.last_risk_level, RiskLevel.RED)

    async def test_empty_token_skips_send_without_state_write(self) -> None:
        database = _database()
        with patch(
            "app.services.alert_dispatch.send_line_notify", new=AsyncMock(return_value=200)
        ) as mock_send:
            decision = await dispatch_risk_alert(
                database=database,
                current_level=RiskLevel.RED,
                valid_at=self.now,
                token="",
                cooldown_hours=3,
                dashboard_url=DASHBOARD_URL,
                now=self.now,
            )

        self.assertFalse(decision.should_send)
        mock_send.assert_not_awaited()
        self.assertIsNone(await read_alert_state(database))


class SchedulerHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_evaluate_dispatches_when_frames_drive_orange(self) -> None:
        from app.ingestion.forecast_cli import _evaluate_and_dispatch_alert
        from app.ingestion.models import ForecastProvider, ForecastRunStatus
        from app.ingestion.mongo_repository import build_mongo_repository
        from app.ingestion.normalizer import build_run_record, normalize_frames
        from app.ingestion.providers import build_provider_client

        client = AsyncMongoMockClient()
        repository = build_mongo_repository(client, "hatyai_flood_warning_test")
        await repository.ensure_indexes()
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

        # Force every rainfall window to score orange so the hook fires
        # regardless of fixture magnitudes; this exercises the wiring, not the
        # thresholds (which have their own tests).
        settings = Settings(
            LINE_NOTIFY_TOKEN=TOKEN,
            risk_rainfall_6h_yellow_mm=0,
            risk_rainfall_6h_orange_mm=0,
            risk_rainfall_6h_red_mm=1000,
        )
        with patch(
            "app.services.alert_dispatch.send_line_notify", new=AsyncMock(return_value=200)
        ) as mock_send:
            reason = await _evaluate_and_dispatch_alert(repository, settings=settings)

        self.assertIsNotNone(reason)
        mock_send.assert_awaited_once()
        stored = await read_alert_state(repository.database)
        assert stored is not None
        self.assertEqual(stored.last_risk_level, RiskLevel.ORANGE)

    async def test_evaluate_returns_none_without_frames(self) -> None:
        from app.ingestion.forecast_cli import _evaluate_and_dispatch_alert
        from app.ingestion.mongo_repository import build_mongo_repository

        client = AsyncMongoMockClient()
        repository = build_mongo_repository(client, "hatyai_flood_warning_test")
        await repository.ensure_indexes()
        with patch(
            "app.services.alert_dispatch.send_line_notify", new=AsyncMock(return_value=200)
        ) as mock_send:
            reason = await _evaluate_and_dispatch_alert(
                repository, settings=Settings(LINE_NOTIFY_TOKEN=TOKEN)
            )
        self.assertIsNone(reason)
        mock_send.assert_not_awaited()


class AlertTestEndpointTests(unittest.TestCase):
    def _client(self) -> TestClient:
        settings = Settings(
            ALERTS_TEST_TOKEN="secret-dev-token",
            LINE_NOTIFY_TOKEN="line-token",
        )
        app = create_app(settings=settings)
        return TestClient(app)

    def test_rejects_missing_token(self) -> None:
        with self._client() as http:
            response = http.post("/api/alerts/test")
        self.assertEqual(response.status_code, 403)

    def test_rejects_bad_token(self) -> None:
        with self._client() as http:
            response = http.post(
                "/api/alerts/test",
                headers={"Authorization": "Bearer wrong-token"},
            )
        self.assertEqual(response.status_code, 403)

    def test_accepts_valid_token(self) -> None:
        with patch(
            "app.api.alerts.send_line_notify", new=AsyncMock(return_value=200)
        ) as mock_send:
            with self._client() as http:
                response = http.post(
                    "/api/alerts/test",
                    headers={"Authorization": "Bearer secret-dev-token"},
                )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "sent")
        self.assertEqual(payload["line_status"], 200)
        mock_send.assert_awaited_once()

    def test_rejects_when_token_unconfigured(self) -> None:
        settings = Settings(ALERTS_TEST_TOKEN="", LINE_NOTIFY_TOKEN="line-token")
        app = create_app(settings=settings)
        with TestClient(app) as http:
            response = http.post(
                "/api/alerts/test",
                headers={"Authorization": "Bearer "},
            )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
