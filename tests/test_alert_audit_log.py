"""Tests for the alert delivery audit log (HFT-76).

Covers:
- Sends are logged with the correct outcome for each dispatch scenario.
- Cooldown-suppressed dispatches are logged as ``skipped_cooldown``.
- No-token dispatches are logged as ``skipped_no_token``.
- Failed sends (raised exceptions) are logged as ``failed`` with error detail.
- Cooldown decision context (previous_level, previous_alerted_at) is persisted.
- ``GET /api/alerts/recent`` returns typed history newest-first.
- The dead-letter (``failed``) record is visible in the recent response.
- Web Push dispatches are also logged with the correct channel.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.core.config import Settings
from app.ingestion.delivery_repository import DeliveryOutcome, DryRunDeliveryRepository
from app.main import create_app
from app.schemas.common import RiskLevel
from app.services.alert_dispatch import (
    AlertState,
    dispatch_risk_alert,
    dispatch_web_push_alert,
    write_alert_state,
)
from app.services.web_push import VapidConfig

DASHBOARD_URL = "https://hatyai-flood-warning.vercel.app"
TOKEN = "test-line-token"
NOW = datetime(2026, 5, 27, 18, 0, tzinfo=UTC)


def _database() -> object:
    client = AsyncMongoMockClient()
    return client["hatyai_flood_warning_test"]


class LineAuditLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_sent_outcome_logged_on_successful_dispatch(self) -> None:
        database = _database()
        delivery_repo = DryRunDeliveryRepository()
        with patch(
            "app.services.alert_dispatch.send_line_notify", new=AsyncMock(return_value=200)
        ):
            await dispatch_risk_alert(
                database=database,
                current_level=RiskLevel.ORANGE,
                valid_at=NOW,
                token=TOKEN,
                cooldown_hours=3,
                dashboard_url=DASHBOARD_URL,
                now=NOW,
                delivery_repository=delivery_repo,
            )

        records = await delivery_repo.recent(limit=10)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.channel, "line")
        self.assertEqual(record.risk_level, RiskLevel.ORANGE)
        self.assertEqual(record.outcome, DeliveryOutcome.SENT)
        self.assertEqual(record.alerted_at, NOW)
        self.assertIsNone(record.error_detail)
        # First send: no prior state
        self.assertIsNone(record.previous_level)
        self.assertIsNone(record.previous_alerted_at)

    async def test_skipped_cooldown_outcome_logged(self) -> None:
        database = _database()
        delivery_repo = DryRunDeliveryRepository()
        prev_alerted = NOW - timedelta(hours=1)
        await write_alert_state(
            database,
            AlertState(
                source="line_notify",
                last_risk_level=RiskLevel.ORANGE,
                alerted_at=prev_alerted,
            ),
        )
        with patch(
            "app.services.alert_dispatch.send_line_notify", new=AsyncMock(return_value=200)
        ) as mock_send:
            await dispatch_risk_alert(
                database=database,
                current_level=RiskLevel.ORANGE,
                valid_at=NOW,
                token=TOKEN,
                cooldown_hours=3,
                dashboard_url=DASHBOARD_URL,
                now=NOW,
                delivery_repository=delivery_repo,
            )

        mock_send.assert_not_awaited()
        records = await delivery_repo.recent(limit=10)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.outcome, DeliveryOutcome.SKIPPED_COOLDOWN)
        self.assertEqual(record.previous_level, RiskLevel.ORANGE)
        self.assertEqual(record.previous_alerted_at, prev_alerted)

    async def test_skipped_no_token_outcome_logged(self) -> None:
        database = _database()
        delivery_repo = DryRunDeliveryRepository()
        with patch(
            "app.services.alert_dispatch.send_line_notify", new=AsyncMock(return_value=200)
        ) as mock_send:
            await dispatch_risk_alert(
                database=database,
                current_level=RiskLevel.RED,
                valid_at=NOW,
                token="",  # empty token
                cooldown_hours=3,
                dashboard_url=DASHBOARD_URL,
                now=NOW,
                delivery_repository=delivery_repo,
            )

        mock_send.assert_not_awaited()
        records = await delivery_repo.recent(limit=10)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].outcome, DeliveryOutcome.SKIPPED_NO_TOKEN)
        self.assertIsNone(records[0].error_detail)

    async def test_failed_outcome_logged_with_error_detail(self) -> None:
        database = _database()
        delivery_repo = DryRunDeliveryRepository()

        async def _raise(*_args: object, **_kwargs: object) -> None:
            msg = "connection refused"
            raise OSError(msg)

        with patch("app.services.alert_dispatch.send_line_notify", new=_raise):
            await dispatch_risk_alert(
                database=database,
                current_level=RiskLevel.ORANGE,
                valid_at=NOW,
                token=TOKEN,
                cooldown_hours=3,
                dashboard_url=DASHBOARD_URL,
                now=NOW,
                delivery_repository=delivery_repo,
            )

        records = await delivery_repo.recent(limit=10)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.outcome, DeliveryOutcome.FAILED)
        self.assertIsNotNone(record.error_detail)
        self.assertIn("connection refused", record.error_detail or "")

    async def test_cooldown_context_persisted_on_upward_transition(self) -> None:
        database = _database()
        delivery_repo = DryRunDeliveryRepository()
        prev_alerted = NOW - timedelta(minutes=20)
        await write_alert_state(
            database,
            AlertState(
                source="line_notify",
                last_risk_level=RiskLevel.ORANGE,
                alerted_at=prev_alerted,
            ),
        )
        with patch(
            "app.services.alert_dispatch.send_line_notify", new=AsyncMock(return_value=200)
        ):
            await dispatch_risk_alert(
                database=database,
                current_level=RiskLevel.RED,
                valid_at=NOW,
                token=TOKEN,
                cooldown_hours=3,
                dashboard_url=DASHBOARD_URL,
                now=NOW,
                delivery_repository=delivery_repo,
            )

        records = await delivery_repo.recent(limit=10)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.outcome, DeliveryOutcome.SENT)
        self.assertEqual(record.previous_level, RiskLevel.ORANGE)
        self.assertEqual(record.previous_alerted_at, prev_alerted)

    async def test_no_delivery_log_without_repository(self) -> None:
        """Dispatch works with no delivery_repository (backward compat)."""
        database = _database()
        with patch(
            "app.services.alert_dispatch.send_line_notify", new=AsyncMock(return_value=200)
        ) as mock_send:
            decision = await dispatch_risk_alert(
                database=database,
                current_level=RiskLevel.ORANGE,
                valid_at=NOW,
                token=TOKEN,
                cooldown_hours=3,
                dashboard_url=DASHBOARD_URL,
                now=NOW,
            )

        self.assertTrue(decision.should_send)
        mock_send.assert_awaited_once()


class WebPushAuditLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_push_sent_outcome_logged(self) -> None:
        from app.ingestion.subscription_repository import DryRunSubscriptionRepository
        from app.schemas.push_subscription import PushSubscription

        database = _database()
        delivery_repo = DryRunDeliveryRepository()
        sub_repo = DryRunSubscriptionRepository()
        await sub_repo.upsert_subscription(
            PushSubscription(
                endpoint="https://push.example.com/v1/a",
                p256dh="key",
                auth="auth",
                created_at=NOW,
            )
        )
        ok_response = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(
            status_code=201
        )
        with patch("app.services.web_push.webpush", return_value=ok_response):
            await dispatch_web_push_alert(
                database=database,
                repository=sub_repo,
                current_level=RiskLevel.ORANGE,
                valid_at=NOW,
                vapid_config=VapidConfig(
                    private_key="test-private-key", subject="mailto:admin@example.com"
                ),
                cooldown_hours=3,
                dashboard_url=DASHBOARD_URL,
                now=NOW,
                delivery_repository=delivery_repo,
            )

        records = await delivery_repo.recent(limit=10)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.channel, "web_push")
        self.assertEqual(record.outcome, DeliveryOutcome.SENT)
        self.assertEqual(record.risk_level, RiskLevel.ORANGE)

    async def test_web_push_skipped_no_token_logged(self) -> None:
        from app.ingestion.subscription_repository import DryRunSubscriptionRepository

        database = _database()
        delivery_repo = DryRunDeliveryRepository()
        sub_repo = DryRunSubscriptionRepository()
        with patch("app.services.web_push.webpush") as mock_wp:
            await dispatch_web_push_alert(
                database=database,
                repository=sub_repo,
                current_level=RiskLevel.ORANGE,
                valid_at=NOW,
                vapid_config=VapidConfig(
                    private_key="", subject="mailto:admin@example.com"
                ),
                cooldown_hours=3,
                dashboard_url=DASHBOARD_URL,
                now=NOW,
                delivery_repository=delivery_repo,
            )
        mock_wp.assert_not_called()

        records = await delivery_repo.recent(limit=10)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].outcome, DeliveryOutcome.SKIPPED_NO_TOKEN)
        self.assertEqual(records[0].channel, "web_push")


class RecentAlertsEndpointTests(unittest.TestCase):
    def _client(
        self,
        delivery_repo: DryRunDeliveryRepository,
    ) -> TestClient:
        from app.ingestion.subscription_repository import DryRunSubscriptionRepository

        settings = Settings()
        app = create_app(
            settings=settings,
            delivery_repository=delivery_repo,
            subscription_repository=DryRunSubscriptionRepository(),
        )
        return TestClient(app)

    def test_recent_returns_empty_list_when_no_records(self) -> None:
        delivery_repo = DryRunDeliveryRepository()
        with self._client(delivery_repo) as http:
            response = http.get("/api/alerts/recent")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["deliveries"], [])

    def test_recent_returns_records_newest_first(self) -> None:
        import asyncio

        from app.schemas.alert_delivery import AlertDelivery

        delivery_repo = DryRunDeliveryRepository()
        older = AlertDelivery(
            channel="line",
            risk_level=RiskLevel.ORANGE,
            alerted_at=datetime(2026, 5, 27, 10, 0, tzinfo=UTC),
            outcome="sent",
            decision_reason="first alert at level orange",
        )
        newer = AlertDelivery(
            channel="line",
            risk_level=RiskLevel.RED,
            alerted_at=datetime(2026, 5, 27, 18, 0, tzinfo=UTC),
            outcome="sent",
            decision_reason="upward transition orange to red bypasses cooldown",
        )
        asyncio.run(delivery_repo.append(older))
        asyncio.run(delivery_repo.append(newer))

        with self._client(delivery_repo) as http:
            response = http.get("/api/alerts/recent")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 2)
        # Newest first
        self.assertEqual(payload["deliveries"][0]["risk_level"], "red")
        self.assertEqual(payload["deliveries"][1]["risk_level"], "orange")

    def test_recent_respects_limit_parameter(self) -> None:
        import asyncio

        from app.schemas.alert_delivery import AlertDelivery

        delivery_repo = DryRunDeliveryRepository()
        for i in range(5):
            asyncio.run(
                delivery_repo.append(
                    AlertDelivery(
                        channel="line",
                        risk_level=RiskLevel.ORANGE,
                        alerted_at=datetime(2026, 5, 27, i, 0, tzinfo=UTC),
                        outcome="sent",
                        decision_reason="test",
                    )
                )
            )

        with self._client(delivery_repo) as http:
            response = http.get("/api/alerts/recent?limit=2")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 2)

    def test_failed_delivery_visible_in_recent(self) -> None:
        import asyncio

        from app.schemas.alert_delivery import AlertDelivery

        delivery_repo = DryRunDeliveryRepository()
        asyncio.run(
            delivery_repo.append(
                AlertDelivery(
                    channel="line",
                    risk_level=RiskLevel.RED,
                    alerted_at=NOW,
                    outcome="failed",
                    decision_reason="first alert at level red",
                    error_detail="connection refused",
                )
            )
        )

        with self._client(delivery_repo) as http:
            response = http.get("/api/alerts/recent")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        record = payload["deliveries"][0]
        self.assertEqual(record["outcome"], "failed")
        self.assertEqual(record["error_detail"], "connection refused")

    def test_recent_response_does_not_expose_tokens(self) -> None:
        import asyncio
        import json

        from app.schemas.alert_delivery import AlertDelivery

        delivery_repo = DryRunDeliveryRepository()
        asyncio.run(
            delivery_repo.append(
                AlertDelivery(
                    channel="line",
                    risk_level=RiskLevel.ORANGE,
                    alerted_at=NOW,
                    outcome="sent",
                    decision_reason="first alert",
                )
            )
        )
        with self._client(delivery_repo) as http:
            response = http.get("/api/alerts/recent")
        raw_text = json.dumps(response.json())
        self.assertNotIn("token", raw_text.lower())
        self.assertNotIn("auth", raw_text.lower())


if __name__ == "__main__":
    unittest.main()
